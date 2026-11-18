"""Build the Apple Silicon Flet/PyInstaller app in an isolated directory."""

import argparse
import hashlib
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from build_desktop import APP_ID
from refresh_release import publish


PROJECT = Path(__file__).resolve().parent.parent


def set_bundle_version(app: Path, version: str, build_number: str | None = None):
    number = build_number or version
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", number):
        raise ValueError("The macOS build number must have one to three numeric components.")
    path = app / "Contents/Info.plist"
    info = plistlib.loads(path.read_bytes())
    if info.get("CFBundleIdentifier") != APP_ID:
        raise ValueError("Unexpected macOS bundle identifier.")
    info["CFBundleShortVersionString"] = version
    info["CFBundleVersion"] = number
    path.write_bytes(plistlib.dumps(info))


def build(output: Path, work_dir: Path | None = None, build_number: str | None = None) -> Path:
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if work_dir is not None:
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yeobaek-macos-", dir=work_dir) as temporary:
        stage = Path(temporary)
        command = [
            sys.executable, "-m", "flet_cli.cli", "pack", str(PROJECT / "main.py"),
            "--name", "Yeobaek", "--icon", str(PROJECT / "assets/icon.icns"),
            "--bundle-id", APP_ID, "--product-version", version,
            "--distpath", str(stage / "dist"), "--yes", "--",
            "--specpath", str(stage / "spec"), "--workpath", str(stage / "work"),
            "--collect-all", "pypdfium2_raw",
        ]
        subprocess.run(command, cwd=stage, check=True)
        app = stage / "dist/Yeobaek.app"
        set_bundle_version(app, version, build_number)
        executable = app / "Contents/MacOS/Yeobaek"
        architectures = subprocess.check_output(["lipo", "-archs", str(executable)], text=True).split()
        if architectures != ["arm64"]:
            raise ValueError(f"Unexpected macOS executable architectures: {architectures}")
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        archive = stage / f"Yeobaek-{version}-macOS-arm64.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True)
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("macOS release ZIP CRC check failed.")
            info = plistlib.loads(bundle.read("Yeobaek.app/Contents/Info.plist"))
            if info["CFBundleShortVersionString"] != version or info["CFBundleVersion"] != (build_number or version):
                raise ValueError("The ZIP contains incorrect macOS version metadata.")
        return publish(archive, output / archive.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--build-number")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("Build this arm64 app with Python on an Apple Silicon Mac.")
    artifact = build(args.output, args.work_dir, args.build_number)
    with artifact.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    print(f"Created {artifact}\nSHA-256 {sha}")


if __name__ == "__main__":
    main()
