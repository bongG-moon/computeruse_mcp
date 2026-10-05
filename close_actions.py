"""One guarded close request followed by native lifecycle verification."""
from operations import OperationError, SELECTOR_SCHEMA, _Execution, _unique, validate_selector

CLOSE_ACTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["click", "hotkey"]},
        "selector": SELECTOR_SCHEMA,
        "keys": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 80}},
    }, "required": ["operation"],
}


def validate_close_action(action):
    if not isinstance(action, dict):
        raise OperationError("종료 버튼 또는 종료 단축키를 지정하세요.")
    if action.get("operation") == "click" and set(action) == {"operation", "selector"}:
        validate_selector(action["selector"])
    elif action.get("operation") == "hotkey" and set(action) == {"operation", "keys"}:
        keys = action["keys"]
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 8
                or any(not isinstance(k, str) or not k.strip() or len(k) > 80 for k in keys)
                or len(set(keys)) != len(keys)):
            raise OperationError("종료 단축키 형식이 올바르지 않습니다.")
    else:
        raise OperationError("종료 동작에는 click+selector 또는 hotkey+keys만 지정하세요.")
    return action


def _must_wait(result, target):
    if result["status"] in {"verified", "unknown"} or result.get("window_closed") is True:
        return True
    rows = result.get("remaining_windows", [])
    parent = next((w for w in rows if w.get("window_id") == target["window_id"] and w.get("pid") == target["pid"]), {})
    # Existing modeless tool/detail windows need not prevent an explicit close
    # of an enabled parent. A disabled/unknown parent with a possible dialog does.
    if parent.get("enabled") is False:
        return True
    return result["status"] == "needs_dialog" and parent.get("enabled") is not True


def request_close(runtime, target, close_action, *, scope="window", delivery_mode="background", timeout_ms=1500):
    """No retries and no popup choice; the client must apply the user's save policy."""
    action = validate_close_action(close_action)
    if runtime.mode != "uia":
        raise OperationError("computer_close는 UIA 방식입니다. 이미지 방식에서는 prepare_close 후 화면 조작과 verify_closed를 사용하세요.")
    if delivery_mode not in ("background", "foreground"):
        raise OperationError("입력 전달 방식을 확인하세요.")
    if type(timeout_ms) is not int or not 0 <= timeout_ms <= 10000:
        raise OperationError("timeout_ms는 0~10000 사이 정수여야 합니다.")
    with runtime.execution_lock:
        runtime.check_active()
        prepared = runtime.closures.prepare(target, scope=scope)
        close_id = prepared["close_id"]
        before = runtime.closures.verify(close_id, timeout_ms=0)
        if _must_wait(before, target):
            return {**before, "input_dispatched": False, "close_request_replayed": False}
        execution = _Execution(runtime, {"operation": action["operation"]}, dict(target), delivery_mode)
        action_error = None
        try:
            snapshot = execution._observe([action["selector"]] if action["operation"] == "click" else [])
            # Lifecycle identity is rechecked after potentially slow accessibility reads.
            before = runtime.closures.verify(close_id, timeout_ms=0)
            if _must_wait(before, target):
                return {**before, "input_dispatched": False, "close_request_replayed": False,
                        "metrics": execution.metrics}
            if action["operation"] == "click":
                execution._mutate("click", _unique(snapshot, action["selector"]), snapshot)
            else:
                execution._call("hotkey", dict(target, keys=action["keys"], delivery_mode=delivery_mode), mutation=True)
        except Exception as exc:
            action_error = {"code": getattr(exc, "code", "close_request_error"), "message": str(exc)[:1000]}
        # A disappearing target can make the Driver acknowledgement fail. Native
        # evidence decides closure; never re-read its UIA or repeat the request.
        verified = runtime.closures.verify(close_id, timeout_ms=timeout_ms)
        return {**verified, "input_dispatched": execution.dispatched,
                "focus_dispatched": execution.focus_dispatched, "close_request_replayed": False,
                "action_error": action_error, "metrics": execution.metrics}
