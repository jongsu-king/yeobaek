"""Release refresh failures must preserve artifacts and include added modules."""

import io
import sys
import tarfile
import tempfile
import unittest
import zipfile
import zipimport
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
sys.path.insert(0, str(PACKAGING))
import refresh_release as refresh


class RefreshReleaseTests(unittest.TestCase):
    def test_failed_copy_keeps_previous_artifact_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.zip"
            destination = root / "release.zip"
            source.write_bytes(b"new complete artifact")
            destination.write_bytes(b"previous verified artifact")

            def failed_copy(original, staged):
                staged.write(original.read(4))
                raise OSError("Simulated disconnected work volume")

            with patch.object(refresh.shutil, "copyfileobj", side_effect=failed_copy):
                with self.assertRaises(OSError):
                    refresh.publish(source, destination)
            self.assertEqual(destination.read_bytes(), b"previous verified artifact")
            self.assertEqual(source.read_bytes(), b"new complete artifact")
            self.assertEqual({path.name for path in root.iterdir()}, {"source.zip", "release.zip"})

    def test_new_mobile_module_is_importable_and_runtime_entries_are_preserved(self):
        original = io.BytesIO()
        with zipfile.ZipFile(original, "w") as archive:
            archive.writestr("yeobaek/__init__.pyc", refresh.compiled(b"", "yeobaek/__init__.py"))
            archive.writestr("assets/runtime.bin", b"existing runtime")
        updated = refresh.rewrite_zip(
            original.getvalue(),
            {"yeobaek/exports.pyc": refresh.compiled(b"MARKER = 42\n", "yeobaek/exports.py")},
            add_python_modules=True,
        )
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "app.zip"
            archive_path.write_bytes(updated)
            importer = zipimport.zipimporter(str(archive_path) + "/yeobaek")
            namespace = {}
            exec(importer.get_code("exports"), namespace)
            self.assertEqual(namespace["MARKER"], 42)
        with zipfile.ZipFile(io.BytesIO(updated)) as archive:
            self.assertEqual(archive.read("assets/runtime.bin"), b"existing runtime")
        with self.assertRaises(ValueError):
            refresh.rewrite_zip(original.getvalue(), {"main.pyc": b"missing"}, add_python_modules=True)

    def test_new_unpacked_python_module_is_written_for_desktop_and_ios(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for bytecode in (False, True):
                with self.subTest(bytecode=bytecode):
                    name = "yeobaek/new_package/module.py" + ("c" if bytecode else "")
                    raw = refresh.compiled(b"VALUE = 7", name[:-1]) if bytecode else b"VALUE = 7"
                    refresh.write_app_files(root, {name: raw}, bytecode=bytecode)
                    self.assertEqual((root / name).read_bytes(), raw)
            with self.assertRaises(ValueError):
                refresh.write_app_files(root, {"missing-config.json": b"{}"})
            self.assertFalse(refresh.new_python_module("yeobaek/../../outside.py"))

    def test_debian_refresh_adds_module_and_keeps_native_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            control = b"Package: yeobaek\nVersion: 0.3.1\nArchitecture: amd64\nInstalled-Size: 100\n"
            entries = {
                "./opt/yeobaek/app/main.py": b"OLD = True\n",
                "./opt/yeobaek/app/yeobaek/__init__.py": b"",
                "./opt/yeobaek/python/runtime.bin": b"native runtime remains unchanged",
            }
            for name, contents in (("control.tar.gz", {"./control": control}), ("data.tar.gz", entries)):
                with tarfile.open(root / name, "w:gz", format=tarfile.GNU_FORMAT) as archive:
                    for path, data in contents.items():
                        member = tarfile.TarInfo(path)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
            (root / "debian-binary").write_bytes(b"2.0\n")
            baseline = root / "baseline.deb"
            refresh.build_desktop.write_ar(baseline, tuple(root / name for name in
                                           ("debian-binary", "control.tar.gz", "data.tar.gz")))
            snapshot = {"main.py": b"updated main", "yeobaek/exports.py": b"VALUE = 42"}
            artifact = refresh.linux(SimpleNamespace(work_dir=root), baseline, root, snapshot, "0.4.0", "amd64")
            with tarfile.open(fileobj=io.BytesIO(refresh.ar_member(artifact, "data.tar.gz"))) as archive:
                self.assertEqual(archive.extractfile("./opt/yeobaek/app/main.py").read(), b"updated main")
                self.assertEqual(archive.extractfile("./opt/yeobaek/app/yeobaek/exports.py").read(), b"VALUE = 42")
                self.assertEqual(archive.extractfile("./opt/yeobaek/python/runtime.bin").read(),
                                 entries["./opt/yeobaek/python/runtime.bin"])
            with tarfile.open(fileobj=io.BytesIO(refresh.ar_member(artifact, "control.tar.gz"))) as archive:
                self.assertIn(b"Version: 0.4.0\n", archive.extractfile("./control").read())


if __name__ == "__main__":
    unittest.main()
