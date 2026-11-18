"""Check an installed release using synthetic notes outside its install folder."""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path


TITLE = "Installer smoke fixture"
PAYLOAD = b"\x00Yeobaek installer binary attachment\xff\x01"


def pdf_fixture():
    content = b"0.1 0.4 0.8 rg 10 10 80 80 re f\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, raw in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + raw + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        data += f"{offset:010d} 00000 n \n".encode()
    data += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(data)


def check_preserved(data_dir, saved_title=None):
    receipt = json.loads((data_dir / "fixture.json").read_text())
    database = data_dir / "Yeobaek/notes.sqlite3"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        records = connection.execute("SELECT payload FROM operations WHERE entity_id = ?", (receipt["note_id"],))
        assert any(json.loads(row[0])["entity"]["title"] == TITLE for row in records)
        raw = connection.execute("SELECT content FROM attachments WHERE digest = ?", (receipt["digest"],)).fetchone()[0]
        assert raw == PAYLOAD
        if saved_title is not None:
            records = connection.execute("SELECT payload FROM operations WHERE kind = 'note'")
            assert any(json.loads(row[0])["entity"]["title"] == saved_title for row in records)
    print("Verified external note and attachment survived uninstall.")


def check_installed(args):
    app = args.app.resolve()
    sys.path[:0] = [str(app), str(app / "site-packages")]
    import yeobaek.store as storage
    from openpyxl import Workbook
    from yeobaek.notes import attachment_link
    from yeobaek.previews import raster_pdf_page, sheet_page

    assert Path(storage.__file__).resolve().is_relative_to(app)
    args.data.mkdir(parents=True, exist_ok=True)
    database = args.data / "Yeobaek/notes.sqlite3"
    store = storage.Store(database)
    receipt_file = args.data / "fixture.json"
    if args.mode == "seed":
        digest = store.save_attachment(PAYLOAD)
        note = store.put("note", {"title": TITLE, "body": attachment_link("fixture.bin", digest)})
        receipt_file.write_text(json.dumps({"note_id": note["entity"]["id"], "digest": digest}))
    receipt = json.loads(receipt_file.read_text())
    fixture_note = next(note for note in store.notes() if note["id"] == receipt["note_id"])
    assert fixture_note["title"] == TITLE
    assert store.attachment(receipt["digest"]) == PAYLOAD
    assert hashlib.sha256(PAYLOAD).hexdigest() == receipt["digest"]

    rendered = raster_pdf_page(pdf_fixture(), 0)
    assert rendered is not None and rendered.page_count == 1 and rendered.png.startswith(b"\x89PNG\r\n\x1a\n")
    workbook = Workbook()
    workbook.active.append(["Measurement", "Value"])
    workbook.active.append(["Fixture", 1.25])
    workbook.active.append(["Formula", "=B2*2"])
    excel = io.BytesIO()
    workbook.save(excel)
    cells = sheet_page("fixture.xlsx", excel.getvalue())
    assert cells.rows[1] == ("Fixture", "1.25") and cells.rows[2][1] == "=B2*2"
    print("Verified installed app source, SQLite, binary attachment, PDFium PNG and Excel preview.")
    exports_present = (app / "yeobaek/exports.py").is_file()
    release_tag = os.environ.get("RELEASE_TAG")
    if release_tag:
        release_version = tuple(int(part) for part in release_tag.removeprefix("v").split("-", 1)[0].split("."))
        if release_version >= (0, 4, 0):
            assert exports_present, "This release must include the portable exports module."
    if exports_present:
        import zipfile
        import yeobaek.exports as exports

        assert Path(exports.__file__).resolve().is_relative_to(app)
        portable = exports.export_note_zip(fixture_note, store.attachment)
        destination = f"attachments/{receipt['digest']}-fixture.bin"
        with zipfile.ZipFile(io.BytesIO(portable)) as archive:
            assert set(archive.namelist()) == {"note.md", destination}
            assert archive.read(destination) == PAYLOAD
            markdown = archive.read("note.md").decode("utf-8")
            assert f"[fixture.bin]({destination})" in markdown
        output = args.data / "fixture-note.zip"
        output.write_bytes(b"previous synthetic export")
        exports.atomic_write_file(output, portable)
        assert output.read_bytes() == portable
        print("Verified installed portable ZIP relative link, original binary attachment and atomic file replacement.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "check", "preserved"))
    parser.add_argument("--app", type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--saved-title")
    args = parser.parse_args()
    if args.mode == "preserved":
        check_preserved(args.data, args.saved_title)
    else:
        if args.app is None:
            parser.error("--app is required for installed runtime checks")
        check_installed(args)


if __name__ == "__main__":
    main()
