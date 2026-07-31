import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mend import cli


class StatusOutputTest(unittest.TestCase):
    def test_prints_human_readable_local_timestamp(self) -> None:
        output = StringIO()
        with (
            patch("mend.cli.datetime") as clock,
            redirect_stdout(output),
        ):
            clock.now.return_value.astimezone.return_value = datetime(
                2026, 7, 31, 18, 5, 1, tzinfo=UTC
            )
            cli.log_status("Restoring episode.mkv")

        self.assertEqual(
            output.getvalue(),
            "[Jul 31 6:05:01 PM] Restoring episode.mkv\n",
        )

    def test_formats_elapsed_time(self) -> None:
        self.assertEqual(cli.format_elapsed(8.6), "9s")
        self.assertEqual(cli.format_elapsed(65), "1m 5s")
        self.assertEqual(cli.format_elapsed(3661), "1h 1m 1s")

    def test_enables_vspipe_progress_in_a_terminal(self) -> None:
        process = MagicMock(returncode=0)
        process.stdout = MagicMock()
        with (
            patch("mend.cli.sys.stderr.isatty", return_value=True),
            patch("mend.cli.subprocess.Popen", return_value=process) as popen,
            patch(
                "mend.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stderr=b""),
            ),
        ):
            cli.render_handoff_video(Path("source.mkv"), Path("output.mkv"), {})

        self.assertIn("--progress", popen.call_args.args[0])


class ResolveSourceTest(unittest.TestCase):
    def test_resolves_unique_spindle_fingerprint_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "spindle" / "rips"
            expected = cache / ("a" * 64)
            expected.mkdir(parents=True)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                self.assertEqual(cli.resolve_source("a" * 12), expected)

    def test_rejects_ambiguous_fingerprint_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "spindle" / "rips"
            (cache / "abc1").mkdir(parents=True)
            (cache / "abc2").mkdir()
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                self.assertRaisesRegex(ValueError, "ambiguous"),
            ):
                cli.resolve_source("abc")


class ParserTest(unittest.TestCase):
    def test_setup_selects_native_plugin_install(self) -> None:
        args = cli.parser().parse_args(["setup"])
        self.assertIs(args.run, cli.setup)

    def test_handoff_selects_spindle_publication(self) -> None:
        args = cli.parser().parse_args(["handoff", "fingerprint"])
        self.assertEqual(args.source, "fingerprint")
        self.assertIs(args.run, cli.handoff)


class SetupTest(unittest.TestCase):
    def test_installs_plugins_in_current_environment(self) -> None:
        with (
            patch("mend.cli.sys.prefix", "/tool/mend"),
            patch("mend.cli.subprocess.run") as run,
        ):
            self.assertEqual(cli.setup(SimpleNamespace()), 0)

        command = run.call_args.args[0]
        self.assertEqual(command[0], "sh")
        self.assertEqual(Path(command[1]).name, "bootstrap-plugins")
        self.assertTrue(Path(command[1]).is_file())
        self.assertEqual(run.call_args.kwargs["env"]["MEND_ENV"], "/tool/mend")
        self.assertTrue(run.call_args.kwargs["check"])


