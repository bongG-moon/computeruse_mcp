"""Private Guard boundary for independently observed native menus.

No public coordinates or native locators are accepted. A durable menu path is
resolved here immediately before one approved action; observations grant no
Driver input handles and are consumed on every outcome.
"""
import copy
import os
import time
import uuid

from operations import OperationError
from vendor.guard import GuardError, validate_arguments, result_summary


def _stable_observation(value):
    # Timestamps are freshness metadata. All native identity, chain, geometry,
    # state and capability evidence must remain identical across approval.
    if isinstance(value, dict):
        return {key: _stable_observation(item) for key, item in value.items() if key != "observed_at_ms"}
    if isinstance(value, list):
        return [_stable_observation(item) for item in value]
    return value


def observe(guard, target, check_active, *, timeout_ms=2000):
    from menu_accessibility import observe as native_observe
    if timeout_ms < 200:
        raise OperationError("메뉴 관찰에 남은 시간이 부족합니다.", "menu_observation_timeout")
    guard.observed_targets.clear()
    guard._native_popup_frame = None
    def check():
        check_active()
        guard.check_stop()
        validate_arguments("get_window_state", target, guard.policy, guard.process_resolver, guard.window_resolver)
    check()
    answer = native_observe(dict(target), timeout=min(30, timeout_ms/1000), check=check)
    check()
    return answer


