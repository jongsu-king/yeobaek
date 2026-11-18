"""Regressions for Flet pack's outer plist and relocated spec paths."""

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import build_macos


class MacPackagingTests(unittest.TestCase):
    def test_outer_bundle_version_is_corrected_without_dropping_other_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Yeobaek.app"
            info_file = app / "Contents/Info.plist"
            info_file.parent.mkdir(parents=True)
            info_file.write_bytes(plistlib.dumps({"CFBundleIdentifier": build_macos.APP_ID,
                                                  "CFBundleShortVersionString": "0.0.0",
                                                  "CFBundleVersion": "0.0.0", "CFBundleIconFile": "icon.icns"}))
            build_macos.set_bundle_version(app, "0.4.0")
            result = plistlib.loads(info_file.read_bytes())
            self.assertEqual(result["CFBundleShortVersionString"], "0.4.0")
            self.assertEqual(result["CFBundleVersion"], "0.4.0")
            self.assertEqual(result["CFBundleIconFile"], "icon.icns")
            build_macos.set_bundle_version(app, "0.4.0", "3")
            self.assertEqual(plistlib.loads(info_file.read_bytes())["CFBundleVersion"], "3")

    def test_external_spec_build_uses_absolute_source_and_icon(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(build_macos.subprocess, "run", side_effect=RuntimeError("stop before pack")) as run:
                with self.assertRaisesRegex(RuntimeError, "stop before pack"):
                    build_macos.build(Path(folder) / "output", Path(folder) / "external")
            command = run.call_args.args[0]
            source = Path(command[command.index("pack") + 1])
            icon = Path(command[command.index("--icon") + 1])
            stage = run.call_args.kwargs["cwd"]
            self.assertEqual(source, build_macos.PROJECT / "main.py")
            self.assertEqual(icon, build_macos.PROJECT / "assets/icon.icns")
            self.assertTrue(source.is_absolute() and icon.is_absolute())
            self.assertEqual(Path(command[command.index("--specpath") + 1]).parent, stage)
            self.assertNotEqual(stage, build_macos.PROJECT)
            self.assertEqual(command[-2:], ["--collect-all", "pypdfium2_raw"])
            self.assertNotIn("--onedir", command)


if __name__ == "__main__":
    unittest.main()
