import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mend import cli


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


class SourceFilesTest(unittest.TestCase):
    def test_lists_only_top_level_mkvs_in_name_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "b.mkv").touch()
            (root / "a.mkv").touch()
            (root / "metadata.json").touch()
            self.assertEqual(
                [path.name for path in cli.source_files(root)], ["a.mkv", "b.mkv"]
            )

    def test_requires_title_when_directory_has_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.mkv").touch()
            (root / "b.mkv").touch()
            with self.assertRaisesRegex(ValueError, "--title is required"):
                cli.select_title(root, None)
            self.assertEqual(cli.select_title(root, "b").name, "b.mkv")


class ParserTest(unittest.TestCase):
    def test_compare_selects_synchronized_comparison(self) -> None:
        args = cli.parser().parse_args(
            ["compare", "fingerprint", "--title", "title.mkv", "--start", "60"]
        )
        self.assertEqual(args.method, "comparison")
        self.assertEqual(args.duration, 10.0)
        self.assertIs(args.run, cli.sample)

    def test_cleanup_selects_locked_profile_comparison(self) -> None:
        args = cli.parser().parse_args(
            ["cleanup", "fingerprint", "--title", "title.mkv", "--start", "60"]
        )
        self.assertEqual(args.method, "cleanup")
        self.assertIs(args.run, cli.sample)

    def test_restore_selects_locked_native_profile(self) -> None:
        args = cli.parser().parse_args(
            ["restore", "fingerprint", "--title", "title.mkv", "--start", "60"]
        )
        self.assertEqual(args.method, "restore")
        self.assertIs(args.run, cli.sample)

    def test_upscale_selects_locked_profile(self) -> None:
        args = cli.parser().parse_args(
            ["upscale", "fingerprint", "--title", "title.mkv", "--start", "60"]
        )
        self.assertEqual(args.method, "upscale")
        self.assertIs(args.run, cli.sample)

    def test_finishing_selects_candidate_comparison(self) -> None:
        args = cli.parser().parse_args(
            ["finishing", "fingerprint", "--title", "title.mkv", "--start", "60"]
        )
        self.assertEqual(args.method, "finishing")
        self.assertIs(args.run, cli.sample)

    def test_ai_selects_temporal_restoration_model(self) -> None:
        args = cli.parser().parse_args(
            [
                "ai",
                "fingerprint",
                "--title",
                "title.mkv",
                "--start",
                "60",
                "--model",
                "compress2",
            ]
        )
        self.assertIs(args.run, cli.sample)
        self.assertEqual(args.model, "compress2")

    def test_ai_long_selects_extended_temporal_context(self) -> None:
        args = cli.parser().parse_args(
            [
                "ai",
                "fingerprint",
                "--title",
                "title.mkv",
                "--start",
                "60",
                "--model",
                "compress2-long",
            ]
        )
        self.assertIs(args.run, cli.sample)
        self.assertEqual(args.model, "compress2-long")

    def test_handoff_selects_spindle_publication(self) -> None:
        args = cli.parser().parse_args(["handoff", "fingerprint"])
        self.assertEqual(args.source, "fingerprint")
        self.assertIs(args.run, cli.handoff)


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

            def render(_source, output, _fingerprint):
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


class ScanTest(unittest.TestCase):
    def test_ranks_problem_windows_and_separates_neighbors(self) -> None:
        lines = []
        for index, frames, interlaced, repeated in (
            (0, 240, 0, 0),
            (1, 300, 300, 0),
            (2, 300, 300, 0),
            (4, 250, 0, 250),
        ):
            for frame in range(frames):
                lines.extend(
                    [
                        f"frame:{frame} pts_time:{index * 10 + frame / 30}",
                        "lavfi.idet.multiple.current_frame="
                        + ("tff" if frame < interlaced else "progressive"),
                        "lavfi.idet.repeated.current_frame="
                        + ("top" if frame < repeated else "neither"),
                        "lavfi.scd.score=0.000",
                    ]
                )

        candidates = cli.rank_scan_metadata(lines, duration=100, window=10, count=2)
        self.assertEqual(
            [candidate["start_seconds"] for candidate in candidates], [10, 40]
        )
        self.assertAlmostEqual(candidates[0]["cadence_excess_percent"], 25.125)
        self.assertEqual(candidates[0]["interlaced_percent"], 100)