def act(guard, target, menu_target, operation, check_active, revalidate_identity, *, timeout_ms=10000):
    from semantic_menus import resolve_menu, validate_menu_target
    from menu_accessibility import act as native_act
    from transient_popup import proven_popup, popup_proof
    from privileges import process_integrity
    started, request_id = time.monotonic(), uuid.uuid4().hex
    sent, phase = False, "preflight"
    attempted = False
    details = {}
    guard.observed_targets.clear()
    guard._native_popup_frame = None
    args = {**target, "operation": operation}
    guard.log("request", request_id=request_id, tool="menu_command" if operation in {"invoke", "command"} else "expand_menu",
              route="native_semantic_menu", arguments=args, mode=guard.policy["mode"])
    def active():
        check_active()
        guard.check_stop()
        if time.monotonic()-started >= timeout_ms/1000:
            raise OperationError("메뉴 단계 시간이 지났습니다.", "menu_action_timeout")
    try:
        if operation not in {"expand", "hover", "click_parent", "invoke", "command"}:
            raise OperationError("지원하지 않는 메뉴 입력입니다.", "menu_action_unsupported")
        validate_menu_target(menu_target)
        active()
        if guard.policy["mode"] != "uia":
            raise OperationError("메뉴 의미 입력은 UIA 세션에서만 실행합니다.", "unsupported_mode")
        validate_arguments("get_window_state", target, guard.policy, guard.process_resolver, guard.window_resolver)
        executable = guard.target_executable(target)
        geometry = guard.image_geometry_resolver(target["window_id"])
        proof = popup_proof(target) if proven_popup(target) else None
        if proof: proof.pop("foreground", None)
        def ready():
            active()
            validate_arguments("get_window_state", target, guard.policy, guard.process_resolver, guard.window_resolver)
            revalidate_identity()
            if guard.target_executable(target) != executable or guard.image_geometry_resolver(target["window_id"]) != geometry:
                raise OperationError("메뉴 창의 실행파일이나 화면 경계가 바뀌었습니다.", "menu_target_changed")
            if guard.checkpoint_ready_resolver(target["window_id"]) is not True and not proven_popup(target):
                raise OperationError("현재 메뉴의 전경 또는 소유 관계를 확인하지 못했습니다.", "menu_foreground_unavailable")
            if proof is not None:
                current = popup_proof(target); current.pop("foreground", None)
                if current != proof:
                    raise OperationError("메뉴 팝업 소유 증거가 바뀌었습니다.", "menu_target_changed")
        ready()
        purpose = "command" if operation in {"invoke", "command"} else "parent"
        def remaining_ms():
            active()
            return max(0, int(timeout_ms-(time.monotonic()-started)*1000))
        selected = resolve_menu(observe(guard, target, active, timeout_ms=min(2000, remaining_ms())), menu_target, purpose=purpose)
        actions = selected["item"].get("supported_actions", [])
        native_operation = ("invoke" if "invoke" in actions else "click_leaf") if operation == "command" else operation
        if native_operation not in actions:
            raise OperationError("현재 접근성 공급자가 해당 메뉴 입력을 지원하지 않습니다.", "menu_action_unsupported")
        if guard.action_count >= guard.policy["max_actions"]:
            raise GuardError("Maximum action count reached.")
        guard.approve("menu_command" if purpose == "command" else "expand_menu", args, request_id,
                      request_deadline=started+timeout_ms/1000)
        ready()
        # This helper inherits the MCP token, not the separate Driver token.
        source, destination = process_integrity(os.getpid()), process_integrity(target["pid"])
        if (source.get("checked") is not True or destination.get("checked") is not True
                or type(source.get("rid")) is not int or type(destination.get("rid")) is not int):
            raise OperationError("네이티브 메뉴 입력 권한을 확인하지 못했습니다.", "menu_integrity_unknown")
        if source.get("ui_access") is not True and source.get("rid", -1) < destination.get("rid", 0):
            raise OperationError("메뉴 대상이 MCP보다 높은 권한입니다.", "elevation_required")
        # Approval may take minutes. Resolve the same path, source and exact
        # evidence again; the native helper independently repeats this check.
        current = resolve_menu(observe(guard, target, active, timeout_ms=min(2000, remaining_ms())), menu_target, purpose=purpose)
        if _stable_observation(current) != _stable_observation(selected):
            raise OperationError("승인 중 메뉴 항목이 바뀌었습니다. 다시 관찰하세요.", "menu_item_changed")
        ready()
        if guard.action_count >= guard.policy["max_actions"]:
            raise GuardError("Maximum action count reached.")
        remaining = min(30, remaining_ms()/1000)
        if remaining < .2:
            raise OperationError("메뉴 입력 전에 단계 시간이 지났습니다.", "menu_action_timeout")
        guard.action_count += 1
        attempted, sent, phase = True, None, "native_dispatch"
        # Following dispatch only stop/deadline is polled. A successful leaf
        # may close the popup; that never triggers a second input.
        details = native_act(dict(target), current["item"]["locator"], current["item"]["evidence"],
                             native_operation, timeout=remaining, check=active)
        data = details.get("structuredContent", details)
        sent = data.get("input_sent", data.get("input_dispatched"))
        if sent is not True and sent is not False and sent is not None: sent = None
        phase = data.get("delivery_phase", "native_returned")
        diagnostic = data.get("diagnostic") if isinstance(data.get("diagnostic"), dict) else {}
        code = diagnostic.get("code", data.get("code", "menu_input_delivered" if sent is True else "menu_input_unknown"))
        message = diagnostic.get("message", data.get("message", "메뉴 입력 결과를 별도 완료 조건에서 확인하세요."))
    except (GuardError, OperationError, OSError, RuntimeError, ValueError) as error:
        code, message = getattr(error, "code", "menu_guard_failed"), str(error)
        if not attempted or code in {"menu_accessibility_helper_missing", "menu_accessibility_helper_start_failed"}: sent = False
    finally:
        guard.observed_targets.clear()
        guard._native_popup_frame = None
    answer = {"status": "awaiting_verification" if sent is True else "needs_review", "input_dispatched": sent,
              "task_verified": False, "business_result_verified": False, "verification_deferred": sent is True,
              "input_backend": "native-menu-accessibility", "delivery_phase": phase,
              "diagnostic": {"code": code, "message": message[:1000], "automatic_replay": False},
              "native_delivery": copy.deepcopy(details),
              "metrics": {"elapsed_ms": round((time.monotonic()-started)*1000, 2), "mutations": int(attempted)}}
    guard.log("result", request_id=request_id, tool="menu_command" if operation in {"invoke", "command"} else "expand_menu",
              route="native_semantic_menu", action_count=guard.action_count, summary=result_summary(answer))
    return answer
