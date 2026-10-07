"""Bounded local export verification. Never searches recursively or opens Excel.

Fresh file evidence is distinct from proof that an application ran the intended
query. A caller must explicitly map a business condition to the export column.
"""
from __future__ import annotations

import csv
import fnmatch
from functools import wraps
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import time
import threading
from urllib.parse import unquote
import uuid
import xml.etree.ElementTree as ET
import zipfile

from consent import _plain_chain


LIMITS = {"directory_entries": 2000, "file_bytes": 64 * 1024 * 1024,
          "baseline_bytes": 256 * 1024 * 1024, "zip_members": 2048,
          "zip_uncompressed_bytes": 256 * 1024 * 1024, "xml_member_bytes": 128 * 1024 * 1024,
          "zip_ratio": 1000, "rows": 100000, "cells": 5000000, "columns": 16384,
          "shared_strings": 500000, "text_characters": 32768, "tickets": 8, "ticket_seconds": 600}
LIMITATIONS = ["파일이 새로 생성되거나 내용이 바뀌었는지와 지정한 열의 값을 확인합니다. 프로그램의 조회 성공 자체를 증명하지 않습니다.",
              "조건 필드와 결과 열의 대응 관계는 호출자가 지정해야 합니다. 표시 서식이 아닌 저장된 문자열 값을 정확히 비교합니다.",
              "검증 열의 수식은 계산하지 않습니다. CSV 또는 명시한 XLSX 시트의 비어 있지 않은 데이터 행만 집계합니다."]


class ResultFileError(ValueError):
    def __init__(self, message, code="result_file_invalid"):
        super().__init__(message)
        self.code = code


def _fail(message, code="result_file_invalid"):
    raise ResultFileError(message, code)


