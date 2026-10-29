"""Fresh menu paths, independently observed accessibility, and one-shot input.

Saved names are search criteria, never native input handles. Selection is
scoped to a proven current menu window; commands require a later result check.
"""
from __future__ import annotations

import copy
import time

from operations import OperationError


def validate_menu_target(value):
    if not isinstance(value, dict) or set(value) != {"path"}:
        raise OperationError("메뉴 대상에는 실제 관찰한 이름 경로가 필요합니다.", "invalid_menu_target")
    path = value["path"]
    if not isinstance(path, list) or not 1 <= len(path) <= 16:
        raise OperationError("메뉴 경로 길이를 확인하세요.", "invalid_menu_target")
    for item in path:
        if (not isinstance(item, dict) or set(item) != {"name", "role"}
                or item["role"] != "MenuItem" or not isinstance(item["name"], str)
                or not 1 <= len(item["name"].strip()) <= 1000 or "\x00" in item["name"]):
            raise OperationError("메뉴 경로는 실제 MenuItem의 표시 이름으로 지정하세요.", "invalid_menu_target")
    return copy.deepcopy(value)


def validate_menu_command(step):
    allowed = {"operation", "menu_target", "completion_mode", "step_timeout_ms"}
    if (not isinstance(step, dict) or set(step) - allowed or step.get("operation") != "menu_command"
            or step.get("completion_mode") != "human"):
        raise OperationError("메뉴 명령에는 별도 결과 확인이 필요합니다.", "invalid_menu_command")
    validate_menu_target(step.get("menu_target"))
    if type(step.get("step_timeout_ms", 10000)) is not int or not 1 <= step.get("step_timeout_ms", 10000) <= 120000:
        raise OperationError("메뉴 명령 제한 시간을 확인하세요.", "invalid_menu_command")
    return copy.deepcopy(step)


def _path(row, rows):
    found, seen = [], set()
    while row is not None:
        index = row.get("index")
        if type(index) is not int or index in seen:
            raise OperationError("메뉴 계층이 잘렸거나 순환합니다.", "menu_observation_incomplete")
        seen.add(index)
        if row.get("role") == "MenuItem":
            found.append({"name": row.get("name"), "role": "MenuItem"})
        parent = row.get("parent_index")
        if parent is None or parent == -1:
            break
        row = rows.get(parent)
        if row is None:
            raise OperationError("메뉴의 부모 관찰이 누락됐습니다.", "menu_observation_incomplete")
    return list(reversed(found))


def resolve_menu(observation, menu_target, *, purpose="presence"):
    """Prefer a complete UIA source; independent MSAA is not a Driver handle."""
    path = validate_menu_target(menu_target)["path"]
    sources = observation.get("sources", {}) if isinstance(observation, dict) else {}
    complete_count = 0
    statuses = {}
    for source_name in ("uia_raw", "direct_msaa"):
        source = sources.get(source_name, {})
        statuses[source_name] = source.get("status", "unknown")
        if source.get("complete") is not True or source.get("truncated") is not False:
            continue
        elements = source.get("elements")
        if not isinstance(elements, list) or len(elements) > 500:
            raise OperationError("메뉴 관찰 범위를 확인하지 못했습니다.", "menu_observation_invalid")
        complete_count += 1
        rows = {row.get("index"): row for row in elements if isinstance(row, dict)}
        if len(rows) != len(elements) or any(type(key) is not int for key in rows):
            raise OperationError("메뉴 관찰 식별자가 중복됐습니다.", "menu_observation_invalid")
        candidates = [row for row in elements if row.get("role") == "MenuItem"
                      and row.get("name") == path[-1]["name"] and _path(row, rows) == path]
        visible = [row for row in candidates if row.get("visible") is True]
        if len(visible) > 1:
            raise OperationError("같은 메뉴 경로에 표시된 항목이 여러 개입니다.", "menu_item_ambiguous")
        if not visible:
            if candidates:
                raise OperationError("해당 메뉴 항목은 현재 표시되지 않습니다.", "menu_item_hidden")
            continue
        row = visible[0]
        if row.get("enabled") is not True:
            raise OperationError("현재 메뉴 항목이 비활성 상태이거나 상태를 읽지 못했습니다.", "menu_item_disabled")
        bounds = row.get("bounds", {})
        if (not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}
                or any(type(v) not in (int, float) or not (-2**30 < v < 2**30) for v in bounds.values())
                or bounds["width"] <= 0 or bounds["height"] <= 0):
            raise OperationError("현재 메뉴 항목의 화면 경계를 확인하지 못했습니다.", "menu_item_bounds_unavailable")
        actions = row.get("supported_actions", [])
        parent = row.get("has_children") is True or row.get("has_popup") is True or row.get("expansion_state") in {"expanded", "collapsed", "partially_expanded"} or "expand" in actions
        if purpose == "parent" and not parent:
            # A provider can expose a name while withholding submenu semantics.
            # Read the independent MSAA provider instead of inventing capability.
            if row.get("has_children") is not False: continue
            raise OperationError("부모 메뉴의 실제 펼침 기능을 확인하지 못했습니다.", "menu_parent_capability_unavailable")
        if purpose == "command" and parent:
            raise OperationError("최종 명령인지 확인되지 않은 부모 항목은 실행하지 않습니다.", "menu_command_not_leaf")
        if purpose == "command" and row.get("has_children") is not False: continue
        if not isinstance(row.get("locator"), dict) or not isinstance(row.get("evidence"), dict):
            raise OperationError("메뉴를 새로 연결할 관찰 증거가 없습니다.", "menu_evidence_unavailable")
        return {"source": source_name, "item": copy.deepcopy(row), "menu_target": copy.deepcopy(menu_target),
                "read_only": True, "authorizes_input": False}
    code = "menu_item_not_found" if complete_count else "menu_observation_incomplete"
    raise OperationError("현재 메뉴 경로를 확인하지 못했습니다. 접근성 출처: " + str(statuses), code)


