"""Refresh Python-only releases while retaining verified native runtimes.

Native Flutter extensions and dependency versions must match the baseline tag.
Android needs official SDK build-tools, a JDK and the baseline signing key.
Windows needs 7-Zip and NSIS. iOS simulator refresh runs on macOS.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import marshal
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

import build_desktop


PROJECT = Path(__file__).resolve().parent.parent
APP_ID = build_desktop.APP_ID
IOS_RESOURCE = "serious_python_darwin_serious_python_darwin.bundle"
TARGETS = ("android", "ios-simulator", "windows", "linux-amd64", "linux-arm64")


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(source: Path, destination: Path) -> Path:
    """Replace a release file only after a complete sibling copy is on disk."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".partial",
                                         dir=destination.parent, delete=False) as staged:
            temporary = Path(staged.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, staged)
            staged.flush()
            os.fsync(staged.fileno())
        shutil.copymode(source, temporary)
        temporary.replace(destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def new_python_module(name: str, *, bytecode=False) -> bool:
    path = Path(name)
    return (not path.is_absolute() and ".." not in path.parts
            and name.startswith("yeobaek/") and name.endswith(".pyc" if bytecode else ".py"))


def write_app_files(folder: Path, replacements: dict[str, bytes], *, bytecode=False):
    for name, data in replacements.items():
        destination = folder / name
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError(f"Expected a regular app file: {name}")
        if not destination.exists() and not new_python_module(name, bytecode=bytecode):
            raise ValueError(f"Expected app file missing from baseline: {name}")
        if bytecode and destination.exists() and name.endswith(".pyc"):
            if destination.read_bytes()[:4] != importlib.util.MAGIC_NUMBER:
                raise ValueError("Use Python 3.12 to refresh this release.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def ar_member(archive: Path, name: str) -> bytes:
    matches = []
    with archive.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("Unexpected Debian ar archive header.")
        while header := stream.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise ValueError("Invalid Debian ar member header.")
            member = header[:16].decode("ascii").strip().rstrip("/")
            size = int(header[48:58])
            if size < 0:
                raise ValueError("Invalid Debian ar member size.")
            if member == name:
                raw = stream.read(size)
                if len(raw) != size:
                    raise ValueError("Truncated Debian ar member.")
                matches.append(raw)
            else:
                stream.seek(size, 1)
            if size % 2:
                stream.seek(1, 1)
    if len(matches) != 1:
        raise ValueError(f"Expected one Debian archive member: {name}")
    return matches[0]


def source_snapshot() -> dict[str, bytes]:
    paths = [PROJECT / "main.py", *(PROJECT / "yeobaek").rglob("*.py")]
    paths += [PROJECT / "README.md", PROJECT / "pyproject.toml"]
    return {str(path.relative_to(PROJECT)): path.read_bytes() for path in sorted(paths)}


def compiled(source: bytes, name: str) -> bytes:
    # Python 3.12 bytecode is portable across the bundled 3.12 runtimes.
    code = compile(source, name, "exec", dont_inherit=True)
    return importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, len(source)) + marshal.dumps(code)


def mobile_snapshot(snapshot: dict[str, bytes]) -> dict[str, bytes]:
    return {
        name + "c" if name.endswith(".py") else name:
        compiled(data, name) if name.endswith(".py") else data
        for name, data in snapshot.items()
    }


def rewrite_zip(data: bytes, replacements: dict[str, bytes], *, strip_signatures=False,
                add_python_modules=False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, "w") as refreshed:
        names = set(original.namelist())
        missing = replacements.keys() - names
        if missing and not (add_python_modules and all(new_python_module(name, bytecode=True) for name in missing)):
            raise ValueError(f"Baseline ZIP is missing expected entries: {sorted(missing)}")
        for member in original.infolist():
            signed = member.filename.startswith("META-INF/") and (
                member.filename.endswith((".RSA", ".DSA", ".EC", ".SF"))
                or member.filename == "META-INF/MANIFEST.MF"
            )
            if strip_signatures and signed:
                continue
            raw = replacements.get(member.filename)
            if raw is None:
                raw = original.read(member)
            if member.filename.endswith(".pyc") and member.filename in replacements:
                if original.read(member)[:4] != importlib.util.MAGIC_NUMBER:
                    raise ValueError("Use Python 3.12 to refresh this release.")
            refreshed.writestr(copy.copy(member), raw)
        for name in sorted(missing):
            member = zipfile.ZipInfo(name)
            member.external_attr = 0o100644 << 16
            refreshed.writestr(member, replacements[name])
    return output.getvalue()


def xml_chunks(data: bytes):
    kind, header, total = struct.unpack_from("<HHI", data)
    if kind != 3 or header != 8 or total != len(data):
        raise ValueError("Unexpected Android binary XML header.")
    offset = header
    while offset < total:
        kind, header, size = struct.unpack_from("<HHI", data, offset)
        if size < header or offset + size > total:
            raise ValueError("Invalid Android binary XML chunk.")
        yield kind, data[offset:offset + size]
        offset += size


def string_length(data: bytes, position: int, utf8: bool):
    if utf8:
        length = data[position]
        position += 1
        if length & 0x80:
            length = ((length & 0x7F) << 8) | data[position]
            position += 1
    else:
        length = struct.unpack_from("<H", data, position)[0]
        position += 2
        if length & 0x8000:
            length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, position)[0]
            position += 2
    return length, position


