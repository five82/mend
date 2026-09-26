import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mend import cli


class SourceDiscoveryTest(unittest.TestCase):
    def test_resolves_explicit_path_and_reports_missing_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "entry"
            source.mkdir()
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                self.assertEqual(cli.resolve_source(str(source)), source.resolve())
                with self.assertRaisesRegex(ValueError, "source not found"):
                    cli.resolve_source("absent")

    def test_empty_entry_and_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            self.assertIsNone(cli.read_cache_metadata(source / "missing"))
            self.assertIsNone(cli.read_cache_metadata(source))
            with self.assertRaisesRegex(ValueError, "no MKV files"):
                cli.source_files(source)
            (source / "b.mkv").touch()
            (source / "a.mkv").touch()
            self.assertEqual(
                [p.name for p in cli.source_files(source)], ["a.mkv", "b.mkv"]
            )
            self.assertIsNone(cli.parse_title_id(source / "unknown.mkv"))
            self.assertEqual(cli.parse_title_id(source / "Episode_t001.mkv"), 1)

    def test_probe_helpers_parse_external_tool_json(self) -> None:
        with patch(
            "mend.cli.subprocess.run",
            return_value=SimpleNamespace(stdout='{"streams": []}'),
        ) as run:
            self.assertEqual(cli.probe_container(Path("video.mkv")), {"streams": []})
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffprobe")
        self.assertEqual(command[-1], "video.mkv")
        self.assertEqual(
            run.call_args.kwargs, {"check": True, "capture_output": True, "text": True}
        )

        with patch(
            "mend.cli.subprocess.run",
            return_value=SimpleNamespace(stdout='{"tracks": []}'),
        ) as run:
            self.assertEqual(cli.identify_matroska(Path("video.mkv")), {"tracks": []})
        self.assertEqual(
            run.call_args.args[0],
            ["mkvmerge", "--identification-format", "json", "--identify", "video.mkv"],
        )
        self.assertEqual(
            run.call_args.kwargs, {"check": True, "capture_output": True, "text": True}
        )


class SourceValidationTest(unittest.TestCase):
    def test_rejects_invalid_cache_entries_before_rendering(self) -> None:
        fingerprint = "a" * 64
        envelope = {
            "version": 1,
            "metadata": {
                "show_title": "The Simpsons",
                "media_type": "tv",
                "disc_source": "dvd",
                "season_number": 6,
            },
            "episodes": [{"title_id": 0}, {"title_id": 1}],
        }
        metadata = {
            "version": 1,
            "fingerprint": fingerprint,
            "title_count": 2,
            "ripspec_data": json.dumps(envelope),
        }
        cases = [
            ("missing metadata", "cache metadata not found", None, None, None),
            (
                "cache version",
                "unsupported Spindle cache version",
                {"version": 2},
                None,
                None,
            ),
            (
                "derivative",
                "already a Mend derivative",
                {"mend_profile": "old"},
                None,
                None,
            ),
            (
                "fingerprint mismatch",
                "fingerprints differ",
                {"fingerprint": "b" * 64},
                None,
                None,
            ),
            (
                "invalid fingerprint",
                "invalid cache fingerprint",
                {"fingerprint": fingerprint.upper()},
                None,
                "uppercase",
            ),
            ("bad json", "invalid cached RipSpec", {"ripspec_data": "{"}, None, None),
            (
                "ripspec version",
                "unsupported RipSpec version",
                None,
                {"version": 2},
                None,
            ),
            (
                "wrong show",
                "only supports The Simpsons",
                None,
                {"metadata": {"title": "Other"}},
                None,
            ),
            (
                "wrong type",
                "requires TV content",
                None,
                {"metadata": {"show_title": "The Simpsons", "media_type": "movie"}},
                None,
            ),
            (
                "wrong disc",
                "requires a DVD source",
                None,
                {
                    "metadata": {
                        "show_title": "The Simpsons",
                        "media_type": "tv",
                        "disc_source": "bluray",
                    }
                },
                None,
            ),
            (
                "wrong season",
                "only supports seasons",
                None,
                {
                    "metadata": {
                        "show_title": "The Simpsons",
                        "media_type": "tv",
                        "disc_source": "dvd",
                        "season_number": 11,
                    }
                },
                None,
            ),
            ("no episodes", "has no episodes", None, {"episodes": []}, None),
            (
                "invalid title",
                "invalid episode title ID",
                None,
                {"episodes": [{"title_id": -1}]},
                None,
            ),
            (
                "duplicate title",
                "multiple episodes to one title",
                None,
                {"episodes": [{"title_id": 0}, {"title_id": 0}]},
                None,
            ),
            ("duplicate file", "multiple MKVs map to title", None, None, "duplicate"),
            ("missing file", "missing episode title files", None, None, "missing"),
            (
                "title count",
                "title count does not match",
                {"title_count": 3},
                None,
                None,
            ),
        ]
        for name, message, metadata_change, envelope_change, file_change in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                cache = Path(temp) / "spindle" / "rips"
                source = cache / (
                    fingerprint.upper() if file_change == "uppercase" else fingerprint
                )
                source.mkdir(parents=True)
                (source / "Episode_t00.mkv").touch()
                if file_change != "missing":
                    (source / "Episode_t01.mkv").touch()
                if file_change == "duplicate":
                    (source / "Other_t00.mkv").touch()
                current = deepcopy(metadata)
                if file_change == "uppercase":
                    current["fingerprint"] = fingerprint.upper()
                if envelope_change is not None:
                    current_envelope = deepcopy(envelope)
                    current_envelope.update(envelope_change)
                    current["ripspec_data"] = json.dumps(current_envelope)
                if metadata_change is not None:
                    current.update(metadata_change)
                if name != "missing metadata":
                    (source / cli.SPINDLE_METADATA_NAME).write_text(json.dumps(current))
                with (
                    patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                    patch("mend.cli.validate_handoff_source_file") as validate,
                    self.assertRaisesRegex(ValueError, message),
                ):
                    cli.handoff_source(source)
                validate.assert_not_called()

    def test_rejects_sources_outside_spindle_cache(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
            self.assertRaisesRegex(ValueError, "Spindle rip-cache entry"),
        ):
            cli.handoff_source(Path(temp))

    def test_source_streams_require_one_mpeg2_video_and_audio(self) -> None:
        video = {
            "codec_type": "video",
            "codec_name": "mpeg2video",
            "width": 720,
            "height": 480,
            "sample_aspect_ratio": "8:9",
        }
        cases = [
            ([], "one video and at least one audio"),
            ([video], "one video and at least one audio"),
            (
                [video, video, {"codec_type": "audio"}],
                "one video and at least one audio",
            ),
            (
                [{**video, "sample_aspect_ratio": "1:1"}, {"codec_type": "audio"}],
                "supported NTSC DVD format",
            ),
            ([video, {"codec_type": "audio"}], None),
        ]
        for streams, error in cases:
            with (
                self.subTest(streams=streams),
                patch("mend.cli.probe_container", return_value={"streams": streams}),
            ):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        cli.validate_handoff_source_file(Path("episode.mkv"))
                else:
                    cli.validate_handoff_source_file(Path("episode.mkv"))


