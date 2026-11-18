"""Portable note archives and atomic writes for user-selected export files."""

import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from .notes import attachment_filename, attachment_links
from .store import ATTACHMENT_MAX_BYTES


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^( {0,3})([-+*]|[0-9]{1,9}[.)])([ \t]+)")
_QUOTE = re.compile(r"^ {0,3}>[ \t]?")
_BLOCK_START = re.compile(r"^ {0,3}(?:#{1,6}(?:[ \t]|$)|[=-]{2,}[ \t]*$|(?:[-*_][ \t]*){3,}$)")
_BACKTICKS = re.compile(r"`+")


def _escaped(value, position):
    preceding = position
    while preceding and value[preceding - 1] == "\\":
        preceding -= 1
    return (position - preceding) % 2 == 1


def _strip_containers(text, containers):
    retained = []
    for kind, width in containers:
        if kind == "quote":
            quote = _QUOTE.match(text)
            if quote is None:
                break
            text = text[quote.end():]
        elif text.strip():
            if not text.startswith(" " * width):
                break
            text = text[width:]
        retained.append((kind, width))
    return text, retained


def _code_mask(body):
    """Mask code spans and blocks without moving their source positions."""
    visible = list(body)
    offset = 0
    fence = None
    containers = []
    indented_code = False
    blank_before = True
    boundaries = {0, len(body)}
    for line in body.splitlines(keepends=True):
        if blank_before:
            boundaries.add(offset)
        text = line.expandtabs(4).rstrip("\r\n")
        logical, retained = _strip_containers(text, containers)
        if len(retained) != len(containers):
            fence = None
            indented_code = False
        containers = retained
        hidden = fence is not None
        if fence is not None:
            delimiter = _FENCE.match(logical)
            if delimiter and delimiter[1].startswith(fence) and not delimiter[2].strip():
                fence = None
        else:
            opened = False
            while True:
                quote = _QUOTE.match(logical)
                item = _LIST_ITEM.match(logical)
                if quote:
                    containers.append(("quote", 0))
                    logical = logical[quote.end():]
                elif item:
                    padding = len(item[3]) if len(item[3]) <= 4 else 1
                    width = len(item[1]) + len(item[2]) + padding
                    containers.append(("list", width))
                    logical = logical[width:]
                else:
                    break
                opened = True
            if opened:
                boundaries.add(offset)
            indented = logical.startswith("    ")
            if indented and (blank_before or indented_code or opened):
                indented_code = True
            elif logical.strip():
                indented_code = False
            hidden = indented_code
            delimiter = _FENCE.match(logical)
            if delimiter and (delimiter[1][0] != "`" or "`" not in delimiter[2]):
                fence = delimiter[1]
                hidden = True
            if _BLOCK_START.match(logical):
                boundaries.update((offset, offset + len(line)))
        if hidden:
            boundaries.update((offset, offset + len(line)))
            visible[offset:offset + len(line)] = ("\n" if char == "\n" else " " for char in line)
        blank_before = not text.strip()
        offset += len(line)
    masked = "".join(visible)
    boundaries = sorted(boundaries)
    for begin, end in zip(boundaries, boundaries[1:]):
        runs = list(_BACKTICKS.finditer(masked, begin, end))
        next_runs = {}
        closers = {}
        for index in range(len(runs) - 1, -1, -1):
            run = runs[index]
            opener = run.start() + int(_escaped(masked, run.start()))
            length = run.end() - opener
            if length and length in next_runs:
                closers[index] = (opener, next_runs[length])
            next_runs[len(run.group())] = index
        index = 0
        while index < len(runs):
            match = closers.get(index)
            if match is None:
                index += 1
                continue
            opener, closing_index = match
            close = runs[closing_index].end()
            visible[opener:close] = ("\n" if char == "\n" else " " for char in body[opener:close])
            index = closing_index + 1
    return visible


def _link_positions(body):
    """Locate attachment links while retaining literal Markdown code examples."""
    visible = _code_mask(body)
    cursor = 0
    for name, digest, reference in attachment_links(body):
        start = body.find(reference, cursor)
        cursor = start + len(reference)
        if visible[start] == "[" and not _escaped(body, start):
            yield start, cursor, name, digest, reference


def _archive_path(name, digest):
    filename = attachment_filename(name)
    if len(filename.encode("utf-8")) > 160:
        suffix = Path(filename).suffix
        suffix_size = len(suffix.encode("utf-8"))
        if suffix_size >= 160:
            suffix = ""
            suffix_size = 0
        stem = filename[:-len(suffix)] if suffix else filename
        filename = stem.encode("utf-8")[:160 - suffix_size].decode("utf-8", errors="ignore") + suffix
    return f"attachments/{digest}-{filename}"


def export_note_zip(note, read_attachment):
    """Return note.md and verified original attachments with portable relative links.

    ``read_attachment`` accepts a SHA-256 digest and returns bytes or ``None``.
    Missing or corrupt attachments raise before an archive can be returned.
    """
    body = note["body"]
    title = note["title"].strip() or "제목 없는 노트"
    output = io.BytesIO()
    pieces = []
    cursor = 0
    written = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for start, end, name, digest, reference in _link_positions(body):
            destination = _archive_path(name, digest)
            if destination not in written:
                content = read_attachment(digest)
                if content is None:
                    raise ValueError(f"{name} 파일이 이 기기에 없습니다. Google Drive 동기화 후 다시 내보내세요.")
                if not isinstance(content, bytes) or len(content) > ATTACHMENT_MAX_BYTES:
                    raise ValueError(f"{name} 첨부 파일을 내보낼 수 없습니다.")
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError(f"{name} 첨부 파일의 내용이 저장된 ID와 일치하지 않습니다.")
                archive.writestr(destination, content)
                written.add(destination)
            link = reference[:reference.rfind("](") + 2] + quote(destination, safe="/") + ")"
            pieces.extend((body[cursor:start], link))
            cursor = end
        pieces.append(body[cursor:])
        archive.writestr("note.md", "# " + title + "\n\n" + "".join(pieces))
    return output.getvalue()


def atomic_write_file(path, content):
    """Replace an export only after its complete contents have reached the disk.

    Resolve the destination first so selecting a symlink keeps its usual target
    semantics. Existing target permissions survive the replacement.
    """
    destination = Path(path).expanduser().resolve()
    mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".yeobaek-export-", suffix=".tmp",
            dir=destination.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            if mode is not None:
                os.chmod(temporary, mode)
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.chmod(0o600)
            temporary.unlink()
