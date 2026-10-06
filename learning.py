"""Local, inert names for observed UIA controls; never records or replays input.

Only stable selectors and control capabilities are persisted. Every use reads a
fresh approved window. Friendly screen names and guidance are user data, not
evidence that a screen is open, instructions to the host, or extra permissions.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from inspection import control_selector, suggested_operations
from operations import OperationError, _elements, _limited, _name, _payload, _unique, validate_selector
from vendor.guard import normalize_exe, utc_now


class LearningError(OperationError):
    pass


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise LearningError(f"{name}은 {'0' if empty else '1'}~{maximum}자 문자열이어야 합니다.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise LearningError(f"{name}에 저장할 수 없는 문자가 있습니다.") from None
    if "\x00" in value:
        raise LearningError(f"{name}에 사용할 수 없는 문자가 있습니다.")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise LearningError("요소 ID에는 80자 이내의 영문, 숫자, 밑줄과 하이픈을 사용하세요.")
    return value


def _protected(element):
    return (any(element.get(key) is True for key in ("is_password", "password", "is_protected", "protected"))
            or element.get("role") == "Password")


def _identity(element):
    """Project identity only, never value/help text/tokens/coordinates/window titles."""
    result = {key: copy.deepcopy(element[key]) for key in
              ("automation_id", "role", "element_index", "parent_index") if key in element}
    label = _name(element)
    # Some providers put the entered/selected value into the accessible name.
    # Such a value is unsuitable for a reusable label and must not be saved.
    if (not _protected(element) and element.get("role") not in ("Window", "Document")
            and isinstance(label, str) and label.strip() and len(label) <= 1000
            and not (isinstance(element.get("value"), str) and element["value"] and label == element["value"])):
        result["name"] = label
    return result


def _fingerprint(element):
    role = _text(element.get("role"), "요소 종류", 1000)
    actions = element.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 64 or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", item) for item in actions):
        raise LearningError("관찰된 요소의 기능 목록이 올바르지 않습니다.", "invalid_control")
    return {"role": role, "actions": sorted(set(actions))}


def stable_selector(snapshot, element):
    """Prefer a uniquely identified parent, including for globally unique IDs."""
    if _protected(element):
        raise LearningError("암호 입력 요소는 학습 목록에 저장하지 않습니다.", "protected_control")
    if type(element.get("element_index")) is not int or element["element_index"] < 0 or element.get("synthetic_ancestor"):
        raise LearningError("실제 관찰된 조작 요소만 학습할 수 있습니다.", "invalid_control")
    _fingerprint(element)
    original = _elements(snapshot)
    safe_elements = [_identity(item) for item in original]
    safe_snapshot = {"elements": safe_elements}
    safe_element = safe_elements[original.index(element)]
    selector = control_selector(safe_snapshot, safe_element)
    if selector is None:
        raise LearningError("이 요소를 구분할 고유한 이름 또는 자동화 ID가 없습니다. 상위 영역을 확인하세요.", "ambiguous_control")
    selector["role"] = element["role"]
    # A selector already scoped by discovery is the strongest available anchor.
    if "within" not in selector:
        by_index = {item["element_index"]: item for item in safe_elements if type(item.get("element_index")) is int}
        parent, seen = safe_element.get("parent_index"), set()
        for _ in range(32):
            if type(parent) is not int or parent in seen or parent not in by_index:
                break
            seen.add(parent)
            ancestor = by_index[parent]
            # Window names usually contain document titles or current content.
            if ancestor.get("role") not in {"Window", "Document"}:
                scope = control_selector(safe_snapshot, ancestor)
                if scope is not None and "within" not in scope:
                    scope = {**scope, **({"role": ancestor["role"]} if ancestor.get("role") else {})}
                    candidate = {**selector, "within": scope}
                    if _unique(safe_snapshot, candidate) is safe_element:
                        selector = candidate
                        break
            parent = ancestor.get("parent_index")
    validate_selector(selector)
    # Verify that sanitizing the projection did not manufacture a unique match.
    if _unique(snapshot, selector) is not element:
        raise LearningError("요소 선택 기준이 현재 화면과 일치하지 않습니다.", "target_mismatch")
    return selector


class ElementLibrary:
    MAX_BYTES = 4 * 1024 * 1024
    MAX_ITEMS = 1000
    LOCK_TIMEOUT = 5
    DEPTH = 32
    COUNT = 5000
    FIELDS = {"id", "label", "screen", "instructions", "program_id", "program_binding", "selector", "fingerprint", "revision", "updated_at"}

    def __init__(self, state_dir, config):
        root = Path(state_dir)
        if (not root.is_absolute() or str(root).startswith(("\\\\", "//")) or ".." in root.parts
                or (os.name == "nt" and any(":" in part for part in root.parts[1:]))):
            raise LearningError("요소 저장 폴더는 이 PC의 연결되지 않은 전체 경로여야 합니다.", "unsafe_store")
        self.path = root / "elements.json"
        self.config = copy.deepcopy(config)
        self.lock = threading.Lock()
        self._check_paths()

    @staticmethod
    def _reject_link(path, *, file=False):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or (file and info.st_nlink > 1):
            raise LearningError("요소 저장 위치에 연결 경로나 연결 파일을 사용할 수 없습니다.", "unsafe_store")
        if file and not stat.S_ISREG(info.st_mode):
            raise LearningError("요소 저장 파일의 종류가 올바르지 않습니다.", "unsafe_store")

    def _check_paths(self):
        for path in reversed(self.path.parent.parents):
            self._reject_link(path)
        self._reject_link(self.path.parent)
        self._reject_link(self.path, file=True)
        self._reject_link(self.path.with_suffix(".lock"), file=True)

    @contextmanager
    def _locked(self):
        with self.lock:
            try:
                self._check_paths()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._check_paths()
                lock_path = self.path.with_suffix(".lock")
                with lock_path.open("a+b") as stream:
                    deadline = time.monotonic() + self.LOCK_TIMEOUT
                    while True:
                        stream.seek(0)
                        try:
                            if os.name == "nt":
                                import msvcrt
                                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise LearningError("다른 연결에서 요소를 저장 중입니다. 잠시 후 다시 시도하세요.", "store_busy") from None
                            time.sleep(0.025)
                    try:
                        # Lock byte 0 even past EOF before initializing it, so
                        # simultaneous first writers cannot flush into another
                        # process's Windows mandatory byte-range lock.
                        if stream.seek(0, os.SEEK_END) == 0:
                            stream.write(b"0")
                            stream.flush()
                        self._check_paths()
                        yield
                    finally:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise LearningError("요소 저장 파일을 읽거나 쓰지 못했습니다. 기존 파일은 보존됩니다.", "store_error") from exc

    @classmethod
    def _validate_entry(cls, item):
        if not isinstance(item, dict) or set(item) != cls.FIELDS:
            raise LearningError("저장된 요소 형식이 올바르지 않습니다. 기존 파일은 보존됩니다.", "invalid_store")
        _id(item["id"])
        _id(item["program_id"])
        _text(item["label"], "요소 이름", 100)
        _text(item["screen"], "화면 분류", 100, empty=True)
        _text(item["instructions"], "사용 설명", 4000, empty=True)
        _text(item["updated_at"], "수정 시각", 80)
        if not isinstance(item["program_binding"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["program_binding"]):
            raise LearningError("저장된 프로그램 연결 정보가 올바르지 않습니다.", "invalid_store")
        validate_selector(item["selector"])
        for selector in (item["selector"], item["selector"].get("within", {})):
            for key, value in selector.items():
                if key != "within":
                    _text(value, "요소 선택 기준", 1000)
        fp = item["fingerprint"]
        if not isinstance(fp, dict) or set(fp) != {"role", "actions"} or _fingerprint(fp) != fp or item["selector"].get("role") != fp["role"]:
            raise LearningError("저장된 요소 종류 또는 기능 정보가 올바르지 않습니다.", "invalid_store")
        if type(item["revision"]) is not int or item["revision"] < 1:
            raise LearningError("저장된 요소 버전이 올바르지 않습니다.", "invalid_store")

    def _read(self):
        if not self.path.exists():
            return []
        with self.path.open("rb") as stream:
            raw = stream.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise LearningError("요소 저장 파일이 4 MB 제한을 넘었습니다.", "store_limit")
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            raise LearningError("요소 저장 파일이 손상되었습니다. 기존 파일은 보존됩니다.", "invalid_store") from None
        if (not isinstance(data, dict) or set(data) != {"version", "elements"} or type(data["version"]) is not int
                or data["version"] != 1 or not isinstance(data["elements"], list) or len(data["elements"]) > self.MAX_ITEMS):
            raise LearningError("요소 저장 파일 형식 또는 개수 제한이 올바르지 않습니다.", "invalid_store")
        ids, names = set(), set()
        for item in data["elements"]:
            self._validate_entry(item)
            name = (item["program_id"], item["screen"].casefold(), item["label"].casefold())
            if item["id"] in ids or name in names:
                raise LearningError("저장된 요소 ID 또는 이름이 중복됩니다. 기존 파일은 보존됩니다.", "invalid_store")
            ids.add(item["id"])
            names.add(name)
        return data["elements"]

    def _write(self, items):
        raw = json.dumps({"version": 1, "elements": items}, ensure_ascii=False, indent=2).encode("utf-8")
        if len(items) > self.MAX_ITEMS or len(raw) > self.MAX_BYTES:
            raise LearningError("요소 저장 한도는 1,000개 또는 4 MB입니다.", "store_limit")
        self._check_paths()
        temporary = self.path.with_name("elements.tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_paths()
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def all(self, program_id=None, screen=None):
        if program_id is not None:
            _id(program_id)
        if screen is not None:
            _text(screen, "화면 분류", 100, empty=True)
        with self._locked():
            return copy.deepcopy([item for item in self._read() if (program_id is None or item["program_id"] == program_id)
                                  and (screen is None or item["screen"] == screen)])

    def get(self, element_id):
        _id(element_id)
        item = next((item for item in self.all() if item["id"] == element_id), None)
        if item is None:
            raise LearningError("저장된 요소 ID를 찾지 못했습니다.", "element_not_found")
        return item

    def delete(self, element_id, *, expected_revision=None):
        _id(element_id)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise LearningError("삭제할 요소의 현재 revision을 지정하세요.")
        with self._locked():
            items = self._read()
            previous = next((item for item in items if item["id"] == element_id), None)
            if expected_revision is not None and (previous is None or previous["revision"] != expected_revision):
                raise LearningError("삭제할 요소가 변경되었습니다. 목록의 최신 revision을 확인하세요.", "revision_conflict")
            kept = [item for item in items if item["id"] != element_id]
            if len(items) == len(kept):
                return False
            self._write(kept)
        return True

    def _program(self, runtime, program_id, target):
        _id(program_id)
        runtime.check_active()
        if runtime.mode != "uia":
            raise LearningError("요소 학습과 이름 찾기는 uia 세션에서만 사용할 수 있습니다.", "unsupported_mode")
        if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                or any(type(value) is not int or value < 1 for value in target.values())):
            raise LearningError("현재 창의 pid와 window_id를 지정하세요.", "invalid_target")
        configured = next((item for item in self.config["programs"] if item["id"] == program_id and item["enabled"]), None)
        approved = next((item for item in runtime.programs if item["id"] == program_id), None)
        if configured is None or approved is None:
            raise LearningError("이번 세션에 승인한 등록 프로그램에만 요소를 연결할 수 있습니다.", "program_not_approved")
        def paths(app):
            return sorted({normalize_exe(path) for path in [app["exe"]] + app.get("control_exes", [])})
        allowed = paths(configured)
        if paths(approved) != allowed:
            raise LearningError("프로그램 경로가 승인 후 변경되었습니다. 세션을 다시 시작하세요.", "program_changed")
        guard = runtime.guard
        if normalize_exe(guard.process_resolver(target["pid"])) not in allowed or guard.window_resolver(target["window_id"]) != target["pid"]:
            raise LearningError("현재 창은 지정한 프로그램에 속하지 않습니다.", "target_mismatch")
        return hashlib.sha256(json.dumps(allowed, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _observe(self, runtime, target, program_id):
        binding = self._program(runtime, program_id, target)
        answer = runtime.call("get_window_state", {**target, "include_accessibility_tree": True, "include_screenshot": False,
                                                    "max_depth": self.DEPTH, "max_elements": self.COUNT})
        if not isinstance(answer, dict) or answer.get("isError"):
            raise LearningError("현재 창을 읽지 못했습니다. 요소를 저장하거나 조작하지 않았습니다.", "observation_failed")
        snapshot = _payload(answer)
        if any(snapshot.get(key) != value for key, value in target.items()):
            raise LearningError("새로 관찰된 창이 요청한 창과 다릅니다.", "target_mismatch")
        if _limited(snapshot, self.DEPTH, self.COUNT):
            raise LearningError("화면 요소가 일부만 읽혀 고유성을 확인할 수 없습니다. 대상 화면을 단순화한 후 다시 읽어 주세요.", "incomplete_observation")
        indices = [element.get("element_index") for element in _elements(snapshot)]
        actual = [index for index in indices if type(index) is int]
        if len(actual) != len(set(actual)):
            raise LearningError("화면 요소 번호가 중복되어 대상을 구분하지 못했습니다.", "invalid_observation")
        if binding != self._program(runtime, program_id, target):
            raise LearningError("관찰 중 프로그램 연결이 변경되었습니다.", "program_changed")
        return snapshot, binding

    def teach(self, runtime, target, program_id, element_index, label, *, screen="", instructions="", id=None,
              expected_selector=None, expected_revision=None):
        label = _text(label, "요소 이름", 100).strip()
        screen = _text(screen, "화면 분류", 100, empty=True).strip()
        _text(instructions, "사용 설명", 4000, empty=True)
        if type(element_index) is not int or element_index < 0:
            raise LearningError("현재 관찰에서 확인한 element_index를 지정하세요.", "invalid_target")
        if expected_selector is None:
            raise LearningError("이전 관찰의 selector를 expected_selector로 함께 지정해야 요소 번호 변경을 확인할 수 있습니다.", "expected_selector_required")
        expected_selector = validate_selector(expected_selector)
        element_id = _id(id) if id is not None else uuid.uuid4().hex
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise LearningError("수정할 요소의 현재 revision을 지정하세요.")
        snapshot, binding = self._observe(runtime, target, program_id)
        targets = [element for element in _elements(snapshot) if element.get("element_index") == element_index]
        if len(targets) != 1 or _unique(snapshot, expected_selector) is not targets[0]:
            raise LearningError("관찰 이후 요소 번호 또는 대상이 변경되었습니다. 화면을 다시 확인한 후 학습하세요.", "stale_selection")
        element = targets[0]
        return self._save_observed(runtime, snapshot, binding, element, program_id, label, screen,
                                   instructions, element_id, expected_revision)

    def teach_picked(self, runtime, target, program_id, selected, label, *, screen="", instructions="",
                     id=None, expected_revision=None, cancel_event=None):
        """One fresh guarded read for a human-confirmed native identity, independent of old indices."""
        from learning_picker import match_picked_element
        if not isinstance(selected, dict) or selected.get("human_confirmed") is not True:
            raise LearningError("사용자가 선택 창에서 후보를 확정해야 요소를 저장할 수 있습니다.", "picker_confirmation_required")
        label = _text(label, "요소 이름", 100).strip()
        screen = _text(screen, "화면 분류", 100, empty=True).strip()
        _text(instructions, "사용 설명", 4000, empty=True)
        element_id = _id(id) if id is not None else uuid.uuid4().hex
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise LearningError("수정할 요소의 현재 revision을 지정하세요.")
        self._check_teaching_active(runtime, cancel_event)
        snapshot, binding = self._observe(runtime, target, program_id)
        evidence = {}
        matched = match_picked_element(snapshot, selected, target, diagnostics=evidence)
        element = _unique(snapshot, matched["expected_selector"])
        entry = self._save_observed(runtime, snapshot, binding, element, program_id, label, screen,
                                    instructions, element_id, expected_revision, cancel_event)
        # Matching diagnostics contain counts and enum-like statuses only, not
        # application names/values, native handles, or saved screen coordinates.
        return {**entry, "teaching_evidence": evidence}

    @staticmethod
    def _check_teaching_active(runtime, cancel_event):
        runtime.check_active()
        if cancel_event is not None and cancel_event.is_set():
            raise LearningError("요소 학습을 취소하여 저장하지 않았습니다.", "picker_cancelled")

    def _save_observed(self, runtime, snapshot, binding, element, program_id, label, screen,
                       instructions, element_id, expected_revision, cancel_event=None):
        entry = {"id": element_id, "label": label, "screen": screen, "instructions": instructions,
                 "program_id": program_id, "program_binding": binding, "selector": stable_selector(snapshot, element),
                 "fingerprint": _fingerprint(element), "revision": 1, "updated_at": utc_now()}
        with self._locked():
            items = self._read()
            previous = next((item for item in items if item["id"] == element_id), None)
            if previous is not None:
                if previous["program_id"] != program_id:
                    raise LearningError("기존 요소를 다른 프로그램으로 바꿀 수 없습니다. 새 이름으로 저장하세요.", "program_changed")
                if expected_revision != previous["revision"]:
                    raise LearningError("요소가 변경되었거나 수정 버전이 없습니다. 목록의 최신 revision을 확인하세요.", "revision_conflict")
                entry["revision"] = previous["revision"] + 1
            elif expected_revision is not None:
                raise LearningError("수정할 요소가 없어졌습니다. 목록을 다시 확인하세요.", "revision_conflict")
            if any(item["id"] != element_id and item["program_id"] == program_id
                   and item["screen"].casefold() == screen.casefold() and item["label"].casefold() == label.casefold() for item in items):
                raise LearningError("같은 프로그램과 화면 분류에 같은 요소 이름이 있습니다. 다른 이름을 사용하거나 기존 요소를 다시 학습하세요.", "duplicate_label")
            self._validate_entry(entry)
            self._check_teaching_active(runtime, cancel_event)
            self._write([item for item in items if item["id"] != element_id] + [entry])
        return copy.deepcopy(entry)

    def resolve(self, runtime, target, element_id):
        entry = self.get(element_id)
        snapshot, binding = self._observe(runtime, target, entry["program_id"])
        if binding != entry["program_binding"]:
            raise LearningError("등록된 프로그램 경로가 학습 당시와 다릅니다. 요소를 다시 학습하세요.", "program_changed")
        element = _unique(snapshot, entry["selector"])
        if (_protected(element) or type(element.get("element_index")) is not int or element["element_index"] < 0
                or element.get("synthetic_ancestor") or _fingerprint(element) != entry["fingerprint"]):
            raise LearningError("요소 종류 또는 기능이 학습 당시와 달라졌습니다. 화면을 확인하고 다시 학습하세요.", "control_changed")
        return {"id": entry["id"], "label": entry["label"], "screen": entry["screen"], "program_id": entry["program_id"],
                "selector": copy.deepcopy(entry["selector"]), "revision": entry["revision"],
                "instructions": entry["instructions"], "instructions_are_untrusted_data": True,
                "screen_is_grouping_label_only": True, "suggested_operations": suggested_operations(element),
                "input_dispatched": False, "task_verified": False}