def _signature(info):
    # Windows path stat and handle fstat can expose different ctime semantics
    # (creation versus metadata change). Identity, size, mtime and two hashes
    # detect replacements/writes without treating that difference as a write.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _filename(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 255 or value in {".", ".."} or
            any(ord(ch) < 32 or ch in '/\\:*?<>|"' for ch in value) or value[-1] in ". "):
        _fail("폴더를 제외한 정확한 파일 이름을 지정하세요.")
    return value


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class ResultFiles:
    def __init__(self, runtime, *, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.tickets = {}
        self._lock = threading.RLock()

    def _active(self):
        self.runtime.check_active()

    def _read(self, path):
        self._active()
        if not _plain_chain(path) or not path.is_file():
            _fail("일반 로컬 파일만 읽을 수 있습니다. 연결·네트워크 경로는 지원하지 않습니다.")
        before = path.stat()
        if before.st_nlink != 1 or before.st_size > LIMITS["file_bytes"]:
            _fail("파일이 연결 파일이거나 읽기 크기 제한을 넘었습니다.", "result_file_limit")
        digest, chunks, count = hashlib.sha256(), [], 0
        with path.open("rb") as stream:
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                _fail("파일이 읽기 직전에 변경됐습니다. 내보내기가 끝난 뒤 확인하세요.", "result_file_unstable")
            while True:
                self._active()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > LIMITS["file_bytes"]:
                    _fail("파일 읽기 크기 제한을 넘었습니다.", "result_file_limit")
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
        if (count != before.st_size or _signature(before) != _signature(after) or
                _signature(before) != _signature(path.stat()) or not _plain_chain(path)):
            _fail("파일이 읽는 동안 변경됐습니다. 내보내기가 끝난 뒤 확인하세요.", "result_file_unstable")
        return b"".join(chunks), digest.hexdigest(), _signature(before)

    @_serialized
    def prepare(self, directory, pattern="*.xlsx"):
        self._active()
        if (not isinstance(directory, str) or not directory or not Path(directory).is_absolute() or
                directory.startswith(("\\\\", "//"))):
            _fail("알고 있는 결과 폴더의 로컬 전체 경로를 지정하세요.")
        path = Path(directory)
        if not _plain_chain(path) or not path.is_dir():
            _fail("결과 폴더가 없거나 연결 경로입니다.")
        if (not isinstance(pattern, str) or not 1 <= len(pattern) <= 255 or
                any(ord(ch) < 32 or ch in '/\\:' for ch in pattern) or
                not pattern.lower().endswith((".xlsx", ".csv"))):
            _fail("폴더 경로 없이 *.xlsx 또는 *.csv 형태의 파일 이름 조건을 지정하세요.")
        baseline, scanned, total = {}, 0, 0
        with os.scandir(path) as entries:
            for entry in entries:
                self._active()
                scanned += 1
                if scanned > LIMITS["directory_entries"]:
                    _fail("결과 폴더의 항목 수가 제한을 넘었습니다. 별도 내보내기 폴더를 지정하세요.", "result_file_limit")
                if not fnmatch.fnmatchcase(entry.name.casefold(), pattern.casefold()) or entry.is_dir(follow_symlinks=False):
                    continue
                candidate = path / entry.name
                total += candidate.stat().st_size
                if total > LIMITS["baseline_bytes"]:
                    _fail("기준 파일의 합계 크기가 제한을 넘었습니다. 파일 이름 조건을 좁혀주세요.", "result_file_limit")
                _, digest, signature = self._read(candidate)
                baseline[entry.name.casefold()] = {"sha256": digest, "signature": signature}
        now = self.clock()
        self.tickets = {key: value for key, value in self.tickets.items() if now - value["created"] <= LIMITS["ticket_seconds"]}
        if len(self.tickets) >= LIMITS["tickets"]:
            _fail("준비 중인 결과 확인이 너무 많습니다. 기존 확인을 끝낸 뒤 다시 준비하세요.", "result_file_limit")
        ticket_id = uuid.uuid4().hex
        self.tickets[ticket_id] = {"directory": str(path.resolve()), "pattern": pattern, "baseline": baseline,
                                   "created": now, "session_id": getattr(self.runtime, "id", None)}
        return {"ok": True, "ticket_id": ticket_id, "directory": str(path.resolve()), "pattern": pattern,
                "baseline_count": len(baseline), "scanned_entries": scanned, "baseline_bytes": total,
                "limits": dict(LIMITS), "task_verified": False,
                "next": "이제 요청한 내보내기를 실행하고 실제 파일 이름·검증할 열 이름·예상 값을 지정하세요.",
                "limitations": list(LIMITATIONS)}

    @_serialized
    def verify(self, ticket_id, filename, column, equals, header_row=1, sheet_name=None):
        self._active()
        ticket = self.tickets.get(ticket_id) if isinstance(ticket_id, str) else None
        if (not ticket or self.clock() - ticket["created"] > LIMITS["ticket_seconds"] or
                ticket["session_id"] != getattr(self.runtime, "id", None)):
            _fail("이 세션에서 준비한 유효한 결과 확인 번호가 아닙니다.", "result_ticket_expired")
        name = _filename(filename)
        if not fnmatch.fnmatchcase(name.casefold(), ticket["pattern"].casefold()):
            _fail("준비할 때 지정한 파일 이름 조건과 다릅니다.")
        if (not isinstance(column, str) or not column.strip() or len(column) > 512 or
                not isinstance(equals, str) or len(equals) > LIMITS["text_characters"] or
                type(header_row) is not int or not 1 <= header_row <= 100 or
                sheet_name is not None and (not isinstance(sheet_name, str) or not 1 <= len(sheet_name) <= 128)):
            _fail("열 이름·예상 문자열·헤더 행(1~100)·시트 이름을 확인하세요.")
        path = Path(ticket["directory"]) / name
        if path.suffix.casefold() not in {".csv", ".xlsx"}:
            _fail("CSV와 XLSX 결과만 지원합니다.")
        try:
            data, digest, signature = self._read(path)
            baseline = ticket["baseline"].get(name.casefold())
            if baseline and baseline["sha256"] == digest:
                _fail("준비 전부터 있던 파일과 내용이 같습니다. 이번 내보내기의 새 결과로 인정하지 않았습니다.", "result_file_unchanged")
            if path.suffix.casefold() == ".csv":
                if sheet_name is not None:
                    _fail("CSV에는 시트 이름을 지정하지 않습니다.")
                result = self._csv(data, column, equals, header_row)
            else:
                result = self._xlsx(data, column, equals, header_row, sheet_name)
            _, final_digest, final_signature = self._read(path)
            if digest != final_digest or signature != final_signature:
                _fail("검증하는 동안 파일이 변경됐습니다. 결과를 확정하지 않았습니다.", "result_file_unstable")
        except ResultFileError:
            raise
        except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile, ET.ParseError, RuntimeError, KeyError) as error:
            raise ResultFileError("결과 파일을 안전하게 읽지 못했습니다. 파일 형식·권한·내보내기 완료 상태를 확인하세요.", "result_file_read_failed") from error
        self._active()
        success = result["data_rows"] > 0 and result["mismatch_count"] == 0
        self.tickets.pop(ticket_id, None)
        return {"ok": success, "file_verified": True, "content_verified": success, "task_verified": False,
                "ticket_id": ticket_id, "filename": name, "column": column, "header_row": header_row,
                "sha256": digest, "file_bytes": len(data), "freshness": "created_or_changed_since_prepare",
                "freshness_kind": "changed" if baseline else "created", **result,
                "limits": dict(LIMITS), "limitations": list(LIMITATIONS)}

    def _count(self, rows, column, expected, header_row):
        selected, count, matches, cells = None, 0, 0, 0
        seen_header = False
        for row_number, row in rows:
            self._active()
            cells += len(row)
            if row_number > LIMITS["rows"] or cells > LIMITS["cells"]:
                _fail("결과의 행·셀 수가 읽기 제한을 넘었습니다.", "result_file_limit")
            if row_number < header_row:
                continue
            if row_number == header_row:
                hits = [index for index, cell in row.items() if cell[0] == column]
                if len(hits) != 1:
                    _fail("지정한 열 이름을 정확히 한 개 찾지 못했습니다. 헤더 행과 열 이름을 확인하세요.", "result_column_ambiguous")
                selected, seen_header = hits[0], True
                if row[selected][1]:
                    _fail("수식으로 계산한 열 이름은 지원하지 않습니다.")
                continue
            if not seen_header:
                _fail("지정한 헤더 행이 없습니다.", "result_column_ambiguous")
            if not any(text.strip() or formula for text, formula in row.values()):
                continue
            text, formula = row.get(selected, ("", False))
            if formula:
                _fail("검증할 열에 수식이 있습니다. 계산된 값을 내보낸 파일로 확인하세요.", "result_formula_unverified")
            count += 1
            matches += int(text == expected)
        if not seen_header:
            _fail("지정한 헤더 행이 없습니다.", "result_column_ambiguous")
        return {"data_rows": count, "matches": matches, "mismatch_count": count - matches,
                "column_index": selected + 1, "comparison": "exact_stored_text"}

    def _csv(self, data, column, expected, header_row):
        try:
            text, encoding = data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            text, encoding = data.decode("cp949"), "cp949"
        if "\x00" in text:
            _fail("CSV의 문자 인코딩을 확인하세요.")
        def rows():
            for index, row in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True), 1):
                if len(row) > LIMITS["columns"] or any(len(value) > LIMITS["text_characters"] for value in row):
                    _fail("CSV 열 수 또는 셀 문자 수 제한을 넘었습니다.", "result_file_limit")
                yield index, {i: (value, False) for i, value in enumerate(row)}
        return {**self._count(rows(), column, expected, header_row), "format": "csv", "encoding": encoding, "sheet_name": None}

    def _xlsx(self, data, column, expected, header_row, sheet_name):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries, total = {}, 0
            for info in archive.infolist():
                self._active()
                parts = PurePosixPath(info.filename).parts
                if (info.filename in entries or len(entries) >= LIMITS["zip_members"] or
                        not parts or info.filename.startswith(("/", "\\")) or ".." in parts or
                        "\\" in info.filename or ":" in info.filename or info.flag_bits & 1 or
                        (info.external_attr >> 16) & 0o170000 == 0o120000):
                    _fail("XLSX에 중복·연결·암호화·경로 이탈 항목이 있거나 파일 수 제한을 넘었습니다.")
                total += info.file_size
                if (total > LIMITS["zip_uncompressed_bytes"] or info.file_size > LIMITS["xml_member_bytes"] or
                        info.file_size > max(1, info.compress_size) * LIMITS["zip_ratio"]):
                    _fail("XLSX 압축 해제 크기 또는 압축률 제한을 넘었습니다.", "result_file_limit")
                entries[info.filename] = info

            def xml(name):
                self._active()
                if name not in entries:
                    _fail("XLSX의 필수 구성 파일이 없습니다.")
                raw = archive.read(name)
                upper = raw.upper()
                if b"\x00" in raw or b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                    _fail("DTD·외부 엔터티·지원하지 않는 XML 인코딩을 포함한 XLSX는 읽지 않습니다.")
                return raw

            def relationships(name):
                result = {}
                for item in ET.fromstring(xml(name)):
                    if item.tag.rsplit("}", 1)[-1] != "Relationship":
                        continue
                    target = item.get("Target", "")
                    decoded = unquote(target)
                    if (item.get("TargetMode", "").lower() == "external" or
                            ":" in decoded or "\\" in decoded or ".." in PurePosixPath(decoded).parts or
                            any(ord(ch) < 32 for ch in decoded)):
                        _fail("외부 링크 또는 경로 이탈 관계를 포함한 XLSX는 읽지 않습니다.", "result_external_link")
                    identity = item.get("Id")
                    if not identity or identity in result:
                        _fail("XLSX 관계 식별자가 중복되거나 없습니다.")
                    result[identity] = item
                return result

            for name in entries:
                if name.endswith(".rels"):
                    relationships(name)
            rels = relationships("xl/_rels/workbook.xml.rels")
            workbook = ET.fromstring(xml("xl/workbook.xml"))
            sheets = [item for item in workbook.iter() if item.tag.rsplit("}", 1)[-1] == "sheet"]
            choices = [item for item in sheets if sheet_name is None or item.get("name") == sheet_name]
            if len(choices) != 1:
                _fail("시트를 정확히 한 개 선택해야 합니다. 시트 이름을 지정하세요.", "result_sheet_ambiguous")
            sheet = choices[0]
            relation_id = next((value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"), None)
            relation = rels.get(relation_id)
            if relation is None or not relation.get("Type", "").endswith("/worksheet"):
                _fail("선택한 워크시트의 관계를 확인하지 못했습니다.")

            def member(target):
                return target.lstrip("/") if target.startswith("/") else "xl/" + target

            shared = []
            shared_rel = [item for item in rels.values() if item.get("Type", "").endswith("/sharedStrings")]
            if len(shared_rel) > 1:
                _fail("문자열 표가 중복된 XLSX입니다.")
            shared_name = member(shared_rel[0].get("Target", "")) if shared_rel else "xl/sharedStrings.xml"
            if shared_name in entries:
                for _, item in ET.iterparse(io.BytesIO(xml(shared_name)), events=("end",)):
                    if item.tag.rsplit("}", 1)[-1] == "si":
                        self._active()
                        value = "".join(node.text or "" for node in item.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                        if len(shared) >= LIMITS["shared_strings"] or len(value) > LIMITS["text_characters"]:
                            _fail("XLSX 문자열 크기 제한을 넘었습니다.", "result_file_limit")
                        shared.append(value)
                        item.clear()

            def rows():
                previous = 0
                for _, row in ET.iterparse(io.BytesIO(xml(member(relation.get("Target", "")))), events=("end",)):
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    self._active()
                    try:
                        row_number = int(row.get("r", previous + 1))
                    except ValueError:
                        _fail("XLSX 행 번호를 확인할 수 없습니다.")
                    if row_number <= previous:
                        _fail("XLSX 행 번호가 중복되거나 순서가 다릅니다.")
                    previous, values, last_column = row_number, {}, -1
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        address = cell.get("r")
                        index = last_column + 1
                        if address:
                            match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)
                            if not match or int(match[2]) != row_number:
                                _fail("XLSX 셀 주소를 확인할 수 없습니다.")
                            index = 0
                            for letter in match[1]:
                                index = index * 26 + ord(letter) - 64
                            index -= 1
                        if index in values or index >= LIMITS["columns"]:
                            _fail("XLSX 열 번호가 중복되거나 제한을 넘었습니다.", "result_file_limit")
                        last_column = index
                        nodes = {node.tag.rsplit("}", 1)[-1]: node for node in cell}
                        value = nodes["v"].text or "" if "v" in nodes else ""
                        kind = cell.get("t", "n")
                        if kind == "s":
                            if not value.isdigit() or int(value) >= len(shared):
                                _fail("XLSX 문자열 참조가 올바르지 않습니다.")
                            value = shared[int(value)]
                        elif kind == "inlineStr":
                            value = "".join(node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                        elif kind == "b":
                            value = {"0": "FALSE", "1": "TRUE"}.get(value, value)
                        elif kind == "e":
                            _fail("오류 값을 포함한 결과 파일은 확인할 수 없습니다.", "result_cell_error")
                        if len(value) > LIMITS["text_characters"]:
                            _fail("XLSX 셀 문자 수 제한을 넘었습니다.", "result_file_limit")
                        values[index] = (value, "f" in nodes)
                    yield row_number, values
                    row.clear()
            return {**self._count(rows(), column, expected, header_row), "format": "xlsx", "sheet_name": sheet.get("name")}
