"""Explicit image recipe steps; images are evidence of location, never success."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from image_targets import validate_image_target
from operations import OperationError, _Execution, _name, _unique, validate_assertions, validate_step

IMAGE_MUTATIONS = {"image_click", "image_double_click", "image_right_click", "image_type_text", "image_press_key", "image_hotkey", "image_scroll"}
IMAGE_OPERATIONS = IMAGE_MUTATIONS | {"wait_for_image"}


def validate_image_step(step):
    if not isinstance(step, dict) or not isinstance(step.get("operation"), str) or step["operation"] not in IMAGE_OPERATIONS:
        raise OperationError("지원하지 않는 이미지 동작입니다.")
    kind = step["operation"]
    extra = {"image_type_text": {"value", "replace_all"}, "image_press_key": {"key"}, "image_hotkey": {"keys"},
             "image_scroll": {"direction", "amount"}, "wait_for_image": {"timeout_ms", "poll_interval_ms"}}.get(kind, set())
    if kind in IMAGE_MUTATIONS:
        extra = extra | {"expect", "verification_timeout_ms", "poll_interval_ms", "step_timeout_ms"}
    if set(step) - ({"operation", "image_target"} | extra):
        raise OperationError("이미지 동작에 지원하지 않는 인자가 있습니다.")
    validate_image_target(step.get("image_target"))
    if kind in IMAGE_MUTATIONS and "expect" in step:
        checks = validate_assertions(step["expect"])
        if not any(check.get("require_change") is True for check in checks):
            raise OperationError("이미지 자동 확인에는 동작 전후 변화를 검사할 require_change:true 조건이 하나 이상 필요합니다.")
        validate_step({"operation": "assert", "expect": checks, **{key: step[key] for key in
            ("verification_timeout_ms", "poll_interval_ms", "step_timeout_ms") if key in step}})
    elif kind in IMAGE_MUTATIONS and any(key in step for key in ("verification_timeout_ms", "poll_interval_ms", "step_timeout_ms")):
        raise OperationError("이미지 자동 확인 시간은 expect 완료 조건과 함께 지정하세요.")
    if kind == "image_type_text":
        value = step.get("value")
        replace = step.get("replace_all", False)
        if type(replace) is not bool or not isinstance(value, str) or not (0 if replace else 1) <= len(value) <= 16000 or "\x00" in value:
            raise OperationError("이미지 입력값과 replace_all 설정을 확인하세요. 전체 교체는 빈 문자열로 지울 수 있습니다.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise OperationError("입력값에 저장할 수 없는 문자가 있습니다.") from None
    if kind in {"image_press_key", "image_hotkey"}:
        keys = [step.get("key")] if kind == "image_press_key" else step.get("keys")
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 8 or any(not isinstance(k, str) or not k.strip() or len(k) > 80 for k in keys)
                or len(set(keys)) != len(keys)):
            raise OperationError("입력할 키 이름을 확인하세요.")
    if kind == "image_scroll" and (step.get("direction") not in {"up", "down", "left", "right"}
                                  or type(step.get("amount")) is not int or not 1 <= step["amount"] <= 20):
        raise OperationError("스크롤 방향과 1~20의 양을 지정하세요.")
    if kind == "wait_for_image":
        if (type(step.get("timeout_ms")) is not int or not 0 <= step["timeout_ms"] <= 60000
                or type(step.get("poll_interval_ms", 500)) is not int or not 100 <= step.get("poll_interval_ms", 500) <= 2000):
            raise OperationError("이미지 대기 시간은 0~60000ms, 확인 간격은 100~2000ms로 지정하세요.")
    return copy.deepcopy(step)


def execute_image_step(runtime, step, target, *, verification_state=None, save_verification=None, verify_only=False):
    step = validate_image_step(step)
    runtime.check_active()
    if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
            or any(type(v) is not int or v < 1 for v in target.values())):
        raise OperationError("이미지를 찾을 현재 창을 지정하세요.", "invalid_target")
    if runtime.mode != "uia":
        raise OperationError("저장한 이미지 단계는 프로세스용 UIA 세션에서 실행하세요.", "unsupported_mode")
    action = getattr(runtime, "image_action", None)
    if not callable(action):
        raise OperationError("이 실행 환경에 이미지 실행 기능이 없습니다.", "image_unavailable")
    if step["operation"] in IMAGE_MUTATIONS and step.get("expect"):
        return _automatic_image(runtime, step, target, verification_state, save_verification, verify_only)
    if verify_only:
        raise OperationError("직접 확인 방식의 이미지 입력은 자동 재실행하지 않습니다.", "image_action_uncertain")
    deadline = time.monotonic()+step.get("timeout_ms", 0)/1000
    while True:
        runtime.check_active()
        result = action(step, copy.deepcopy(target))
        runtime.check_active()
        if step["operation"] == "wait_for_image":
            result = {**result, "verification_scope": "image_presence_only", "business_result_verified": False}
        if step["operation"] != "wait_for_image" or result.get("diagnostic", {}).get("code") != "image_not_found":
            return result
        if time.monotonic() >= deadline:
            return {**result, "diagnostic": {"code": "image_wait_timeout", "automatic_replay": False,
                                            "message": "제한 시간 안에 이미지가 나타나지 않았습니다."}}
        runtime.stop_event.wait(min(step.get("poll_interval_ms", 500)/1000, max(0, deadline-time.monotonic())))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_image_verification(state):
    """A small durable proof stores hashes, never observed field contents."""
    fields = {"expect_hash", "identity_hash", "baseline_hashes", "change_seen", "input_attempted", "input_dispatched"}
    if not isinstance(state, dict) or set(state) != fields:
        raise OperationError("이미지 완료 확인 기록이 불완전합니다.", "image_baseline_required")
    hashes = state["baseline_hashes"]
    if (any(not isinstance(state[k], str) or not re.fullmatch(r"[a-f0-9]{64}", state[k]) for k in ("expect_hash", "identity_hash"))
            or not isinstance(hashes, list) or not 1 <= len(hashes) <= 20
            or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)) for v in hashes)
            or not isinstance(state["change_seen"], list) or len(state["change_seen"]) != len(hashes)
            or any(type(v) is not bool for v in state["change_seen"])
            or type(state["input_attempted"]) is not bool
            or (state["input_dispatched"] is not None and type(state["input_dispatched"]) is not bool)):
        raise OperationError("이미지 완료 확인 기록 형식이 올바르지 않습니다.", "image_baseline_required")
    return copy.deepcopy(state)


def _identity(runtime, target):
    factory = getattr(runtime, "create_transition_probe", None)
    if not callable(factory):
        # Lightweight test/embedded runtimes can resume only in their same session.
        return _hash({"session_id": runtime.id, "target": target})
    probe = factory(copy.deepcopy(target))
    try:
        state = probe.capture()
        row = next((row for row in state.get("windows", []) if row.get("window_id") == target["window_id"]), None)
        if (state.get("process_exited") is not False or state.get("target_present") is not True
                or not state.get("creation_time") or not state.get("executable") or not row
                or not row.get("class_name") or not row.get("thread_id")):
            raise OperationError("현재 프로그램과 창의 식별 정보를 확인하지 못했습니다.", "image_target_identity_unavailable")
        return _hash({"target": target, "creation_time": state["creation_time"], "executable": state["executable"],
            "class_name": row["class_name"], "thread_id": row["thread_id"]})
    finally:
        probe.close()


def _actual(snapshot, check):
    element = _unique(snapshot, check["selector"])
    prop = check["property"]
    present = "name" in element or "label" in element if prop == "name" else prop in element
    if not present:
        raise OperationError("완료 조건의 속성을 읽지 못했습니다.", "property_unavailable")
    actual = _name(element) if prop == "name" else element[prop]
    return actual, _hash({"type": type(actual).__name__, "value": actual})


def _automatic_image(runtime, step, target, state, save_state, verify_only):
    checks = step["expect"]
    class CompletionRuntime:
        # First-time image completion also needs non-actionable UIA properties.
        # It never grants Driver element tokens or opts the recipe into fast mode.
        def __getattr__(self, key):
            return getattr(runtime, key)
        def observe_controls(self, target, selectors, *, timeout_ms):
            observer = getattr(runtime, "observe_completion_controls", None)
            if not callable(observer): observer = getattr(runtime, "observe_controls", None)
            if not callable(observer): raise NotImplementedError("scoped_completion_unavailable")
            return observer(target, selectors, timeout_ms=timeout_ms)
    engine = _Execution(CompletionRuntime(), {"operation": "assert", "expect": checks, **{key: step[key] for key in
        ("verification_timeout_ms", "poll_interval_ms", "step_timeout_ms") if key in step}}, dict(target), "foreground")
    action_result = None
    def persist():
        if callable(save_state):
            save_state(copy.deepcopy(state))
    def report(stage):
        callback = getattr(runtime, "report_progress", None)
        if callable(callback):
            callback(stage, operation=step["operation"])
    def result(passed, code, message):
        proof = state if isinstance(state, dict) else {}
        engine.dispatched = proof.get("input_attempted") is True and proof.get("input_dispatched") is not False
        answer = engine._result("verified" if passed else "needs_review", code, message)
        answer.update(operation=step["operation"], verification_deferred=False,
            verification_scope="changed_uia_postconditions", automatic_replay=False,
            input_dispatched=proof.get("input_dispatched", False),
            input_attempted=proof.get("input_attempted", False),
            input_replayed=False, result_reobserved=verify_only)
        answer["delivery"]["requested_mode"] = "read_only" if verify_only else "foreground"
        answer["delivery"]["acknowledgement"] = "no_input_replayed" if verify_only else "see_input_result"
        if action_result is not None:
            answer["input_result"] = {key: copy.deepcopy(action_result[key]) for key in
                ("status", "input_dispatched", "diagnostic") if key in action_result}
        return answer
    try:
        identity = _identity(runtime, target)
        if verify_only:
            state = validate_image_verification(state)
            if state["expect_hash"] != _hash(checks) or state["identity_hash"] != identity or len(state["baseline_hashes"]) != len(checks):
                raise OperationError("동작 전의 대상 또는 완료 조건이 현재와 다릅니다. 입력을 재실행하지 않았습니다.", "image_baseline_target_changed")
            if any(check.get("require_change") and state["baseline_hashes"][i] is None for i, check in enumerate(checks)):
                raise OperationError("동작 전의 변화 기준값이 없습니다.", "image_baseline_required")
            if not state["input_attempted"] or state["input_dispatched"] is False:
                raise OperationError("이 단계의 입력을 보냈다는 기록이 없습니다. 자동 재실행하지 않았습니다.", "image_input_not_dispatched")
        else:
            report("verifying")
            snapshot = engine._observe_assertions(checks)
            hashes = [_actual(snapshot, check)[1] if check.get("require_change") else None for check in checks]
            state = {"expect_hash": _hash(checks), "identity_hash": identity, "baseline_hashes": hashes,
                "change_seen": [False] * len(checks), "input_attempted": True, "input_dispatched": None}
            # Persist before dispatch. A crash at/after this boundary can only re-observe.
            persist()
            engine._remaining()
            if _identity(runtime, target) != identity:
                raise OperationError("입력 전에 대상 창의 식별 정보가 바뀌었습니다.", "image_baseline_target_changed")
            raw_step = {key: value for key, value in step.items() if key not in
                {"expect", "verification_timeout_ms", "poll_interval_ms", "step_timeout_ms"}}
            report("running_step")
            began = time.monotonic()
            before_count = getattr(getattr(runtime, "guard", None), "action_count", None)
            try:
                action_result = runtime.image_action(raw_step, copy.deepcopy(target))
            finally:
                engine.metrics["action_ms"] = round((time.monotonic()-began)*1000, 2)
                after_count = getattr(getattr(runtime, "guard", None), "action_count", None)
                engine.metrics["mutations"] = max(0, after_count-before_count) if type(before_count) is int and type(after_count) is int else 1
            if not isinstance(action_result, dict):
                raise OperationError("이미지 입력 결과의 형식을 확인하지 못했습니다.", "image_input_unconfirmed")
            state["input_dispatched"] = action_result.get("input_dispatched") if type(action_result.get("input_dispatched")) is bool else None
            persist()
            engine._remaining()
            if state["input_dispatched"] is not True:
                return result(False, "image_input_unconfirmed", "입력 전달을 확인하지 못했습니다. 같은 입력을 반복하지 않았습니다.")
            if action_result.get("verification_deferred") is not True:
                return result(False, "image_input_unconfirmed", "입력 중 오류가 보고되었습니다. 이어가기는 완료 조건만 다시 검사하며 입력은 반복하지 않습니다.")
        report("verifying")
        timeout = 0 if verify_only else step.get("verification_timeout_ms", 10000)
        deadline = min(engine.deadline, time.monotonic()+timeout/1000)
        while True:
            engine._remaining()
            if _identity(runtime, target) != identity:
                raise OperationError("완료 확인 중 대상 창의 식별 정보가 바뀌었습니다.", "image_baseline_target_changed")
            snapshot = engine._observe_assertions(checks)
            if timeout and time.monotonic() >= deadline:
                raise OperationError("제한 시간 뒤 도착한 관찰을 완료로 판단하지 않았습니다.", "verification_timeout")
            engine.checks = []
            previous_changes = list(state["change_seen"])
            for index, check in enumerate(checks):
                observed = {**copy.deepcopy(check), "passed": False}
                try:
                    value, fingerprint = _actual(snapshot, check)
                    observed["observed"] = value
                    observed["passed"] = type(value) is type(check["equals"]) and value == check["equals"]
                    if check.get("require_change"):
                        state["change_seen"][index] |= fingerprint != state["baseline_hashes"][index]
                        observed["change_observed"] = state["change_seen"][index]
                        observed["passed"] &= state["change_seen"][index]
                        if not state["change_seen"][index]: observed["reason"] = "change_not_observed"
                except OperationError as error:
                    if error.code in {"ambiguous_selector", "ambiguous_scope"}: raise
                    observed["reason"] = error.code
                engine.checks.append(observed)
            if previous_changes != state["change_seen"]:
                persist()
            if all(check["passed"] for check in engine.checks):
                engine._remaining()
                if _identity(runtime, target) != identity:
                    raise OperationError("완료 확인 뒤 대상 창이 바뀌었습니다.", "image_baseline_target_changed")
                engine._remaining()
                if timeout and time.monotonic() >= deadline:
                    raise OperationError("마지막 창 확인이 제한 시간을 넘었습니다.", "verification_timeout")
                return result(True, "image_postconditions_verified", "새 관찰에서 동작 전후 변화와 모든 완료 조건을 확인했습니다.")
            if not timeout or time.monotonic() >= deadline:
                return result(False, "image_postconditions_unverified", "입력 후 변화 또는 완료 조건을 확인하지 못했습니다. 입력을 반복하지 않았습니다.")
            report("waiting")
            runtime.stop_event.wait(min(step.get("poll_interval_ms", 150)/1000, max(0, deadline-time.monotonic())))
    except Exception as error:
        return result(False, getattr(error, "code", "image_verification_interrupted"), str(error)[:1000])