def read_strings(chunk: bytes) -> list[str]:
    _, header, _ = struct.unpack_from("<HHI", chunk)
    count, _, flags, start, _ = struct.unpack_from("<IIIII", chunk, 8)
    strings = []
    for index in range(count):
        position = start + struct.unpack_from("<I", chunk, header + index * 4)[0]
        length, position = string_length(chunk, position, bool(flags & 0x100))
        if flags & 0x100:
            length, position = string_length(chunk, position, True)
            value = chunk[position:position + length].decode("utf-8")
        else:
            value = chunk[position:position + length * 2].decode("utf-16le")
        strings.append(value)
    return strings


def encoded_length(length: int, utf8: bool) -> bytes:
    if utf8:
        if length > 0x7FFF:
            raise ValueError("Android XML string is too long.")
        return bytes([0x80 | (length >> 8), length & 255]) if length > 127 else bytes([length])
    if length > 0x7FFF:
        return struct.pack("<HH", 0x8000 | (length >> 16), length & 0xFFFF)
    return struct.pack("<H", length)


def replace_string_pool(chunk: bytes, replacements: dict[int, str]) -> bytes:
    _, header_size, _ = struct.unpack_from("<HHI", chunk)
    count, styles, flags, _, style_start = struct.unpack_from("<IIIII", chunk, 8)
    strings = read_strings(chunk)
    for index, value in replacements.items():
        strings[index] = value
    offsets = []
    values = bytearray()
    for value in strings:
        offsets.append(len(values))
        units = len(value.encode("utf-16le")) // 2
        if flags & 0x100:
            raw = value.encode("utf-8")
            values += encoded_length(units, True) + encoded_length(len(raw), True) + raw + b"\0"
        else:
            values += encoded_length(units, False) + value.encode("utf-16le") + b"\0\0"
    values += b"\0" * (-len(values) % 4)
    style_offsets = chunk[header_size + 4 * count:header_size + 4 * (count + styles)]
    style_data = chunk[style_start:] if style_start else b""
    start = header_size + 4 * (count + styles)
    output = bytearray(chunk[:header_size])
    output += struct.pack("<" + "I" * count, *offsets) + style_offsets + values + style_data
    struct.pack_into("<I", output, 4, len(output))
    struct.pack_into("<III", output, 16, flags & ~1, start, start + len(values) if styles else 0)
    return bytes(output)


def refresh_android_manifest(data: bytes, version: str, version_code: int) -> bytes:
    chunks = list(xml_chunks(data))
    pool_index = next(index for index, (kind, _) in enumerate(chunks) if kind == 1)
    strings = read_strings(chunks[pool_index][1])
    version_string = None
    previous_code = None
    package = None
    for index, (kind, chunk) in enumerate(chunks):
        if kind != 0x102:
            continue
        _, name, start, size, count = struct.unpack_from("<IIHHH", chunk, 16)
        if strings[name] != "manifest":
            continue
        changed = bytearray(chunk)
        for attribute in range(count):
            position = 16 + start + attribute * size
            _, name, raw, _, _, value_type, value = struct.unpack_from("<IIIHBBI", chunk, position)
            attribute_name = strings[name]
            if attribute_name == "package":
                package = strings[value]
            elif attribute_name == "versionName" and value_type == 3:
                version_string = value
            elif attribute_name == "versionCode" and value_type == 16:
                previous_code = value
                struct.pack_into("<I", changed, position + 16, version_code)
        chunks[index] = (kind, bytes(changed))
    if package != APP_ID or version_string is None or previous_code is None:
        raise ValueError("Unexpected Android package/version attributes.")
    if version_code <= previous_code:
        raise ValueError("Android versionCode must increase for an update.")
    chunks[pool_index] = (1, replace_string_pool(chunks[pool_index][1], {version_string: version}))
    body = b"".join(chunk for _, chunk in chunks)
    return struct.pack("<HHI", 3, 8, len(body) + 8) + body


