"""Declarative, read-only binding of an owned popup recorded in one application."""
from __future__ import annotations

import re
import time
import os

from operations import OperationError

REF = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


def validate_window_wait(step):
    allowed = {"operation", "program_id", "window_ref", "owner_ref", "title", "class_name", "timeout_ms"}
    if (not isinstance(step, dict) or set(step) - allowed or step.get("operation") != "wait_for_window"
            or any(not isinstance(step.get(key), str) or not REF.fullmatch(step[key]) for key in ("window_ref", "owner_ref"))
            or step["window_ref"] == step["owner_ref"]
            or not isinstance(step.get("title"), str) or len(step["title"]) > 1000
            or not isinstance(step.get("class_name"), str) or not 1 <= len(step["class_name"]) <= 256
            or type(step.get("timeout_ms", 5000)) is not int or not 0 <= step.get("timeout_ms", 5000) <= 10000):
        raise OperationError("팝업의 창 이름·소유 창·정확한 제목과 종류를 확인하세요.", "invalid_window_wait")
    return dict(step)


def dynamic_window_keys(steps):
    defined, used = set(), set()
    declared = {(step.get("program_id"), step.get("window_ref", "main")) for step in steps if step.get("operation") == "wait_for_window"}
    for step in steps:
        key = (step.get("program_id"), step.get("window_ref", "main"))
        if step.get("operation") == "wait_for_window":
            validate_window_wait(step)
            if key in defined or key in used:
                raise OperationError("팝업 창 이름은 처음 사용할 때 한 번만 연결하세요.", "invalid_window_wait")
            owner = (step.get("program_id"), step["owner_ref"])
            if owner in declared and owner not in defined:
                raise OperationError("팝업의 소유 창을 먼저 연결하세요.", "invalid_window_wait")
            defined.add(key)
        used.add(key)
    return defined


def owned_by(row, owner_id, rows):
    """No newly observed same-process window is trusted without an owner edge."""
    by_id = {item.get("window_id"): item for item in rows}
    pending, seen = [row], set()
    while pending and len(seen) < 64:
        item = pending.pop()
        if item.get("pid") != row.get("pid"):
            continue
        ident = item.get("window_id")
        if ident in seen:
            continue
        seen.add(ident)
        for edge in (item.get("owner_window_id"), item.get("root_owner_window_id")):
            if edge == owner_id:
                return True
            if edge in by_id and edge not in seen:
                pending.append(by_id[edge])
    return False


def combo_list_owned_by(owner_id, list_id, pid):
    """Some standard dropdown lists have no GW_OWNER; use exact native linkage."""
    if os.name != "nt": return False
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    class ComboInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("item", wintypes.RECT), ("button", wintypes.RECT),
                    ("state", wintypes.DWORD), ("combo", wintypes.HWND), ("edit", wintypes.HWND), ("listing", wintypes.HWND)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]; user.GetAncestor.restype = wintypes.HWND
    user.GetComboBoxInfo.argtypes = [wintypes.HWND, ctypes.POINTER(ComboInfo)]; user.GetComboBoxInfo.restype = wintypes.BOOL
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    def same_process(hwnd):
        found = wintypes.DWORD()
        return bool(user.GetWindowThreadProcessId(hwnd, ctypes.byref(found))) and found.value == pid
    if not same_process(owner_id) or not same_process(list_id): return False
    related = False
    @callback_type
    def check(hwnd, unused):
        nonlocal related
        if not same_process(hwnd) or user.GetAncestor(hwnd, 2) != owner_id: return True
        info = ComboInfo(); info.size = ctypes.sizeof(info)
        if user.GetComboBoxInfo(hwnd, ctypes.byref(info)) and info.combo == hwnd and info.listing == list_id:
            related = True
            return False
        return True
    user.EnumChildWindows(owner_id, check, 0)
    return related


def execute_window_wait(runtime, step, bindings):
    validate_window_wait(step)
    began = time.monotonic()
    owner = bindings.get((step["program_id"], step["owner_ref"]))
    probe = None
    result = {"operation": "wait_for_window", "status": "unknown", "task_verified": False,
              "input_dispatched": False, "checks": [], "candidates": []}
    try:
        if not isinstance(owner, dict) or set(owner) != {"pid", "window_id"}:
            raise OperationError("먼저 원래 창의 현재 연결을 확인하세요.", "window_owner_unavailable")
        runtime.check_active()
        probe = runtime.create_transition_probe(dict(owner))
        initial = probe.capture()
        identity = (initial.get("creation_time"), initial.get("executable"))
        initial_owner = next((row for row in initial.get("windows", []) if row.get("window_id") == owner["window_id"]), None)
        if (not all(identity) or initial_owner is None or initial_owner.get("pid") != owner["pid"]
                or not initial_owner.get("class_name") or not initial_owner.get("thread_id")):
            raise OperationError("원래 프로그램의 수명을 확인하지 못했습니다.", "window_owner_unavailable")
        deadline = began + step.get("timeout_ms", 5000) / 1000
        while True:
            runtime.check_active()
            state = probe.snapshot()
            if state.get("process_exited") or not state.get("target_present") or (state.get("creation_time"), state.get("executable")) != identity:
                raise OperationError("팝업을 연 원래 프로그램이나 창이 없어졌습니다.", "window_owner_unavailable")
            rows = state.get("windows", [])
            if initial_owner is not None:
                current_owner = next((row for row in rows if row.get("window_id") == owner["window_id"]), {})
                if any(current_owner.get(key) != initial_owner.get(key) for key in ("pid", "class_name", "thread_id")):
                    raise OperationError("기다리는 동안 원래 창의 소유 정보가 바뀌었습니다.", "window_owner_unavailable")
            matches = [row for row in rows if row.get("pid") == owner["pid"] and row.get("window_id") != owner["window_id"]
                       and row.get("visible") is True and row.get("title") == step["title"]
                       and row.get("class_name") == step["class_name"] and (owned_by(row, owner["window_id"], rows)
                       or (row.get("class_name") == "ComboLBox" and combo_list_owned_by(owner["window_id"], row["window_id"], owner["pid"])))]
            result["candidates"] = [{key: row.get(key) for key in ("pid", "window_id", "title", "class_name")} for row in matches[:10]]
            if len(matches) > 1:
                raise OperationError("같은 조건의 관련 팝업이 여러 개라 선택하지 않았습니다.", "recording_window_ambiguous")
            if len(matches) == 1:
                target = {key: matches[0][key] for key in ("pid", "window_id")}
                runtime.check_active()
                result.update(status="verified", task_verified=True, target=target)
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationError("기다린 관련 팝업이 나타나지 않았습니다. 입력은 반복하지 않았습니다.", "recording_window_missing")
            runtime.stop_event.wait(min(.1, remaining))
    except Exception as error:
        result["diagnostic"] = {"code": getattr(error, "code", "recording_window_unavailable"), "message": str(error), "automatic_replay": False}
        return result
    finally:
        if probe is not None:
            probe.close()
        result["metrics"] = {"elapsed_ms": round((time.monotonic() - began) * 1000, 2), "mutations": 0}
