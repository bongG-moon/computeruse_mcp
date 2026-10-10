"""Read-only image replay readiness shared by recording and execution.

An application's DPI-awareness flag alone neither proves nor disproves a safe
pixel-to-input mapping. Legacy apps require a measured 1:1 coordinate contract;
all actual inputs still capture and validate again immediately before dispatch.
"""
from __future__ import annotations

import copy
import time

from operations import OperationError


def validate_geometry(geometry):
    if (not isinstance(geometry, tuple) or len(geometry) != 5
            or any(type(value) is not int for value in geometry)
            or geometry[2] <= geometry[0] or geometry[3] <= geometry[1]
            or geometry[4] not in {0, 1, 2}):
        raise OperationError("현재 창의 실제 크기와 DPI 정보를 확인하지 못했습니다.", "image_geometry_unavailable")
    return geometry


def input_coordinate_contract(geometry, proof=None, screenshot_size=None):
    geometry = validate_geometry(geometry)
    if geometry[4] in {1, 2}:
        return {"mode": "dpi_aware", "awareness": geometry[4]}
    physical = list(geometry[:4])
    if (not isinstance(proof, dict) or proof.get("physical_bounds") != physical
            or proof.get("logical_bounds") != physical or proof.get("monitor_scale_percent") != 100
            or proof.get("single_monitor") is not True):
        raise OperationError("이 프로그램은 DPI 비인식 방식이며 현재 화면의 캡처·입력 좌표가 1:1인지 확인되지 않았습니다. "
            "100% 배율의 한 모니터 안에서 다시 확인하거나 요소 방식으로 기록하세요. 설정이나 레지스트리는 바꾸지 않았습니다.",
            "image_dpi_unsupported")
    expected = (geometry[2]-geometry[0], geometry[3]-geometry[1])
    if screenshot_size is not None and screenshot_size != expected:
        raise OperationError("실제 캡처 크기와 프로그램의 입력 좌표 크기가 다릅니다. 입력하지 않았습니다.", "image_coordinate_mismatch")
    return {"mode": "legacy_verified_1to1", "awareness": 0, "monitor_scale_percent": 100,
            "capture_size_verified": screenshot_size is not None}


