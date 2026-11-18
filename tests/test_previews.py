import csv
import io
import re
import unittest
import zipfile

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

from yeobaek.previews import SHEET_COLUMNS, SHEET_ROWS, sheet_page


def workbook_bytes(*, write_only=False, dimension=None):
    book = Workbook(write_only=write_only)
    worksheet = book.create_sheet("Measurements") if write_only else book.active
    worksheet.title = "Measurements"
    for number in range(1, 28):
        worksheet.append([f"row {number}", *range(2, 11)])
    output = io.BytesIO()
    book.save(output)
    data = output.getvalue()
    if dimension is None:
        return data
    changed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(changed, "w") as target:
        for entry in source.infolist():
            value = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                value = re.sub(rb'<dimension ref="[^"]+"/>', dimension, value)
            target.writestr(entry, value)
    return changed.getvalue()


class SpreadsheetPreviewTests(unittest.TestCase):
    def check_pagination(self, data):
        first = sheet_page("measurements.xlsx", data)
        self.assertEqual(first.names, ("Measurements",))
        self.assertEqual(len(first.rows), SHEET_ROWS)
        self.assertEqual(first.rows[0][0], "row 1")
        self.assertEqual(first.width, SHEET_COLUMNS)
        self.assertTrue(first.more_rows)
        self.assertTrue(first.more_columns)
        right = sheet_page("measurements.xlsx", data, column=SHEET_COLUMNS)
        self.assertEqual(right.rows[0], ("9", "10"))
        self.assertEqual(right.width, 2)
        self.assertFalse(right.more_columns)
        below = sheet_page("measurements.xlsx", data, row=SHEET_ROWS)
        self.assertEqual(below.rows[0][0], "row 25")
        self.assertEqual(len(below.rows), 3)
        self.assertFalse(below.more_rows)

    def test_excel_without_dimensions_keeps_rows_and_columns_accessible(self):
        self.check_pagination(workbook_bytes(write_only=True))

    def test_excel_with_underreported_dimensions_keeps_cells_accessible(self):
        self.check_pagination(workbook_bytes(dimension=b'<dimension ref="A1:A1"/>'))

    def test_normal_excel_still_paginates(self):
        self.check_pagination(workbook_bytes())

    def test_chart_sheet_does_not_hide_the_data_worksheet(self):
        book = Workbook()
        worksheet = book.active
        worksheet.title = "Measurements"
        worksheet.append(["value"])
        worksheet.append([4])
        chart = BarChart()
        chart.add_data(Reference(worksheet, min_col=1, min_row=1, max_row=2), titles_from_data=True)
        book.create_chartsheet("Graph", index=0).add_chart(chart)
        output = io.BytesIO()
        book.save(output)
        result = sheet_page("chart.xlsx", output.getvalue())
        self.assertEqual(result.names, ("Measurements",))
        self.assertEqual(result.rows, (("value",), ("4",)))

    def test_korean_excel_csv_encoding(self):
        result = sheet_page("measurements.csv", "항목,내용\r\n시료,측정값\r\n".encode("cp949"))
        self.assertEqual(result.rows, (("항목", "내용"), ("시료", "측정값")))

    def test_utf16_tabular_cells_preserve_newlines(self):
        data = '항목\t내용\r\n시료\t"첫째 줄\n둘째 줄"\r\n'.encode("utf-16")
        result = sheet_page("measurements.tsv", data)
        self.assertEqual(result.rows, (("항목", "내용"), ("시료", "첫째 줄\n둘째 줄")))

    def test_long_csv_cell_is_truncated_and_field_limit_restored(self):
        previous = csv.field_size_limit()
        data = ("first," + "x" * (previous + 100) + "\nnext,value\n").encode()
        result = sheet_page("measurements.csv", data)
        self.assertEqual(result.rows[0], ("first", "x" * 260))
        self.assertEqual(result.rows[1], ("next", "value"))
        self.assertEqual(csv.field_size_limit(), previous)

    def test_bad_encoding_restores_field_limit(self):
        previous = csv.field_size_limit()
        with self.assertRaisesRegex(ValueError, "인코딩"):
            sheet_page("measurements.csv", b"bad,\xff\xff\xff")
        self.assertEqual(csv.field_size_limit(), previous)


if __name__ == "__main__":
    unittest.main()
