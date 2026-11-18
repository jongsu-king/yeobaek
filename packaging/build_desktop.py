"""Package Yeobaek with the official Flet clients for Windows and Linux."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from textwrap import indent
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT = Path(__file__).resolve().parent.parent
FLET_VERSION = "1.1.2"
PYTHON_VERSION = "3.12.16"
PYTHON_RELEASE = "20261110"
APP_ID = "io.github.jongsuking.yeobaek"
WINDOWS_MUTEX = "Local\\io.github.jongsuking.yeobaek.release"
FLET_URL = f"https://github.com/flet-dev/flet/releases/download/v{FLET_VERSION}/"
PYTHON_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/"
RUNTIMES = {
    "windows-x64": (
        "x86_64-pc-windows-msvc",
        "flet-windows.zip",
        "da44d5d64c51796ae3f80e9c980ac373e7f25fc5965bbaf6236b3b28bb2e4071",
        "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc",
    ),
    "linux-amd64": (
        "x86_64-unknown-linux-gnu",
        "flet-linux-ubuntu22.04-light-amd64.tar.gz",
        "bc9b167c6045e7d7b50c382853fb4aa37802d123235addfd27b642e791f6c89c",
        "6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39",
    ),
    "linux-arm64": (
        "aarch64-unknown-linux-gnu",
        "flet-linux-ubuntu22.04-light-arm64.tar.gz",
        "c9a2c79b17ad46f965821d02e209e99fe68e76023d21c9ed887414f6ed4b0593",
        "b6a41defec4975d94b4ff1950236e7383e5c02ba40ea48c9ec33c66087a35aab",
    ),
}

LAUNCHER = """import sys
sys.dont_write_bytecode = True

import os
import runpy
from pathlib import Path

app_dir = Path(__file__).resolve().parent
os.chdir(app_dir)
os.environ["FLET_VIEW_PATH"] = str(app_dir.parent / "client")
os.environ.setdefault("FLET_APP_ID", "{app_id}")
sys.path[:0] = [str(app_dir), str(app_dir / "site-packages")]
runpy.run_path(str(app_dir / "main.py"), run_name="__main__")
"""

WINDOWS_GUARD = """import sys
sys.dont_write_bytecode = True

import ctypes
from ctypes import wintypes

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel.CreateMutexW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
ctypes.set_last_error(0)
guard = kernel.CreateMutexW(None, False, {mutex!r})
if not guard or ctypes.get_last_error() == 183:
    if guard:
        kernel.CloseHandle(guard)
    ctypes.windll.user32.MessageBoxW(
        None, "Close Yeobaek and any Yeobaek setup windows, then try again.", "Yeobaek", 0x10
    )
    raise SystemExit(1)