def android(args, base: Path, output: Path, snapshot, version: str) -> Path:
    if not args.android_tools or not args.keystore or not args.android_version_code:
        raise ValueError("Android requires --android-tools, --keystore and --android-version-code.")
    tools = args.android_tools.resolve()
    signer = str(tools / "apksigner")
    baseline = run([signer, "verify", "--print-certs", str(base)], capture_output=True, text=True).stdout
    with zipfile.ZipFile(base) as archive:
        app = rewrite_zip(archive.read("assets/app.zip"), mobile_snapshot(snapshot), add_python_modules=True)
        extension = "yeobaek_mobile_auth/__init__.pyc"
        source = PROJECT / "mobile_auth/src/yeobaek_mobile_auth/__init__.py"
        site = rewrite_zip(archive.read("assets/sitepackages.zip"), {extension: compiled(source.read_bytes(), extension[:-1])})
        manifest = refresh_android_manifest(archive.read("AndroidManifest.xml"), version, args.android_version_code)
    refreshed = rewrite_zip(base.read_bytes(), {"assets/app.zip": app, "assets/sitepackages.zip": site,
                                               "AndroidManifest.xml": manifest}, strip_signatures=True)
    with tempfile.TemporaryDirectory(prefix="android-", dir=args.work_dir) as folder:
        folder = Path(folder)
        unsigned = folder / "unsigned.apk"
        aligned = folder / "aligned.apk"
        signed = folder / "signed.apk"
        unsigned.write_bytes(refreshed)
        run([str(tools / "zipalign"), "-P", "16", "-f", "4", str(unsigned), str(aligned)])
        run([signer, "sign", "--ks", str(args.keystore.resolve()), "--ks-key-alias", args.key_alias,
             "--ks-pass", "env:" + args.password_env, "--key-pass", "env:" + args.password_env,
             "--out", str(signed), str(aligned)])
        verified = run([signer, "verify", "--print-certs", str(signed)], capture_output=True, text=True).stdout
        certificate_pattern = r"Signer #1 certificate SHA-256 digest: ([a-f0-9]+)"
        if re.search(certificate_pattern, baseline).group(1) != re.search(certificate_pattern, verified).group(1):
            raise ValueError("Android signing certificate changed.")
        run([str(tools / "zipalign"), "-c", "-P", "16", "4", str(signed)])
        badging = run([str(tools / "aapt2"), "dump", "badging", str(signed)], capture_output=True, text=True).stdout
        expected = f"package: name='{APP_ID}' versionCode='{args.android_version_code}' versionName='{version}'"
        if expected not in badging or "native-code: 'arm64-v8a'" not in badging:
            raise ValueError("Android package metadata did not match the requested release.")
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip() is not None:
                raise ValueError("Android archive CRC check failed.")
            with zipfile.ZipFile(io.BytesIO(archive.read("assets/app.zip"))) as app_zip:
                for name, raw in mobile_snapshot(snapshot).items():
                    if app_zip.read(name) != raw:
                        raise ValueError(f"Android packaged source mismatch: {name}")
        destination = output / f"Yeobaek-{version}-Android-arm64.apk"
        return publish(signed, destination)


def ios_simulator(args, base: Path, output: Path, snapshot, version: str) -> Path:
    with tempfile.TemporaryDirectory(prefix="ios-", dir=args.work_dir) as folder:
        folder = Path(folder)
        run(["ditto", "-x", "-k", str(base), str(folder)])
        app = folder / "yeobaek.app"
        bundle = app / IOS_RESOURCE
        write_app_files(bundle / "app", mobile_snapshot(snapshot), bytecode=True)
        extension = bundle / "site-packages/yeobaek_mobile_auth/__init__.pyc"
        source = PROJECT / "mobile_auth/src/yeobaek_mobile_auth/__init__.py"
        extension.write_bytes(compiled(source.read_bytes(), "yeobaek_mobile_auth/__init__.py"))
        info_file = app / "Info.plist"
        info = plistlib.loads(info_file.read_bytes())
        if info.get("CFBundleIdentifier") != APP_ID:
            raise ValueError("Unexpected iOS bundle identifier.")
        info["CFBundleShortVersionString"] = version
        info["CFBundleVersion"] = str(int(info["CFBundleVersion"]) + 1)
        info_file.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        run(["codesign", "--force", "--sign", "-", str(bundle)], capture_output=True)
        run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True)
        run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True)
        destination = output / f"Yeobaek-{version}-iOS-Simulator-arm64.zip"
        temporary = folder / destination.name
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(temporary)])
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("iOS archive CRC check failed.")
            for name, data in mobile_snapshot(snapshot).items():
                if archive.read(f"yeobaek.app/{IOS_RESOURCE}/app/{name}") != data:
                    raise ValueError(f"iOS packaged source mismatch: {name}")
        return publish(temporary, destination)