def observe_menu(runtime, target, *, timeout_ms=2000):
    runtime.check_active()
    callback = getattr(runtime, "observe_menu", None)
    if not callable(callback):
        raise OperationError("메뉴 접근성 도우미가 없습니다. 전체 설치본을 갱신하세요.", "menu_accessibility_unavailable")
    observation = callback(target, timeout_ms=timeout_ms)
    runtime.check_active()
    actual = observation.get("target", observation) if isinstance(observation, dict) else {}
    if (any(actual.get(k) != v for k, v in target.items()) or observation.get("read_only") is not True
            or observation.get("authorizes_input") is not False):
        raise OperationError("메뉴 관찰의 대상과 읽기 전용 범위를 확인하지 못했습니다.", "menu_observation_invalid")
    return observation


def _answer(operation, *, verified=False, sent=False, code, message, **extra):
    return {"operation": operation, "status": "verified" if verified else "needs_review",
            "task_verified": verified, "business_result_verified": False, "input_dispatched": sent,
            "step_delivery": "sent" if sent is True else "not_sent" if sent is False else "unknown",
            "diagnostic": {"code": code, "message": message, "automatic_replay": False}, **extra}


def verify_menu_precondition(runtime, step, target):
    started = time.monotonic()
    try:
        selected = resolve_menu(observe_menu(runtime, target), step["menu_target"], purpose="parent" if step.get("menu_purpose", "parent") == "parent" else "command")
        return _answer("checkpoint", verified=True, code="menu_parent_ready",
            message="현재 메뉴 경로, 표시 상태와 기능을 확인했습니다. 강조색은 사전 조건으로 사용하지 않았습니다.",
            verification_scope="menu_semantic_presence_only", menu_source=selected["source"],
            metrics={"elapsed_ms": round((time.monotonic()-started)*1000, 2), "image_comparisons": 0})
    except OperationError as error:
        return _answer("checkpoint", code=error.code, message=str(error))


