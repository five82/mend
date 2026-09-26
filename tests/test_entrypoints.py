import runpy
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch


class EntryPointTest(unittest.TestCase):
    def test_python_module_exits_with_cli_status(self) -> None:
        with (
            patch("mend.cli.main", return_value=7) as main,
            self.assertRaises(SystemExit) as exit_status,
        ):
            runpy.run_module("mend.__main__", run_name="__main__")
        self.assertEqual(exit_status.exception.code, 7)
        main.assert_called_once_with()

    def test_plugin_check_reports_missing_names_in_order(self) -> None:
        from mend import check_plugins

        plugins = [SimpleNamespace(namespace=name) for name in ("tivtc", "bs")]
        with (
            patch.object(
                check_plugins,
                "vs",
                SimpleNamespace(core=SimpleNamespace(plugins=lambda: plugins)),
            ),
            self.assertRaisesRegex(
                RuntimeError, "missing VapourSynth plugins: bwdif, rcnv"
            ),
        ):
            check_plugins.main()

    def test_plugin_check_module_exits_successfully(self) -> None:
        from mend import check_plugins

        plugins = [
            SimpleNamespace(namespace=name) for name in ("bs", "bwdif", "rcnv", "tivtc")
        ]
        vapoursynth = SimpleNamespace(core=SimpleNamespace(plugins=lambda: plugins))
        with (
            patch.dict(sys.modules, {"vapoursynth": vapoursynth}),
            redirect_stdout(StringIO()),
            self.assertRaises(SystemExit) as exit_status,
        ):
            runpy.run_path(check_plugins.__file__, run_name="__main__")
        self.assertEqual(exit_status.exception.code, 0)

    def test_plugin_check_accepts_complete_stack(self) -> None:
        from mend import check_plugins

        plugins = [
            SimpleNamespace(namespace=name)
            for name in ("tivtc", "bs", "bwdif", "rcnv", "other")
        ]
        output = StringIO()
        with (
            patch.object(
                check_plugins,
                "vs",
                SimpleNamespace(core=SimpleNamespace(plugins=lambda: plugins)),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(check_plugins.main(), 0)
        self.assertIn("plugin stack is ready", output.getvalue())


if __name__ == "__main__":
    unittest.main()
