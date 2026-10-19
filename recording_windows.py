"""Declarative, read-only binding of an owned popup recorded in one application."""
from __future__ import annotations

import re
import time
import os

from operations import OperationError

REF = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
# WinForms app-domain suffixes are runtime identities, not durable selectors.
# Keep the control kind/style intact; this is NOT a general prefix match.
WINFORMS_CLASS = re.compile(r"(WindowsForms10\.[A-Za-z0-9_]+\.[0-9a-fA-F]+)\.app\.0\.[0-9a-fA-F]+_r[0-9]+_ad[0-9]+\Z")


def popup_class_matches(expected, actual):
    if expected == actual:
        return True
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    left, right = WINFORMS_CLASS.fullmatch(expected), WINFORMS_CLASS.fullmatch(actual)
    return bool(left and right and left[1] == right[1])


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


def execute_window_wait(runtime, step, bindings, *, image_hint=None):
    validate_window_wait(step)
    began = time.monotonic()
    owner = bindings.get((step["program_id"], step["owner_ref"]))
    probe = None
    result = {"operation": "wait_for_window", "status": "unknown", "task_verified": False,
              "input_dispatched": False, "checks": [], "candidates": [], "observed_candidates": []}
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
            snapshot = getattr(probe, "transition_snapshot", probe.snapshot)
            state = snapshot()
            if state.get("process_exited") or not state.get("target_present") or (state.get("creation_time"), state.get("executable")) != identity:
                raise OperationError("팝업을 연 원래 프로그램이나 창이 없어졌습니다.", "window_owner_unavailable")
            rows = state.get("windows", [])
            if initial_owner is not None:
                current_owner = next((row for row in rows if row.get("window_id") == owner["window_id"]), {})
                if any(current_owner.get(key) != initial_owner.get(key) for key in ("pid", "class_name", "thread_id")):
                    raise OperationError("기다리는 동안 원래 창의 소유 정보가 바뀌었습니다.", "window_owner_unavailable")
            matches, observed = [], []
            for row in rows:
                if row.get("pid") != owner["pid"] or row.get("window_id") == owner["window_id"]:
                    continue
                reasons = []
                if row.get("visible") is not True: reasons.append("not_visible")
                if row.get("title") != step["title"]: reasons.append("title_mismatch")
                if not popup_class_matches(step["class_name"], row.get("class_name")): reasons.append("class_mismatch")
                related = owned_by(row, owner["window_id"], rows)
                path = []
                # EnumWindows omits hosted WinForms/WPF child windows. Resolve
                # the actual owner/parent chain only for otherwise valid candidates.
                if not related and not reasons and callable(getattr(probe, "ownership_path", None)):
                    path = probe.ownership_path(row["window_id"], owner["window_id"])
                    related = bool(path)
                if not related and not reasons and row.get("class_name") == "ComboLBox":
                    related = combo_list_owned_by(owner["window_id"], row["window_id"], owner["pid"])
                if not related: reasons.append("owner_mismatch")
                observed.append({**{key: row.get(key) for key in ("window_id", "pid", "title", "class_name", "visible", "owner_window_id", "root_owner_window_id")},
                                 "expected_owner_window_id": owner["window_id"],
                                 "relationship_path": path, "rejected_by": reasons})
                if not reasons: matches.append(row)
            result["observed_candidates"] = sorted(observed, key=lambda item: (
                item["rejected_by"] != ["owner_mismatch"], len(item["rejected_by"])))[:10]
            result["observation_summary"] = {
                "scope": "top_level_and_child_hwnds" if callable(getattr(probe, "transition_snapshot", None)) else "top_level_hwnds",
                "observed_count": len(observed), "returned_count": len(result["observed_candidates"]),
                "truncated": len(observed) > 10,
                "popup_absence_proven": False,
                "uia_only_controls_scanned": False,
            }
            result["candidates"] = [{key: row.get(key) for key in ("pid", "window_id", "title", "class_name")} for row in matches[:10]]
            if len(matches) > 1:
                raise OperationError("같은 조건의 관련 팝업이 여러 개라 선택하지 않았습니다.", "recording_window_ambiguous")
            if len(matches) == 1:
                target = {key: matches[0][key] for key in ("pid", "window_id")}
                runtime.check_active()
                result.update(status="verified", task_verified=True, target=target,
                              class_match="exact" if matches[0]["class_name"] == step["class_name"] else "winforms_runtime_suffix",
                              verification_scope="owned_window_presence_only", business_result_verified=False)
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                recovery = getattr(runtime, "recover_msaa_dropdown", None)
                if image_hint and callable(recovery):
                    recovered = recovery(owner, rows, image_hint, probe)
                    runtime.check_active()
                    fresh = probe.snapshot()
                    if fresh.get("process_exited") or not fresh.get("target_present") or (fresh.get("creation_time"), fresh.get("executable")) != identity:
                        raise OperationError("MSAA 확인 중 원래 창이 바뀌었습니다.", "window_owner_unavailable")
                    if recovered:
                        result.update(status="verified", task_verified=True, target=recovered,
                                      accessibility_scope="msaa_list", verification_scope="msaa_list_and_saved_image",
                                      business_result_verified=False)
                        return result
                result["missing_reason"] = "candidate_conditions_not_met" if observed else "no_same_process_popup_observed"
                detail = ("제목·종류·프로세스·표시 조건이 일치하는 팝업은 있지만 등록된 창까지의 소유자·부모 연결을 확인하지 못했습니다. observed_candidates의 실제 owner_window_id와 expected_owner_window_id를 확인하세요."
                          if any(item["rejected_by"] == ["owner_mismatch"] for item in observed) else
                          "같은 프로그램의 다른 창은 관찰했지만 제목·종류·소유 관계·표시 조건이 일치하지 않습니다. observed_candidates의 rejected_by를 확인하세요."
                          if observed else "확인 시점에 같은 프로그램의 팝업 후보가 없습니다. 팝업이 잠깐 열렸다 닫힌 경우도 포함합니다.")
                raise OperationError("조건에 맞는 관련 팝업을 찾지 못했습니다. " + detail + " 이 결과만으로 클릭 실패나 드롭다운이 열리지 않았다고 판단할 수 없습니다. HWND가 없는 접근성 목록은 이 창 검색에 포함되지 않습니다. 입력은 반복하지 않았습니다.", "recording_window_missing")
            runtime.stop_event.wait(min(.1, remaining))
    except Exception as error:
        result["diagnostic"] = {"code": getattr(error, "code", "recording_window_unavailable"), "message": str(error), "automatic_replay": False}
        return result
    finally:
        if probe is not None:
            probe.close()
        result["metrics"] = {"elapsed_ms": round((time.monotonic() - began) * 1000, 2), "mutations": 0}