try:
"""


def check_hash(file: Path, expected: str) -> None:
    with file.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {file.name}")


def get_archive(cache: Path, base_url: str, name: str, expected: str) -> Path:
    file = cache / name
    if file.exists():
        check_hash(file, expected)
        return file
    request = Request(base_url + quote(name), headers={"User-Agent": "Yeobaek desktop build"})
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temp:
        temp_file = Path(temp.name)
        try:
            with urlopen(request, timeout=90) as response:
                shutil.copyfileobj(response, temp)
            temp.flush()
            check_hash(temp_file, expected)
            temp.close()
            temp_file.replace(file)
        finally:
            temp_file.unlink(missing_ok=True)
    return file


def extract(archive: Path, directory: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(directory)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            # The Linux terminal database contains entries distinguished only
            # by case, which cannot be unpacked on a default macOS volume.
            # Yeobaek's graphical launcher does not use terminal definitions.
            members = (
                item for item in bundle
                if not item.name.startswith("python/share/terminfo/")
            )
            bundle.extractall(directory, members=members, filter="data")


def stage_runtime(target: str, cache: Path, payload: Path) -> str:
    platform, client_name, client_sha, python_sha = RUNTIMES[target]
    python_name = (
        f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{platform}-install_only_stripped.tar.gz"
    )
    extract(get_archive(cache, PYTHON_URL, python_name, python_sha), payload)
    extract(get_archive(cache, FLET_URL, client_name, client_sha), payload)
    (payload / "flet").rename(payload / "client")
    if target == "windows-x64":
        executables = (payload / "python/pythonw.exe", payload / "client/flet.exe")
        magic = b"MZ"
    else:
        executables = (payload / "python/bin/python3.12", payload / "client/flet")
        magic = b"\x7fELF"
    for executable in executables:
        with executable.open("rb") as stream:
            if stream.read(len(magic)) != magic:
                raise ValueError(f"Unexpected executable type for {executable.name}")
    return platform


def snapshot_app(directory: Path) -> None:
    shutil.copy2(PROJECT / "main.py", directory)
    excluded = shutil.ignore_patterns("__pycache__", "*.pyc", ".*", "*.sqlite*", "client_secret*.json", "mobile_oauth.json")
    shutil.copytree(PROJECT / "yeobaek", directory / "yeobaek", ignore=excluded)
    (directory / "assets").mkdir()
    shutil.copy2(PROJECT / "assets/icon.png", directory / "assets/icon.png")
    (directory / "launch.py").write_text(LAUNCHER.format(app_id=APP_ID), encoding="utf-8")


def stage_app(snapshot: Path, payload: Path, cache: Path, platform: str, dependencies: list[str]) -> None:
    app = payload / "app"
    shutil.copytree(snapshot, app)
    env = {**os.environ, "UV_CACHE_DIR": str(cache / "uv")}
    subprocess.run(
        [
            "uv", "pip", "install", "--target", str(app / "site-packages"),
            "--python-version", PYTHON_VERSION, "--python-platform", platform,
            "--only-binary", ":all:", "--link-mode", "copy",
            *dependencies, f"flet-desktop=={FLET_VERSION}",
        ],
        env=env,
        check=True,
    )


def desktop_dependencies(project: dict) -> list[str]:
    preview = project.get("optional-dependencies", {}).get("desktop-preview", [])
    return [*project["dependencies"], *preview]


def root_owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def write_ar(output: Path, members: tuple[Path, ...]) -> None:
    # A .deb uses the common ar archive format, without a symbol table.
    with output.open("wb") as archive:
        archive.write(b"!<arch>\n")
        for member in members:
            size = member.stat().st_size
            name = member.name + "/"
            header = f"{name:<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{size:<10}`\n"
            archive.write(header.encode("ascii"))
            with member.open("rb") as source:
                shutil.copyfileobj(source, archive)
            if size % 2:
                archive.write(b"\n")


def build_deb(stage: Path, payload: Path, output: Path, version: str, arch: str) -> Path:
    root = stage / "root"
    (root / "opt").mkdir(parents=True)
    payload.rename(root / "opt/yeobaek")
    executable = root / "usr/bin/yeobaek"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!/bin/sh\nexec /opt/yeobaek/python/bin/python3.12 -B -I /opt/yeobaek/app/launch.py "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o111)
    desktop_entry = root / f"usr/share/applications/{APP_ID}.desktop"
    desktop_entry.parent.mkdir(parents=True)
    desktop_entry.write_text(
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Yeobaek\n"
        "GenericName=Notes\nComment=Personal notes with Google Drive sync\n"
        f"Exec=/usr/bin/yeobaek\nIcon={APP_ID}\nCategories=Office;Utility;\nTerminal=false\n",
        encoding="utf-8",
    )
    icon = root / f"usr/share/icons/hicolor/512x512/apps/{APP_ID}.png"
    icon.parent.mkdir(parents=True)
    shutil.copy2(PROJECT / "assets/icon.png", icon)

    installed_size = (sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) + 1023) // 1024
    control = stage / "control"
    control.write_text(
        f"Package: yeobaek\nVersion: {version}\nSection: editors\nPriority: optional\n"
        f"Architecture: {arch}\nInstalled-Size: {installed_size}\n"
        "Depends: libc6 (>= 2.35), libgtk-3-0 | libgtk-3-0t64, libgl1, libegl1, "
        "libstdc++6, libsecret-1-0, xdg-utils, zenity | kdialog\n"
        "Maintainer: Yeobaek <noreply@localhost>\nDescription: Personal notes with Google Drive sync\n",
        encoding="utf-8",
    )
    (stage / "debian-binary").write_bytes(b"2.0\n")
    # dpkg supports GNU long paths and does not accept PAX extended headers.
    with tarfile.open(stage / "control.tar.gz", "w:gz", format=tarfile.GNU_FORMAT, compresslevel=6) as archive:
        archive.add(control, arcname="./control", filter=root_owned)
    with tarfile.open(stage / "data.tar.gz", "w:gz", format=tarfile.GNU_FORMAT, compresslevel=6) as archive:
        archive.add(root, arcname=".", filter=root_owned)
    result = stage / f"yeobaek_{version}_{arch}.deb"
    members = ("debian-binary", "control.tar.gz", "data.tar.gz")
    write_ar(result, tuple(stage / name for name in members))
    archive_members = [
        name.rstrip("/") for name in subprocess.check_output(["ar", "t", result], text=True).splitlines()
    ]
    if archive_members != list(members):
        raise RuntimeError(f"Unexpected Debian archive members: {archive_members}")
    return Path(shutil.move(result, output / result.name))


def windows_payload_manifest(payload: Path, destination: Path) -> None:
    files = set()
    directories = set()
    cache_tag = "cpython-" + "".join(PYTHON_VERSION.split(".")[:2])
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Unexpected Windows payload symlink: {path.relative_to(payload)}")
        relative = "\\".join(path.relative_to(payload).parts).replace("$", "$$")
        if '"' in relative or "\n" in relative or "\r" in relative:
            raise ValueError("Invalid Windows payload path")
        collection = directories if path.is_dir() else files
        collection.add(relative)
        if path.is_file() and path.suffix == ".py":
            parent, _, filename = relative.rpartition("\\")
            cache_directory = f"{parent}\\__pycache__" if parent else "__pycache__"
            directories.add(cache_directory)
            stem = filename[:-3]
            for optimization in ("", ".opt-1", ".opt-2"):
                files.add(f"{cache_directory}\\{stem}.{cache_tag}{optimization}.pyc")
    files = sorted(files)
    directories = sorted(directories, key=lambda path: (path.count("\\"), path))
    # Only paths compiled into this particular installer are owned by it.
    commands = (
        ("VerifyOwnedDirectories", '!insertmacro RejectReparse', directories),
        ("DeleteOwnedFiles", "Delete", files),
        ("RemoveEmptyDirectories", "RMDir", reversed(directories)),
    )
    with destination.open("w", encoding="utf-8") as manifest:
        for macro, instruction, paths in commands:
            manifest.write(f"!macro {macro} ROOT\n")
            for path in paths:
                manifest.write(f'  {instruction} "${{ROOT}}\\{path}"\n')
            manifest.write("!macroend\n\n")


def build_windows(stage: Path, payload: Path, output: Path, version: str, makensis: str) -> Path:
    ico = PROJECT / "packaging/yeobaek.ico"
    shutil.copy2(ico, payload / "app/yeobaek.ico")
    launcher = WINDOWS_GUARD.format(mutex=WINDOWS_MUTEX) + indent(LAUNCHER.format(app_id=APP_ID), "    ")
    launcher += "finally:\n    kernel.CloseHandle(guard)\n"
    (payload / "app/launch.py").write_text(launcher, encoding="utf-8")
    manifest = stage / "payload.nsh"
    windows_payload_manifest(payload, manifest)
    result = stage / f"Yeobaek-{version}-Windows-x64-Setup.exe"
    template = PROJECT / "packaging/windows.nsi"
    subprocess.run(
        [
            makensis, "-V2", f"-DPAYLOAD={payload}", f"-DICON_FILE={ico}",
            f"-DOUT_FILE={result}", f"-DAPP_VERSION={version}",
            f"-DMUTEX_NAME={WINDOWS_MUTEX}", f"-DOWNED_MANIFEST={manifest}", str(template),
        ],
        check=True,
    )
    with result.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("Invalid Windows installer header")
    return Path(shutil.move(result, output / result.name))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", choices=RUNTIMES, nargs="+")
    parser.add_argument("--cache", type=Path, required=True, help="Directory for downloaded build inputs")
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    parser.add_argument("--makensis", default="makensis")
    args = parser.parse_args()
    cache = args.cache.resolve()
    output = args.output.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = desktop_dependencies(project)
    if f"flet=={FLET_VERSION}" not in dependencies:
        raise ValueError("Update the pinned Flet client archives to match the app dependency")
    with tempfile.TemporaryDirectory(prefix="app-", dir=cache) as source:
        snapshot = Path(source)
        snapshot_app(snapshot)
        for target in args.targets:
            print(f"Building {target} ...", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"{target}-", dir=cache) as directory:
                stage = Path(directory)
                payload = stage / "payload"
                payload.mkdir()
                platform = stage_runtime(target, cache, payload)
                stage_app(snapshot, payload, cache, platform, dependencies)
                if target == "windows-x64":
                    package = build_windows(stage, payload, output, project["version"], args.makensis)
                else:
                    package = build_deb(stage, payload, output, project["version"], target.split("-", 1)[1])
                print(f"Created {package}", flush=True)


if __name__ == "__main__":
    main()
