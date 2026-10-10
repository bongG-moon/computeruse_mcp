"""Human-authored declarative processes, using the same guarded picker and task store."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from inspection import suggested_operations
from learning import ElementLibrary, stable_selector, _text
from learning_picker import _run_helper, _helper_visible, _end_helper, match_picked_element
from operations import OperationError, _name, _unique
from teaching_sessions import _Cancellation
from workflows import validate_recipe, WINDOW_REF
from recording_windows import validate_window_wait


HELPER_NAME = "Computer Use MCP 프로세스 만들기.exe"
VISUAL_HELPER_NAME = "Computer Use MCP 이미지 도구.exe"
IMAGE_ACTIONS = {"click": "image_click", "double_click": "image_double_click", "right_click": "image_right_click",
                 "set_value": "image_type_text", "press_key": "image_press_key", "hotkey": "image_hotkey",
                 "scroll": "image_scroll", "wait_for_element": "wait_for_image"}
IMAGE_FALLBACK_ERRORS = {"picker_not_found", "picker_controls_not_exposed"}


def _recognition_fallback(error):
    """Explain the native UIA / Driver distinction without exposing app data.

    A native candidate can be a Button and still be absent from the Driver's
    actionable projection. Do not tell the user it had no button information,
    and retain the bounded matching evidence for diagnosing that difference.
    """
    source = getattr(error, "picker_diagnostic", {})
    source = source if isinstance(source, dict) else {}
    reason = source.get("reason")
    explanations = {
        "no_controls_projected": "이 화면에서 자동 조작할 요소 목록을 받지 못했습니다.",
        "empty_projection_unconfirmed": "현재 화면의 요소 읽기 결과가 비어 있습니다.",
        "role_not_projected": "선택 창은 요소 유형을 인식했지만, 자동 조작 목록에는 같은 유형이 없습니다.",
        "geometry_mismatch": "선택한 요소와 현재 조작 대상의 위치가 달라 같은 대상인지 확인하지 못했습니다.",
        "missing_driver_id_without_matching_geometry": "요소 유형은 인식했지만 조작 목록의 식별 정보가 부족해 같은 대상인지 확인하지 못했습니다.",
        "identity_not_projected": "선택 창은 요소 유형을 인식했지만, 같은 요소를 자동 조작 대상으로 연결하지 못했습니다.",
    }
    explanation = explanations.get(reason, "선택한 요소를 자동 조작 대상으로 연결하지 못했습니다.")
    if error.code == "picker_controls_not_exposed" and reason not in explanations:
        explanation = "이 화면은 버튼 대신 화면 영역만 요소 정보로 제공합니다."
    diagnostic = {"code": error.code, "reason": reason if reason in explanations else "unmatched_projection"}
    # These are bounded counts / booleans only; selected text, values, window
    # handles and screen coordinates never enter the normal status response.
    for key in ("projected_element_count", "role_candidates", "name_role_candidates", "exact_identity_candidates",
                "matched_candidates", "geometry_rejected", "missing_id_name_candidates"):
        if type(source.get(key)) is int and 0 <= source[key] <= 100000:
            diagnostic[key] = source[key]
    for key in ("native_has_automation_id", "native_has_name", "window_translation_applied"):
        if type(source.get(key)) is bool:
            diagnostic[key] = source[key]
    return explanation, diagnostic


RECORDING_WARNINGS = {
    "outside_target_not_recorded": "다른 프로그램이나 소유 관계가 확인되지 않은 창의 동작은 기록되지 않았습니다.",
    "recording_window_ambiguous": "같은 제목·종류의 팝업이 여러 개라 해당 창의 동작은 기록하지 않았습니다.",
    "recording_window_limit": "추가 팝업 5개 한도에 도달해 이후 창은 기록하지 않았습니다.",
    "uia_text_unavailable": "UIA 응답 지연으로 일부 입력값을 자동 기록하지 못했습니다.",
    "uia_probe_unavailable": "시작 점검에서 UIA 응답을 확인하지 못해 이미지·수동 기록을 사용했습니다.",
    "recording_timeout_partial": "제한 시간이 끝나 그때까지의 동작만 기록했습니다.",
    "recording_hook_unavailable": "입력 후크를 설치하지 못했습니다.",
    "recording_mouse_failed": "마우스 기록 오류 이후의 동작은 기록되지 않았습니다.",
    "recording_keyboard_failed": "키보드 기록 오류 이후의 동작은 기록되지 않았습니다.",
    "recording_progress_failed": "녹화 상태 전달 오류로 기록을 중지했습니다.",
    "semantic_target_unverified": "일부 요소·최종값을 Driver와 대조하지 못했습니다. 해당 단계를 직접 확인하세요.",
    "recording_event_limit": "기록 가능한 동작 수에 도달하여 이후 동작은 기록되지 않았습니다.",
    "recording_final_value_unconfirmed": "마지막 입력의 최종값을 확정하지 못했습니다.",
    "recording_method_unverified": "콤보 상자의 실제 입력·확정 방법을 확인하지 못했습니다. 자동 선택으로 대신하지 않고 직접 설정 단계로 남겼습니다."}
ACTION_LABELS = {"click": "클릭", "double_click": "두 번 클릭", "right_click": "오른쪽 클릭",
    "set_value": "글자 입력", "press_key": "키 누르기", "hotkey": "단축키", "select_option": "목록 선택",
    "set_checked": "체크 변경", "wait_for_element": "요소가 나타날 때까지 대기", "delay": "고정 대기",
    "checkpoint": "화면 이미지 확인", "scroll": "스크롤"}
ACTION_LABELS["wait_for_state"] = "완료 조건이 될 때까지 대기"
ACTION_LABELS["wait_for_window"] = "관련 팝업 연결"
ACTION_LABELS.update({mapped: ACTION_LABELS.get(action, "스크롤") + " (이미지)" for action, mapped in IMAGE_ACTIONS.items()})
ACTION_LABELS["manual_entry"] = "기록 내용 확인 필요"
MANUAL_REASONS = {"unknown_input": "입력할 내용을 직접 지정하세요.",
    "protected_input": "보호된 입력칸의 내용은 기록하지 않습니다. 이 단계를 삭제하고 해당 입력은 직접 처리하세요.",
    "existing_text_requires_review": "기존 글자가 있는 입력칸입니다. 실행할 때 넣을 내용을 직접 지정하세요.",
    "shortcut_requires_manual_setup": "이 단축키는 자동 기록하지 않았습니다. 이 단계를 삭제하고 키보드 동작을 직접 추가하세요.",
    "drag_requires_manual_setup": "드래그는 자동 기록하지 않습니다. 이 단계를 삭제하고 지원하는 동작으로 다시 구성하세요.",
    "image_capture_required": "대상 이미지를 확보하지 못했습니다. 이 단계를 삭제하고 이미지 선택으로 동작을 직접 추가하세요.",
    "focus_target_requires_selection": "입력 위치가 바뀌어 대상을 확인하지 못했습니다. 이 단계를 삭제하고 입력 대상을 직접 선택하세요.",
    "image_occluded_requires_selection": "대상이 다른 창에 가려져 기록하지 못했습니다. 이 단계를 삭제하고 가림을 해제한 뒤 직접 추가하세요.",
    "template_low_detail": "구분할 수 있는 이미지가 부족합니다. 이 단계를 삭제하고 주변 문구를 포함해 이미지를 직접 선택하세요."}
MANUAL_REASONS["semantic_target_unverified"] = "기록한 요소나 최종값을 재확인하지 못했습니다. 이 단계를 삭제하고 요소와 동작을 직접 추가하세요."
MANUAL_REASONS["recording_method_unverified"] = "콤보 입력과 확정 방법을 확인하지 못했습니다. 이 단계를 삭제하고 검증한 입력·확정 동작을 직접 추가하세요."
MANUAL_REASONS["image_replay_unavailable"] = "이 창의 이미지 재생 좌표를 확인하지 못했습니다. ‘이 단계만 다시 지정’에서 요소를 선택하거나 창의 배율·위치를 확인하세요."
for _repair_reason in ("image_capture_required", "focus_target_requires_selection", "image_occluded_requires_selection", "template_low_detail", "semantic_target_unverified"):
    MANUAL_REASONS[_repair_reason] = MANUAL_REASONS[_repair_reason].split("이 단계를 삭제하고", 1)[0] + "‘이 단계만 다시 지정’을 눌러 대상을 선택하세요. 다른 단계와 팝업 연결은 유지됩니다."


def task_view(task):
    """Image templates stay in the local store; normal MCP text contains metadata only."""
    value = copy.deepcopy(task)
    def redact(item):
        if isinstance(item, dict):
            if "template_png" in item:
                item.pop("template_png")
                item["template_stored_locally"] = True
            for child in item.values(): redact(child)
        elif isinstance(item, list):
            for child in item: redact(child)
    redact(value)
    return value


def _leaf_steps(steps):
    for step in steps:
        if step.get("operation") == "foreach":
            yield from _leaf_steps(step.get("steps", []))
        else:
            yield step


class ProcessDraft:
    """No executable code or transient window/element handles can enter a recipe."""
    def __init__(self, programs, task=None):
        if task and task.get("id"):
            from task_revision import stable_task
            task = stable_task(task)
        self.programs = copy.deepcopy(programs)
        self.targets = {(p["program_id"], p.get("window_ref", "main")): p for p in programs}
        self.selections = {}
        self.steps = copy.deepcopy((task or {}).get("steps", []))
        self.variables = copy.deepcopy((task or {}).get("variables", {}))
        self.task_id = (task or {}).get("id")
        self.revision = (task or {}).get("revision", 1)
        self.labels = [step.get("selector", {}).get("name", "저장한 이미지" if "image_target" in step else "저장된 요소") for step in self.steps]
        review = (task or {}).get("recording_review", {})
        self.recording_warnings = list(review.get("warning_codes", []))
        self.recording_acknowledged = bool(review.get("acknowledged", False)) if self.recording_warnings else True
        # Session-local provenance. Only an explicit repair of each associated
        # target can resolve these warnings; an inherited warning has no such
        # evidence and still needs the ordinary review path.
        self._target_warning_steps = set()
        self._persistent_semantic_warning = "semantic_target_unverified" in self.recording_warnings
        if self.steps:
            validate_recipe(self.steps, self.variables, list({p["program_id"] for p in programs}))
            for step in _leaf_steps(self.steps):
                if step["operation"] == "wait_for_window":
                    key = (step["program_id"], step["window_ref"])
                    owner = self.targets.get((step["program_id"], step["owner_ref"]))
                    if key not in self.targets and owner is not None:
                        target = {"program_id": step["program_id"], "window_ref": step["window_ref"], "pid": owner["pid"],
                                  "window_id": 0, "label": (step["title"] or step["class_name"]) + " · 실행 시 팝업 연결"}
                        self.targets[key] = target
                        self.programs.append(target)
            if any((s["program_id"], s.get("window_ref", "main")) not in self.targets for s in _leaf_steps(self.steps)):
                raise OperationError("기존 프로세스의 프로그램과 창을 모두 연결하세요.", "missing_target")

    def target(self, payload):
        key = (payload.get("program_id"), payload.get("window_ref", "main"))
        target = self.targets.get(key)
        if target is None or target["window_id"] <= 0 or any(payload.get(k) != target[k] for k in ("pid", "window_id")):
            raise OperationError("편집 창에 연결한 정확한 프로그램과 창을 선택하세요.", "target_mismatch")
        return target

    def selection(self, selection_id, target):
        item = self.selections.get(selection_id) if isinstance(selection_id, str) else None
        if item is None or item["target"] != (target["program_id"], target.get("window_ref", "main")):
            raise OperationError("먼저 이 프로그램의 요소를 선택하세요.", "selection_required")
        return item

    def remember(self, target, snapshot, native):
        if native.get("human_confirmed") is not True:
            raise OperationError("선택 창에서 요소를 확정해야 합니다.", "picker_confirmation_required")
        matched = match_picked_element(snapshot, native, {k: target[k] for k in ("pid", "window_id")})
        element = _unique(snapshot, matched["expected_selector"])
        selector = stable_selector(snapshot, element)
        identity = uuid.uuid4().hex
        view = {"selection_id": identity, "label": _name(element) or selector.get("automation_id", "선택 요소"),
                "role": element["role"], "recognition": "uia", "actions": suggested_operations(element)}
        self.selections[identity] = {**view, "selector": selector,
                                    "target": (target["program_id"], target.get("window_ref", "main"))}
        return copy.deepcopy(view)

    def remember_image(self, target, native):
        from image_targets import validate_image_target
        if native.get("human_confirmed") is not True or any(native.get(k) != target[k] for k in ("pid", "window_id")):
            raise OperationError("지정한 창에서 직접 선택하고 확정한 이미지만 사용할 수 있습니다.", "image_confirmation_required")
        image = validate_image_target({"format": "computer-image-target/v1", **{key: native[key] for key in
            ("template_png", "width", "height", "anchor", "capture_window")}, "min_score": .94, "ambiguity_margin": .03,
            **({"source_size": native["source_size"]} if "source_size" in native else {})})
        identity = uuid.uuid4().hex
        view = {"selection_id": identity, "label": "직접 선택한 이미지", "role": "Image", "recognition": "image",
                "actions": list(IMAGE_ACTIONS), "thumbnail_png": image["template_png"]}
        self.selections[identity] = {**view, "image_target": image,
                                    "target": (target["program_id"], target.get("window_ref", "main"))}
        return copy.deepcopy(view)

    @staticmethod
    def checkpoint(target):
        return {"operation": "checkpoint", "program_id": target["program_id"], "window_ref": target.get("window_ref", "main"),
                "message": "실행한 동작의 결과가 맞는지 화면을 확인한 뒤 계속하세요."}

    def validate(self, steps=None, *, allow_pending=False):
        proposed = self.steps if steps is None else steps
        if not allow_pending and self.recording_warnings and not self.recording_acknowledged:
            raise OperationError("녹화 누락·오류 경고를 확인한 뒤 [누락 경고 확인]을 눌러야 저장할 수 있습니다.", "recording_review_required")
        if any(step.get("operation") == "manual_entry" for step in proposed):
            if not allow_pending:
                raise OperationError("녹화 중 확인되지 않은 단계가 있습니다. 빨간색 단계의 안내에 따라 입력 내용을 지정하거나 직접 교체·삭제하세요.", "recording_input_required")
            # Validate all executable steps as one recipe; pending text never reaches TaskStore.
            proposed = [s for s in proposed if s.get("operation") != "manual_entry"]
        if proposed:
            validate_recipe(proposed, self.variables, [p["program_id"] for p in self.programs])
        if len(self.steps if steps is None else steps) > 30:
            raise OperationError("반복 작업은 확인 단계를 포함해 최대 30단계입니다.")

    def recording_review(self):
        return {"partial": bool(self.recording_warnings), "warning_codes": list(self.recording_warnings),
                "acknowledged": self.recording_acknowledged,
                "messages": [RECORDING_WARNINGS.get(code, "녹화 경고를 확인하세요.") for code in self.recording_warnings]}

    def _semantic_step(self, event, target, snapshots, final_values=None, *, historical=False):
        native, after = event.get("native_target"), event.get("after")
        action = event.get("operation")
        button = action == "click" and isinstance(native, dict) and native.get("element", {}).get("role") == "Button"
        if not isinstance(native, dict) or (not button and (not isinstance(after, dict) or set(after) != {"property", "equals"})):
            raise OperationError("녹화한 요소의 관찰 증거가 없습니다.", "recording_semantic_evidence_missing")
        snapshot = snapshots.get((target["program_id"], target.get("window_ref", "main")))
        if not isinstance(snapshot, dict): raise OperationError("녹화 후 Driver 관찰이 없습니다.", "recording_snapshot_missing")
        matched = match_picked_element(snapshot, native, {k: target[k] for k in ("pid", "window_id")})
        element = _unique(snapshot, matched["expected_selector"])
        if action not in suggested_operations(element): raise OperationError("Driver가 해당 요소 동작을 지원하지 않습니다.", "recording_action_unsupported")
        selector = stable_selector(snapshot, element)
        if button:
            return {"operation": "click", "program_id": target["program_id"], "window_ref": target.get("window_ref", "main"),
                    "selector": selector, "completion_mode": "human"}, _name(element) or "녹화한 버튼"
        prop = after.get("property")
        expected = event.get("checked") if action == "set_checked" else event.get("value")
        final_expected = expected
        evidence = event.get("event_evidence", {})
        if self._event_evidence(event):
            # The owned recorder observed this value at the time of the action.
            # Verify the latest value for the SAME runtime identity against the
            # Driver, rather than discarding W when the user later selected N.
            final_expected = (final_values or {}).get((target["program_id"], target.get("window_ref", "main"), evidence["identity"], prop), expected)
            if (historical and action == "set_checked" and self._physical_click(event)
                    and any(other != selector for other in historical)):
                # Another independently verified checkbox click can turn this
                # one off. Its own physical-click + after-input evidence still
                # describes the historical action. Text/ComboBox histories keep
                # the strict same-runtime-identity terminal-value requirement.
                final_expected = element.get(prop)
        if (action not in {"set_value", "select_option", "set_checked"} or prop != ("selected" if action == "set_checked" else "value")
                or type(after["equals"]) is not type(expected) or after["equals"] != expected
                or prop not in element or type(element[prop]) is not type(expected)
                or type(element[prop]) is not type(final_expected) or element[prop] != final_expected):
            raise OperationError("기록한 최종값이 현재 Driver 관찰과 일치하지 않습니다.", "recording_final_value_mismatch")
        step = {"operation": action, "program_id": target["program_id"], "window_ref": target.get("window_ref", "main"),
                "selector": selector, "expect": [{"selector": copy.deepcopy(selector), "property": prop, "equals": expected}]}
        step["checked" if action == "set_checked" else "value"] = expected
        return step, _name(element) or selector.get("automation_id", "녹화한 요소")

    @staticmethod
    def _event_evidence(event):
        proof = event.get("event_evidence")
        return (isinstance(proof, dict) and set(proof) == {"source", "identity", "after_input"}
                and proof["source"] == "uia_recording" and proof["after_input"] is True
                and isinstance(proof["identity"], str) and 0 < len(proof["identity"]) <= 256
                and isinstance(event.get("after"), dict) and set(event["after"]) == {"property", "equals"})

    @staticmethod
    def _physical_click(event):
        proof = event.get("pointer_evidence")
        return (event.get("requested_operation") == "click" and isinstance(proof, dict)
                and set(proof) == {"source", "operation", "before_input"}
                and proof == {"source": "native_mouse_hook", "operation": "click", "before_input": True})

    def recorded(self, events, *, snapshots=None, warning_codes=(), windows=(), review_mode="human"):
        from image_targets import validate_image_target
        if not isinstance(events, list) or not events or len(events) > 30:
            raise OperationError("기록한 동작이 없습니다. [기록 시작] 후 실제로 클릭·입력해야 합니다. 마우스를 올려 요소를 모으는 것은 동작 녹화가 아닙니다." if not events else "기록한 동작이 너무 많습니다.", "recording_invalid_events")
        if not isinstance(warning_codes, (list, tuple)) or any(not isinstance(code, str) or code not in RECORDING_WARNINGS for code in warning_codes):
            raise OperationError("녹화 경고 형식을 확인하지 못했습니다.", "recording_invalid_events")
        warnings = list(dict.fromkeys([*self.recording_warnings, *warning_codes]))
        target_warning_steps = set(self._target_warning_steps)
        persistent_semantic_warning = self._persistent_semantic_warning or "semantic_target_unverified" in warning_codes
        proposed, labels = copy.deepcopy(self.steps), list(self.labels)
        import_diagnostics = []
        discovered = self.recorded_windows(windows)
        targets = {**self.targets, **{key: value[0] for key, value in discovered.items()}}
        connected = {(step["program_id"], step.get("window_ref", "main")) for step in proposed if step["operation"] == "wait_for_window"}
        last_event = {(event.get("program_id"), event.get("window_ref", "main")): index for index, event in enumerate(events) if isinstance(event, dict)}
        closed = {(row["program_id"], row["window_ref"]): row["owner_ref"] for row in windows if row.get("closed") is True}
        final_values, terminal_checks = {}, []
        for index, event in enumerate(events):
            if isinstance(event, dict) and self._event_evidence(event):
                binding = (event.get("program_id"), event.get("window_ref", "main"))
                if binding not in targets or event.get("input_method") == "keyboard_unverified": continue
                try:
                    # Only a real, supported, non-protected current Driver
                    # match can anchor historical values for this identity.
                    verified, _ = self._semantic_step(event, targets[binding], snapshots or {})
                except OperationError:
                    continue
                key = (*binding, event["event_evidence"]["identity"], event["after"]["property"])
                final_values[key] = event["after"]["equals"]
                if event.get("operation") == "set_checked" and self._physical_click(event):
                    terminal_checks.append((index, binding, event["event_evidence"]["identity"], verified["selector"]))
        for event_index, event in enumerate(events):
            if not isinstance(event, dict):
                raise OperationError("녹화 응답의 형식을 확인하지 못했습니다.", "recording_invalid_events")
            binding_key = (event.get("program_id"), event.get("window_ref", "main"))
            target = targets.get(binding_key)
            if target is None:
                raise OperationError("녹화에 연결하지 않은 프로그램의 동작이 포함되어 있습니다.", "target_mismatch")
            if binding_key in discovered and binding_key not in connected:
                binding_step = discovered[binding_key][1]
                owner_key = (target["program_id"], binding_step["owner_ref"])
                opening = (len(proposed) >= 2 and proposed[-1]["operation"] == "checkpoint"
                           and not proposed[-1].get("return_from") and not proposed[-1].get("opened_from")
                           and (proposed[-2]["operation"].startswith("image_") or proposed[-2].get("completion_mode") == "human") and not proposed[-2].get("expect")
                           and all((item["program_id"], item.get("window_ref", "main")) == owner_key for item in proposed[-2:]))
                if opening:
                    # A modal popup owns foreground now. Review that proven
                    # popup, not an inaccessible screenshot of its parent.
                    proposed.pop(); labels.pop()
                proposed.append(binding_step); labels.append(target["label"]); connected.add(binding_key)
                if opening:
                    check = self.checkpoint(target)
                    check.update(opened_from=binding_step["owner_ref"], message="관련 팝업 화면에서 앞선 동작의 결과를 확인한 뒤 계속하세요.")
                    proposed.append(check); labels.append("열린 팝업 확인")
            kind = event.get("operation")
            if kind not in {*IMAGE_ACTIONS, "manual_entry", "select_option", "set_checked"}:
                raise OperationError("지원하지 않는 녹화 동작입니다.", "recording_invalid_events")
            semantic_failed = False
            semantic_error = None
            method_unverified = event.get("input_method") == "keyboard_unverified" or event.get("reason") == "recording_method_unverified"
            if method_unverified and "recording_method_unverified" not in warnings:
                warnings.append("recording_method_unverified")
            if "native_target" in event:
                try:
                    if method_unverified:
                        raise OperationError("녹화한 입력·확정 방법을 확인하지 못했습니다.", "recording_method_unverified")
                    historical_checks = [selector for index, binding, identity, selector in terminal_checks
                        if index > event_index and binding == binding_key and identity != event.get("event_evidence", {}).get("identity")]
                    semantic, label = self._semantic_step(event, target, snapshots or {}, final_values, historical=historical_checks)
                    proposed.append(semantic); labels.append(label)
                    if semantic.get("completion_mode") == "human":
                        if binding_key in closed and event_index == last_event[binding_key]:
                            check = self.checkpoint(targets[(target["program_id"], closed[binding_key])])
                            check["return_from"] = target["window_ref"]
                            check["message"] = "버튼 동작과 원래 창으로 돌아온 결과를 확인하세요."
                        else:
                            check = self.checkpoint(target)
                            check["message"] = "요소 클릭 뒤 업무 결과가 맞는지 확인하세요. 결과 조건을 지정하면 자동 확인으로 바꿀 수 있습니다."
                        proposed.append(check); labels.append("클릭 결과 확인")
                    continue
                except OperationError as error:
                    # A native Button does not prove Driver actionability. Keep
                    # its actual recorded click as an image when available.
                    semantic_failed = kind != "click"
                    semantic_error = getattr(error, "code", "recording_semantic_unavailable")
                    import_diagnostics.append({"event_index": event_index, "recorded_operation": kind,
                        "code": getattr(error, "code", "recording_semantic_unavailable"),
                        "has_image": "template_png" in event, "physical_click_verified": self._physical_click(event)})
                    if event.get("capture_issue") in MANUAL_REASONS:
                        import_diagnostics[-1]["capture_issue"] = event["capture_issue"]
            image = None
            if "template_png" in event:
                image = validate_image_target({"format": "computer-image-target/v1", **{key: event[key] for key in
                    ("template_png", "width", "height", "anchor", "capture_window")}, "min_score": .94, "ambiguity_margin": .03,
                    **({"source_size": event["source_size"]} if "source_size" in event else {})})
            reason = event.get("reason", "unknown_input")
            if (semantic_failed and kind == "set_checked" and image is not None and self._physical_click(event)
                    and not method_unverified and reason not in {"protected_input", "drag_requires_manual_setup"}
                    and semantic_error not in {"protected_control", "protected_element", "target_mismatch", "picker_invalid_response"}):
                # Preserve the physical action actually performed. Do not
                # invent set_checked support from a native TogglePattern.
                kind, semantic_failed = "click", False
                if import_diagnostics: import_diagnostics[-1]["fallback"] = "recorded_image_click"
            semantic_warning_created = semantic_failed or kind in {"select_option", "set_checked"}
            if semantic_warning_created:
                kind, reason = "manual_entry", "semantic_target_unverified"
                if not method_unverified and "semantic_target_unverified" not in warnings: warnings.append("semantic_target_unverified")
                if image is None and event.get("capture_issue") in {"template_low_detail", "image_occluded_requires_selection", "image_capture_required", "image_replay_unavailable"}:
                    reason = event["capture_issue"]
            if method_unverified:
                kind, reason = "manual_entry", "recording_method_unverified"
            if image is None:
                kind = "manual_entry"
                if reason not in {"template_low_detail", "image_occluded_requires_selection", "focus_target_requires_selection", "protected_input", "drag_requires_manual_setup", "shortcut_requires_manual_setup", "semantic_target_unverified", "recording_method_unverified", "image_replay_unavailable"}:
                    reason = "image_capture_required"
            step = {"operation": IMAGE_ACTIONS.get(kind, kind), "program_id": target["program_id"],
                    "window_ref": target.get("window_ref", "main")}
            if image is not None: step["image_target"] = image
            if step["operation"] == "image_type_text": step["replace_all"] = True
            for key in ("value", "key", "keys", "direction", "amount"):
                if key in event: step[key] = copy.deepcopy(event[key])
            if kind == "manual_entry":
                # Never retain unknown typed characters or key sequences.
                step = {key: val for key, val in step.items() if key not in {"value", "key", "keys"}}
                step["manual_reason"] = reason if reason in MANUAL_REASONS else "image_capture_required"
                requested = event.get("requested_operation", event.get("operation"))
                if requested in IMAGE_ACTIONS and requested not in {"set_value", "press_key", "hotkey"}:
                    step["repair_action"] = requested
                if semantic_warning_created and not method_unverified:
                    if semantic_error in IMAGE_FALLBACK_ERRORS and reason not in {"protected_input", "drag_requires_manual_setup"}:
                        step["step_id"] = uuid.uuid4().hex
                        target_warning_steps.add(step["step_id"])
                    else:
                        persistent_semantic_warning = True
            label = "녹화한 이미지" if image is not None else "대상 확인 필요"
            if isinstance(event.get("observed_selection"), str) and len(event["observed_selection"]) <= 2000:
                label += " · 관찰한 선택값: " + event["observed_selection"]
            proposed.append(step); labels.append(label)
            if kind != "manual_entry" and binding_key in closed and event_index == last_event[binding_key]:
                owner = targets[(target["program_id"], closed[binding_key])]
                check = self.checkpoint(owner)
                check["return_from"] = target["window_ref"]
                check["message"] = "팝업에서 수행한 동작과 원래 창으로 돌아온 결과를 확인한 뒤 계속하세요."
                proposed.append(check); labels.append("원래 창으로 복귀 확인")
            else:
                proposed.append(self.checkpoint(target)); labels.append("현재 창")
        if review_mode not in {"human", "visual"}: raise OperationError("화면 확인 방법을 선택하세요.")
        if review_mode == "visual":
            for item in proposed[len(self.steps):]:
                if item.get("operation") == "checkpoint": item["review_mode"] = "visual"
        self.validate(proposed, allow_pending=True)
        self.steps, self.labels = proposed, labels
        for key in connected:
            if key in discovered and key not in self.targets:
                self.targets[key] = discovered[key][0]
                self.programs.append(discovered[key][0])
        self.recording_warnings = warnings
        self.recording_import_diagnostics = import_diagnostics
        self._target_warning_steps = target_warning_steps
        self._persistent_semantic_warning = persistent_semantic_warning
        if warnings: self.recording_acknowledged = False

    def recorded_windows(self, windows):
        if not isinstance(windows, (list, tuple)) or len(windows) > 5:
            raise OperationError("녹화한 팝업 연결 정보를 확인하지 못했습니다.", "recording_invalid_windows")
        found = {}
        for row in windows:
            required = {"program_id", "window_ref", "owner_ref", "pid", "window_id", "owner_window_id", "title", "class_name", "owner_verified"}
            if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {"closed"} or type(row.get("closed", False)) is not bool:
                raise OperationError("녹화한 팝업의 소유 관계 정보가 없습니다.", "recording_invalid_windows")
            key = (row["program_id"], row["window_ref"])
            owner = self.targets.get((row["program_id"], row["owner_ref"]))
            if (key in self.targets or key in found or owner is None or row["owner_verified"] is not True
                    or row["pid"] != owner["pid"] or row["owner_window_id"] != owner["window_id"]
                    or type(row["window_id"]) is not int or row["window_id"] <= 0 or row["window_id"] == owner["window_id"]):
                raise OperationError("승인한 원래 창과 연결되지 않은 팝업입니다.", "recording_invalid_windows")
            step = {key: row[key] for key in ("program_id", "window_ref", "owner_ref", "title", "class_name")}
            step.update(operation="wait_for_window", timeout_ms=5000)
            validate_window_wait(step)
            target = {key: row[key] for key in ("program_id", "window_ref", "pid", "window_id")}
            target["label"] = (row["title"] or row["class_name"]) + " · " + row["window_ref"]
            found[(row["program_id"], row["window_ref"])] = (target, step)
        return found

    def resolve_input(self, payload):
        index = payload.get("index")
        if set(payload) != {"index", "value"} or type(index) is not int or not 0 <= index < len(self.steps):
            raise OperationError("입력을 지정할 단계를 목록에서 선택하세요.")
        if self.steps[index]["operation"] != "manual_entry":
            raise OperationError("입력 내용 확인이 필요한 단계만 여기에서 수정할 수 있습니다.")
        if "image_target" not in self.steps[index] or self.steps[index].get("manual_reason") not in {"unknown_input", "existing_text_requires_review"}:
            raise OperationError("이 단계는 입력 내용만 지정할 수 없습니다. 안내에 따라 삭제하고 동작을 직접 추가하세요.")
        value = _text(payload["value"], "입력 내용", 16000, empty=True)
        proposed = copy.deepcopy(self.steps)
        proposed[index].update(operation="image_type_text", value=value, replace_all=True)
        proposed[index].pop("manual_reason", None)
        self.validate(proposed, allow_pending=True)
        self.steps = proposed

    def retarget(self, index, native):
        if type(index) is not int or not 0 <= index < len(self.steps) or "image_target" not in self.steps[index]:
            raise OperationError("이미지가 있는 동작 단계를 선택하세요.")
        step = self.steps[index]
        target = self.targets[(step["program_id"], step.get("window_ref", "main"))]
        view = self.remember_image(target, native)
        proposed = copy.deepcopy(self.steps)
        proposed[index]["image_target"] = copy.deepcopy(self.selections[view["selection_id"]]["image_target"])
        self.validate(proposed, allow_pending=True)
        self.steps = proposed
        self.labels[index] = "다시 선택한 이미지"

    def repair(self, index, selection, *, action=None, value=None):
        """Replace one target without deleting its popup binding or neighbours."""
        if type(index) is not int or not 0 <= index < len(self.steps):
            raise OperationError("다시 지정할 동작 단계를 선택하세요.")
        previous = self.steps[index]
        if previous.get("manual_reason") == "protected_input":
            raise OperationError("보호된 입력은 녹화 단계로 복구하지 않습니다. 해당 입력은 직접 처리하세요.")
        if previous["operation"] != "manual_entry" and not any(k in previous for k in ("image_target", "selector")):
            raise OperationError("동작할 대상이 있는 단계를 선택하세요.")
        target = self.targets[(previous["program_id"], previous.get("window_ref", "main"))]
        selected = self.selection(selection["selection_id"], target)
        original = next((name for name, mapped in IMAGE_ACTIONS.items() if mapped == previous["operation"]), previous.get("repair_action", previous["operation"]))
        chosen = action or original
        if chosen not in {"click", "double_click", "right_click", "set_value", "select_option", "set_checked", "press_key", "hotkey", "scroll", "wait_for_element"}:
            raise OperationError("이 단계에서 실행할 동작을 직접 선택하세요.")
        if previous["operation"] != "manual_entry" and chosen != original:
            raise OperationError("대상 재지정은 기존 동작을 유지합니다.")
        step = {"program_id": target["program_id"], "window_ref": target.get("window_ref", "main"), "operation": chosen}
        for key in ("step_id", "value", "key", "keys", "direction", "amount", "timeout_ms", "expect", "checked", "option_order", "completion_mode"):
            if key in previous: step[key] = copy.deepcopy(previous[key])
        if chosen == "set_value" and previous["operation"] == "manual_entry":
            if value is None: raise OperationError("다시 실행할 때 넣을 입력 내용을 직접 지정하세요.")
            step["value"] = _text(value, "입력 내용", 16000, empty=True)
        if selected.get("recognition") == "image":
            if chosen not in IMAGE_ACTIONS:
                raise OperationError("이 동작은 값 정보를 제공하는 요소가 필요합니다. 대상 요소를 다시 선택하세요.")
            step.update(operation=IMAGE_ACTIONS[chosen], image_target=copy.deepcopy(selected["image_target"]))
            if chosen == "set_value": step["replace_all"] = True
        else:
            if chosen not in selected["actions"]:
                raise OperationError("선택한 요소가 이 동작을 지원하지 않습니다. 원래 동작에 맞는 요소를 선택하세요.")
            step["selector"] = copy.deepcopy(selected["selector"])
            for check in step.get("expect", []):
                if previous.get("selector") and check.get("selector") == previous["selector"]:
                    check["selector"] = copy.deepcopy(step["selector"])
            if chosen in {"click", "double_click", "right_click"} and not step.get("expect"):
                step["completion_mode"] = "human"
        proposed = copy.deepcopy(self.steps); proposed[index] = step
        self.validate(proposed, allow_pending=True)
        self.steps = proposed; self.labels[index] = selected["label"]
        issue = previous.get("step_id")
        if issue in self._target_warning_steps:
            self._target_warning_steps.remove(issue)
            if not self._target_warning_steps and not self._persistent_semantic_warning:
                self.recording_warnings = [code for code in self.recording_warnings if code != "semantic_target_unverified"]

    def edit(self, payload):
        """Edit one authored step; image bytes and untouched fields never round-trip through the UI."""
        index = payload.get("index")
        allowed = {"index", "value", "key", "keys", "seconds", "expected", "variable", "variable_label", "checked"}
        if set(payload) - allowed or type(index) is not int or not 0 <= index < len(self.steps):
            raise OperationError("수정할 단계를 선택하세요.")
        proposed, variables = copy.deepcopy(self.steps), copy.deepcopy(self.variables)
        step = proposed[index]
        operation = step["operation"]
        if operation == "manual_entry":
            if set(payload) != {"index", "value"}: raise OperationError("먼저 확인되지 않은 입력 내용을 지정하세요.")
            return self.resolve_input(payload)
        field = "value" if operation in {"set_value", "select_option", "image_type_text"} else "message" if operation == "checkpoint" else None
        old_value = step.get(field) if field else None
        if "value" in payload:
            if field is None: raise OperationError("이 단계에는 수정할 입력값이 없습니다.")
            step[field] = _text(payload["value"], "입력 내용", 16000, empty=True)
        if "key" in payload:
            if operation not in {"press_key", "image_press_key"}: raise OperationError("키 입력 단계가 아닙니다.")
            step["key"] = payload["key"]
        if "keys" in payload:
            if operation not in {"hotkey", "image_hotkey"}: raise OperationError("단축키 단계가 아닙니다.")
            step["keys"] = copy.deepcopy(payload["keys"])
        if "checked" in payload:
            if operation != "set_checked" or type(payload["checked"]) is not bool: raise OperationError("체크 상태는 예 또는 아니요로 지정하세요.")
            before_checked = step.get("checked")
            step["checked"] = payload["checked"]
            for check in step.get("expect", []):
                if check.get("selector") == step.get("selector") and check.get("property") == "selected" and check.get("equals") == before_checked:
                    check["equals"] = step["checked"]
        if "seconds" in payload:
            timing = "duration_ms" if operation == "delay" else "timeout_ms" if operation in {"wait_for_element", "wait_for_image", "wait_for_state", "wait_for_window"} else None
            if timing is None: raise OperationError("이 단계에는 대기 시간이 없습니다.")
            step[timing] = self.milliseconds(payload["seconds"])
        if "variable" in payload:
            from workflows import VARIABLE
            name = payload["variable"]
            if field != "value" or not isinstance(name, str) or (name and not VARIABLE.fullmatch(name)):
                raise OperationError("실행 입력 이름은 영문자로 시작하는 영문·숫자·밑줄 1~40자로 지정하세요.")
            if name:
                default = payload.get("value", old_value)
                if not isinstance(default, str) or len(default) > 4000: raise OperationError("실행 입력의 기본값은 4,000자 이하로 지정하세요.")
                previous = variables.get(name)
                spec = {"type": "text", "description": _text(payload.get("variable_label", name), "입력 설명", 4000, empty=True), "default": default}
                if previous and previous.get("type", "text") != "text":
                    raise OperationError("이름이 같은 다른 형식의 실행 입력이 있습니다. 다른 이름을 사용하세요.")
                variables[name] = {**(previous or {}), **spec}
                step["value"] = "${" + name + "}"
        # Editing a value also edits its existing self-verification, but never
        # unrelated completion conditions belonging to another control.
        if field == "value" and step.get("value") != old_value:
            for check in step.get("expect", []):
                if check.get("selector") == step.get("selector") and check.get("property") == "value" and check.get("equals") == old_value:
                    check["equals"] = copy.deepcopy(step["value"])
        if "expected" in payload:
            if len(step.get("expect", [])) != 1: raise OperationError("단일 완료 조건이 있는 단계를 선택하세요.")
            step["expect"][0]["equals"] = copy.deepcopy(payload["expected"])
        prior = self.variables
        try:
            self.variables = variables
            self.validate(proposed, allow_pending=True)
        except Exception:
            self.variables = prior
            raise
        self.steps = proposed

    def completion(self, payload):
        if set(payload) != {"index", "selection_id", "property", "equals"} or type(payload["index"]) is not int or not 0 <= payload["index"] < len(self.steps):
            raise OperationError("완료 기준을 바꿀 단계와 확인 대상을 선택하세요.")
        index = payload["index"]
        step = copy.deepcopy(self.steps[index])
        was_deferred = not step.get("expect") and (step.get("completion_mode") == "human" or step["operation"].startswith("image_"))
        if step["operation"] not in {*IMAGE_ACTIONS.values(), "click", "double_click", "right_click", "set_value", "select_option", "set_checked", "press_key", "hotkey", "wait_for_state"}:
            raise OperationError("동작 또는 완료 대기 단계에 결과 조건을 지정하세요.")
        target = self.targets[(step["program_id"], step.get("window_ref", "main"))]
        selected = self.selection(payload["selection_id"], target)
        if selected.get("recognition") == "image": raise OperationError("값을 제공하는 확인 요소를 선택하세요.")
        check = {"selector": copy.deepcopy(selected["selector"]), "property": payload["property"], "equals": copy.deepcopy(payload["equals"])}
        if step["operation"].startswith("image_"): check["require_change"] = True
        step["expect"] = [check]
        step.pop("completion_mode", None)
        proposed, labels = copy.deepcopy(self.steps), list(self.labels)
        proposed[index] = step
        if was_deferred and index + 1 < len(proposed):
            following = proposed[index + 1]
            if (following.get("operation") == "checkpoint" and not any(k in following for k in ("opened_from", "return_from"))
                    and (following["program_id"], following.get("window_ref", "main")) == (step["program_id"], step.get("window_ref", "main"))):
                proposed.pop(index + 1); labels.pop(index + 1)
        self.validate(proposed, allow_pending=True)
        self.steps, self.labels = proposed, labels

    def add(self, payload):
        if set(payload) - {"action", "program_id", "pid", "window_id", "window_ref", "selection_id", "value", "key", "keys",
                           "seconds", "timeout_seconds", "option_order", "expect", "checked", "direction", "amount", "completion_mode"}:
            raise OperationError("지원하지 않는 단계 설정입니다.")
        target = self.target(payload)
        action = payload.get("action")
        completion = payload.get("completion_mode", "human")
        if completion not in {"human", "automatic", "visual"}:
            raise OperationError("완료 확인 방법을 선택하세요.")
        if action not in ACTION_LABELS:
            raise OperationError("동작 종류를 선택하세요.")
        step = {"operation": action, "program_id": target["program_id"], "window_ref": target.get("window_ref", "main")}
        label = "현재 창"
        if action == "delay":
            step["duration_ms"] = self.milliseconds(payload.get("seconds", 1))
        elif action == "checkpoint":
            step["message"] = payload.get("value", "현재 화면을 확인하세요.")
        elif action == "wait_for_state":
            step["timeout_ms"] = self.milliseconds(payload.get("timeout_seconds", 20))
        else:
            selected = self.selection(payload.get("selection_id"), target)
            label = selected["label"]
            if selected.get("recognition") == "image":
                if action not in IMAGE_ACTIONS:
                    raise OperationError("이미지는 클릭·입력·키보드·스크롤·대기 동작에 사용할 수 있습니다.")
                step["operation"] = IMAGE_ACTIONS[action]
                step["image_target"] = copy.deepcopy(selected["image_target"])
            else:
                step["selector"] = copy.deepcopy(selected["selector"])
            if action == "wait_for_element":
                step["timeout_ms"] = self.milliseconds(payload.get("timeout_seconds", 20))
            elif action in {"set_value", "select_option"}:
                step["value"] = payload.get("value", "")
                if step["operation"] == "image_type_text": step["replace_all"] = True
                if action == "select_option" and payload.get("option_order"):
                    step["option_order"] = copy.deepcopy(payload["option_order"])
            elif action == "set_checked":
                step["checked"] = payload.get("checked")
            elif action == "press_key":
                step["key"] = payload.get("key")
            elif action == "hotkey":
                step["keys"] = copy.deepcopy(payload.get("keys"))
            elif action == "scroll":
                step.update(direction=payload.get("direction", "down"), amount=payload.get("amount", 3))
        if "expect" in payload:
            expected = payload["expect"]
            if not isinstance(expected, dict) or set(expected) != {"selection_id", "property", "equals"}:
                raise OperationError("확인할 요소와 예상 결과를 지정하세요.")
            selected = self.selection(expected["selection_id"], target)
            if selected.get("recognition") == "image":
                raise OperationError("이미지 동작은 자동으로 추가되는 화면 확인 단계에서 결과를 확인합니다.")
            step["expect"] = [{"selector": copy.deepcopy(selected["selector"]),
                               "property": expected["property"], "equals": expected["equals"]}]
            if completion == "automatic" and step["operation"].startswith("image_"):
                step["expect"][0]["require_change"] = True
        if completion == "automatic" and step["operation"].startswith("image_") and not step.get("expect"):
            raise OperationError("자동 확인할 요소와 작업 후 예상값을 먼저 선택하세요.")
        if payload.get("completion_mode") in {"human", "visual"} and step["operation"] in {"click", "double_click", "right_click"} and not step.get("expect"):
            step["completion_mode"] = "human"
        additions, labels = [step], [label]
        if (step["operation"].startswith("image_") or step.get("completion_mode") == "human") and not step.get("expect"):
            check = self.checkpoint(target)
            if completion == "visual": check["review_mode"] = "visual"
            additions.append(check); labels.append("현재 창")
        proposed = [*self.steps, *additions]
        self.validate(proposed, allow_pending=True)
        self.steps, self.labels = proposed, [*self.labels, *labels]

    @staticmethod
    def milliseconds(seconds):
        if type(seconds) not in {int, float} or not 0 <= seconds <= 60:
            raise OperationError("대기 시간은 0~60초로 지정하세요.")
        return round(seconds * 1000)

    def change(self, action, payload):
        allowed = {"index", "direction"} if action == "move_step" else {"index"}
        index = payload.get("index")
        if set(payload) != allowed or type(index) is not int or not 0 <= index < len(self.steps):
            raise OperationError("목록에서 변경할 단계를 선택하세요.")
        # Image mutations and their required human-review checkpoint form one
        # unit, so reordering cannot detach the verification from its action.
        groups = []
        cursor = 0
        while cursor < len(self.steps):
            length = 2 if ((self.steps[cursor]["operation"].startswith("image_") or self.steps[cursor].get("completion_mode") == "human") and not self.steps[cursor].get("expect")) or self.steps[cursor]["operation"] == "manual_entry" else 1
            if (length == 2 and cursor + 2 < len(self.steps) and self.steps[cursor + 1]["operation"] == "wait_for_window"
                    and self.steps[cursor + 2]["operation"] == "checkpoint" and self.steps[cursor + 2].get("opened_from") == self.steps[cursor].get("window_ref", "main")):
                length = 3
            groups.append((self.steps[cursor:cursor+length], self.labels[cursor:cursor+length], cursor))
            cursor += length
        group_index = next(i for i, g in enumerate(groups) if g[2] <= index < g[2] + len(g[0]))
        if action == "remove_step":
            groups.pop(group_index)
        else:
            direction = payload["direction"]
            if type(direction) is not int or direction not in {-1, 1} or not 0 <= group_index + direction < len(groups):
                raise OperationError("이 방향으로 단계를 이동할 수 없습니다.")
            other = group_index + direction
            groups[group_index], groups[other] = groups[other], groups[group_index]
        self.steps = [step for group in groups for step in group[0]]
        self.labels = [label for group in groups for label in group[1]]

    def summaries(self):
        rows = []
        for index, (step, label) in enumerate(zip(self.steps, self.labels)):
            action = step["operation"]
            if action == "foreach":
                programs = list(dict.fromkeys(self.targets[(child["program_id"], child.get("window_ref", "main"))]["label"] for child in _leaf_steps(step.get("steps", []))))
                variable = step.get("input", "목록")
                rows.append({"index": index, "program": ", ".join(programs), "label": variable, "action": action,
                             "action_label": "목록 반복", "recognition": "group", "requires_input": False, "editable_input": False,
                             "detail": str(len(step.get("steps", []))) + "개 동작을 목록의 각 값에 반복 · 값은 채팅에서 변경 가능",
                             "repairable": False, "repair_action": "", "summary": str(index + 1) + ". 목록 반복 · " + variable})
                continue
            target = self.targets[(step["program_id"], step.get("window_ref", "main"))]
            detail = (str(step.get("duration_ms", 0)/1000) + "초" if action == "delay" else
                      str(step.get("timeout_ms", 0)/1000) + "초 이내" if action in {"wait_for_element", "wait_for_image", "wait_for_state", "wait_for_window"} else
                      str(step.get("message", step.get("value", step.get("key", "+".join(step.get("keys", [])))))))
            if step.get("expect"):
                check = step["expect"][0]
                detail += " → 확인: " + str(check["selector"].get("name", check["selector"].get("automation_id", "요소"))) + " = " + str(check["equals"])
            action_label = ACTION_LABELS.get(action, action)
            editable_input = action in {"set_value", "set_checked", "select_option", "image_type_text", "checkpoint", "delay", "wait_for_element", "wait_for_image", "wait_for_state", "wait_for_window", "press_key", "image_press_key", "hotkey", "image_hotkey"} or len(step.get("expect", [])) == 1 or action == "manual_entry" and "image_target" in step and step.get("manual_reason") in {"unknown_input", "existing_text_requires_review"}
            if action == "manual_entry": detail = MANUAL_REASONS.get(step.get("manual_reason"), "이 단계를 삭제하고 동작을 직접 추가하세요.") + " 확인 전에는 저장할 수 없습니다."
            rows.append({"index": index, "program": target["label"], "label": label, "action": action,
                         "action_label": action_label, "recognition": "image" if "image_target" in step else "unresolved" if action == "manual_entry" else "uia",
                         "requires_input": action == "manual_entry", "editable_input": editable_input, "detail": detail,
                         "repairable": (action == "manual_entry" or "image_target" in step or "selector" in step) and step.get("manual_reason") != "protected_input",
                         "repair_action": step.get("repair_action", ""),
                         "summary": f"{index+1}. {action_label} · {label} · {detail}"})
        return rows


class ProcessEditors:
    def __init__(self, library, tasks):
        self.library, self.tasks = library, tasks
        self.lock = threading.RLock()
        self.jobs = {}

    def pending(self, runtime=None):
        with self.lock:
            return next((j for j in self.jobs.values() if (runtime is None or j["runtime"] is runtime)
                         and (not j["done"].is_set() or j["result"].get("cleanup_pending"))), None)

    def _view(self, job, **extra):
        with self.lock:
            return copy.deepcopy({**job["result"], "editor_id": job["id"], "input_dispatched": job["result"].get("last_test", {}).get("input_dispatched") if "last_test" in job["result"] else False,
                                  "automatic_retry": False, "pending": not job["done"].is_set() or bool(job["result"].get("cleanup_pending")),
                                  "steps": job["summaries"], **extra})

    def _check(self, job):
        if job["cancel"].is_set() or job["runtime"].stop_event.is_set():
            raise OperationError("프로세스 만들기를 취소했습니다.", "editor_cancelled")
        job["runtime"].check_active()

    @staticmethod
    def _hosted_identity(runtime, target):
        reader = getattr(runtime.guard, "hosted_target", None)
        return copy.deepcopy(reader(target) if callable(reader) else None)

    def _program(self, job, program_id, target):
        binding = self.library._program(job["runtime"], program_id, target)
        key = (target["pid"], target["window_id"])
        hosted = self._hosted_identity(job["runtime"], target)
        identities = job.setdefault("hosted_targets", {})
        if key in identities and identities[key] != hosted:
            raise OperationError("편집 창에 연결한 앱이 바뀌었습니다. 현재 앱으로 편집 창을 다시 여세요.", "editor_target_changed")
        identities[key] = hosted
        return binding

    def start(self, runtime, args, cancel_event=None):
        runtime.check_active()
        if runtime.mode != "uia":
            raise OperationError("프로세스 만들기는 UIA 세션에서 시작하세요.", "unsupported_mode")
        programs, hosted_targets = [], {}
        for target in args["targets"]:
            if (not isinstance(target, dict) or set(target) - {"program_id", "pid", "window_id", "window_ref"}
                    or not {"program_id", "pid", "window_id"} <= set(target)
                    or not isinstance(target["program_id"], str)
                    or any(type(target[k]) is not int or target[k] < 1 for k in ("pid", "window_id"))
                    or not isinstance(target.get("window_ref", "main"), str)
                    or not WINDOW_REF.fullmatch(target.get("window_ref", "main"))):
                raise OperationError("프로그램과 현재 창을 정확히 지정하세요.")
            self.library._program(runtime, target["program_id"], {k: target[k] for k in ("pid", "window_id")})
            hosted_targets[(target["pid"], target["window_id"])] = self._hosted_identity(runtime, {k: target[k] for k in ("pid", "window_id")})
            label = next(p["name"] for p in runtime.programs if p["id"] == target["program_id"])
            programs.append({**target, "label": label + " · " + target.get("window_ref", "main")})
        if not 1 <= len(programs) <= 10 or len({(p["program_id"], p.get("window_ref", "main")) for p in programs}) != len(programs):
            raise OperationError("중복 없는 프로그램/창을 1~10개 연결하세요.")
        task = self.tasks.get(args["task_id"]) if args.get("task_id") else None
        draft = ProcessDraft(programs, task)
        visual_enabled = callable(getattr(self, "visual_review_enabled", None)) and self.visual_review_enabled(runtime) is True
        with self.lock:
            active = self.pending()
            if active:
                return self._view(active, reused=active["args"] == args, busy=active["args"] != args,
                                  next_tool="computer_process_status")
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "runtime": runtime, "draft": draft, "cancel": _Cancellation(cancel_event),
                   "hosted_targets": hosted_targets,
                   "interaction_lock": threading.RLock(),
                   "started": threading.Event(), "done": threading.Event(), "summaries": draft.summaries(),
                   "args": copy.deepcopy(args), "name": args.get("name", (task or {}).get("name", "새 프로세스")),
                   "description": (task or {}).get("instructions", ""), "visual_review_enabled": visual_enabled, "result": {"status": "starting", "editor_visible": False}}
            self.jobs[job_id] = job
            for key in [k for k, j in self.jobs.items() if j["done"].is_set() and not j["result"].get("cleanup_pending")][:-19]:
                del self.jobs[key]
            job["worker"] = threading.Thread(target=self._work, args=(job,), daemon=True, name="process-editor-" + job_id[:8])
            job["worker"].start()
        if not job["started"].wait(9):
            job["cancel"].set()
            return self._view(job, status="cancelling", message="편집 창 표시를 확인하지 못해 취소하고 있습니다.")
        return self._view(job)

    def _command(self, job, command):
        self._check(job)
        action, payload = command.get("action"), command.get("payload")
        if not isinstance(payload, dict):
            raise OperationError("편집 요청 형식이 올바르지 않습니다.")
        draft, runtime = job["draft"], job["runtime"]
        extra = {}
        message = "반영했습니다."
        if action == "preview_step":
            index = payload.get("index")
            if set(payload) != {"index"} or type(index) is not int or not 0 <= index < len(draft.steps):
                raise OperationError("대상을 확인할 단계를 선택하세요.")
            step = draft.steps[index]
            preview = draft.summaries()[index]
            if "image_target" in step: preview["thumbnail_png"] = step["image_target"]["template_png"]
            preview["editable"] = {key: copy.deepcopy(step[key]) for key in ("value", "message", "key", "keys", "duration_ms", "timeout_ms", "checked") if key in step}
            if len(step.get("expect", [])) == 1:
                preview["editable"]["expected"] = copy.deepcopy(step["expect"][0]["equals"])
            variable = step.get("value")
            if isinstance(variable, str) and variable.startswith("${") and variable.endswith("}") and variable[2:-1] in draft.variables:
                preview["editable"].update(variable=variable[2:-1], variable_spec=copy.deepcopy(draft.variables[variable[2:-1]]))
            count = 1
            while "input_" + str(count) in draft.variables: count += 1
            preview["editable"]["suggested_variable"] = "input_" + str(count)
            return {"status": "ok", "message": "저장할 대상의 미리보기입니다. 프로그램에 입력하지 않았습니다.", "preview": preview}
        if action == "retarget_step":
            index = payload.get("index")
            if set(payload) - {"index", "action", "value", "method"} or payload.get("method", "auto") not in ("auto", "image") or type(index) is not int or not 0 <= index < len(draft.steps) or not draft.summaries()[index]["repairable"]:
                raise OperationError("보완이 필요한 단계 또는 이미지 동작을 선택하세요.")
            step = draft.steps[index]
            target = draft.targets[(step["program_id"], step.get("window_ref", "main"))]
            exact = {k: target[k] for k in ("pid", "window_id")}
            self._program(job, target["program_id"], exact)
            native = self._visual(job, "pick", {**exact, "label": "이 단계의 대상을 다시 선택하세요", "auto_capture": True,
                                                   "include_native": payload.get("method", "auto") != "image"}, 120)
            self._program(job, target["program_id"], exact)
            selected = None
            if isinstance(native.get("native_target"), dict) and payload.get("method", "auto") != "image":
                try:
                    with runtime.execution_lock:
                        self._check(job)
                        snapshot, _ = self.library._observe(runtime, exact, target["program_id"])
                        selected = draft.remember(target, snapshot, native["native_target"])
                except OperationError as exc:
                    if exc.code not in IMAGE_FALLBACK_ERRORS or getattr(exc, "helper_cleanup_pending", False): raise
            if selected is None:
                selected = draft.remember_image(target, native)
            draft.repair(index, selected, action=payload.get("action"), value=payload.get("value"))
            message = "이 단계의 대상만 다시 지정했습니다. 순서와 프로그램·팝업 연결은 그대로 유지했습니다."
        elif action in {"pick_target", "pick_completion"}:
            step_index = None
            if action == "pick_completion":
                step_index = payload.get("index")
                if set(payload) != {"index"} or type(step_index) is not int or not 0 <= step_index < len(draft.steps): raise OperationError("완료 기준을 지정할 단계를 선택하세요.")
                step = draft.steps[step_index]
                picked_target = draft.targets[(step["program_id"], step.get("window_ref", "main"))]
                payload = {**picked_target, "purpose": "expect"}
                payload.pop("label", None)
            if set(payload) - {"program_id", "pid", "window_id", "window_ref", "purpose"} or payload.get("purpose") not in {"action", "expect"}:
                raise OperationError("대상 프로그램과 선택 용도를 확인하세요.")
            target = draft.target(payload)
            exact = {k: target[k] for k in ("pid", "window_id")}
            self._program(job, target["program_id"], exact)
            native = self._visual(job, "pick", {**exact, "label": "동작할 대상을 화면에서 선택하세요", "auto_capture": True, "include_native": True, "include_text": payload["purpose"] == "expect"}, 120)
            self._program(job, target["program_id"], exact)
            picked = None
            if isinstance(native.get("native_target"), dict):
                try:
                    with runtime.execution_lock:
                        self._check(job)
                        snapshot, _ = self.library._observe(runtime, exact, target["program_id"])
                        picked = draft.remember(target, snapshot, native["native_target"])
                        picked["thumbnail_png"] = native["template_png"]
                except OperationError as error:
                    if error.code not in IMAGE_FALLBACK_ERRORS: raise
            if picked is None:
                if payload["purpose"] == "expect":
                    raise OperationError("이 영역은 값 정보를 제공하지 않습니다. 자동 확인은 값이 읽히는 요소를 선택하거나 화면 확인 단계를 사용하세요.", "completion_not_exposed")
                picked = draft.remember_image(target, native)
            extra = {"purpose": payload["purpose"], "selection": picked}
            if step_index is not None: extra["completion_index"] = step_index
            message = "선택한 대상을 기억했습니다. 요소 정보가 있으면 요소로, 없으면 같은 선택 이미지를 사용합니다."
        elif action in {"pick_element", "pick_image"}:
            if set(payload) - {"program_id", "pid", "window_id", "window_ref", "purpose"} or payload.get("purpose") not in {"action", "expect"}:
                raise OperationError("요소 선택의 대상과 용도를 확인하세요.")
            target = draft.target(payload)
            exact = {k: target[k] for k in ("pid", "window_id")}
            self._program(job, target["program_id"], exact)
            use_image = action == "pick_image"
            fallback_explanation = None
            fallback_diagnostic = None
            if use_image and payload["purpose"] != "action":
                raise OperationError("이미지 동작의 결과는 화면 확인 단계에서 검토합니다.")
            if not use_image:
                try:
                    selected = _run_helper(runtime, exact, "동작할 요소" if payload["purpose"] == "action" else "결과를 확인할 요소", 120,
                                           on_ready=lambda _info: self._check(job), cancel_event=job["cancel"])
                    with runtime.execution_lock:
                        self._check(job)
                        snapshot, _binding = self.library._observe(runtime, exact, target["program_id"])
                        extra = {"purpose": payload["purpose"], "selection": draft.remember(target, snapshot, selected)}
                except OperationError as exc:
                    if (exc.code not in IMAGE_FALLBACK_ERRORS or payload["purpose"] != "action"
                            or getattr(exc, "helper_cleanup_pending", False)):
                        raise
                    use_image = True
                    fallback_explanation, fallback_diagnostic = _recognition_fallback(exc)
                    message = fallback_explanation + " 직접 확정한 이미지를 선택했습니다. 동작 뒤 화면 확인 단계가 함께 추가됩니다."
            if use_image:
                self._check(job)
                self._program(job, target["program_id"], exact)
                # Retain the reason even if image capture is cancelled or fails;
                # support can inspect it without another selection/read cycle.
                with self.lock:
                    if fallback_diagnostic:
                        job["result"]["last_recognition"] = {"method": "image_fallback", **fallback_diagnostic}
                    else:
                        job["result"].pop("last_recognition", None)
                native = self._visual(job, "pick", {**exact,
                    "label": fallback_explanation + " 이미지에서 대상을 직접 선택하세요." if fallback_explanation else "이미지로 사용할 영역을 선택하세요"}, 120)
                self._program(job, target["program_id"], exact)
                extra = {"purpose": "action", "selection": draft.remember_image(target, native)}
                if fallback_diagnostic:
                    extra["recognition_diagnostic"] = fallback_diagnostic
            else:
                with self.lock:
                    job["result"].pop("last_recognition", None)
        elif action == "record":
            from replay_preflight import image_replay_preflight, prepare_image_foreground
            if set(payload) - {"review_mode"} or payload.get("review_mode", "human") not in {"human", "visual"}:
                raise OperationError("녹화 범위는 편집 창에 연결한 프로그램으로 고정됩니다.")
            if payload.get("review_mode") == "visual" and not job.get("visual_review_enabled"):
                raise OperationError("화면을 읽는 모델 연결이 확인되지 않았습니다. 직접 화면 확인을 사용하세요.")
            # Reserve room for up to five new popup-binding waits as well as
            # one review checkpoint for each image action.
            maximum_events = (30 - len(draft.steps) - 5) // 2
            if maximum_events < 1:
                raise OperationError("녹화 동작과 화면 확인을 추가할 공간이 없습니다. 기존 단계를 삭제하거나 새 프로세스를 만드세요.")
            roots = [target for target in draft.programs if (target["program_id"], target.get("window_ref", "main")) not in
                     {(step["program_id"], step["window_ref"]) for step in draft.steps if step["operation"] == "wait_for_window"}]
            for target in roots:
                self._program(job, target["program_id"], {k: target[k] for k in ("pid", "window_id")})
            preflight = []
            recording_targets = []
            for target in roots:
                check = image_replay_preflight(runtime, {k: target[k] for k in ("pid", "window_id")}, capture=False)
                if check["ready_for_input"]:
                    with runtime.execution_lock:
                        self._check(job)
                        prepared = prepare_image_foreground(runtime, {k: target[k] for k in ("pid", "window_id")})
                        if prepared is None:
                            check = image_replay_preflight(runtime, {k: target[k] for k in ("pid", "window_id")}, capture=True)
                        else:
                            check = {**check, "ready_for_input": False, "capture_verified": False, "status": "needs_foreground",
                                     "diagnostic": prepared.get("diagnostic", {})}
                preflight.append({"program_id": target["program_id"], "window_ref": target.get("window_ref", "main"), **check})
                recording_targets.append({**target, "image_allowed": check["ready_for_input"],
                                          "preflight_message": check.get("diagnostic", {}).get("message", "")})
            with self.lock:
                job["result"]["recording_preflight"] = copy.deepcopy(preflight)
            blocked = [row for row in preflight if row.get("ready_for_input") is not True]
            if blocked:
                reason = blocked[0].get("diagnostic", {})
                raise OperationError("아직 녹화를 시작하지 않았습니다. " + reason.get("message", "대상 창의 화면 읽기와 재생 준비를 확인하지 못했습니다.")
                    + " 현재 초안은 유지했습니다. 연결 상태와 대상 창을 확인한 뒤 [동작 녹화]를 다시 누르세요.",
                    reason.get("code", "recording_not_ready"))
            recorded = self._visual(job, "record", {"targets": recording_targets, "max_events": maximum_events}, min(600, job["args"].get("timeout_seconds", 600)))
            receipt = job.get("last_recording")
            if receipt is None or receipt["data"] != recorded:
                receipt = self._retain_recording(job, recorded)
            receipt["review_mode"] = payload.get("review_mode", "human")
            draft = self._import_recording(job, receipt)
            extra["programs"] = copy.deepcopy(draft.programs)
            message = "녹화한 동작을 초안에 추가했습니다. 순서와 입력 내용을 검토한 뒤 저장하세요. 아직 재실행하지 않았습니다."
            if draft.recording_warnings: message = "일부 동작이 빠졌거나 확인되지 않은 부분 기록입니다. [누락 경고 확인]과 각 단계를 검토하세요."
        elif action == "reimport_recording":
            if payload:
                raise OperationError("최근 녹화 원본만 다시 분석할 수 있습니다.")
            receipt = job.get("last_recording")
            if receipt is None:
                raise OperationError("이 편집창에서 보관한 녹화 원본이 없습니다.", "recording_receipt_missing")
            expected = receipt.get("after", receipt["before"])
            if draft.__dict__ != expected.__dict__:
                raise OperationError("녹화 뒤 편집한 내용이 있어 다시 분석으로 덮어쓰지 않았습니다. 필요한 단계만 다시 지정하세요.", "recording_draft_changed")
            draft = self._import_recording(job, receipt)
            extra["programs"] = copy.deepcopy(draft.programs)
            message = "보관한 녹화 원본을 다시 분석했습니다. 실제 클릭·입력은 실행하지 않았습니다."
        elif action == "ack_recording_warnings":
            if payload != {"acknowledged": True} or not draft.recording_warnings:
                raise OperationError("녹화 경고를 직접 확인한 뒤 확인 버튼을 누르세요.")
            draft.recording_acknowledged = True
            message = "누락 경고를 확인했습니다. 확인되지 않은 단계는 교체·삭제해야 저장할 수 있습니다."
        elif action == "resolve_input":
            draft.resolve_input(payload)
        elif action == "edit_step":
            draft.edit(payload)
            message = "선택한 단계의 설정만 반영했습니다. 다른 단계와 저장 이미지는 유지했습니다."
        elif action == "set_completion":
            draft.completion(payload)
            message = "자동 완료 기준을 지정했습니다. 이 동작의 별도 화면 확인은 생략합니다."
        elif action == "test_run":
            if set(payload) != {"name", "description"}: raise OperationError("프로세스 이름과 설명을 확인하세요.")
            draft.validate()
            if not draft.steps: raise OperationError("시험할 동작을 먼저 추가하세요.")
            runner = getattr(self, "test_runner", None)
            if not callable(runner): raise OperationError("시험 실행 연결을 확인하지 못했습니다. MCP를 최신 버전으로 다시 연결하세요.", "editor_test_unavailable")
            task = {"id": "draft-" + job["id"], "name": _text(payload["name"], "프로세스 이름", 100),
                    "instructions": _text(payload["description"], "설명", 4000, empty=True), "expected": "설정한 완료 조건 확인",
                    "program_ids": list(dict.fromkeys(s["program_id"] for s in _leaf_steps(draft.steps))),
                    "steps": copy.deepcopy(draft.steps), "variables": copy.deepcopy(draft.variables)}
            targets = [{key: target[key] for key in ("program_id", "pid", "window_id", "window_ref") if key in target}
                       for target in draft.programs if target["window_id"] > 0]
            tested = runner(runtime, task, targets)
            if not isinstance(tested, dict): raise OperationError("시험 실행 결과의 형식을 확인하지 못했습니다.", "editor_test_invalid_result")
            extra["test_result"] = self._trial_summary(tested, task)
            with self.lock:
                job["test_task"], job["test_targets"], job["test_result_full"] = copy.deepcopy(task), copy.deepcopy(targets), copy.deepcopy(tested)
                job["result"]["last_test"] = copy.deepcopy(extra["test_result"])
            message = "시험 실행 결과를 확인하세요. 저장한 작업은 변경하지 않았습니다."
        elif action == "add_step":
            if payload.get("completion_mode") == "visual" and not job.get("visual_review_enabled"):
                raise OperationError("화면을 읽는 모델 연결이 확인되지 않았습니다. 직접 화면 확인을 사용하세요.")
            draft.add(payload)
        elif action in {"remove_step", "move_step"}:
            draft.change(action, payload)
        elif action == "save":
            if set(payload) != {"name", "description"}:
                raise OperationError("프로세스 이름과 설명을 입력하세요.")
            name = _text(payload["name"], "프로세스 이름", 100).strip()
            description = _text(payload["description"], "설명", 4000, empty=True)
            draft.validate()
            if not draft.steps:
                raise OperationError("저장할 동작을 먼저 추가하세요.")
            # Linearize acceptance against cancellation without holding the
            # editor lock during a potentially blocked task-store write. Stop
            # remains responsive; a save already accepted reports its result.
            with self.lock:
                self._check(job)
                job["save_started"] = True
            review = ({"recording_review": {key: draft.recording_review()[key] for key in ("partial", "warning_codes", "acknowledged")}} if draft.recording_warnings else {})
            save_args = {"name": name, "instructions": description or "저장한 단계 순서대로 실행합니다.",
                "expected": "각 단계에 지정한 완료 조건과 화면 확인 기준을 확인합니다.",
                "program_ids": list(dict.fromkeys(s["program_id"] for s in _leaf_steps(draft.steps))),
                "steps": draft.steps, "variables": draft.variables, **review}
            if draft.task_id:
                from task_revision import TaskRevisions
                saved = TaskRevisions(self.tasks).save({**save_args, "id": draft.task_id}, expected_revision=draft.revision)
            else:
                saved = self.tasks.save(save_args)
            draft.task_id, draft.revision = saved["id"], saved.get("revision", 1)
            extra["saved_task"] = {"id": saved["id"], "name": saved["name"], "revision": draft.revision, "step_count": len(saved["steps"])}
            with self.lock:
                job["result"] = {"status": "saved", "editor_visible": True, **extra,
                                 "message": "프로세스를 저장했습니다."}
        else:
            raise OperationError("지원하지 않는 편집 요청입니다.")
        with self.lock:
            job["summaries"] = draft.summaries()
            job["result"]["recording_review"] = draft.recording_review()
            recorded_import = job["result"].get("recording_import")
            if recorded_import is not None:
                recorded_import.setdefault("unresolved_at_import", recorded_import.get("unresolved_steps", 0))
                recorded_import["unresolved_steps"] = sum(step["operation"] == "manual_entry" for step in _leaf_steps(draft.steps))
                recorded_import["diagnostics_phase"] = "original_import"
        return {"status": "ok", "message": "저장했습니다." if action == "save" else message, "steps": draft.summaries(), "recording_review": draft.recording_review(),
                "recording_receipt_available": bool(job.get("last_recording")), **extra}

    def _retain_recording(self, job, recorded):
        """Preserve the validated owned-helper receipt before transient cleanup.

        Only metadata leaves the session. Original pixels/native identities stay
        local, so normalization failures never force a new user action merely
        because their evidence was deleted.
        """
        receipt_id = uuid.uuid4().hex
        folder = Path(job["runtime"].run_dir) / "recording-receipts"
        path = folder / (receipt_id + ".json")
        self._check_path(path); folder.mkdir(parents=True, exist_ok=True)
        self._write(path, {"format": "computer-recording-receipt/v1", "editor_id": job["id"],
                           "receipt_id": receipt_id, "recording": recorded})
        receipt = {"id": receipt_id, "path": path, "data": copy.deepcopy(recorded),
                   "before": copy.deepcopy(job["draft"]), "review_mode": "human"}
        with self.lock:
            job["last_recording"] = receipt
            job["result"]["recording_receipt"] = {"id": receipt_id, "stored_locally": True,
                "event_count": len(recorded.get("events", [])), "reanalysis_available": True}
        return receipt

    def _import_recording(self, job, receipt):
        """Read-only re-analysis into an atomic replacement of this import."""
        from replay_preflight import image_replay_preflight
        draft, runtime, recorded = copy.deepcopy(receipt["before"]), job["runtime"], receipt["data"]
        roots = [target for target in draft.programs if (target["program_id"], target.get("window_ref", "main")) not in
                 {(step["program_id"], step["window_ref"]) for step in draft.steps if step["operation"] == "wait_for_window"}]
        snapshots = {}
        popup_targets = [item[0] for item in draft.recorded_windows(recorded.get("windows", [])).values()]
        recorded_events = copy.deepcopy(recorded.get("events"))
        closed_popups = {(row["program_id"], row["window_ref"]) for row in recorded.get("windows", []) if row.get("closed") is True}
        for target in popup_targets:
            key = (target["program_id"], target.get("window_ref", "main"))
            if key in closed_popups: continue
            check = image_replay_preflight(runtime, {k: target[k] for k in ("pid", "window_id")}, capture=False)
            if not check["ready_for_input"] and isinstance(recorded_events, list):
                for event in recorded_events:
                    if not isinstance(event, dict) or (event.get("program_id"), event.get("window_ref", "main")) != key: continue
                    for field in ("template_png", "width", "height", "anchor", "capture_window", "source_size"): event.pop(field, None)
                    event["requested_operation"] = event.get("operation")
                    event["reason"] = "image_replay_unavailable"
        for target in roots:
            self._program(job, target["program_id"], {k: target[k] for k in ("pid", "window_id")})
        for target in [*roots, *popup_targets]:
            if (target["program_id"], target.get("window_ref", "main")) in closed_popups: continue
            if any(isinstance(event, dict) and event.get("program_id") == target["program_id"] and event.get("window_ref", "main") == target.get("window_ref", "main") and "native_target" in event for event in recorded.get("events", [])):
                try:
                    with runtime.execution_lock:
                        self._check(job)
                        self._program(job, target["program_id"], {k: target[k] for k in ("pid", "window_id")})
                        snapshots[(target["program_id"], target.get("window_ref", "main"))], _ = self.library._observe(runtime, {k: target[k] for k in ("pid", "window_id")}, target["program_id"])
                except OperationError:
                    self._check(job)
        draft.recorded(recorded_events, snapshots=snapshots, warning_codes=recorded.get("warnings", []),
                       windows=recorded.get("windows", []), review_mode=receipt["review_mode"])
        with self.lock:
            self._check(job)
            job["draft"] = draft
            receipt["after"] = copy.deepcopy(draft)
            job["result"]["recording_import"] = {"receipt_id": receipt["id"],
                "diagnostics": copy.deepcopy(draft.recording_import_diagnostics), "input_dispatched": False,
                "unresolved_steps": sum(step["operation"] == "manual_entry" for step in _leaf_steps(draft.steps)),
                "unresolved_at_import": sum(step["operation"] == "manual_entry" for step in _leaf_steps(draft.steps)),
                "diagnostics_phase": "original_import"}
        return draft

    @staticmethod
    def _trial_summary(tested, task):
        result = {key: copy.deepcopy(tested[key]) for key in ("status", "state", "task_verified", "run_id", "completed_steps", "total_steps", "step_index", "diagnostic", "checkpoint_id", "next_step", "visual_review", "checkpoint") if key in tested}
        result["task_id"] = task["id"]
        result["input_dispatched"] = tested.get("input_dispatched")
        result["automatic_retry"] = False
        return result

    def _trial_signal(self, job, state, result=None):
        exchange = job.get("exchange")
        if not exchange: raise OperationError("편집 창 연결이 없습니다.", "editor_closed")
        job["trial_sequence"] = job.get("trial_sequence", 0) + 1
        sequence = job["trial_sequence"]
        self._write(exchange["trial"], {"nonce": exchange["nonce"], "seq": sequence, "state": state, "result": result or {}})
        if state != "running": return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self._check(job)
            if exchange["trial_ready"].exists():
                answer = self._read(exchange["trial_ready"], exchange["nonce"])
                if (answer.get("seq") == sequence and answer.get("hidden") is True
                        and answer.get("helper_pid") == job["result"].get("helper_pid")):
                    return
            job["cancel"].event.wait(.03)
        raise OperationError("편집 창을 잠시 숨기는 준비를 확인하지 못했습니다. 입력하지 않았습니다.", "editor_test_not_ready")

    def resume_test(self, editor_id, runtime, *, observation_review=None, acknowledge_checkpoint=None):
        with self.lock:
            job = self.jobs.get(editor_id)
            if job is None or job["runtime"] is not runtime: raise OperationError("이 연결의 시험 실행을 찾지 못했습니다.", "editor_not_found")
        if not job["interaction_lock"].acquire(blocking=False): raise OperationError("편집 창에서 선택 또는 녹화가 진행 중입니다.", "editor_busy")
        trial_signalled = False
        try:
            self._check(job)
            if job["done"].is_set() or "test_task" not in job: raise OperationError("열린 편집 창에서 먼저 시험 실행을 시작하세요.", "editor_test_not_found")
            previous = job["test_result_full"]
            if previous.get("status") != "needs_review" or not previous.get("run_id"):
                raise OperationError("화면 확인을 기다리는 시험 실행만 이어갈 수 있습니다. 입력을 다시 보내지 않았습니다.", "editor_test_not_pending")
            trial_signalled = True
            self._trial_signal(job, "running")
            result = self.test_runner(runtime, copy.deepcopy(job["test_task"]), copy.deepcopy(job["test_targets"]),
                resume_run_id=previous["run_id"], observation_review=observation_review, acknowledge_checkpoint=acknowledge_checkpoint)
            if not isinstance(result, dict): raise OperationError("시험 실행 결과를 확인하지 못했습니다.", "editor_test_invalid_result")
            with self.lock:
                job["test_result_full"] = copy.deepcopy(result)
                job["result"]["last_test"] = self._trial_summary(result, job["test_task"])
            return self.status(editor_id)
        finally:
            try:
                if trial_signalled:
                    try: self._trial_signal(job, "finished", job["result"].get("last_test", {}))
                    except (OperationError, OSError):
                        # The editor may close while its run is being cancelled.
                        # Preserve the actual run error/result and always release
                        # ownership; a closed helper cannot receive more input.
                        pass
            finally: job["interaction_lock"].release()

    def _visual(self, job, mode, arguments, timeout):
        """Owned visible helper; bound scope, nonces, bounded reads and cleanup."""
        self._check(job)
        def check_hosted():
            for (pid, hwnd), identity in job.get("hosted_targets", {}).items():
                if identity is not None and self._hosted_identity(job["runtime"], {"pid": pid, "window_id": hwnd}) != identity:
                    raise OperationError("선택하거나 녹화하는 동안 대상 창의 앱이 바뀌었습니다. 현재 앱으로 다시 연결하세요.", "editor_target_changed")
        check_hosted()
        helper = Path(__file__).resolve().with_name(VISUAL_HELPER_NAME)
        self._check_path(helper)
        if not helper.is_file():
            raise OperationError("이미지·녹화 도구가 없습니다. 새 배포 ZIP 전체를 풀어 실행하세요.", "visual_helper_missing")
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        folder = Path(job["runtime"].run_dir) / "visual"
        request, response = (folder / (nonce[:16] + suffix) for suffix in (".req.json", ".res.json"))
        ready = Path(str(response) + ".ready.json")
        progress = Path(str(response) + ".progress.json")
        child = None
        try:
            self._check_path(request); folder.mkdir(parents=True, exist_ok=True)
            self._write(request, {"nonce": nonce, "timeout_seconds": timeout, **arguments})
            child = subprocess.Popen([str(helper), "--" + mode, str(request), str(response)], cwd=str(helper.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            started, visible = time.monotonic(), False
            progress_modified, progress_at, ready_at = None, None, None
            while True:
                check_hosted()
                self._check(job)
                if not visible and ready.exists():
                    shown = self._read(ready, nonce)
                    if (shown.get("status") != "ready" or shown.get("helper_pid") != child.pid
                            or type(shown.get("helper_window_id")) is not int
                            or shown["helper_window_id"] <= 0):
                        raise OperationError("이미지·녹화 창 표시를 확인하지 못했습니다.", "visual_helper_not_visible")
                    # Capture temporarily hides the picker; WinForms may also
                    # recreate its HWND on the first DPI transition. Never
                    # acknowledge an invisible/foreign window, but allow the
                    # owned helper to publish its next visible-ready snapshot.
                    visible = _helper_visible(child.pid, shown["helper_window_id"])
                    if visible: ready_at = time.monotonic()
                if mode == "record" and visible and progress.exists():
                    modified = progress.stat().st_mtime_ns
                    if modified != progress_modified:
                        update = self._recording_progress(self._read(progress, nonce, maximum_bytes=16384), child.pid)
                        progress_modified, progress_at = modified, time.monotonic()
                        with self.lock: job["result"]["recording"] = update
                    elif progress_at is not None and time.monotonic() - progress_at > 5 and not response.exists():
                        raise OperationError("녹화 상태 응답이 끊겨 기록 도구를 중지했습니다. 완료로 간주하지 마세요.", "recording_heartbeat_timeout")
                if response.exists():
                    answer = self._read(response, nonce, maximum_bytes=16 * 1024 * 1024)
                    if answer.get("status") in {"cancelled", "timeout"}:
                        raise OperationError("이미지 선택 또는 녹화를 취소했습니다. 기존 초안은 유지됩니다.", "visual_cancelled")
                    if (not visible or answer.get("status") != ("selected" if mode == "pick" else "recorded")
                            or answer.get("human_confirmed") is not True):
                        raise OperationError("이미지·녹화 결과를 확정하지 못했습니다: " + str(answer.get("code", "visual_invalid_response"))[:100],
                                             "visual_invalid_response")
                    if mode == "record" and progress_at is None:
                        raise OperationError("녹화 진행 상태를 한 번도 확인하지 못했습니다. 전체 배포 파일을 갱신하세요.", "recording_progress_missing")
                    if mode == "record": self._retain_recording(job, answer)
                    return answer
                if child.poll() is not None:
                    raise OperationError("이미지·녹화 창이 결과 없이 종료되었습니다.", "visual_helper_closed")
                elapsed = time.monotonic() - started
                if not visible and elapsed > 8:
                    raise OperationError("이미지·녹화 창을 표시하지 못했습니다.", "visual_helper_start_timeout")
                if mode == "record" and ready_at is not None and progress_at is None and time.monotonic() - ready_at > 5:
                    raise OperationError("녹화 창은 표시됐지만 시작 점검 상태를 받지 못했습니다.", "recording_progress_missing")
                if elapsed > timeout + 5:
                    raise OperationError("이미지·녹화 대기 시간이 지났습니다.", "visual_timeout")
                job["cancel"].event.wait(.05)
        except Exception as error:
            if mode == "record":
                with self.lock:
                    previous = job["result"].get("recording", {})
                    job["result"]["recording"] = {**previous, "state": "cancelled" if getattr(error, "code", "") == "visual_cancelled" else "failed", "diagnostic_code": getattr(error, "code", "recording_failed"), "task_verified": False}
            raise
        finally:
            try:
                _end_helper(child)
            finally:
                for path in (request, response, ready, progress):
                    try:
                        self._check_path(path); path.unlink(missing_ok=True)
                    except (OSError, OperationError): pass

    @staticmethod
    def _recording_progress(value, pid):
        states = {"checking", "ready", "recording", "paused", "outside_target", "review", "failed"}
        if (type(value.get("helper_pid")) is not int or value.get("helper_pid") != pid or not isinstance(value.get("state"), str) or value.get("state") not in states
                or any(type(value.get(k)) is not int or not 0 <= value[k] <= 15 for k in ("event_count", "manual_count", "max_events"))
                or not 1 <= value["max_events"] <= 15 or value["manual_count"] > value["event_count"] or value["event_count"] > value["max_events"]
                or not isinstance(value.get("warning_codes"), list) or len(value["warning_codes"]) > 30
                or any(not isinstance(code, str) or code not in RECORDING_WARNINGS for code in value["warning_codes"])):
            raise OperationError("녹화 진행 응답을 확인하지 못했습니다.", "recording_invalid_progress")
        probe = value.get("probe")
        if not isinstance(probe, dict) or set(probe) != {"hooks", "uia"} or any(not isinstance(v, str) or v not in {"checking", "available", "unavailable", "timeout"} for v in probe.values()):
            raise OperationError("녹화 시작 점검 결과를 확인하지 못했습니다.", "recording_invalid_progress")
        result = {key: copy.deepcopy(value[key]) for key in ("state", "event_count", "manual_count", "max_events", "warning_codes", "probe")}
        last = value.get("last_event")
        if isinstance(last, dict) and last.get("operation") in {*IMAGE_ACTIONS, "manual_entry", "select_option", "set_checked"}:
            result["last_event"] = {"operation": last["operation"], "recognition": "uia_candidate" if last.get("recognition") == "uia_candidate" else "image_or_manual"}
        result.update(hover_is_action=False, task_verified=False)
        return result

    @staticmethod
    def _check_path(path):
        if not path.is_absolute():
            raise OperationError("편집 교환 경로는 전체 경로여야 합니다.", "editor_unsafe_path")
        for parent in reversed(path.parents):
            ElementLibrary._reject_link(parent)
        ElementLibrary._reject_link(path, file=True)

    @classmethod
    def _write(cls, path, data):
        cls._check_path(path)
        # Each random exchange path has exactly one Python writer. A fixed,
        # exclusive temporary suffix avoids exceeding legacy Windows MAX_PATH.
        temporary = path.with_name(path.name + ".tmp")
        cls._check_path(temporary)
        created = False
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                created = True
                json.dump(data, stream, ensure_ascii=False)
            deadline = time.monotonic() + .5
            while True:
                try:
                    cls._check_path(path)
                    cls._check_path(temporary)
                    os.replace(temporary, path)
                    break
                except OSError as exc:
                    # Windows readers can briefly deny an atomic replacement.
                    # Retry only this same file, never the accepted command.
                    if not cls._sharing_error(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _sharing_error(exc):
        return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {32, 33}

    @classmethod
    def _read(cls, path, nonce, maximum_bytes=2 * 1024 * 1024):
        deadline = time.monotonic() + .5
        while True:
            try:
                cls._check_path(path)
                if path.stat().st_size > maximum_bytes:
                    raise OperationError("편집 응답이 너무 큽니다.", "editor_invalid_response")
                raw = path.read_text(encoding="utf-8-sig")
                break
            except OSError as exc:
                # ReplaceFile can also briefly leave the name unresolved;
                # absence is retried for reads only, within the same bound.
                if not (cls._sharing_error(exc) or isinstance(exc, FileNotFoundError)) or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("nonce") != nonce:
            raise OperationError("편집 창의 실행 정보를 확인하지 못했습니다.", "editor_invalid_response")
        return data

    def _work(self, job):
        child = None
        runtime = job["runtime"]
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        folder = runtime.run_dir / "process-editor"
        # Keep the full nonce inside messages; shorter random filenames leave
        # room for atomic-write suffixes on legacy Windows MAX_PATH runtimes.
        request = folder / (nonce[:16] + ".request.json")
        response = folder / (nonce[:16] + ".response.json")
        ready, command, event = [Path(str(response) + suffix) for suffix in (".ready.json", ".command.json", ".event.json")]
        trial, trial_ready = Path(str(response) + ".trial.json"), Path(str(response) + ".trial.ready.json")
        job["exchange"] = {"nonce": nonce, "trial": trial, "trial_ready": trial_ready}
        try:
            self._check(job)
            helper = Path(__file__).resolve().with_name(HELPER_NAME)
            if not helper.is_file():
                raise OperationError("프로세스 편집 실행파일이 없습니다. 새 배포 ZIP 전체를 풀어 실행하세요.", "editor_missing")
            self._check_path(request)
            folder.mkdir(parents=True, exist_ok=True)
            timeout = job["args"].get("timeout_seconds", 600)
            self._write(request, {"nonce": nonce, "timeout_seconds": timeout, "programs": job["draft"].programs,
                                 "visual_review_enabled": job.get("visual_review_enabled", False),
                                 "draft": {"name": job["name"], "description": job["description"], "steps": job["summaries"], "recording_review": job["draft"].recording_review()}})
            child = subprocess.Popen([str(helper), "--edit", str(request), str(response)], cwd=str(helper.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            started, sequence = time.monotonic(), 0
            visible = False
            while True:
                self._check(job)
                if not visible and ready.exists():
                    info = self._read(ready, nonce)
                    if (info.get("status") != "ready" or info.get("helper_pid") != child.pid
                            or type(info.get("helper_window_id")) is not int
                            or not _helper_visible(child.pid, info["helper_window_id"])):
                        raise OperationError("프로세스 편집 창 표시를 확인하지 못했습니다.", "editor_not_visible")
                    visible = True
                    with self.lock:
                        job["result"] = {"status": "editing", "editor_visible": True,
                            "helper_pid": child.pid, "helper_window_id": info["helper_window_id"],
                            "next_tool": "computer_process_status", "message": "편집 창에서 요소와 동작을 단계로 추가한 뒤 저장하세요. 직접 요소 선택은 입력을 보내지 않습니다. 동작 녹화 중 사용자의 클릭·입력은 실제 프로그램에 적용됩니다."}
                    job["started"].set()
                if command.exists():
                    data = self._read(command, nonce)
                    seq = data.get("seq")
                    if type(seq) is not int or seq < 1 or seq > sequence + 1:
                        raise OperationError("편집 요청 순서를 확인하지 못했습니다.", "editor_invalid_sequence")
                    if seq == sequence + 1:
                        if not visible or job["result"].get("status") == "saved":
                            raise OperationError("편집 가능한 상태가 아닙니다.", "editor_invalid_state")
                        try:
                            with job["interaction_lock"]:
                                answer = self._command(job, data)
                        except Exception as exc:
                            # A failed nested-picker cleanup is terminal. Do
                            # not turn it into a recoverable form error that
                            # could launch a second picker over the orphan.
                            if getattr(exc, "helper_cleanup_pending", False):
                                raise
                            with self.lock:
                                if job["result"].get("status") != "saved":
                                    job["save_started"] = False
                            self._check(job)
                            answer = {"status": "error", "message": str(exc), "steps": job["draft"].summaries(),
                                      "code": getattr(exc, "code", "editor_command_failed"),
                                      "recording_receipt_available": bool(job.get("last_recording"))}
                        self._write(event, {"nonce": nonce, "seq": seq, **answer})
                        sequence = seq
                if response.exists():
                    final = self._read(response, nonce)
                    if job["result"].get("status") != "saved":
                        if final.get("status") not in {"cancelled", "timeout"}:
                            raise OperationError("프로세스 저장 확인 없이 편집 창이 종료되었습니다.", "editor_unsaved")
                        raise OperationError("저장하지 않고 프로세스 만들기를 종료했습니다.", "editor_cancelled")
                    break
                if child.poll() is not None:
                    if job["result"].get("status") == "saved":
                        break
                    raise OperationError("프로세스 편집 창이 응답 없이 종료되었습니다.", "editor_closed")
                if not visible and time.monotonic() - started > 8:
                    raise OperationError("8초 안에 프로세스 편집 창을 표시하지 못했습니다.", "editor_start_timeout")
                if time.monotonic() - started > timeout + 3:
                    raise OperationError("프로세스 작성 시간이 지나 종료했습니다.", "editor_timeout")
                job["cancel"].event.wait(.05)
        except Exception as exc:
            with self.lock:
                if job["result"].get("status") != "saved":
                    job["result"] = {"status": "cancelled" if getattr(exc, "code", "") == "editor_cancelled" else "failed",
                        "message": str(exc), "diagnostic": {"code": getattr(exc, "code", "editor_failed")}, "editor_visible": False}
                    if getattr(exc, "helper_cleanup_pending", False):
                        job["result"].update(cleanup_pending=True, picker_cleanup_pending=True)
        finally:
            try:
                _end_helper(child)
            except Exception:
                with self.lock:
                    job["result"]["cleanup_pending"] = True
                    job["result"]["editor_visible"] = None
            else:
                with self.lock:
                    job["result"]["editor_visible"] = False
            for path in (request, response, ready, command, event, trial, trial_ready):
                try:
                    self._check_path(path)
                    path.unlink(missing_ok=True)
                except (OSError, OperationError):
                    pass
            job["done"].set()
            job["started"].set()

    def status(self, editor_id, *, wait_ms=0, cancel=False):
        if type(wait_ms) is not int or not 0 <= wait_ms <= 5000 or type(cancel) is not bool:
            raise OperationError("상태 대기 시간은 0~5000ms로 지정하세요.")
        with self.lock:
            job = self.jobs.get(editor_id)
            if job is None:
                raise OperationError("이 연결의 프로세스 편집 ID를 찾지 못했습니다.", "editor_not_found")
            if cancel:
                job["cancel"].set()
        job["done"].wait(wait_ms/1000)
        result = self._view(job, **({"status": "cancelling", "save_already_started": bool(job.get("save_started")),
            "message": "저장이 이미 시작되어 결과를 확인한 뒤 창을 닫습니다." if job.get("save_started") else "저장하지 않고 편집 창을 닫고 있습니다."}
            if cancel and not job["done"].is_set() else {}))
        tested = job.get("test_result_full", {})
        # The facade consumes this privately to open the local human review
        # window; the central delivery filter excludes it from text clients.
        if "checkpoint_content" in tested:
            result["checkpoint_content"] = copy.deepcopy(tested["checkpoint_content"])
        if callable(getattr(self, "visual_review_enabled", None)) and self.visual_review_enabled(job["runtime"]) is True:
            for key in ("observation_content", "visual_review"):
                if key in tested: result[key] = copy.deepcopy(tested[key])
        return result

    def stop(self, runtime=None):
        with self.lock:
            for job in self.jobs.values():
                if runtime is None or job["runtime"] is runtime:
                    job["cancel"].set()

    def close(self, timeout=3):
        self.stop()
        deadline = time.monotonic() + timeout
        with self.lock:
            workers = [job["worker"] for job in self.jobs.values()]
        for worker in workers:
            worker.join(max(0, deadline-time.monotonic()))
        return self.pending() is None