def linux(args, base: Path, output: Path, snapshot, version: str, arch: str) -> Path:
    with tempfile.TemporaryDirectory(prefix="linux-", dir=args.work_dir) as folder:
        folder = Path(folder)
        members = ("debian-binary", "control.tar.gz", "data.tar.gz")
        (folder / "debian-binary").write_bytes(b"2.0\n")
        expected = {"./opt/yeobaek/app/" + name: data for name, data in snapshot.items() if name.endswith(".py")}
        seen = set()
        size_change = 0
        payload = ar_member(base, "data.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as original, tarfile.open(
            folder / "data.tar.gz", "w:gz", format=tarfile.GNU_FORMAT, compresslevel=6,
        ) as refreshed:
            for member in original:
                raw = expected.get(member.name)
                if raw is None:
                    refreshed.addfile(member, original.extractfile(member) if member.isfile() else None)
                    continue
                if not member.isfile():
                    raise ValueError(f"Expected regular Linux source file: {member.name}")
                size_change += len(raw) - member.size
                member.size = len(raw)
                refreshed.addfile(member, io.BytesIO(raw))
                seen.add(member.name)
            for name in sorted(expected.keys() - seen):
                relative = name.removeprefix("./opt/yeobaek/app/")
                if not new_python_module(relative):
                    raise ValueError(f"Linux source file missing from baseline: {name}")
                raw = expected[name]
                member = tarfile.TarInfo(name)
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                member.mode = 0o644
                member.size = len(raw)
                refreshed.addfile(member, io.BytesIO(raw))
                size_change += len(raw)
        control = ar_member(base, "control.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(control), mode="r:gz") as original, tarfile.open(
            folder / "control.tar.gz", "w:gz", format=tarfile.GNU_FORMAT,
        ) as refreshed:
            for member in original:
                raw = original.extractfile(member).read() if member.isfile() else None
                if member.name.lstrip("./") == "control":
                    text = raw.decode()
                    text, count = re.subn(r"(?m)^Version: .+$", "Version: " + version, text)
                    if count != 1 or f"Architecture: {arch}\n" not in text:
                        raise ValueError("Unexpected baseline Debian control metadata.")
                    match = re.search(r"(?m)^Installed-Size: (\d+)$", text)
                    if match:
                        installed = max(1, int(match.group(1)) + (size_change + 1023) // 1024)
                        text = text[:match.start(1)] + str(installed) + text[match.end(1):]
                    raw = text.encode()
                    member.size = len(raw)
                refreshed.addfile(member, io.BytesIO(raw) if raw is not None else None)
        from build_desktop import write_ar

        destination = output / f"yeobaek_{version}_{arch}.deb"
        temporary = folder / destination.name
        write_ar(temporary, tuple(folder / name for name in members))
        return publish(temporary, destination)


def windows(args, base: Path, output: Path, snapshot, version: str) -> Path:
    if not args.sevenzip or not args.makensis:
        raise ValueError("Windows refresh requires --sevenzip and --makensis.")
    from build_desktop import build_windows

    with tempfile.TemporaryDirectory(prefix="windows-", dir=args.work_dir) as folder:
        folder = Path(folder)
        extracted = folder / "extracted"
        run([str(args.sevenzip.resolve()), "x", "-y", f"-o{extracted}", str(base)], capture_output=True)
        roots = [item.parent for item in extracted.rglob("app")
                 if item.is_dir() and (item / "main.py").is_file()
                 and (item.parent / "client").is_dir() and (item.parent / "python").is_dir()]
        if len(roots) != 1:
            raise ValueError("Expected exactly one Windows native payload.")
        payload = folder / "payload"
        payload.mkdir()
        for name in ("app", "client", "python"):
            source = roots[0] / name
            if not source.is_dir() or source.is_symlink():
                raise ValueError(f"Expected Windows payload directory missing: {name}")
            source.rename(payload / name)
        write_app_files(payload / "app", {name: data for name, data in snapshot.items() if name.endswith(".py")})
        built = folder / "built"
        built.mkdir()
        artifact = build_windows(folder, payload, built, version, str(args.makensis.resolve()))
        return publish(artifact, output / artifact.name)


def compatible_baseline(tag: str):
    current = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    baseline = tomllib.loads(run(["git", "show", f"{tag}:pyproject.toml"], cwd=PROJECT,
                                capture_output=True, text=True).stdout)
    for key in ("dependencies", "optional-dependencies"):
        if current["project"].get(key) != baseline["project"].get(key):
            raise ValueError("Dependency changes require a full native build.")
    for key in ("org", "product", "company", "android", "ios"):
        if current["tool"]["flet"].get(key) != baseline["tool"]["flet"].get(key):
            raise ValueError("Native Flet configuration changed; perform a full native build.")
    native = PROJECT / "mobile_auth/src/flutter"
    for path in native.rglob("*"):
        if not path.is_file():
            continue
        name = str(path.relative_to(PROJECT))
        original = run(["git", "show", f"{tag}:{name}"], cwd=PROJECT, capture_output=True).stdout
        if original != path.read_bytes():
            raise ValueError("Flutter extension changes require a full native build.")
    return current["project"]["version"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", choices=TARGETS, nargs="+")
    parser.add_argument("--base-dir", type=Path, default=PROJECT / "dist")
    parser.add_argument("--base-version", default="0.3.0")
    parser.add_argument("--baseline-checksums", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--android-tools", type=Path)
    parser.add_argument("--android-version-code", type=int)
    parser.add_argument("--keystore", type=Path)
    parser.add_argument("--key-alias", default="androiddebugkey")
    parser.add_argument("--password-env", default="YEOBAEK_ANDROID_STORE_PASSWORD")
    parser.add_argument("--sevenzip", type=Path)
    parser.add_argument("--makensis", type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Run this refresh with Python 3.12.")
    version = compatible_baseline("v" + args.base_version)
    if version == args.base_version:
        raise ValueError("Bump the project version before refreshing release artifacts.")
    args.base_dir = args.base_dir.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.work_dir:
        args.work_dir = args.work_dir.resolve()
        args.work_dir.mkdir(parents=True, exist_ok=True)
    checksum_file = args.baseline_checksums
    if checksum_file is None:
        checksum_file = args.base_dir / f"SHA256SUMS-{args.base_version}.txt"
        if not checksum_file.is_file():
            checksum_file = args.base_dir / "SHA256SUMS.txt"
    sums = {}
    for line in checksum_file.read_text().splitlines():
        expected, name = line.split("  ", 1)
        sums[name] = expected
    snapshot = source_snapshot()
    base_names = {
        "android": f"Yeobaek-{args.base_version}-Android-arm64.apk",
        "ios-simulator": f"Yeobaek-{args.base_version}-iOS-Simulator-arm64.zip",
        "windows": f"Yeobaek-{args.base_version}-Windows-x64-Setup.exe",
        "linux-amd64": f"yeobaek_{args.base_version}_amd64.deb",
        "linux-arm64": f"yeobaek_{args.base_version}_arm64.deb",
    }
    receipt = {"version": version, "source_sha256": {name: digest(data) for name, data in snapshot.items()}, "artifacts": {}}
    for target in args.targets:
        name = base_names[target]
        base = args.base_dir / name
        base_sha = digest(base.read_bytes())
        if sums.get(name) != base_sha:
            raise ValueError(f"Baseline checksum mismatch: {name}")
        print(f"Refreshing {target} from verified baseline...", flush=True)
        if target == "android":
            artifact = android(args, base, args.output, snapshot, version)
        elif target == "ios-simulator":
            artifact = ios_simulator(args, base, args.output, snapshot, version)
        elif target == "windows":
            artifact = windows(args, base, args.output, snapshot, version)
        else:
            artifact = linux(args, base, args.output, snapshot, version, target.split("-", 1)[1])
        receipt["artifacts"][artifact.name] = {"baseline_sha256": base_sha, "sha256": digest(artifact.read_bytes()),
                                               "size": artifact.stat().st_size}
        print(f"Created {artifact}", flush=True)
    print(json.dumps(receipt, indent=2), flush=True)


if __name__ == "__main__":
    main()
