import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mend import cli


class RenderVideoTest(unittest.TestCase):
    def test_render_sets_color_properties_and_closes_pipe(self) -> None:
        first = MagicMock(returncode=0)
        second = SimpleNamespace(returncode=0, stderr=b"")
        color = {
            "color_range": "tv",
            "color_space": "smpte170m",
            "color_transfer": "smpte170m",
            "color_primaries": "smpte170m",
            "chroma_location": "left",
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("mend.cli.Path.is_file", return_value=False),
            patch("mend.cli.subprocess.Popen", return_value=first) as popen,
            patch("mend.cli.subprocess.run", side_effect=[second, second]) as run,
        ):
            cli.render_handoff_video(Path("in.mkv"), Path("out.mkv"), color)
        command = run.call_args_list[0].args[0]
        self.assertIn("-chroma_sample_location", command)
        self.assertIn("-colorspace", command)
        self.assertEqual(run.call_args_list[0].kwargs["stdin"], first.stdout)
        self.assertEqual(
            run.call_args_list[1].args[0][:4],
            ["mkvpropedit", "out.mkv", "--edit", "track:v1"],
        )
        self.assertIn("color-matrix-coefficients=6", run.call_args_list[1].args[0])
        first.stdout.close.assert_called_once_with()
        first.wait.assert_called_once_with()
        self.assertIn("--container", popen.call_args.args[0])
        self.assertNotIn("VK_ICD_FILENAMES", popen.call_args.kwargs["env"])

    def test_uses_nvidia_icd_if_present(self) -> None:
        first = MagicMock(returncode=0)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("mend.cli.Path.is_file", return_value=True),
            patch("mend.cli.subprocess.Popen", return_value=first) as popen,
            patch(
                "mend.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stderr=b""),
            ),
        ):
            cli.render_handoff_video(Path("in.mkv"), Path("out.mkv"), {})
        self.assertEqual(
            popen.call_args.kwargs["env"]["VK_ICD_FILENAMES"],
            "/usr/share/vulkan/icd.d/nvidia_icd.json",
        )

    def test_failed_pipeline_removes_partial_output_and_reports_errors(self) -> None:
        for first_status, second_status, stderr, expected in (
            (2, 0, b"", "vspipe exited with status 2"),
            (0, 3, b"bad input", "bad input"),
            (0, 3, b"", "ffmpeg exited with status 3"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "out.mkv"
                output.touch()
                first = MagicMock(returncode=first_status)
                second = SimpleNamespace(returncode=second_status, stderr=stderr)
                with (
                    patch("mend.cli.subprocess.Popen", return_value=first),
                    patch("mend.cli.subprocess.run", return_value=second),
                    self.assertRaisesRegex(RuntimeError, expected),
                ):
                    cli.render_handoff_video(Path("in.mkv"), output, {})
                self.assertFalse(output.exists())
                first.stdout.close.assert_called_once_with()

    def test_failed_metadata_update_removes_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.mkv"
            output.touch()
            first = MagicMock(returncode=0)
            results = [
                SimpleNamespace(returncode=0, stderr=b""),
                SimpleNamespace(returncode=1, stderr=b"bad metadata"),
            ]
            with (
                patch("mend.cli.subprocess.Popen", return_value=first),
                patch("mend.cli.subprocess.run", side_effect=results),
                self.assertRaisesRegex(
                    RuntimeError, "metadata update failed: bad metadata"
                ),
            ):
                cli.render_handoff_video(Path("in.mkv"), output, {"color_range": "tv"})
            self.assertFalse(output.exists())


class RenderTitleTest(unittest.TestCase):
    def test_muxes_validated_video_and_cleans_temporaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "episode.mkv"
            source = Path("source.mkv")

            def render(_source, path, _color):
                path.write_bytes(b"video")

            def mux(command, **_kwargs):
                Path(command[2]).write_bytes(b"muxed")
                return SimpleNamespace(returncode=0)

            with (
                patch(
                    "mend.cli.probe_container",
                    return_value={
                        "streams": [
                            {"codec_type": "audio"},
                            {"codec_type": "video", "color_range": "tv"},
                        ]
                    },
                ),
                patch("mend.cli.render_handoff_video", side_effect=render) as video,
                patch("mend.cli.subprocess.run", side_effect=mux) as run,
                patch("mend.cli.validate_handoff_title") as validate,
            ):
                cli.render_handoff_title(source, output)
            self.assertEqual(output.read_bytes(), b"muxed")
            self.assertEqual(video.call_args.args[2]["color_range"], "tv")
            self.assertEqual(run.call_args.args[0][-2:], ["--no-video", str(source)])
            validate.assert_called_once_with(source, Path(temp) / ".episode.mux.mkv")
            self.assertFalse((Path(temp) / ".episode.video.mkv").exists())
            self.assertFalse((Path(temp) / ".episode.mux.mkv").exists())

    def test_mux_failure_cleans_temporaries_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "episode.mkv"
            video_temp = Path(temp) / ".episode.video.mkv"
            mux_temp = Path(temp) / ".episode.mux.mkv"
            video_temp.touch()
            mux_temp.touch()
            with (
                patch(
                    "mend.cli.probe_container",
                    return_value={"streams": [{"codec_type": "video"}]},
                ),
                patch(
                    "mend.cli.render_handoff_video",
                    side_effect=lambda *_: video_temp.touch(),
                ),
                patch(
                    "mend.cli.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=1, stderr="mux error", stdout=""
                    ),
                ),
                patch("mend.cli.validate_handoff_title") as validate,
                self.assertRaisesRegex(RuntimeError, "handoff mux failed.*mux error"),
            ):
                cli.render_handoff_title(Path("source.mkv"), output)
            validate.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(video_temp.exists())
            self.assertFalse(mux_temp.exists())


if __name__ == "__main__":
    unittest.main()
