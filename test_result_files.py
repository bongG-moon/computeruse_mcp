"""Synthetic result files only. No real Excel, COM, external links or app calls."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
import zipfile

from result_files import ResultFiles, ResultFileError, LIMITS


class Runtime:
    id = "test-session"
    active = True
    checks = 0
    def check_active(self):
        self.checks += 1
        if not self.active:
            raise RuntimeError("session ended")


def worksheet(rows):
    parts = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for number, values in enumerate(rows, 1):
        parts.append(f'<row r="{number}">')
        for column, value in enumerate(values):
            address, remaining = "", column + 1
            while remaining:
                remaining, digit = divmod(remaining - 1, 26)
                address = chr(digit + 65) + address
            if value is None:
                parts.append(f'<c r="{address}{number}" s="1"/>')
            else:
                parts.append(f'<c r="{address}{number}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        parts.append("</row>")
    return "".join(parts) + "</sheetData></worksheet>"


def workbook_bytes(rows=None, *, sheets=None, extras=None, overrides=None):
    sheets = sheets or {"Data": worksheet(rows or [["Category"], ["W"]])}
    names, relations = [], []
    contents = {}
    for index, (name, xml) in enumerate(sheets.items(), 1):
        names.append(f'<sheet name="{escape(name)}" sheetId="{index}" r:id="s{index}"/>')
        relations.append(f'<Relationship Id="s{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
        contents[f"xl/worksheets/sheet{index}.xml"] = xml
    contents["xl/workbook.xml"] = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                                 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
                                 "".join(names) + "</sheets></workbook>")
    contents["xl/_rels/workbook.xml.rels"] = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
                                            "".join(relations) + "</Relationships>")
    contents.update(extras or {})
    contents.update(overrides or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in contents.items():
            archive.writestr(name, value)
    return output.getvalue()


class ResultFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="result-files-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = Runtime()
        self.files = ResultFiles(self.runtime)

    def prepare(self, pattern="*.csv"):
        return self.files.prepare(str(self.root), pattern)["ticket_id"]

    def verify(self, ticket, name="result.csv", **kwargs):
        options = dict(column="Category", equals="W")
        options.update(kwargs)
        return self.files.verify(ticket, name, **options)

    def write(self, text, name="result.csv"):
        (self.root / name).write_text(text, encoding="utf-8-sig")

    def assert_code(self, code, callable_):
        with self.assertRaises(ResultFileError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)

    def test_fresh_csv_counts_all_rows_and_reports_only_aggregate_evidence(self):
        ticket = self.prepare()
        self.write("Category,Record\nW,001\nW,002\n,,\n\n")
        answer = self.verify(ticket)
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["content_verified"])
        self.assertFalse(answer["task_verified"])
        self.assertEqual((answer["data_rows"], answer["matches"], answer["mismatch_count"]), (2, 2, 0))
        self.assertEqual(answer["freshness"], "created_or_changed_since_prepare")
        self.assertEqual(answer["freshness_kind"], "created")
        self.assertNotIn("rows", answer)
        self.assertNotIn("001", str(answer))
        self.assert_code("result_ticket_expired", lambda: self.verify(ticket))

    def test_unchanged_file_and_timestamp_only_change_are_not_fresh(self):
        self.write("Category\nW\n")
        ticket = self.prepare()
        self.assert_code("result_file_unchanged", lambda: self.verify(ticket))
        path = self.root / "result.csv"
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2000000000))
        self.assert_code("result_file_unchanged", lambda: self.verify(ticket))

    def test_changed_contents_accepted_even_same_length_and_restored_mtime(self):
        self.write("Category\nN\n")
        path = self.root / "result.csv"
        before = path.stat()
        ticket = self.prepare()
        self.write("Category\nW\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        answer = self.verify(ticket)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["freshness_kind"], "changed")

    def test_mismatch_and_empty_export_never_report_content_success(self):
        ticket = self.prepare()
        self.write("Category,Record\nW,001\nN,002\n,003\n")
        answer = self.verify(ticket)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["file_verified"])
        self.assertEqual((answer["data_rows"], answer["matches"], answer["mismatch_count"]), (3, 1, 2))
        ticket = self.prepare()
        self.write("Category,Record\n,,\n\n")
        answer = self.verify(ticket)
        self.assertFalse(answer["content_verified"])
        self.assertEqual(answer["data_rows"], 0)

    def test_exact_header_unique_column_and_explicit_header_row(self):
        for text in ("Category,Category\nW,W\n", "Other\nW\n", " Category \nW\n"):
            ticket = self.prepare()
            self.write(text)
            self.assert_code("result_column_ambiguous", lambda: self.verify(ticket))
        ticket = self.prepare()
        self.write("Export title\nCategory,Record\nW,001\n")
        answer = self.verify(ticket, header_row=2)
        self.assertEqual(answer["data_rows"], 1)
        self.assertEqual(answer["column_index"], 1)

    def test_prepare_is_direct_directory_only_and_filtered(self):
        nested = self.root / "child"
        nested.mkdir()
        (nested / "unrelated.csv").write_text("private", encoding="utf-8")
        self.write("unrelated", "notes.txt")
        result = self.files.prepare(str(self.root), "export-*.csv")
        self.assertEqual(result["baseline_count"], 0)
        self.write("Category\nW\n", "export-one.csv")
        answer = self.verify(result["ticket_id"], "export-one.csv")
        self.assertTrue(answer["ok"])

    def test_paths_and_patterns_cannot_escape_or_search_recursively(self):
        for directory in ("relative", r"\\server\share", str(self.root / "missing")):
            with self.subTest(directory=directory), self.assertRaises(ResultFileError):
                self.files.prepare(directory)
        for pattern in ("../*.csv", "**/*.xlsx", "*.exe", r"child\*.csv"):
            with self.subTest(pattern=pattern), self.assertRaises(ResultFileError):
                self.files.prepare(str(self.root), pattern)
        ticket = self.prepare()
        for filename in ("../result.csv", r"child\result.csv", "result.csv:stream", str(self.root / "result.csv")):
            with self.subTest(filename=filename), self.assertRaises(ResultFileError):
                self.verify(ticket, filename)

    def test_linked_file_is_rejected_before_open(self):
        ticket = self.prepare()
        self.write("Category\nW\n")
        with patch("result_files._plain_chain", return_value=False), self.assertRaises(ResultFileError):
            self.verify(ticket)

    def test_session_and_ticket_expiry_are_enforced(self):
        now = [0]
        self.files = ResultFiles(self.runtime, clock=lambda: now[0])
        ticket = self.prepare()
        self.write("Category\nW\n")
        now[0] = LIMITS["ticket_seconds"] + 1
        self.assert_code("result_ticket_expired", lambda: self.verify(ticket))
        now[0] = 0
        ticket = self.prepare()
        self.runtime.id = "other-session"
        self.assert_code("result_ticket_expired", lambda: self.verify(ticket))
        self.runtime.active = False
        with self.assertRaisesRegex(RuntimeError, "session ended"):
            self.files.prepare(str(self.root))

    def test_modified_during_parse_is_not_verified_even_with_same_mtime_and_size(self):
        ticket = self.prepare()
        self.write("Category\nW\n")
        before = (self.root / "result.csv").stat()
        original = self.files._csv
        def mutate(*args):
            answer = original(*args)
            self.write("Category\nN\n")
            os.utime(self.root / "result.csv", ns=(before.st_atime_ns, before.st_mtime_ns))
            return answer
        with patch.object(self.files, "_csv", side_effect=mutate):
            self.assert_code("result_file_unstable", lambda: self.verify(ticket))

    def test_xlsx_ignores_empty_formatted_tail_and_counts_nonempty_data(self):
        ticket = self.prepare("*.xlsx")
        (self.root / "result.xlsx").write_bytes(workbook_bytes([["Category", "Record"], ["W", "001"], ["W", "002"], [None, None], ["", ""]]))
        answer = self.verify(ticket, "result.xlsx")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["data_rows"], 2)
        self.assertEqual(answer["sheet_name"], "Data")

    def test_xlsx_requires_named_sheet_when_multiple_exist(self):
        ticket = self.prepare("*.xlsx")
        (self.root / "result.xlsx").write_bytes(workbook_bytes(sheets={"One": worksheet([["Category"], ["N"]]), "Two": worksheet([["Category"], ["W"]])}))
        self.assert_code("result_sheet_ambiguous", lambda: self.verify(ticket, "result.xlsx"))
        answer = self.verify(ticket, "result.xlsx", sheet_name="Two")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["sheet_name"], "Two")

    def test_shared_string_xlsx_and_exact_stored_values(self):
        ticket = self.prepare("*.xlsx")
        sheet = '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>'
        extra = {"xl/sharedStrings.xml": '<sst><si><t>Category</t></si><si><r><t>W</t></r></si></sst>'}
        (self.root / "result.xlsx").write_bytes(workbook_bytes(sheets={"Data": sheet}, extras=extra))
        answer = self.verify(ticket, "result.xlsx")
        self.assertTrue(answer["content_verified"])

    def test_formula_in_selected_column_is_not_treated_as_calculated_proof(self):
        ticket = self.prepare("*.xlsx")
        sheet = worksheet([["Category"], ["W"]]).replace('<c r="A2" t="inlineStr"><is><t>W</t></is></c>', '<c r="A2" t="str"><f>"W"</f><v>W</v></c>')
        (self.root / "result.xlsx").write_bytes(workbook_bytes(sheets={"Data": sheet}))
        self.assert_code("result_formula_unverified", lambda: self.verify(ticket, "result.xlsx"))

    def test_xlsx_external_relationship_never_followed(self):
        ticket = self.prepare("*.xlsx")
        extra = {"xl/worksheets/_rels/sheet1.xml.rels": '<Relationships><Relationship Id="external" TargetMode="External" Target="https://example.test/data"/></Relationships>'}
        (self.root / "result.xlsx").write_bytes(workbook_bytes(extras=extra))
        self.assert_code("result_external_link", lambda: self.verify(ticket, "result.xlsx"))

    def test_xlsx_dtd_and_entity_and_malformed_xml_rejected(self):
        for xml in ('<!DOCTYPE x [<!ENTITY payload "W">]><worksheet>&payload;</worksheet>',
                    '<worksheet><broken>', '<!DOCTYPE x SYSTEM "file:///secret"><worksheet/>'):
            ticket = self.prepare("*.xlsx")
            (self.root / "result.xlsx").write_bytes(workbook_bytes(sheets={"Data": xml}))
            with self.assertRaises(ResultFileError):
                self.verify(ticket, "result.xlsx")

    def test_archive_traversal_and_zip_bomb_limits_are_rejected(self):
        ticket = self.prepare("*.xlsx")
        (self.root / "result.xlsx").write_bytes(workbook_bytes(extras={"../escape.xml": "bad"}))
        with self.assertRaises(ResultFileError):
            self.verify(ticket, "result.xlsx")
        ticket = self.prepare("*.xlsx")
        (self.root / "result.xlsx").write_bytes(workbook_bytes(extras={"xl/unused.xml": "x" * 100000}))
        with patch.dict(LIMITS, {"zip_uncompressed_bytes": 10000}):
            self.assert_code("result_file_limit", lambda: self.verify(ticket, "result.xlsx"))

    def test_file_directory_and_row_limits_are_visible_and_enforced(self):
        with patch.dict(LIMITS, {"directory_entries": 1}):
            self.write("x", "one.txt")
            self.write("x", "two.txt")
            self.assert_code("result_file_limit", lambda: self.prepare())
        ticket = self.prepare()
        self.write("Category\nW\nW\n")
        with patch.dict(LIMITS, {"rows": 2}):
            self.assert_code("result_file_limit", lambda: self.verify(ticket))
        with patch.dict(LIMITS, {"file_bytes": 2}):
            self.assert_code("result_file_limit", lambda: self.verify(ticket))

    def test_legacy_korean_csv_encoding_is_reported(self):
        ticket = self.prepare()
        (self.root / "result.csv").write_bytes("분류\n완료\n".encode("cp949"))
        answer = self.verify(ticket, column="분류", equals="완료")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["encoding"], "cp949")


if __name__ == "__main__":
    unittest.main()
