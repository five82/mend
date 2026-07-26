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

    def test_cleanup_selects_locked_temporal_baseline(self) -> None:
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
