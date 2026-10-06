"""Explicit image recipe steps; images are evidence of location, never success."""
from __future__ import annotations

import copy
import time

from image_targets import validate_image_target
from operations import OperationError

IMAGE_MUTATIONS = {"image_click", "image_double_click", "image_right_click", "image_type_text", "image_press_key", "image_hotkey", "image_scroll"}
IMAGE_OPERATIONS = IMAGE_MUTATIONS | {"wait_for_image"}


def validate_image_step(step):
    if not isinstance(step, dict) or not isinstance(step.get("operation"), str) or step["operation"] not in IMAGE_OPERATIONS:
        raise OperationError("지원하지 않는 이미지 동작입니다.")
    kind = step["operation"]
    extra = {"image_type_text": {"value", "replace_all"}, "image_press_key": {"key"}, "image_hotkey": {"keys"},
             "image_scroll": {"direction", "amount"}, "wait_for_image": {"timeout_ms", "poll_interval_ms"}}.get(kind, set())
    if set(step) - ({"operation", "image_target"} | extra):
        raise OperationError("이미지 동작에 지원하지 않는 인자가 있습니다.")
    validate_image_target(step.get("image_target"))
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


def execute_image_step(runtime, step, target):
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
    deadline = time.monotonic()+step.get("timeout_ms", 0)/1000
    while True:
        runtime.check_active()
        result = action(step, copy.deepcopy(target))
        runtime.check_active()
        if step["operation"] != "wait_for_image" or result.get("diagnostic", {}).get("code") != "image_not_found":
            return result
        if time.monotonic() >= deadline:
            return {**result, "diagnostic": {"code": "image_wait_timeout", "automatic_replay": False,
                                            "message": "제한 시간 안에 이미지가 나타나지 않았습니다."}}
        runtime.stop_event.wait(min(step.get("poll_interval_ms", 500)/1000, max(0, deadline-time.monotonic())))