def image_replay_preflight(runtime, target, *, capture=True):
    """Inspect readiness without focusing, clicking, or changing app settings.

The UI may call capture=False while it is in front. A ready result in that mode
means geometry is supported; capture_verified remains false and the actual
record/replay capture must still be checked. A non-foreground capture request is
reported as needs_foreground, never as an unsupported application.
"""
    from image_targets import png_dimensions
    from vendor.guard import Guard, _allowed_pid, validate_arguments
    answer = {"status": "blocked", "ready_for_input": False, "ready_for_capture": False,
              "capture_verified": False, "input_dispatched": False, "read_only": True}
    try:
        runtime.check_active()
        guard = runtime.guard
        policy = copy.deepcopy(guard.policy)
        policy.update(mode="visual", log_detail="metadata")
        validate_arguments("get_window_state", target, policy, guard.process_resolver, guard.window_resolver)
        executable = _allowed_pid(target["pid"], guard.policy, guard.process_resolver)
        geometry = validate_geometry(guard.image_geometry_resolver(target["window_id"]))
        answer["ready_for_capture"] = True
        proof = guard.image_coordinate_resolver(target["window_id"]) if geometry[4] == 0 else None
        contract = input_coordinate_contract(geometry, proof)
        answer.update(coordinate_contract=contract, ready_for_capture=True)
        if not capture:
            return {**answer, "status": "ready", "ready_for_input": True,
                    "diagnostic": {"code": "image_geometry_ready", "message": "좌표 조건을 확인했습니다. 재생 전 실제 캡처를 다시 검사합니다."}}
        if guard.checkpoint_ready_resolver(target["window_id"]) is not True:
            return {**answer, "status": "needs_foreground", "diagnostic": {
                "code": "image_requires_foreground", "message": "녹화할 프로그램을 앞으로 가져오면 캡처 확인을 계속할 수 있습니다."}}
        visual = Guard(policy, transport=guard.transport, process_resolver=guard.process_resolver,
            window_resolver=guard.window_resolver, checkpoint_ready_resolver=guard.checkpoint_ready_resolver,
            image_geometry_resolver=guard.image_geometry_resolver, image_coordinate_resolver=guard.image_coordinate_resolver)
        captured = visual.call("get_window_state", target)
        runtime.check_active()
        images = [row for row in captured.get("content", []) if row.get("type") == "image"]
        if captured.get("isError") or len(images) != 1 or images[0].get("mimeType") != "image/png":
            raise OperationError("현재 창의 캡처를 확인하지 못했습니다.", "image_capture_failed")
        size = png_dimensions(images[0].get("data"))
        metadata = captured.get("structuredContent", {})
        if (any(metadata.get(key) != value for key, value in target.items())
                or (metadata.get("screenshot_width"), metadata.get("screenshot_height")) != size):
            raise OperationError("Driver 캡처의 대상·크기가 실제 PNG와 다릅니다.", "image_coordinate_mismatch")
        if (guard.window_resolver(target["window_id"]) != target["pid"]
                or _allowed_pid(target["pid"], guard.policy, guard.process_resolver) != executable
                or guard.image_geometry_resolver(target["window_id"]) != geometry
                or guard.checkpoint_ready_resolver(target["window_id"]) is not True
                or (geometry[4] == 0 and guard.image_coordinate_resolver(target["window_id"]) != proof)):
            raise OperationError("확인 중 창의 위치·크기 또는 소유 프로그램이 달라졌습니다.", "image_target_changed")
        contract = input_coordinate_contract(geometry, proof, size)
        return {**answer, "status": "ready", "ready_for_input": True, "capture_verified": True,
                "coordinate_contract": contract, "screenshot_size": {"width": size[0], "height": size[1]},
                "diagnostic": {"code": "image_replay_ready", "message": "현재 창의 캡처와 재생 좌표를 확인했습니다."}}
    except (ValueError, OSError, RuntimeError, AttributeError) as error:
        return {**answer, "diagnostic": {"code": getattr(error, "code", "image_preflight_unavailable"),
                "message": str(error)[:1000]}}


def prepare_image_foreground(runtime, target):
    """One explicitly requested activation before any image/business input.

    Returns None when ready (or a lightweight runtime has no native readiness
    provider). Failure positively proves that the image input was not called.
    """
    ready = getattr(runtime.guard, "checkpoint_ready_resolver", None)
    if not callable(ready):
        return None
    focused = False
    try:
        runtime.check_active()
        if ready(target["window_id"]) is True:
            return None
        observed = runtime.call("get_window_state", {**target, "max_depth": 1, "max_elements": 50})
        if observed.get("isError"):
            raise OperationError("대상 창을 확인하지 못해 앞으로 가져오지 않았습니다.", "image_target_unavailable")
        focused = True
        answer = runtime.call("bring_to_front", dict(target))
        if answer.get("isError"):
            raise OperationError("대상 창을 앞으로 가져오지 못했습니다. 작업 표시줄에서 대상 창을 선택한 뒤 이어가세요.", "image_requires_foreground")
        deadline = time.monotonic() + 1.5
        while True:
            runtime.check_active()
            if ready(target["window_id"]) is True:
                return None
            if time.monotonic() >= deadline:
                raise OperationError("대상 창이 앞으로 표시되지 않았습니다. 해당 창을 선택한 뒤 이어가세요.", "image_requires_foreground")
            runtime.stop_event.wait(min(.05, max(0, deadline-time.monotonic())))
    except (ValueError, OSError, RuntimeError) as error:
        return {"status": "needs_review", "task_verified": False, "input_dispatched": False,
                "focus_dispatched": focused, "diagnostic": {"code": getattr(error, "code", "image_preparation_failed"),
                    "message": str(error)[:1000], "automatic_replay": False}}