class SampleTest(unittest.TestCase):
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

    def test_preserves_source_sample_aspect_ratio(self) -> None:
        title = Path("/source/title.mkv")
        with (
            tempfile.TemporaryDirectory() as output,
            patch("mend.cli.resolve_source", return_value=title),
            patch("mend.cli.select_title", return_value=title),
            patch("mend.cli.render_sample") as render,
            patch(
                "mend.cli.probe",
                return_value={
                    "streams": [
                        {
                            "sample_aspect_ratio": "8:9",
                            "color_space": "smpte170m",
                        }
                    ],
                    "format": {"duration": "100"},
                },
            ),
        ):
            args = SimpleNamespace(
                source="source",
                title=None,
                start=1.0,
                duration=2.0,
                method="comparison",
                output=output,
                field_order="tff",
            )
            self.assertEqual(cli.sample(args), 0)
        self.assertEqual(render.call_args.args[-2], "8:9")
        self.assertEqual(render.call_args.args[-1]["color_space"], "smpte170m")

    def test_uses_square_pixels_for_upscale(self) -> None:
        title = Path("/source/title.mkv")
        with (
            tempfile.TemporaryDirectory() as output,
            patch("mend.cli.resolve_source", return_value=title),
            patch("mend.cli.select_title", return_value=title),
            patch("mend.cli.render_sample") as render,
            patch(
                "mend.cli.probe",
                return_value={
                    "streams": [{"sample_aspect_ratio": "8:9"}],
                    "format": {"duration": "100"},
                },
            ),
        ):
            args = SimpleNamespace(
                source="source",
                title=None,
                start=1.0,
                duration=2.0,
                method="upscale",
                output=output,
                field_order="tff",
            )
            self.assertEqual(cli.sample(args), 0)
        self.assertEqual(render.call_args.args[-2], "1:1")

    def test_uses_square_pixels_for_finishing_comparison(self) -> None:
        title = Path("/source/title.mkv")
        with (
            tempfile.TemporaryDirectory() as output,
            patch("mend.cli.resolve_source", return_value=title),
            patch("mend.cli.select_title", return_value=title),
            patch("mend.cli.render_sample") as render,
            patch(
                "mend.cli.probe",
                return_value={
                    "streams": [{"sample_aspect_ratio": "8:9"}],
                    "format": {"duration": "100"},
                },
            ),
        ):
            args = SimpleNamespace(
                source="source",
                title=None,
                start=1.0,
                duration=2.0,
                method="finishing",
                output=output,
                field_order="tff",
            )
            self.assertEqual(cli.sample(args), 0)
        self.assertEqual(render.call_args.args[-2], "1:1")


class AnalyzeFileTest(unittest.TestCase):
    @patch("mend.cli.frame_counts", return_value=(33_725, 41_270))
    @patch("mend.cli.probe")
    def test_reports_coded_excess_from_source_duration(self, probe, _counts) -> None:
        probe.return_value = {
            "streams": [{"codec_name": "mpeg2video", "width": 720, "height": 480}],
            "format": {"duration": "1377.042", "size": "1018186285"},
        }
        result = cli.analyze_file(Path("episode.mkv"))
        self.assertEqual(result["rff_display_frames"], 41_270)
        self.assertAlmostEqual(result["rff_duration_seconds"], 1377.042333, places=5)
        self.assertAlmostEqual(result["coded_excess_percent"], 2.147, places=2)


if __name__ == "__main__":
    unittest.main()
