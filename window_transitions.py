"""Read-only, same-process window transition evidence. Never sends input."""
from __future__ import annotations

import copy
import time


WINDOW_TRANSITION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "mode": {"enum": ["auto", "same_window", "new_window"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 10000,
            "description": "Native candidate discovery wait, default 1500ms. After discovery, a fresh UIA result read uses the step budget and must finish before verification_timeout_ms. Zero makes one discovery attempt."},
    }, "required": ["mode"],
    "description": "Default auto: after the original window disappears, verify a unique new or owner-related window in the SAME retained process. new_window requires its exact title. Closure uses computer_close, never disappearance as success. timeout_ms defaults to 1500; no input replay.",
}


class TransitionError(ValueError):
    def __init__(self, message, code="transition_identity_unavailable"):
        super().__init__(message)
        self.code = code


def validate_transition(value):
    if (not isinstance(value, dict) or set(value) - {"mode", "title", "timeout_ms"}
            or value.get("mode") not in ("auto", "same_window", "new_window")):
        raise TransitionError("창 전환은 auto/same_window/new_window로 지정하세요. 창 또는 프로그램 종료는 computer_close를 사용하세요.", "invalid_operation")
    if "title" in value and (not isinstance(value["title"], str) or not value["title"].strip() or len(value["title"]) > 1000):
        raise TransitionError("전환할 창의 정확한 제목을 지정하세요.", "invalid_operation")
    if value["mode"] == "new_window" and "title" not in value:
        raise TransitionError("new_window에는 전환할 창의 정확한 title이 필요합니다.", "invalid_operation")
    if value["mode"] == "same_window" and "title" in value:
        raise TransitionError("same_window에는 title을 지정하지 마세요.", "invalid_operation")
    if type(value.get("timeout_ms", 1500)) is not int or not 0 <= value.get("timeout_ms", 1500) <= 10000:
        raise TransitionError("창 전환 timeout_ms는 0~10000 사이 정수여야 합니다.", "invalid_operation")
    return copy.deepcopy(value)