def choose_menu_window(runtime, probe, owner, candidates, menu_target, *, timeout_ms=4000):
    """All candidates already passed PID/class/owner; never guess unread peers."""
    if not 1 <= len(candidates) <= 4:
        raise OperationError("메뉴 창 후보가 너무 많아 구별하지 못했습니다.", "menu_window_ambiguous")
    selected = []
    before = copy.deepcopy(candidates)
    deadline = time.monotonic()+timeout_ms/1000
    for row in candidates:
        target = {key: row[key] for key in ("pid", "window_id")}
        try:
            remaining = int((deadline-time.monotonic())*1000)
            if remaining < 200:
                raise OperationError("메뉴 후보 관찰 시간이 지났습니다.", "menu_observation_timeout")
            resolve_menu(observe_menu(runtime, target, timeout_ms=min(2000,remaining)), menu_target)
            if time.monotonic() >= deadline:
                raise OperationError("메뉴 후보 관찰 시간이 지났습니다.", "menu_observation_timeout")
            selected.append(row)
        except OperationError as error:
            if error.code not in {"menu_item_not_found", "menu_item_hidden"}:
                raise
    fresh = getattr(probe, "transition_snapshot", probe.snapshot)()
    from recording_windows import owned_by
    rows = fresh.get("windows", [])
    if time.monotonic() >= deadline:
        raise OperationError("메뉴 후보를 다시 확인하는 동안 시간이 지났습니다.", "menu_observation_timeout")
    for old in before:
        current = next((row for row in rows if row.get("window_id") == old["window_id"]), {})
        if (any(current.get(k) != old.get(k) for k in ("pid", "class_name", "thread_id", "owner_window_id", "visible"))
                or not (owned_by(current, owner["window_id"], rows) or callable(getattr(probe, "ownership_path", None)) and probe.ownership_path(current["window_id"], owner["window_id"]))):
            raise OperationError("메뉴 후보를 읽는 동안 소유 관계나 수명이 바뀌었습니다.", "menu_child_changed")
    if len(selected) > 1:
        raise OperationError("같은 메뉴 경로가 여러 팝업에 있습니다.", "menu_window_ambiguous")
    return selected[0] if selected else None


def execute_menu_command(runtime, step, target):
    validate_menu_command(step)
    answer = runtime.menu_action(target, step["menu_target"], "command", timeout_ms=step.get("step_timeout_ms", 10000))
    sent = answer.get("input_dispatched", answer.get("input_sent"))
    diagnostic = answer.get("diagnostic", {})
    return _answer("menu_command", sent=sent,
        code=diagnostic.get("code", "menu_command_awaiting_result"),
        message=diagnostic.get("message", "메뉴 명령을 한 번 전달했습니다. 다음 단계에서 결과를 별도로 확인하세요."),
        verification_deferred=sent is True, verification_scope="menu_command_delivery_only",
        menu_command={"menu_path": [row["name"] for row in step["menu_target"]["path"]],
                      "command_delivery": "sent" if sent is True else "not_sent" if sent is False else "unknown"},
        native_delivery=answer)