class PublishedValidationTest(unittest.TestCase):
    def test_rejects_corrupt_or_incomplete_derived_entries(self) -> None:
        fingerprint = "b" * 64
        source_metadata = {"fingerprint": "a" * 64}
        valid = {
            "fingerprint": fingerprint,
            "mend_profile": cli.HANDOFF_PROFILE,
            "mend_source_fingerprint": "a" * 64,
            "title_count": 1,
            "total_bytes": 4,
            "ripspec_data": json.dumps(
                {"fingerprint": fingerprint, "assets": {}, "attributes": {}}
            ),
        }
        cases = [
            ("missing metadata", "no metadata", None, True, False),
            (
                "wrong profile",
                "metadata does not match",
                {"mend_profile": "old"},
                True,
                False,
            ),
            ("missing output", "incomplete", {}, False, False),
            ("extra output", "unexpected MKVs", {}, True, True),
            (
                "wrong size",
                "size metadata does not match",
                {"total_bytes": 5},
                True,
                False,
            ),
            (
                "stale assets",
                "stale pipeline results",
                {
                    "ripspec_data": json.dumps(
                        {
                            "fingerprint": fingerprint,
                            "assets": {"encoded": [1]},
                            "attributes": {},
                        }
                    )
                },
                True,
                False,
            ),
        ]
        for name, message, change, create_output, extra_output in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)
                source = Path("episode.mkv")
                if change is not None:
                    (destination / cli.SPINDLE_METADATA_NAME).write_text(
                        json.dumps({**valid, **change})
                    )
                if create_output:
                    (destination / source.name).write_bytes(b"data")
                if extra_output:
                    (destination / "extra.mkv").touch()
                with (
                    patch("mend.cli.validate_handoff_title") as validate,
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    cli.validate_published_handoff(
                        destination, source_metadata, [source], fingerprint
                    )
                if name in ("wrong size", "stale assets"):
                    validate.assert_called_once_with(source, destination / source.name)
                else:
                    validate.assert_not_called()

    def test_accepts_complete_derived_entry(self) -> None:
        fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            source = Path("episode.mkv")
            (destination / source.name).write_bytes(b"data")
            (destination / cli.SPINDLE_METADATA_NAME).write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "mend_profile": cli.HANDOFF_PROFILE,
                        "mend_source_fingerprint": "a" * 64,
                        "title_count": 1,
                        "total_bytes": 4,
                        "ripspec_data": json.dumps(
                            {"fingerprint": fingerprint, "assets": {}, "attributes": {}}
                        ),
                    }
                )
            )
            with patch("mend.cli.validate_handoff_title") as validate:
                cli.validate_published_handoff(
                    destination, {"fingerprint": "a" * 64}, [source], fingerprint
                )
            validate.assert_called_once_with(source, destination / source.name)


if __name__ == "__main__":
    unittest.main()