class HandoffTest(unittest.TestCase):
    def envelope(self) -> dict:
        return {
            "version": 1,
            "fingerprint": "a" * 64,
            "metadata": {
                "title": "The Simpsons",
                "show_title": "The Simpsons",
                "media_type": "tv",
                "season_number": 6,
                "disc_source": "dvd",
            },
            "episodes": [
                {"key": "s06_001", "title_id": 0},
                {"key": "s06_002", "title_id": 1},
            ],
            "assets": {"encoded": [{"episode_key": "s06_001", "path": "stale.mkv"}]},
            "attributes": {"content_id": {"completed": True}},
        }

    def test_reads_episode_files_from_supported_spindle_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "spindle" / "rips" / ("a" * 64)
            source.mkdir(parents=True)
            (source / "Episode_t01.mkv").touch()
            (source / "Episode_t00.mkv").touch()
            metadata = {
                "version": 1,
                "fingerprint": "a" * 64,
                "disc_title": "The Simpsons Season 06",
                "title_count": 2,
                "ripspec_data": json.dumps(self.envelope()),
            }
            (source / cli.SPINDLE_METADATA_NAME).write_text(json.dumps(metadata))
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                patch("mend.cli.validate_handoff_source_file") as validate,
            ):
                got_metadata, got_envelope, files = cli.handoff_source(source)

        self.assertEqual(got_metadata["fingerprint"], "a" * 64)
        self.assertEqual(got_envelope["metadata"]["season_number"], 6)
        self.assertEqual(
            [path.name for path in files], ["Episode_t00.mkv", "Episode_t01.mkv"]
        )
        self.assertEqual(validate.call_count, 2)

    def test_rejects_wrong_source_video_format(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "sample_aspect_ratio": "1:1",
                },
                {"codec_type": "audio"},
            ]
        }
        with (
            patch("mend.cli.probe_container", return_value=probe),
            self.assertRaisesRegex(ValueError, "supported NTSC DVD format"),
        ):
            cli.validate_handoff_source_file(Path("episode.mkv"))

    def test_builds_fresh_derived_ripspec(self) -> None:
        metadata = {
            "fingerprint": "a" * 64,
            "disc_title": "The Simpsons Season 06",
            "metadata_json": '{"media_type":"tv"}',
        }
        derived = cli.handoff_fingerprint(metadata["fingerprint"])
        result = cli.clean_handoff_metadata(metadata, self.envelope(), derived, 1234)
        envelope = json.loads(result["ripspec_data"])

        self.assertEqual(result["fingerprint"], derived)
        self.assertEqual(result["mend_profile"], cli.HANDOFF_PROFILE)
        self.assertEqual(result["mend_source_fingerprint"], "a" * 64)
        self.assertEqual(result["total_bytes"], 1234)
        self.assertEqual(envelope["fingerprint"], derived)
        self.assertEqual(envelope["assets"], {})
        self.assertEqual(envelope["attributes"], {})

    def test_maps_ntsc_color_metadata_to_matroska_properties(self) -> None:
        self.assertEqual(
            cli.matroska_color_properties(
                {
                    "color_space": "smpte170m",
                    "color_range": "tv",
                    "color_transfer": "smpte170m",
                    "color_primaries": "smpte170m",
                }
            ),
            [
                "--set",
                "color-matrix-coefficients=6",
                "--set",
                "color-range=1",
                "--set",
                "color-transfer-characteristics=6",
                "--set",
                "color-primaries=6",
            ],
        )

    def test_rejects_unvalidated_color_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported color_space"):
            cli.matroska_color_properties({"color_space": "bt709"})

    def test_validates_lossless_output_and_preserved_tracks(self) -> None:
        output_probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "ffv1",
                    "width": 1440,
                    "height": 1080,
                    "sample_aspect_ratio": "1:1",
                    "pix_fmt": "yuv420p10le",
                    "field_order": "progressive",
                    "avg_frame_rate": "24000/1001",
                    "color_range": "tv",
                    "color_space": "smpte170m",
                    "color_transfer": "smpte170m",
                    "color_primaries": "smpte170m",
                },
                {"codec_type": "audio", "codec_name": "ac3"},
            ],
            "format": {"size": str(11 * 1024 * 1024), "duration": "10"},
        }
        source_identify = {
            "tracks": [
                {
                    "type": "video",
                    "codec": "MPEG-1/2",
                    "properties": {"tag_duration": "00:00:10.000"},
                },
                {
                    "type": "audio",
                    "codec": "AC-3",
                    "properties": {"language": "eng", "default_track": True},
                },
            ],
            "chapters": [{"num_entries": 2}],
            "attachments": [],
        }
        output_identify = {
            "tracks": [
                {
                    "type": "video",
                    "codec": "FFV1",
                    "properties": {"tag_duration": "00:00:10.042"},
                },
                source_identify["tracks"][1],
            ],
            "chapters": [{"num_entries": 2}],
            "attachments": [],
        }
        with (
            patch("mend.cli.probe_container", return_value=output_probe),
            patch(
                "mend.cli.identify_matroska",
                side_effect=[source_identify, output_identify],
            ),
        ):
            cli.validate_handoff_title(Path("source.mkv"), Path("output.mkv"))

    def test_publishes_atomically_with_clean_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spindle" / "rips"
            root.mkdir(parents=True)
            source_files = [Path("Episode_t00.mkv"), Path("Episode_t01.mkv")]
            metadata = {
                "version": 1,
                "fingerprint": "a" * 64,
                "disc_title": "The Simpsons Season 06",
                "metadata_json": "{}",
            }

            def render(_source, output):
                output.write_bytes(b"restored")

            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                patch(
                    "mend.cli.handoff_source",
                    return_value=(metadata, self.envelope(), source_files),
                ),
                patch("mend.cli.render_handoff_title", side_effect=render) as renderer,
            ):
                fingerprint, destination = cli.publish_handoff(Path("source"))

            self.assertEqual(fingerprint, cli.handoff_fingerprint("a" * 64))
            self.assertEqual(renderer.call_count, 2)
            self.assertTrue(destination.is_dir())
            self.assertFalse(cli.handoff_work_dir(fingerprint).exists())
            published = json.loads(
                (destination / cli.SPINDLE_METADATA_NAME).read_text()
            )
            self.assertEqual(published["total_bytes"], len(b"restored") * 2)
            self.assertEqual(json.loads(published["ripspec_data"])["assets"], {})

    def test_handoff_queues_exact_derived_fingerprint(self) -> None:
        derived = "b" * 64
        with (
            patch("mend.cli.resolve_source", return_value=Path("source")),
            patch(
                "mend.cli.publish_handoff",
                return_value=(derived, Path("derived")),
            ),
            patch("mend.cli.shutil.which", return_value="/bin/spindle"),
            patch(
                "mend.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            self.assertEqual(cli.handoff(SimpleNamespace(source="source")), 0)
        run.assert_called_once_with(
            ["/bin/spindle", "cache", "process", derived], check=False
        )


if __name__ == "__main__":
    unittest.main()