def execute_semantic_expansion(runtime, step, target, *, verify_only=False):
    started = time.monotonic()
    sent = False
    probe = None
    child_target = None
    source = "unknown"
    last = {}
    deadline = started+step.get("step_timeout_ms",10000)/1000
    def read_timeout(child_deadline=None):
        end = min(deadline, child_deadline) if child_deadline is not None else deadline
        remaining = int((end-time.monotonic())*1000)
        if remaining < 200:
            raise OperationError("메뉴 확인 제한 시간이 지났습니다. 입력을 반복하지 않습니다.", "menu_observation_timeout")
        return min(2000,remaining)
    def result(code, message, verified=False):
        return _answer("expand_menu", verified=verified, sent=sent, code=code, message=message,
            child_target=child_target, verification_scope="menu_child_visibility_only",
            menu_expansion={"stage": "child_visible" if verified else "child_observation" if sent is not False else "parent_observation",
                "menu_path": [row["name"] for row in step["menu_target"]["path"]], "menu_depth": step.get("menu_depth", 1),
                "expansion_method": step["expansion_method"], "verify_only": verify_only,
                "expansion_delivery": "sent" if sent is True else "not_sent" if sent is False else "unknown",
                "child_command_delivery": "not_sent", "child_representation": "owned_popup" if "child_window" in step else "logical_menu_child",
                "source": source, "observation": last, "business_result_verified": False},
            metrics={"elapsed_ms": round((time.monotonic()-started)*1000, 2), "parent_image_comparisons": 0})
    try:
        probe = runtime.create_transition_probe(dict(target))
        from interaction import _identity
        baseline = _identity(probe.capture(), target["window_id"])
        def identity():
            runtime.check_active()
            state = getattr(probe, "transition_snapshot", probe.snapshot)()
            if _identity(state, target["window_id"]) != baseline:
                raise OperationError("메뉴 창이 교체됐습니다.", "menu_parent_changed")
        identity()
        parent = resolve_menu(observe_menu(runtime, target, timeout_ms=read_timeout()), step["menu_target"], purpose="parent")
        source = parent["source"]
        owner = dict(target)
        from transient_popup import proven_popup, popup_owner
        if proven_popup(target):
            owner = popup_owner(target)
        def child(child_deadline=None):
            nonlocal last
            identity()
            if "child_window" not in step:
                try:
                    resolve_menu(observe_menu(runtime, target, timeout_ms=read_timeout(child_deadline)), step["child_menu_target"])
                    return dict(target)
                except OperationError as error:
                    if error.code in {"menu_item_not_found", "menu_item_hidden"}: return None
                    raise
            from recording_windows import execute_window_wait
            wait = {"operation": "wait_for_window", "program_id": "menu", "owner_ref": "owner", "window_ref": "child",
                    "timeout_ms": 0, **step["child_window"]}
            hint = step.get("child_menu_target")
            last = execute_window_wait(runtime, wait, {("menu", "owner"): owner},
                menu_hint=hint, menu_budget_ms=read_timeout(child_deadline),
                image_hint=None if hint else step.get("child_image_target"), exclude_window_ids={target["window_id"]})
            identity()
            if last.get("task_verified") is True:
                found = last["target"]
                if not hint:
                    # A unique HWND proves ownership, not the saved child row.
                    # Image-only leaf evidence remains strict and read-only.
                    last = runtime.image_action({"operation":"wait_for_image", "image_target":step["child_image_target"],
                                                 "timeout_ms":0}, found)
                    identity()
                    if last.get("task_verified") is not True:
                        code = last.get("diagnostic",{}).get("code", "menu_child_image_unavailable")
                        if code in {"image_not_found","image_wait_timeout"}: return None
                        raise OperationError("자식 메뉴 이미지를 유일하게 확인하지 못했습니다.",code)
                return found
            if last.get("diagnostic", {}).get("code") != "recording_window_missing":
                raise OperationError(last.get("diagnostic", {}).get("message", "자식 메뉴 확인 실패"), last.get("diagnostic", {}).get("code", "menu_child_unavailable"))
            return None
        child_target = child()
        if time.monotonic() >= deadline:
            return result("menu_observation_timeout", "메뉴 관찰 시간이 지났습니다. 입력을 보내거나 늦은 결과를 승인하지 않았습니다.")
        if child_target:
            return result("menu_already_expanded", "현재 자식 메뉴를 확인했습니다. 펼침 입력을 반복하지 않았습니다.", True)
        if not verify_only:
            identity()
            actions = parent["item"].get("supported_actions", [])
            method = "expand" if "expand" in actions else "hover" if step["expansion_method"] == "hover" else "click_parent" if step["expansion_method"] == "click" else "expand"
            sent = None
            answer = runtime.menu_action(target, step["menu_target"], method, timeout_ms=max(1,int((deadline-time.monotonic())*1000)))
            sent = answer.get("input_dispatched", answer.get("input_sent"))
            last = answer
            if sent is not True:
                return result(answer.get("diagnostic", {}).get("code", "menu_expansion_input_unknown"), "메뉴 전개 입력을 확인하지 못했습니다. 입력은 반복하지 않습니다.")
        interval = step.get("verification_timeout_ms",1500)
        child_deadline = min(deadline,time.monotonic()+interval/1000) if interval else deadline
        while True:
            child_target = child(child_deadline)
            if time.monotonic() >= child_deadline:
                return result("menu_child_timeout", "메뉴 확인 시간이 지났습니다. 늦게 도착한 결과를 성공으로 처리하지 않습니다.")
            if child_target: return result("menu_child_visible", "현재 자식 메뉴를 확인했습니다. 최종 명령은 별도로 실행합니다.", True)
            remaining = child_deadline-time.monotonic()
            if interval == 0 or remaining <= 0: return result("menu_child_timeout", "자식 메뉴가 아직 확인되지 않습니다. 부모 입력은 반복하지 않습니다.")
            runtime.stop_event.wait(min(remaining, step.get("poll_interval_ms", 100)/1000))
    except (OperationError, OSError, RuntimeError) as error:
        return result(getattr(error, "code", "menu_expansion_failed"), str(error))
    finally:
        if probe is not None: probe.close()
