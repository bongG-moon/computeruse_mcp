"""Read-only timing, element waits, and explicit screenshot review checkpoints."""
from __future__ import annotations

import copy
import math
import re
import time

from operations import Operations, OperationError, _elements, _limited, _payload, _unique, validate_selector, validate_assertions


PROCESS_OPERATIONS = {"delay", "wait_for_element", "wait_for_state", "checkpoint"}


def validate_process_step(step):
    if not isinstance(step, dict) or not isinstance(step.get("operation"), str) or step["operation"] not in PROCESS_OPERATIONS:
        raise OperationError("지원하지 않는 프로세스 단계입니다.")
    operation = step["operation"]
    allowed = {"operation"} | ({"duration_ms"} if operation == "delay" else
        {"selector", "timeout_ms", "poll_interval_ms"} if operation == "wait_for_element" else
        {"expect", "timeout_ms", "poll_interval_ms"} if operation == "wait_for_state" else {"message", "return_from", "opened_from"})
    if set(step) - allowed:
        raise OperationError("이 프로세스 단계에 지원하지 않는 설정이 있습니다.")
    if operation == "delay":
        if type(step.get("duration_ms")) is not int or not 0 <= step["duration_ms"] <= 60000:
            raise OperationError("고정 대기 시간 duration_ms는 0~60000ms로 지정하세요.")
    elif operation in ("wait_for_element", "wait_for_state"):
        if operation == "wait_for_element":
            validate_selector(step.get("selector"))
        else:
            validate_assertions(step.get("expect"))
        if type(step.get("timeout_ms")) is not int or not 0 <= step["timeout_ms"] <= 60000:
            raise OperationError("요소 대기 제한 시간 timeout_ms는 0~60000ms로 지정하세요.")
        interval = step.get("poll_interval_ms", 250)
        if type(interval) is not int or not 100 <= interval <= 2000:
            raise OperationError("요소 확인 간격 poll_interval_ms는 100~2000ms로 지정하세요.")
    else:
        if "return_from" in step and "opened_from" in step:
            raise OperationError("화면 확인은 팝업 열기와 원래 창 복귀 중 하나만 지정하세요.")
        for key in ("return_from", "opened_from"):
            if key in step and (not isinstance(step[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", step[key])):
                raise OperationError("화면 확인에서 연결할 원래 창 또는 팝업 창 이름을 확인하세요.")
        message = step.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 2000 or "\x00" in message:
            raise OperationError("화면 확인 단계에는 1~2000자의 확인 내용 message가 필요합니다.")
        try:
            message.encode("utf-8")
        except UnicodeError:
            raise OperationError("화면 확인 설명에 저장할 수 없는 문자가 있습니다.") from None
    return copy.deepcopy(step)


def _pause(runtime, seconds):
    runtime.check_active()
    event = getattr(runtime, "stop_event", None)
    if event is not None:
        event.wait(max(0, seconds))
    else:
        time.sleep(min(.1, max(0, seconds)))
    runtime.check_active()


def execute_process_step(runtime, step, target):
    """Never dispatches input. Screenshot success deliberately remains unverified."""
    step = validate_process_step(step)
    if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
            or any(type(value) is not int or value < 1 for value in target.values())):
        raise OperationError("현재 승인한 창의 pid/window_id가 필요합니다.", "invalid_target")
    if runtime.mode != "uia":
        raise OperationError("프로세스 단계는 UIA 세션에서 실행하세요.", "unsupported_mode")
    runtime.check_active()
    started, observations = time.monotonic(), 0
    operation = step["operation"]
    def report(stage):
        callback = getattr(runtime, "report_progress", None)
        if callable(callback): callback(stage)
    if operation == "wait_for_state":
        report("waiting")
        return Operations(runtime).wait_for_state(step["expect"], target,
            timeout_ms=step["timeout_ms"], poll_interval_ms=step.get("poll_interval_ms", 250))
    def result(passed, code, message, **extra):
        return {"status": "verified" if passed else "needs_review", "operation": operation,
                "task_verified": passed, "input_dispatched": False,
                "diagnostic": {"code": code, "message": message, "automatic_replay": False},
                "metrics": {"elapsed_ms": round((time.monotonic()-started)*1000, 2), "observations": observations}, **extra}
    if operation == "delay":
        report("waiting")
        deadline = started + step["duration_ms"]/1000
        while time.monotonic() < deadline:
            _pause(runtime, min(.1, deadline-time.monotonic()))
        runtime.check_active()
        return result(True, "delay_completed", "지정한 고정 대기를 완료했습니다. 화면 변경을 확인한 것은 아닙니다.")
    if operation == "checkpoint":
        report("observing")
        capture = getattr(runtime, "capture_checkpoint", None)
        if not callable(capture):
            return result(False, "checkpoint_unavailable", "이 실행 환경에서 명시적 화면 확인을 지원하지 않습니다.")
        answer = capture(copy.deepcopy(target))
        runtime.check_active()
        if not isinstance(answer, dict) or answer.get("isError"):
            if (isinstance(answer, dict) and isinstance(answer.get("structuredContent"), dict)
                    and answer["structuredContent"].get("error_code") == "checkpoint_requires_foreground"):
                return result(False, "checkpoint_requires_foreground", "대상 창을 최소화하지 않은 상태로 맨 앞으로 가져온 뒤, 승인 ID 없이 이어가기로 화면을 다시 확인하세요. 자동으로 창을 전환하지 않았습니다.")
            return result(False, "checkpoint_capture_failed", "확인할 화면을 캡처하지 못했습니다. 다음 단계는 실행하지 않았습니다.")
        metadata = answer.get("structuredContent", {})
        if isinstance(metadata, dict) and any(key in metadata and metadata[key] != value for key, value in target.items()):
            return result(False, "target_mismatch", "캡처한 창이 요청한 창과 다릅니다.")
        images = [item for item in answer.get("content", []) if isinstance(item, dict) and item.get("type") == "image"]
        if (not 1 <= len(images) <= 2 or any(not isinstance(item.get("data"), str) or not item["data"]
                or len(item["data"]) > 16*1024*1024 or item.get("mimeType") not in {"image/png", "image/jpeg", "image/webp"} for item in images)):
            return result(False, "checkpoint_image_missing", "확인 가능한 화면 이미지가 반환되지 않았습니다.")
        report("needs_review")
        return result(False, "checkpoint_review_required", "화면 이미지를 확인한 뒤 이 체크포인트를 명시적으로 승인해야 이어갑니다.",
                      checkpoint_content=[{key: copy.deepcopy(image[key]) for key in ("type", "data", "mimeType")} for image in images],
                      checkpoint_ready=True, image_verified=False)
    deadline = started + step["timeout_ms"]/1000
    read_deadline = started + max(30000, step["timeout_ms"])/1000
    while True:
        runtime.check_active()
        if observations and time.monotonic() >= deadline:
            return result(False, "element_wait_timeout", "제한 시간 안에 지정한 요소가 나타나지 않았습니다.")
        args = {**target, "include_accessibility_tree": True, "include_screenshot": False,
                "max_depth": 32, "max_elements": 5000}
        bounded = getattr(runtime, "call_with_timeout", None)
        # Do not turn a short polling wait into a Driver connection timeout.
        # The current read finishes within the whole wait's bounded read budget;
        # a result arriving after the polling deadline is still rejected below.
        remaining = max(1, math.floor((read_deadline-time.monotonic())*1000+1e-6))
        answer = bounded("get_window_state", args, timeout_ms=remaining) if callable(bounded) else runtime.call("get_window_state", args)
        observations += 1
        runtime.check_active()
        if step["timeout_ms"] and time.monotonic() >= deadline:
            return result(False, "element_wait_timeout", "제한 시간이 지난 뒤 도착한 관찰을 완료로 판정하지 않았습니다.")
        if not isinstance(answer, dict) or answer.get("isError"):
            return result(False, "observation_failed", "화면 읽기에 실패했습니다. 접근 오류를 요소 부재로 간주하거나 반복하지 않습니다.")
        try:
            snapshot = _payload(answer)
            if any(snapshot.get(key) != value for key, value in target.items()):
                return result(False, "target_mismatch", "관찰된 창이 요청한 창과 다릅니다.")
            if _limited(snapshot, 32, 5000):
                return result(False, "incomplete_observation", "화면 일부만 읽어 요소의 존재와 고유성을 확인하지 못했습니다.")
            _elements(snapshot)
            _unique(snapshot, step["selector"])
            return result(True, "element_appeared", "지정한 요소를 현재 화면에서 하나로 확인했습니다.")
        except OperationError as error:
            if error.code != "selector_not_found":
                return result(False, error.code, str(error))
        if time.monotonic() >= deadline:
            return result(False, "element_wait_timeout", "제한 시간 안에 지정한 요소가 나타나지 않았습니다.")
        report("waiting")
        _pause(runtime, min(step.get("poll_interval_ms", 250)/1000, deadline-time.monotonic()))
