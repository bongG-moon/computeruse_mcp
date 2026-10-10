"""Human hover/F8 teaching. No application input, screenshots, or values are stored.

The fixed native helper only reads identity at a human-selected point. It never
selects a process from an executable path. Both sides require the same approved
PID/window; the final element is resolved again through the guarded Driver.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import subprocess
import time
import uuid

from learning import stable_selector
from operations import OperationError, _elements, _find, _name, _payload


HELPER_NAME = "Computer Use MCP 요소 선택.exe"
MAX_RESPONSE_BYTES = 32768
HELPER_READ_SECONDS = 6
HELPER_START_SECONDS = 8


def _target(target):
    if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
            or any(type(value) is not int or value <= 0 for value in target.values())):
        raise OperationError("학습할 현재 창의 pid/window_id가 필요합니다.", "invalid_target")
    return dict(target)


def _stopped(runtime):
    return runtime.stop_event.is_set() or (Path(runtime.run_dir) / "stop.flag").exists()


def _descriptor(value):
    if not isinstance(value, dict) or value.get("is_password") is not False:
        raise OperationError("비밀번호 요소 또는 확인되지 않은 보호 요소는 학습하지 않습니다.", "protected_element")
    role = value.get("role")
    if not isinstance(role, str) or not role or len(role) > 80:
        raise OperationError("선택한 요소의 종류를 확인하지 못했습니다.", "picker_invalid_response")
    result = {"role": role}
    for key in ("automation_id", "name"):
        text = value.get(key)
        if text is not None and (not isinstance(text, str) or len(text) > 1000):
            raise OperationError("선택한 요소의 식별 정보가 올바르지 않습니다.", "picker_invalid_response")
        if text and text.strip():
            result[key] = text
    return result


def _contains(bounds, point):
    if (not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}
            or not isinstance(point, dict) or set(point) != {"x", "y"}
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in list(bounds.values()) + list(point.values()))):
        return False
    return (bounds["width"] > 0 and bounds["height"] > 0
            and bounds["x"] <= point["x"] < bounds["x"] + bounds["width"]
            and bounds["y"] <= point["y"] < bounds["y"] + bounds["height"])


def _identity_match(element, identity):
    if element.get("role") != identity["role"]:
        return False
    if identity.get("automation_id"):
        return element.get("automation_id") == identity["automation_id"]
    return bool(identity.get("name")) and _name(element) == identity["name"]


def _ancestry(element, elements):
    by_index = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int}
    seen = set()
    parent = element.get("parent_index")
    for _ in range(32):
        if type(parent) is not int or parent in seen or parent not in by_index:
            return
        seen.add(parent)
        row = by_index[parent]
        yield row
        parent = row.get("parent_index")


def _rectangle(value, *, frame=False):
    keys = ("x", "y", "w", "h") if frame else ("x", "y", "width", "height")
    if not isinstance(value, dict) or any(key not in value for key in keys):
        return None
    numbers = [value[key] for key in keys]
    if any(type(number) not in (int, float) or not math.isfinite(number) for number in numbers):
        return None
    if numbers[2] <= 0 or numbers[3] <= 0:
        return None
    return dict(zip(("x", "y", "width", "height"), numbers))


def _driver_rectangle(element):
    # Driver 0.28.2 uses desktop-physical frame{x,y,w,h}. Earlier wrappers
    # exposed bounds{x,y,width,height}. Do not guess between coordinate spaces.
    if "frame" in element:
        return _rectangle(element["frame"], frame=True), "frame"
    if "bounds" in element:
        return _rectangle(element["bounds"]), "bounds"
    return None, None


def _selection_geometry(snapshot, selected):
    point = dict(selected["point"])
    bounds = dict(selected["bounds"])
    before, after = _rectangle(selected.get("window_bounds")), _rectangle(snapshot.get("window_bounds"))
    translated = False
    # A user may move the same window while reviewing the candidate. Only
    # translate when two observed, equally sized physical window rectangles
    # prove the offset. Never invent a scale factor or a relative coordinate.
    if before is not None and after is not None and all(abs(before[key] - after[key]) <= 2 for key in ("width", "height")):
        dx, dy = after["x"] - before["x"], after["y"] - before["y"]
        point["x"] += dx; point["y"] += dy
        bounds["x"] += dx; bounds["y"] += dy
        translated = bool(dx or dy)
    return point, bounds, translated


def _same_rectangle(left, right):
    return all(abs(left[key] - right[key]) <= 2 for key in ("x", "y", "width", "height"))


def match_picked_element(snapshot, selected, target, *, diagnostics=None):
    """Resolve confirmed identity, corroborating known provider projection gaps.

    Exact AutomationId/role remain preferred. When Driver drops AutomationId,
    accept only exact name+role AND the same physical rectangle. Never use
    location alone, silently substitute a parent, or ignore a conflicting ID.
    """
    target = _target(target)
    if (not isinstance(selected, dict) or selected.get("status") != "selected"
            or any(selected.get(key) != value for key, value in target.items())
            or any(key in snapshot and snapshot[key] != value for key, value in target.items())):
        raise OperationError("선택한 위치 또는 관찰한 창이 학습 대상 창과 다릅니다.", "target_mismatch")
    identity = _descriptor(selected.get("element"))
    if not _contains(selected.get("bounds"), selected.get("point")):
        raise OperationError("선택한 요소의 위치 정보가 올바르지 않아 저장하지 않았습니다.", "picker_geometry_changed")
    ancestors = selected.get("ancestors", [])
    if not isinstance(ancestors, list) or len(ancestors) > 16:
        raise OperationError("선택한 요소의 부모 영역을 확인하지 못했습니다.", "picker_invalid_response")
    ancestors = [_descriptor(value) for value in ancestors]
    elements = _elements(snapshot)
    projected = [e for e in elements if type(e.get("element_index")) is int
                 and e["element_index"] >= 0 and not e.get("synthetic_ancestor")]
    same_role = [e for e in projected if e.get("role") == identity["role"]]
    same_name = [e for e in same_role if identity.get("name") and _name(e) == identity["name"]]
    matches = [e for e in same_role if _identity_match(e, identity)]
    point, native_bounds, translated = _selection_geometry(snapshot, selected)
    normal = snapshot.get("accessibility_normalization", {})
    if not isinstance(normal, dict):
        normal = {}
    info = {"stage": "matching_driver_projection", "selected_role": identity["role"],
            "native_has_automation_id": bool(identity.get("automation_id")), "native_has_name": bool(identity.get("name")),
            "projected_element_count": len(projected), "role_candidates": len(same_role),
            "name_role_candidates": len(same_name), "exact_identity_candidates": len(matches),
            "geometry_rejected": 0, "window_translation_applied": translated,
            "matching_method": "automation_id_role" if identity.get("automation_id") else "name_role",
            "normalization_status": normal.get("status", "not_reported"),
            "normalization_reason": normal.get("reason")}
    def fail(reason, message, code="picker_not_found"):
        info["reason"] = reason
        info["matched_candidates"] = len(matches)
        if diagnostics is not None:
            diagnostics.update(info)
        error = OperationError(message, code)
        error.picker_diagnostic = dict(info)
        raise error
    if not projected:
        tree = snapshot.get("tree_markdown")
        lines = [line.strip() for line in tree.splitlines() if line.strip()] if isinstance(tree, str) else []
        root_only = len(lines) == 1 and bool(re.fullmatch(r"-\s*(?:\[\d+\]\s*)?Window(?:\s+.*)?", lines[0]))
        native_container = (identity["role"] in {"Pane", "Group", "Custom"}
                            and selected["element"].get("has_control_patterns") is False)
        rejected_empty = normal.get("status") == "rejected" and normal.get("reason") == "element_size_or_type"
        if root_only or (native_container and rejected_empty):
            fail("no_controls_projected", "현재 화면에서 창 또는 화면 영역 정보만 관찰되었고, 선택한 버튼·입력란의 UIA 정보는 받지 못했습니다. F8 반복 대신 이미지 기반 선택이 필요하며, 현재의 저장 UIA 요소 기능으로는 이 영역을 지원하지 않습니다.", "picker_controls_not_exposed")
        fail("empty_projection_unconfirmed", "현재 관찰에 조작할 요소 정보가 없습니다. 읽기 결과가 비어 있어 이 화면의 UIA 지원 여부를 확정할 수 없으며 저장하지 않았습니다.")
    checked = []
    for element in matches:
        rectangle, field = _driver_rectangle(element)
        if field is None or (rectangle is not None and _contains(rectangle, point)):
            checked.append(element)
        else:
            info["geometry_rejected"] += 1
    matches = checked
    # Projection may omit IDs even though native UIA exposes them. This occurs
    # when the rendered tree cannot be safely normalized. Missing is not the
    # same as conflicting: an explicit different Driver ID is never accepted.
    if not matches and identity.get("automation_id") and not info["exact_identity_candidates"]:
        fallback = [e for e in same_name if not e.get("automation_id")]
        info["missing_id_name_candidates"] = len(fallback)
        for element in fallback:
            rectangle, field = _driver_rectangle(element)
            if rectangle is not None and _contains(rectangle, point) and _same_rectangle(rectangle, native_bounds):
                matches.append(element)
        if matches:
            info["matching_method"] = "exact_name_role_and_native_rectangle_missing_driver_id"
    if len(matches) > 1:
        for ancestor in ancestors:
            if not (ancestor.get("automation_id") or ancestor.get("name")):
                continue
            scopes = [e for e in elements if _identity_match(e, ancestor)]
            if len(scopes) != 1:
                continue
            scoped = [e for e in matches if any(a is scopes[0] for a in _ancestry(e, elements))]
            if scoped:
                matches = scoped
            if len(matches) == 1:
                break
    if len(matches) != 1:
        if len(matches) > 1:
            fail("ambiguous_identity", "사용자 선택은 확인했지만 같은 식별 정보를 가진 요소가 여러 개입니다. 서로 구분되는 부모 영역을 포함해 지정해야 합니다.", "ambiguous_selector")
        reason = ("role_not_projected" if not same_role else
                  "geometry_mismatch" if info["geometry_rejected"] else
                  "missing_driver_id_without_matching_geometry" if info.get("missing_id_name_candidates") else
                  "identity_not_projected")
        fail(reason, "사용자 선택은 확인했지만 Driver가 같은 요소를 조작 대상으로 제공하는지 확인하지 못했습니다. 선택을 반복하지 말고 진단의 역할·후보 수·식별 정보 누락 원인을 확인하세요. 다른 부모 요소로 자동 대체하지 않았습니다.")
    element = matches[0]
    if any(element.get(key) is True for key in ("is_password", "password", "is_protected", "protected")):
        raise OperationError("비밀번호 또는 보호 요소는 학습하지 않습니다.", "protected_element")
    try:
        selector = stable_selector(snapshot, element)
        if len(_find(snapshot, selector)) != 1:
            fail("no_unique_reusable_selector", "선택한 요소를 재사용할 고유한 기준으로 구분하지 못했습니다.", "ambiguous_selector")
    except OperationError as error:
        if not hasattr(error, "picker_diagnostic"):
            info.update(reason="no_unique_reusable_selector", matched_candidates=1)
            error.picker_diagnostic = dict(info)
        raise
    info.update(matched_candidates=1, reason="matched")
    if diagnostics is not None:
        diagnostics.update(info)
    return {"element_index": element["element_index"], "expected_selector": selector}


def _end_helper(child):
    if child is None:
        return
    try:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)
    except (OSError, subprocess.SubprocessError) as exc:
        error = OperationError("요소 선택 창 종료를 확인하지 못했습니다. 새 선택 창을 열지 않습니다.", "picker_close_failed")
        error.helper_cleanup_pending = True
        raise error from exc


def _helper_visible(pid, window_id):
    """Verify an owned, restored, on-screen window on the visible desktop."""
    if (os.name != "nt" or type(pid) is not int or pid <= 0
            or type(window_id) is not int or window_id <= 0):
        return False
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        function = getattr(user, name)
        function.argtypes, function.restype = [wintypes.HWND], wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.MonitorFromWindow.argtypes, user.MonitorFromWindow.restype = [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
    hwnd, owner, rect = wintypes.HWND(window_id), wintypes.DWORD(), wintypes.RECT()
    if (not user.IsWindow(hwnd) or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            or owner.value != pid or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd)
            or not user.GetWindowRect(hwnd, ctypes.byref(rect)) or rect.right <= rect.left or rect.bottom <= rect.top
            or not user.MonitorFromWindow(hwnd, 0)):
        return False
    try:
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        cloaked = wintypes.DWORD()
        if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0 or cloaked.value:
            return False
    except OSError:
        return False
    # Recheck ownership after the native queries; a stale/reused HWND is not
    # proof that this retained helper process is visible.
    return (bool(user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))) and owner.value == pid
            and bool(user.IsWindowVisible(hwnd)) and not bool(user.IsIconic(hwnd)))


def _read_exchange(path, nonce):
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        raise OperationError("요소 선택 응답이 너무 큽니다.", "picker_invalid_response")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("nonce") != nonce:
        raise OperationError("요소 선택 응답의 실행 정보를 확인하지 못했습니다.", "picker_invalid_response")
    return value


def _selection_result(value):
    status = value.get("status")
    messages = {"cancelled": ("요소 학습을 취소했습니다.", "picker_cancelled"),
                "timeout": ("요소 선택 시간이 지나 취소했습니다. 선택 창을 다시 열려면 학습 시작을 요청하세요.", "picker_timeout"),
                "read_timeout": ("프로그램의 요소 응답이 늦어 학습을 중단했습니다. 선택 창은 열렸으나 요소 읽기에 실패했습니다.", "picker_read_timeout"),
                "hotkey_unavailable": ("선택 도우미 단축키를 등록하지 못했습니다.", "picker_hotkey_unavailable"),
                "outside_target": ("선택한 위치가 지정한 학습 창에 속하지 않습니다. 현재 창 연결을 확인하세요.", "target_mismatch"),
                "protected": ("비밀번호 또는 보호 요소는 학습하지 않습니다.", "protected_element"),
                "unavailable": ("이 위치의 접근성 요소를 읽지 못했습니다. 같은 F8 안내를 반복하지 말고 오류를 전달하세요.", "picker_unavailable"),
                "target_unavailable": ("학습 대상 창이 닫혔거나 현재 사용할 수 없습니다. 대상 프로그램의 현재 창을 다시 연결하세요.", "target_unavailable"),
                "ready_failed": ("선택 도우미 실행 후 창 표시 확인에 실패했습니다. F8을 누를 단계가 아닙니다.", "picker_not_visible"),
                "runtime_failed": ("요소 선택 창에서 오류가 발생해 학습을 중단했습니다. 선택 도우미 진단을 확인하세요.", "picker_runtime_failed"),
                "controls_not_exposed": ("현재 앱이 선택한 버튼의 UIA 정보를 제공하지 않고 화면 영역만 노출합니다. F8을 반복해도 해결되지 않습니다. 이미지 기반 선택이 필요한 화면이며, 현재의 저장 UIA 요소 기능으로는 지원하지 않습니다.", "picker_controls_not_exposed"),
                "startup_failed": ("요소 선택 창을 표시하지 못했습니다. 도우미 시작 진단을 확인하세요.", "picker_startup_failed")}
    if status != "selected":
        message, code = messages.get(status, ("요소 선택 응답을 확인하지 못했습니다.", "picker_invalid_response"))
        exc = OperationError(message, code)
        exc.picker_diagnostic = {key: value[key] for key in ("code", "stage", "error_type")
                                 if isinstance(value.get(key), str) and len(value[key]) <= 80}
        exc.picker_diagnostic.update({key: value[key] for key in ("winerror", "hresult", "errno")
                                     if type(value.get(key)) is int})
        raise exc
    return value


def _run_helper(runtime, target, label, timeout_seconds, *, on_ready=None, cancel_event=None):
    helper = Path(__file__).resolve().with_name(HELPER_NAME)
    if not helper.is_file():
        raise OperationError("요소 선택 도구가 없습니다. 새 배포 ZIP을 모두 압축 해제해 주세요.", "picker_missing")
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    folder = Path(runtime.run_dir) / "learning"
    # The native .NET Framework helper observes Windows MAX_PATH. Keep the
    # exchange filename short while retaining the full 256-bit nonce in IPC.
    request, response = folder / (nonce[:16] + ".request.json"), folder / (nonce[:16] + ".response.json")
    ready_path = response.with_name(response.name + ".ready.json")
    child = None
    result = None
    stage = "request_setup"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = request.with_name(request.name + ".tmp")
        try:
            # One writer owns this random request. A second 32-character nonce
            # in the temporary filename would defeat the short-path protocol.
            temporary.write_text(json.dumps({"nonce": nonce, **target, "label": label,
                                             "timeout_seconds": timeout_seconds}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, request)
        finally:
            temporary.unlink(missing_ok=True)
        runtime.check_active()
        stage = "process_start"
        child = subprocess.Popen([str(helper), "--pick", str(request), str(response)],
                                 cwd=str(helper.parent), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        stage = "waiting_for_ready"
        started = time.monotonic()
        deadline = started + timeout_seconds + HELPER_READ_SECONDS + 3
        ready = False
        while not _stopped(runtime) and not (cancel_event is not None and cancel_event.is_set()):
            if not ready and ready_path.exists():
                shown = _read_exchange(ready_path, nonce)
                if (shown.get("status") != "ready" or any(shown.get(k) != v for k, v in target.items())
                        or type(shown.get("helper_pid")) is not int or shown["helper_pid"] != child.pid
                        or type(shown.get("helper_window_id")) is not int or shown["helper_window_id"] <= 0):
                    raise OperationError("선택 도우미가 실제 화면에 표시되었는지 확인하지 못했습니다.", "picker_not_visible")
                # Authenticated identity does not imply visibility. During a
                # handle/desktop transition, wait only within the original
                # startup budget and reread the helper's current HWND.
                if _helper_visible(child.pid, shown["helper_window_id"]):
                    ready = True
                    stage = "waiting_for_selection"
                    if on_ready is not None:
                        info = {k: shown[k] for k in ("helper_pid", "helper_window_id")}
                        info.update({k: shown[k] for k in ("f8_available", "escape_available") if type(shown.get(k)) is bool})
                        on_ready(info)
            if response.exists():
                result = _read_exchange(response, nonce)
                if on_ready is not None and result.get("status") == "selected":
                    if not ready:
                        raise OperationError("선택 창 표시 확인 전에 선택 결과가 도착해 저장하지 않았습니다.", "picker_not_visible")
                    if result.get("human_confirmed") is not True:
                        raise OperationError("사용자가 후보를 확정한 결과가 아니므로 저장하지 않았습니다.", "picker_confirmation_required")
                break
            if child.poll() is not None:
                raise OperationError("요소 선택 도우미가 응답 없이 종료되었습니다 (종료 코드: " + str(child.returncode) + ").", "picker_closed")
            if not ready and on_ready is not None and time.monotonic() - started >= HELPER_START_SECONDS:
                raise OperationError(f"요소 선택 도우미를 실행했지만 {HELPER_START_SECONDS}초 안에 창 표시를 확인하지 못했습니다. F8을 누를 단계가 아닙니다.", "picker_start_timeout")
            if time.monotonic() >= deadline:
                raise OperationError("요소 선택 대기 시간이 지나 취소했습니다.", "picker_timeout")
            runtime.stop_event.wait(0.05)
        if _stopped(runtime) or (cancel_event is not None and cancel_event.is_set()):
            raise OperationError("화면 작업이 중지되어 요소 학습도 취소했습니다.", "picker_cancelled")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, OperationError):
            raise
        error = OperationError("요소 선택 도구를 시작하지 못했습니다. 시작 단계와 Windows 오류 번호를 확인하세요."
                               if stage == "process_start" else "요소 선택 도구의 응답을 확인하지 못했습니다.",
                               "picker_launch_failed" if stage == "process_start" else "picker_invalid_response")
        error.picker_diagnostic = {"stage": stage, "error_type": type(exc).__name__}
        error.picker_diagnostic.update({key: getattr(exc, key) for key in ("winerror", "errno")
                                       if type(getattr(exc, key, None)) is int})
        raise error from exc
    finally:
        try:
            _end_helper(child)
        finally:
            for path in (request, response, ready_path, response.with_name(response.name + ".tmp"),
                         ready_path.with_name(ready_path.name + ".tmp")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    return _selection_result(result)


def pick_element(runtime, target, label, timeout_seconds=60):
    target = _target(target)
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise OperationError("학습할 요소의 별명을 1~200자로 입력해 주세요.")
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 180:
        raise OperationError("요소 선택 시간은 10~180초로 지정하세요.")
    runtime.check_active()
    if runtime.mode != "uia":
        raise OperationError("요소 학습은 uia 방식으로 화면 작업을 시작한 뒤 사용할 수 있습니다.", "unsupported_mode")
    args = {**target, "include_accessibility_tree": True, "include_screenshot": False,
            "max_depth": 32, "max_elements": 5000}
    # Guard authorizes the executable, user/session, PID and window before the
    # helper reads anything. This also refuses stale/foreign targets up front.
    first = runtime.call("get_window_state", args)
    if first.get("isError"):
        raise OperationError("학습 대상 창을 읽지 못했습니다. 먼저 창 연결을 확인해 주세요.", "picker_observation_failed")
    before = _payload(first)
    if any(key in before and before[key] != value for key, value in target.items()):
        raise OperationError("학습 대상으로 관찰한 창이 요청한 창과 다릅니다.", "target_mismatch")
    selected = _run_helper(runtime, target, label.strip(), timeout_seconds)
    runtime.check_active()
    answer = runtime.call("get_window_state", args)
    if answer.get("isError"):
        raise OperationError("선택 후 화면을 다시 읽지 못했습니다. 학습 결과를 저장하지 않았습니다.", "picker_observation_failed")
    runtime.check_active()
    return match_picked_element(_payload(answer), selected, target)
