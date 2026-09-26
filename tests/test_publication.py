import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mend import cli


class PublicationTest(unittest.TestCase):
    def test_existing_entry_is_validated_without_restoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spindle" / "rips"
            root.mkdir(parents=True)
            metadata = {"fingerprint": "a" * 64}
            destination = root / cli.handoff_fingerprint(metadata["fingerprint"])
            destination.mkdir()
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                patch(
                    "mend.cli.handoff_source",
                    return_value=(metadata, {}, [Path("episode.mkv")]),
                ),
                patch("mend.cli.validate_published_handoff") as validate,
                patch("mend.cli.render_handoff_title") as render,
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(
                    cli.publish_handoff(Path("source")), (destination.name, destination)
                )
            validate.assert_called_once_with(
                destination, metadata, [Path("episode.mkv")], destination.name
            )
            render.assert_not_called()

    def test_resumes_validated_title_and_rejects_extra_work_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spindle" / "rips"
            root.mkdir(parents=True)
            metadata = {"fingerprint": "a" * 64, "disc_title": "Disc"}
            derived = cli.handoff_fingerprint(metadata["fingerprint"])
            work = Path(temp) / "mend" / "handoffs" / derived
            work.mkdir(parents=True)
            (work / "episode.mkv").write_bytes(b"complete")
            (work / "extra.mkv").touch()
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                patch(
                    "mend.cli.handoff_source",
                    return_value=(
                        metadata,
                        {"episodes": [{"title_id": 0}]},
                        [Path("episode.mkv")],
                    ),
                ),
                patch("mend.cli.validate_handoff_title") as validate,
                patch("mend.cli.render_handoff_title") as render,
                redirect_stdout(StringIO()),
                self.assertRaisesRegex(RuntimeError, "unexpected MKVs"),
            ):
                cli.publish_handoff(Path("source"))
            validate.assert_called_once_with(Path("episode.mkv"), work / "episode.mkv")
            render.assert_not_called()
            self.assertFalse((root / derived).exists())

    def test_publish_rename_failure_keeps_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spindle" / "rips"
            root.mkdir(parents=True)
            metadata = {"fingerprint": "a" * 64, "disc_title": "Disc"}
            derived = cli.handoff_fingerprint(metadata["fingerprint"])
            work = Path(temp) / "mend" / "handoffs" / derived

            def render(_source, output):
                output.write_bytes(b"data")

            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}),
                patch(
                    "mend.cli.handoff_source",
                    return_value=(
                        metadata,
                        {"episodes": [{"title_id": 0}]},
                        [Path("episode.mkv")],
                    ),
                ),
                patch("mend.cli.render_handoff_title", side_effect=render),
                patch.object(Path, "rename", side_effect=OSError("disk full")),
                redirect_stdout(StringIO()),
                self.assertRaisesRegex(
                    RuntimeError, "publish derived cache entry: disk full"
                ),
            ):
                cli.publish_handoff(Path("source"))
            self.assertEqual((work / "episode.mkv").read_bytes(), b"data")
            self.assertEqual(
                json.loads((work / cli.SPINDLE_METADATA_NAME).read_text())[
                    "total_bytes"
                ],
                4,
            )


class CliErrorTest(unittest.TestCase):
    def test_main_returns_error_status_for_user_errors(self) -> None:
        for error in (ValueError("invalid source"), RuntimeError("render failed")):
            with self.subTest(error=error):
                stderr = StringIO()
                with (
                    patch("mend.cli.resolve_source", side_effect=error),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(cli.main(["handoff", "source"]), 1)
                self.assertEqual(stderr.getvalue(), f"mend: {error}\n")

    def test_handoff_reports_missing_spindle_after_publication(self) -> None:
        fingerprint = "b" * 64
        with (
            patch("mend.cli.resolve_source", return_value=Path("source")),
            patch(
                "mend.cli.publish_handoff", return_value=(fingerprint, Path("derived"))
            ),
            patch("mend.cli.shutil.which", return_value=None),
            self.assertRaisesRegex(
                RuntimeError, f"spindle cache process {fingerprint}"
            ),
        ):
            cli.handoff(SimpleNamespace(sources=["source"]))

    def test_handoff_reports_failed_spindle_queue(self) -> None:
        with (
            patch("mend.cli.resolve_source", return_value=Path("source")),
            patch("mend.cli.publish_handoff", return_value=("b" * 64, Path("derived"))),
            patch("mend.cli.shutil.which", return_value="/bin/spindle"),
            patch(
                "mend.cli.subprocess.run", return_value=SimpleNamespace(returncode=1)
            ),
            redirect_stdout(StringIO()),
            self.assertRaisesRegex(RuntimeError, "derivative remains at derived"),
        ):
            cli.handoff(SimpleNamespace(sources=["source"]))


if __name__ == "__main__":
    unittest.main()
