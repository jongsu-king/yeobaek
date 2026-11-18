"""Bounded, local previews of attached files."""

import csv
import io
import math
import struct
import threading
import zipfile
import zlib
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

_PDFIUM_LOCK = threading.Lock()
_CSV_LOCK = threading.Lock()
_XLSX = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TEXT = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".xml", ".py", ".r", ".tex", ".ini", ".toml"}
SHEET_ROWS = 24
SHEET_COLUMNS = 8


@dataclass(frozen=True)
class PDFPage:
    page_count: int
    png: bytes | None = None
    text: str = ""


@dataclass(frozen=True)
class SheetPage:
    names: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    width: int
    more_rows: bool
    more_columns: bool


def preview_kind(name, data):
    ext = Path(name).suffix.lower()
    if ext == ".pdf" or b"%PDF-" in data[:1024]:
        return "pdf"
    if ext in _XLSX or ext in {".xls", ".csv", ".tsv"}:
        return "sheet"
    if ext in _IMAGES or data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", b"GIF87a")):
        return "image"
    if ext in _TEXT:
        return "text"
    return None


def _png_chunk(kind, payload):
    checksum = zlib.crc32(kind + payload)
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _bitmap_png(bitmap):
    pixels = memoryview(bitmap.buffer).cast("B")
    row_bytes = bitmap.width * 3
    rows = b"".join(
        b"\x00" + pixels[offset:offset + row_bytes].tobytes()
        for offset in range(0, bitmap.stride * bitmap.height, bitmap.stride)
    )
    header = struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(rows)) + _png_chunk(b"IEND", b""))


def raster_pdf_page(data, index, width=920):
    """Render with the installed desktop PDFium runtime if available."""
    try:
        import pypdfium2 as pdfium
    except (ImportError, OSError, RuntimeError):
        return None
    with _PDFIUM_LOCK:
        with pdfium.PdfDocument(data) as document:
            count = len(document)
            if not count:
                return PDFPage(0)
            page = document[min(index, count - 1)]
            try:
                page_width, page_height = page.get_size()
                if not all(math.isfinite(value) and value > 0 for value in (page_width, page_height)):
                    raise ValueError("PDF 페이지의 크기를 읽을 수 없어요.")
                scale = min(width / page_width, 1360 / page_height, 2)
                bitmap = page.render(scale=scale, rev_byteorder=True)
                try:
                    if bitmap.mode != "RGB":
                        raise ValueError("PDF 페이지의 색상 형식을 읽을 수 없어요.")
                    return PDFPage(count, png=_bitmap_png(bitmap))
                finally:
                    bitmap.close()
            finally:
                page.close()


def text_pdf_page(data, index):
    from pypdf import PdfReader

    document = PdfReader(io.BytesIO(data))
    if document.is_encrypted and not document.decrypt(""):
        raise ValueError("암호가 설정된 PDF입니다. 원본 파일로 열어 주세요.")
    count = len(document.pages)
    if not count:
        return PDFPage(0)
    content = document.pages[min(index, count - 1)].extract_text() or ""
    return PDFPage(count, text=content[:32_000])


def _cell(value):
    return "" if value is None else str(value)[:260]


def _sheet_result(names, rows, column_count, row, column, *, more_rows=None):
    visible_rows = tuple(tuple(_cell(value) for value in values) for values in islice(rows, SHEET_ROWS + 1))
    return SheetPage(tuple(names), visible_rows[:SHEET_ROWS], max(1, min(SHEET_COLUMNS, column_count - column)),
                     len(visible_rows) > SHEET_ROWS if more_rows is None else more_rows,
                     column_count > column + SHEET_COLUMNS)


def _xlsx_page(data, sheet, row, column):
    from openpyxl import load_workbook

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError("이 Excel 파일은 미리보기에 펼치기에는 너무 큽니다. 원본 파일로 열어 주세요.")
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    try:
        names = [worksheet.title for worksheet in book.worksheets]
        if not names:
            raise ValueError("이 Excel 파일에 표시할 수 있는 표 시트가 없습니다.")
        worksheet = book[names[min(sheet, len(names) - 1)]]
        # Producers may omit dimensions or report A1:A1 despite writing more cells.
        # Read one bounded row page from the actual XML before slicing its columns.
        worksheet.reset_dimensions()
        rows = list(worksheet.iter_rows(
            min_row=row + 1, max_row=row + SHEET_ROWS + 1, values_only=True,
        ))
        column_count = max((len(values) for values in rows), default=0)
        visible = (values[column:column + SHEET_COLUMNS] for values in rows)
        return _sheet_result(names, visible, column_count, row, column)
    finally:
        book.close()


def _xls_page(data, sheet, row, column):
    import xlrd

    workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        names = workbook.sheet_names()
        worksheet = workbook.sheet_by_index(min(sheet, len(names) - 1))
        rows = (
            worksheet.row_values(index, start_colx=column, end_colx=column + SHEET_COLUMNS)
            for index in range(row, min(row + SHEET_ROWS + 1, worksheet.nrows))
        )
        return _sheet_result(names, rows, worksheet.ncols, row, column)
    finally:
        workbook.release_resources()


def sheet_page(name, data, sheet=0, row=0, column=0):
    ext = Path(name).suffix.lower()
    if ext in _XLSX:
        return _xlsx_page(data, sheet, row, column)
    if ext == ".xls":
        return _xls_page(data, sheet, row, column)
    delimiter = "\t" if ext == ".tsv" else ","
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp949")
    # csv's field limit is process-wide. Restore it even after malformed input,
    # while allowing a long cell within an already size-limited attachment.
    with _CSV_LOCK:
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(max(previous_limit, len(data)))
            for encoding in encodings:
                try:
                    with io.TextIOWrapper(io.BytesIO(data), encoding=encoding, newline="") as stream:
                        rows = list(islice(csv.reader(stream, delimiter=delimiter), row, row + SHEET_ROWS + 1))
                    break
                except UnicodeDecodeError:
                    if encoding == encodings[-1]:
                        raise ValueError("CSV·TSV 파일의 문자 인코딩을 읽을 수 없습니다. UTF-8로 저장해 주세요.") from None
        finally:
            csv.field_size_limit(previous_limit)
    width = max((len(values) for values in rows), default=0)
    visible = (values[column:column + SHEET_COLUMNS] for values in rows)
    return _sheet_result((), visible, width, row, column)


def spreadsheet_column(number):
    parts = []
    while number:
        number, remainder = divmod(number - 1, 26)
        parts.append(chr(ord("A") + remainder))
    return "".join(reversed(parts))