class WindowTransition:
    """A retained kernel handle prevents PID reuse from becoming a new target.

    Normal steps only capture one cheap native baseline. No Driver enumeration,
    accessibility traversal, UI process, or polling starts unless needed.
    """
    def __init__(self, runtime, target, spec):
        self.runtime, self.original, self.spec = runtime, dict(target), spec
        self.probe = None
        self.initial = None
        self.error = None
        self.error_detail = None
        self.elapsed_ms = 0
        self.candidates = []

    def prepare(self):
        began = time.monotonic()
        try:
            factory = getattr(self.runtime, "create_transition_probe", None)
            if not callable(factory):
                raise TransitionError("창 전환의 실행 프로세스 정보를 확보하지 못했습니다.")
            self.probe = factory(dict(self.original))
            self.initial = self.probe.capture()
            self._validate(self.initial)
            if self.initial["process_exited"] or not self.initial["target_present"]:
                raise TransitionError("입력 전의 창을 확인하지 못했습니다.")
        except Exception as error:
            self.error = getattr(error, "code", "transition_identity_unavailable")
            self.error_detail = type(error).__name__ + ": " + str(error)[:300]
            self.close()
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def _validate(self, state):
        if (not isinstance(state, dict) or type(state.get("process_exited")) is not bool
                or type(state.get("target_present")) is not bool or not isinstance(state.get("windows"), list)
                or any(not isinstance(row, dict) for row in state.get("windows", []))
                or not state.get("creation_time") or not isinstance(state.get("executable"), str)):
            raise TransitionError("실행 프로세스 또는 창 식별 정보가 불완전합니다.")
        if self.initial is not None and any(state.get(k) != self.initial.get(k) for k in ("creation_time", "executable")):
            raise TransitionError("동작 전후의 실행 프로세스가 달라졌습니다.", "transition_process_changed")

    def inspect(self):
        if self.probe is None or self.error:
            raise TransitionError("입력 전의 프로세스 식별 정보를 확보하지 못해 다른 창으로 자동 연결하지 않았습니다.")
        self.runtime.check_active()
        began = time.monotonic()
        try:
            state = self.probe.snapshot()
            self._validate(state)
            if state["process_exited"]:
                raise TransitionError("원래 프로그램이 종료되었습니다. 정상 완료나 충돌 여부는 확인되지 않았습니다. 종료 목적이면 computer_close를 사용하세요.", "transition_process_exited")
            baseline = self.initial["windows"]
            old_ids = {row.get("window_id") for row in baseline}
            previously_visible = {row.get("window_id") for row in baseline if row.get("visible") is True}
            original_row = next((row for row in baseline if row.get("window_id") == self.original["window_id"]), {})
            relatives = {original_row.get("owner_window_id"), original_row.get("root_owner_window_id")}
            rows = []
            for row in state["windows"]:
                if (not isinstance(row, dict) or row.get("pid") != self.original["pid"]
                        or type(row.get("window_id")) is not int or row["window_id"] <= 0
                        or row["window_id"] == self.original["window_id"] or row.get("visible") is not True
                        or row.get("class_name") in {"IME", "MSCTFIME UI"}):
                    continue
                owned = self.original["window_id"] in (row.get("owner_window_id"), row.get("root_owner_window_id"))
                appeared = row["window_id"] not in previously_visible
                related = row["window_id"] not in old_ids or owned or row["window_id"] in relatives or (self.spec["mode"] == "new_window" and appeared)
                if self.spec["mode"] == "new_window" and row["window_id"] in previously_visible:
                    continue
                if not related or ("title" in self.spec and row.get("title") != self.spec["title"]):
                    continue
                rows.append({key: row.get(key) for key in ("pid", "window_id", "title", "class_name", "owner_window_id", "root_owner_window_id", "thread_id")})
            self.candidates = rows[:20]
            if self.spec["mode"] == "auto" and state["target_present"]:
                raise TransitionError("기존 창이 아직 존재합니다. 다른 창으로 자동 전환하지 않았습니다.", "transition_original_present")
            if len(rows) > 1:
                raise TransitionError("관련 창이 여러 개입니다. 전환할 창의 정확한 제목을 지정하세요.", "transition_ambiguous")
            return rows[0] if rows else None
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def original_present(self):
        """Cheap precheck also avoids sending a stale HWND to the scoped reader."""
        self.runtime.check_active()
        began = time.monotonic()
        try:
            state = self.probe.snapshot()
            self._validate(state)
            if state["process_exited"]:
                raise TransitionError("원래 실행 프로세스가 종료되어 결과 창을 확인할 수 없습니다.", "transition_process_exited")
            if state["target_present"]:
                before = next((row for row in self.initial["windows"] if row.get("window_id") == self.original["window_id"]), {})
                after = next((row for row in state["windows"] if row.get("window_id") == self.original["window_id"]), {})
                if not before or not after or any(before.get(key) != after.get(key) for key in ("class_name", "thread_id")):
                    raise TransitionError("같은 창 번호의 클래스 또는 소유 스레드가 바뀌었습니다. 자동으로 계속하지 않았습니다.", "transition_target_identity_changed")
            return state["target_present"]
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def confirm(self, target, expected_row=None):
        row = self.inspect()
        if row is None or any(row.get(k) != target[k] for k in ("pid", "window_id")):
            raise TransitionError("결과 확인 중 전환된 창이 다시 바뀌었습니다.", "transition_target_changed")
        if expected_row is not None and any(row.get(key) != expected_row.get(key)
                for key in ("class_name", "thread_id", "owner_window_id", "root_owner_window_id")):
            raise TransitionError("전환된 창의 클래스·소유 관계가 결과 확인 중 바뀌었습니다.", "transition_target_identity_changed")

    def close(self):
        probe, self.probe = self.probe, None
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass
