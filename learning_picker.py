"""Human hover/F8 teaching. No application input, screenshots, or values are stored.

The fixed native helper only reads identity at a human-selected point. It never
selects a process from an executable path. Both sides require the same approved
PID/window; the final element is resolved again through the guarded Driver.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

from consent import _atomic_json
from learning import stable_selector
from operations import OperationError, _elements, _find, _name, _payload


HELPER_NAME = "Computer Use MCP 요소 선택.exe"
MAX_RESPONSE_BYTES = 32768
HELPER_READ_SECONDS = 6


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


def match_picked_element(snapshot, selected, target):
    """Match safe native identity to a fresh Driver snapshot, never by coordinates alone."""
    target = _target(target)
    if (not isinstance(selected, dict) or selected.get("status") != "selected"
            or any(selected.get(key) != value for key, value in target.items())
            or any(key in snapshot and snapshot[key] != value for key, value in target.items())):
        raise OperationError("선택한 위치 또는 관찰한 창이 학습 대상 창과 다릅니다.", "target_mismatch")
    identity = _descriptor(selected.get("element"))
    if not _contains(selected.get("bounds"), selected.get("point")):
        raise OperationError("선택한 요소의 위치가 바뀌었습니다. 다시 가리켜 주세요.", "picker_geometry_changed")
    ancestors = selected.get("ancestors", [])
    if not isinstance(ancestors, list) or len(ancestors) > 16:
        raise OperationError("선택한 요소의 부모 영역을 확인하지 못했습니다.", "picker_invalid_response")
    ancestors = [_descriptor(value) for value in ancestors]
    elements = _elements(snapshot)
    matches = [e for e in elements if type(e.get("element_index")) is int
               and e["element_index"] >= 0 and not e.get("synthetic_ancestor")
               and _identity_match(e, identity)]
    # A Driver may omit geometry. When it provides geometry it must still agree.
    matches = [e for e in matches if not isinstance(e.get("bounds"), dict)
               or _contains(e["bounds"], selected["point"])]
    if len(matches) > 1:
        # Use only a native ancestor whose own stable identity is unique in the
        # Driver tree, so duplicated unnamed panes cannot silently pick a row.
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
        raise OperationError("이 요소를 화면에서 다시 확인하지 못했습니다. 요소 가까이 가리킨 뒤 다시 시도해 주세요." if not matches
                             else "같은 요소가 여러 개여서 안전하게 구분하지 못했습니다. 이름이 있는 부모 영역과 함께 학습해 주세요.",
                             "picker_not_found" if not matches else "ambiguous_selector")
    element = matches[0]
    if any(element.get(key) is True for key in ("is_password", "password", "is_protected", "protected")):
        raise OperationError("비밀번호 또는 보호 요소는 학습하지 않습니다.", "protected_element")
    selector = stable_selector(snapshot, element)
    if len(_find(snapshot, selector)) != 1:
        raise OperationError("선택한 요소를 하나로 구분하지 못했습니다.", "ambiguous_selector")
    return {"element_index": element["element_index"], "expected_selector": selector}


def _end_helper(child):
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise OperationError("요소 선택 창 종료를 확인하지 못했습니다.", "picker_close_failed") from exc


def _run_helper(runtime, target, label, timeout_seconds):
    helper = Path(__file__).resolve().with_name(HELPER_NAME)
    if not helper.is_file():
        raise OperationError("요소 선택 도구가 없습니다. 새 배포 ZIP을 모두 압축 해제해 주세요.", "picker_missing")
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    folder = Path(runtime.run_dir) / "learning"
    request, response = folder / (nonce + ".request.json"), folder / (nonce + ".response.json")
    child = None
    result = None
    try:
        _atomic_json(request, {"nonce": nonce, **target, "label": label, "timeout_seconds": timeout_seconds})
        runtime.check_active()
        child = subprocess.Popen([str(helper), "--pick", str(request), str(response)],
                                 cwd=str(helper.parent), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        deadline = time.monotonic() + timeout_seconds + HELPER_READ_SECONDS + 2
        while not _stopped(runtime):
            if response.exists():
                if response.stat().st_size > MAX_RESPONSE_BYTES:
                    raise OperationError("요소 선택 응답이 너무 큽니다.", "picker_invalid_response")
                result = json.loads(response.read_text(encoding="utf-8-sig"))
                if not isinstance(result, dict) or result.get("nonce") != nonce:
                    raise OperationError("요소 선택 응답을 확인하지 못했습니다.", "picker_invalid_response")
                break
            if child.poll() is not None:
                raise OperationError("요소 선택 창이 응답 없이 종료되었습니다.", "picker_closed")
            if time.monotonic() >= deadline:
                raise OperationError("요소 선택 대기 시간이 지나 취소했습니다.", "picker_timeout")
            runtime.stop_event.wait(0.05)
        if _stopped(runtime):
            raise OperationError("화면 작업이 중지되어 요소 학습도 취소했습니다.", "picker_cancelled")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, OperationError):
            raise
        raise OperationError("요소 선택 도구의 응답을 확인하지 못했습니다.", "picker_invalid_response") from exc
    finally:
        _end_helper(child)
        for path in (request, response, response.with_name(response.name + ".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    status = result.get("status")
    messages = {"cancelled": ("요소 학습을 취소했습니다.", "picker_cancelled"),
                "timeout": ("요소 선택 대기 시간이 지나 취소했습니다.", "picker_timeout"),
                "read_timeout": ("프로그램의 요소 응답이 늦어 학습을 중단했습니다.", "picker_read_timeout"),
                "hotkey_unavailable": ("F8 또는 Esc 키를 다른 프로그램이 사용 중입니다. 해당 단축키를 해제한 뒤 다시 시도해 주세요.", "picker_hotkey_unavailable"),
                "outside_target": ("학습 대상 프로그램의 지정한 창 안에서 요소를 가리켜 주세요.", "target_mismatch"),
                "protected": ("비밀번호 또는 보호 요소는 학습하지 않습니다.", "protected_element"),
                "unavailable": ("이 위치의 요소를 읽지 못했습니다. 프로그램이 준비된 뒤 다시 시도해 주세요.", "picker_unavailable")}
    if status != "selected":
        message, code = messages.get(status, ("요소 선택 응답을 확인하지 못했습니다.", "picker_invalid_response"))
        raise OperationError(message, code)
    return result


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
