"""Declarative process authoring and native IPC; business programs receive no input."""
import copy
import base64
import gc
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import struct
import zlib
from unittest import mock

from operations import OperationError
from process_editor import ProcessDraft, ProcessEditors, task_view
from task_inputs import MAX_RECIPE_STEPS, MAX_RECORDING_EVENTS, MAX_RECORDED_WINDOWS
from server import ComputerManager
from test_learning import TARGET
from test_learning_tools import MutableRuntime, FIELD_SELECTOR
from test_server import config_at
from test_teaching_sessions import native_choice
from vendor.guard import atomic_json


def image_choice(**extra):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\x00" + bytes(range(24))) * 8)) + chunk(b"IEND", b"")
    return {**TARGET, "human_confirmed": True, "status": "selected", "template_png": base64.b64encode(raw).decode(),
            "width": 8, "height": 8, "anchor": {"x": .5, "y": .5}, "capture_window": {"width": 800, "height": 600}, **extra}


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.program = {"program_id": "editor", **TARGET, "label": "시험 앱"}
        self.draft = ProcessDraft([self.program])
        snapshot = {**TARGET, "elements": [
            {"element_index": 0, "role": "Pane", "automation_id": "filters", "name": "조건", "actions": []},
            {"element_index": 3, "parent_index": 0, "role": "Edit", "automation_id": "applicant", "name": "신청자",
             "value": "PRIVATE", "element_token": "TRANSIENT", "actions": ["set_value"]}]}
        self.selection = self.draft.remember(self.program, snapshot, native_choice())
        self.base = {**TARGET, "program_id": "editor", "selection_id": self.selection["selection_id"]}

    def add(self, action, **extra):
        self.draft.add({**self.base, "action": action, **extra})

    def test_selected_identity_only_is_used_for_saved_step(self):
        self.add("set_value", value="user-supplied")
        step = self.draft.steps[0]
        self.assertEqual(step["selector"], FIELD_SELECTOR)
        self.assertEqual(step["value"], "user-supplied")
        serialized = json.dumps(self.draft.steps)
        for forbidden in ("element_index", "parent_index", "element_token", "window_id", '"pid"', "TRANSIENT", "PRIVATE", "selection_id"):
            self.assertNotIn(forbidden, serialized)

    def test_thousand_unicode_previews_fit_ipc_without_truncating_saved_inputs(self):
        original = "복잡한 업무 조건😀" * 1000
        self.draft.steps = [{"operation": "set_value", "program_id": "editor", "value": original,
            "selector": {"name": "입력 대상😀" * 200},
            "expect": [{"selector": {"name": "확인 대상😀" * 200}, "property": "value", "equals": original}]} for _ in range(MAX_RECIPE_STEPS)]
        self.draft.labels = ["선택한 요소😀" * 200] * MAX_RECIPE_STEPS
        self.draft.programs[0]["label"] = "프로그램😀" * 200
        self.draft.targets[("editor", "main")]["label"] = self.draft.programs[0]["label"]
        summaries = self.draft.summaries()
        payload = {"nonce": "a" * 64, "timeout_seconds": 600, "max_steps": MAX_RECIPE_STEPS,
            "programs": self.draft.program_views(), "draft": {"name": "large task", "description": "", "steps": summaries,
                "recording_review": self.draft.recording_review()}}
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.assertLess(len(encoded), 2 * 1024 * 1024)
        self.assertEqual(len(summaries), 1000)
        self.assertTrue(summaries[-1]["detail"].endswith("…"))
        self.assertEqual(self.draft.steps[-1]["value"], original)
        self.assertEqual(self.draft.steps[-1]["expect"][0]["equals"], original)

    def test_direct_selector_or_handle_cannot_override_human_selection(self):
        for extra in ({"selector": {"name": "other"}}, {"element_token": "foreign"}, {"element_index": 2}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                self.add("set_value", value="x", **extra)
        self.assertEqual(self.draft.steps, [])

    def test_target_and_selection_must_belong_to_bound_window(self):
        for extra in ({"pid": 99}, {"window_id": 99}, {"window_ref": "other"}, {"selection_id": "not-observed"}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                self.add("set_value", value="x", **extra)
        self.assertEqual(self.draft.steps, [])

    def test_click_and_keys_need_result_condition_selected_by_human(self):
        for action, parameters in (("click", {}), ("press_key", {"key": "Enter"}), ("hotkey", {"keys": ["ctrl", "s"]})):
            with self.subTest(action=action), self.assertRaises(OperationError):
                self.add(action, **parameters)
            self.add(action, expect={"selection_id": self.selection["selection_id"], "property": "value", "equals": "done"}, **parameters)
        self.assertEqual(len(self.draft.steps), 3)
        for step in self.draft.steps:
            self.assertEqual(step["expect"][0]["selector"], FIELD_SELECTOR)

    def test_delay_and_element_wait_use_seconds_and_require_no_raw_handles(self):
        self.add("delay", seconds=1.25)
        self.add("wait_for_element", timeout_seconds=12.5)
        self.assertEqual(self.draft.steps[0]["duration_ms"], 1250)
        self.assertNotIn("selector", self.draft.steps[0])
        self.assertEqual(self.draft.steps[1]["timeout_ms"], 12500)
        for seconds in (-1, 61, True, float("nan"), float("inf"), "2"):
            with self.subTest(seconds=seconds), self.assertRaises(OperationError):
                self.add("delay", seconds=seconds)
        self.assertEqual(len(self.draft.steps), 2)

    def test_checkpoint_is_review_step_not_claim_of_verification(self):
        self.add("checkpoint", value="결과 화면을 확인하세요.")
        self.assertEqual(self.draft.steps[0], {"operation": "checkpoint", "program_id": "editor", "window_ref": "main",
                                              "message": "결과 화면을 확인하세요."})

    def test_reorder_and_remove_preserve_matching_labels(self):
        self.add("set_value", value="first")
        self.add("delay", seconds=2)
        self.draft.change("move_step", {"index": 1, "direction": -1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["delay", "set_value"])
        self.assertEqual([s["label"] for s in self.draft.summaries()], ["현재 창", "신청자"])
        self.draft.change("remove_step", {"index": 0})
        self.assertEqual(self.draft.steps[0]["value"], "first")
        with self.assertRaises(OperationError):
            self.draft.change("move_step", {"index": 0, "direction": -1})

    def test_existing_task_is_copied_and_requires_all_window_bindings(self):
        self.add("set_value", value="first")
        original = {"steps": copy.deepcopy(self.draft.steps), "variables": {}}
        draft = ProcessDraft([self.program], original)
        draft.steps[0]["value"] = "edited"
        self.assertEqual(original["steps"][0]["value"], "first")
        original["steps"][0]["window_ref"] = "detail"
        with self.assertRaises(OperationError):
            ProcessDraft([self.program], original)

    def test_edit_value_preserves_other_steps_and_updates_its_own_verification(self):
        self.add("set_value", value="W", expect={"selection_id": self.selection["selection_id"], "property": "value", "equals": "W"})
        self.add("delay", seconds=2)
        original = copy.deepcopy(self.draft.steps)
        self.draft.edit({"index": 0, "value": "N"})
        self.assertEqual(self.draft.steps[0]["value"], "N")
        self.assertEqual(self.draft.steps[0]["expect"][0]["equals"], "N")
        self.assertEqual(self.draft.steps[0]["selector"], original[0]["selector"])
        self.assertEqual(self.draft.steps[1:], original[1:])

    def test_edit_variable_binds_input_and_result_and_has_default(self):
        self.add("set_value", value="W", expect={"selection_id": self.selection["selection_id"], "property": "value", "equals": "W"})
        self.draft.edit({"index": 0, "value": "N", "variable": "condition", "variable_label": "조회 조건"})
        self.assertEqual(self.draft.steps[0]["value"], "${condition}")
        self.assertEqual(self.draft.steps[0]["expect"][0]["equals"], "${condition}")
        self.assertEqual(self.draft.variables["condition"]["default"], "N")
        self.assertEqual(self.draft.variables["condition"]["description"], "조회 조건")
        self.draft.edit({"index": 0, "value": "W", "variable": ""})
        self.assertEqual(self.draft.steps[0]["value"], "W")
        self.assertEqual(self.draft.steps[0]["expect"][0]["equals"], "W")

    def test_invalid_partial_edit_rolls_back_both_steps_and_variables(self):
        self.add("set_value", value="W")
        before = copy.deepcopy(self.draft.steps)
        for change in ({"value": 42}, {"seconds": 10}, {"variable": "not allowed"}, {"selector": {"name": "unobserved"}}):
            with self.subTest(change=change), self.assertRaises(OperationError):
                self.draft.edit({"index": 0, **change})
            self.assertEqual(self.draft.steps, before)
            self.assertEqual(self.draft.variables, {})

    def test_retarget_uia_input_updates_self_check_without_changing_value(self):
        self.add("set_value", value="W", expect={"selection_id": self.selection["selection_id"], "property": "value", "equals": "W"})
        original = copy.deepcopy(self.draft.steps[0])
        self.draft.selections[self.selection["selection_id"]]["selector"] = {"automation_id": "replacement", "role": "Edit"}
        self.draft.repair(0, self.selection)
        self.assertEqual(self.draft.steps[0]["value"], "W")
        self.assertEqual(self.draft.steps[0]["selector"]["automation_id"], "replacement")
        self.assertEqual(self.draft.steps[0]["expect"][0]["selector"], self.draft.steps[0]["selector"])
        self.assertNotEqual(self.draft.steps[0]["selector"], original["selector"])

    def test_completion_removes_only_paired_deferred_checkpoint(self):
        self.add("click", completion_mode="human")
        self.add("delay", seconds=2)
        tail = copy.deepcopy(self.draft.steps[2])
        self.draft.completion({"index": 0, "selection_id": self.selection["selection_id"], "property": "value", "equals": "done"})
        self.assertEqual(len(self.draft.steps), 2)
        self.assertNotIn("completion_mode", self.draft.steps[0])
        self.assertEqual(self.draft.steps[0]["expect"][0]["equals"], "done")
        self.assertEqual(self.draft.steps[1], tail)

    def test_completion_preserves_explicit_checkpoint_after_verified_input(self):
        self.add("set_value", value="W")
        self.add("checkpoint", value="추가 업무 확인")
        self.draft.completion({"index": 0, "selection_id": self.selection["selection_id"], "property": "value", "equals": "W"})
        self.assertEqual(len(self.draft.steps), 2)
        self.assertEqual(self.draft.steps[1]["message"], "추가 업무 확인")

    def test_visual_completion_is_explicit_and_existing_checks_remain_human(self):
        self.add("click", completion_mode="human")
        self.add("click", completion_mode="visual")
        self.assertNotIn("review_mode", self.draft.steps[1])
        self.assertEqual(self.draft.steps[3]["review_mode"], "visual")

    def test_existing_repeat_group_survives_editor_reorder_and_save_draft(self):
        task = {"id": "a" * 32, "variables": {"devices": {"type": "list", "items": {"type": "text"}, "default": ["first", "second"]}},
                "steps": [{"operation": "foreach", "step_id": "device-group", "input": "devices", "item": "device", "steps": [
                    {"operation": "set_value", "program_id": "editor", "selector": FIELD_SELECTOR, "value": "${device}"}]},
                    {"operation": "delay", "program_id": "editor", "duration_ms": 100}]}
        draft = ProcessDraft([self.program], task)
        group = copy.deepcopy(draft.steps[0])
        self.assertEqual(draft.summaries()[0]["action_label"], "목록 반복")
        draft.change("move_step", {"index": 1, "direction": -1})
        self.assertEqual(draft.steps[1], group)
        draft.validate()


class RecordingSemanticTests(unittest.TestCase):
    def setUp(self):
        self.program = {"program_id": "editor", **TARGET, "label": "시험 앱"}
        self.draft = ProcessDraft([self.program])

    def event(self, operation="set_value", value="final"):
        role, actions, prop = {"set_value": ("Edit", ["set_value"], "value"),
                               "select_option": ("ComboBox", ["select", "expand"], "value"),
                               "set_checked": ("CheckBox", ["toggle"], "selected")}[operation]
        native = native_choice(); native["element"].update(role=role)
        event = {**image_choice(), "program_id": "editor", "operation": operation,
                 "native_target": native, "after": {"property": prop, "equals": value},
                 "checked" if operation == "set_checked" else "value": value}
        snapshot = {**TARGET, "elements": [{"element_index": 1, "role": role, "automation_id": "applicant",
                     "name": "신청자", "actions": actions, prop: value, "bounds": native["bounds"]}]}
        return event, {("editor", "main"): snapshot}

    def test_verified_edit_combo_and_checkbox_become_semantic_steps_with_expected_final_state(self):
        for operation, value in (("set_value", "final"), ("select_option", "A"), ("set_checked", True)):
            event, snapshots = self.event(operation, value)
            self.draft.recorded([event], snapshots=snapshots)
            step = self.draft.steps[-1]
            self.assertEqual(step["operation"], operation)
            self.assertEqual(step["expect"][0]["equals"], value)
            self.assertEqual(step["selector"]["automation_id"], "applicant")
            for forbidden in ("native_target", "point", "pid", "window_id", "template_png"):
                self.assertNotIn(forbidden, step)
        self.assertEqual(len(self.draft.steps), 3)
        self.draft.validate()

    def test_missing_stale_ambiguous_unsupported_or_protected_semantics_never_replay_recorded_value(self):
        for failure in ("missing", "stale", "ambiguous", "unsupported", "protected"):
            draft = ProcessDraft([self.program]); event, snapshots = self.event(value="SENSITIVE_RECORDED_VALUE")
            element = snapshots[("editor", "main")]["elements"][0]
            if failure == "missing": snapshots = {}
            elif failure == "stale": element["value"] = "different"
            elif failure == "ambiguous": snapshots[("editor", "main")]["elements"].append({**element, "element_index": 2})
            elif failure == "unsupported": element["actions"] = []
            elif failure == "protected": element["is_password"] = True
            with self.subTest(failure=failure):
                draft.recorded([event], snapshots=snapshots)
                self.assertEqual(draft.steps[0]["operation"], "manual_entry")
                self.assertNotIn("SENSITIVE_RECORDED_VALUE", json.dumps(draft.steps))
                self.assertIn("semantic_target_unverified", draft.recording_warnings)
                with self.assertRaises(OperationError): draft.validate()

    def test_plain_click_keeps_image_and_checkpoint_without_inventing_a_postcondition(self):
        self.draft.recorded([{**image_choice(), "program_id": "editor", "operation": "click"}])
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "checkpoint"])
        self.assertNotIn("expect", self.draft.steps[0])

    def test_native_toggle_button_keeps_real_pointer_click_when_driver_has_no_semantic_target(self):
        event, snapshots = self.event("set_checked", True)
        event["native_target"]["element"]["role"] = "Button"
        snapshots[("editor", "main")]["elements"][0].update(role="Button", actions=["click"])
        event.update(requested_operation="click", pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": True})
        self.draft.recorded([event], snapshots=snapshots, review_mode="visual")
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "checkpoint"])
        self.assertEqual(self.draft.steps[0]["image_target"]["template_png"], event["template_png"])
        self.assertNotIn("checked", self.draft.steps[0]); self.assertNotIn("expect", self.draft.steps[0])
        self.assertEqual(self.draft.steps[1]["review_mode"], "visual")
        self.assertEqual(self.draft.recording_import_diagnostics[0]["fallback"], "recorded_image_click")
        self.assertEqual(self.draft.recording_warnings, [])
        self.assertNotIn("신청자", json.dumps(self.draft.recording_import_diagnostics, ensure_ascii=False))

    def test_toggle_fallback_requires_own_preinput_pixels_and_cannot_bypass_protected_control(self):
        for failure in ("no_proof", "not_preinput", "no_pixels", "protected", "drag", "keyboard"):
            event, snapshots = self.event("set_checked", True)
            event.update(requested_operation="click", pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": True})
            snapshots[("editor", "main")]["elements"][0]["actions"] = []
            if failure == "no_proof": event.pop("pointer_evidence")
            elif failure == "not_preinput": event["pointer_evidence"]["before_input"] = False
            elif failure == "no_pixels": event.pop("template_png")
            elif failure == "protected": snapshots[("editor", "main")]["elements"][0]["is_password"] = True
            elif failure == "drag": event["reason"] = "drag_requires_manual_setup"
            else: event["input_method"] = "keyboard_unverified"
            draft = ProcessDraft([self.program])
            with self.subTest(failure=failure):
                draft.recorded([event], snapshots=snapshots)
                self.assertEqual(draft.steps[0]["operation"], "manual_entry")

    def test_historical_checked_value_survives_later_recorded_radio_side_effect(self):
        first, snapshots = self.event("set_checked", True)
        second = copy.deepcopy(first)
        second["native_target"]["element"].update(automation_id="second", name="Second")
        first["event_evidence"] = {"source": "uia_recording", "identity": "first-runtime", "after_input": True}
        second["event_evidence"] = {"source": "uia_recording", "identity": "second-runtime", "after_input": True}
        for event in (first, second):
            event.update(requested_operation="click", pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": True})
        element = snapshots[("editor", "main")]["elements"][0]
        snapshots[("editor", "main")]["elements"].append({**element, "element_index": 2, "automation_id": "second", "name": "Second"})
        element["selected"] = False
        self.draft.recorded([first, second], snapshots=snapshots)
        self.assertEqual([s["operation"] for s in self.draft.steps], ["set_checked", "set_checked"])
        self.assertTrue(all(s["checked"] is True and s["expect"][0]["equals"] is True for s in self.draft.steps))

    def test_failed_native_semantics_preserve_actual_capture_failure_without_promoting_rejected_pixels(self):
        event, snapshots = self.event("set_checked", True)
        event.pop("template_png")
        event.update(capture_issue="template_low_detail", rejected_capture={"png": "PRIVATE_REJECTED_PIXELS"},
            requested_operation="click", pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": False})
        self.draft.recorded([event], snapshots={})
        self.assertEqual(self.draft.steps[0]["manual_reason"], "template_low_detail")
        self.assertEqual(self.draft.steps[0]["repair_action"], "click")
        self.assertEqual(self.draft.recording_import_diagnostics[0]["capture_issue"], "template_low_detail")
        self.assertNotIn("PRIVATE_REJECTED_PIXELS", json.dumps(self.draft.steps))
        self.assertNotIn("PRIVATE_REJECTED_PIXELS", json.dumps(self.draft.recording_import_diagnostics))
        self.assertNotIn("image_target", self.draft.steps[0])
        with self.assertRaises(OperationError): self.draft.validate()

    def test_explicit_target_repairs_clear_only_the_associated_warning_after_last_step(self):
        event, _ = self.event("set_checked", True)
        event.pop("template_png")
        event.update(requested_operation="click", capture_issue="template_low_detail")
        with mock.patch.object(self.draft, "_semantic_step", side_effect=OperationError("missing target", "picker_not_found")):
            self.draft.recorded([event, copy.deepcopy(event)])
        ids = [self.draft.steps[i]["step_id"] for i in (0, 2)]
        self.assertNotEqual(*ids)
        selection = self.draft.remember_image(self.program, image_choice())
        self.draft.repair(0, selection, action="click")
        self.assertEqual(self.draft.recording_warnings, ["semantic_target_unverified"])
        self.assertFalse(self.draft.recording_acknowledged)
        with self.assertRaises(OperationError): self.draft.validate()
        self.draft.repair(2, selection, action="click")
        self.assertEqual(self.draft.recording_warnings, [])
        self.assertFalse(self.draft.recording_acknowledged, "Target repair is not a fabricated warning acknowledgement")
        self.assertFalse(self.draft.recording_review()["partial"])
        self.draft.validate()

    def test_target_repair_preserves_missing_keyboard_and_inherited_semantic_warnings(self):
        for warning in ("outside_target_not_recorded", "recording_event_limit", "recording_timeout_partial",
                        "recording_keyboard_failed", "recording_method_unverified", "semantic_target_unverified"):
            with self.subTest(warning=warning):
                draft = ProcessDraft([self.program])
                event, _ = self.event("set_checked", True)
                event.pop("template_png"); event["requested_operation"] = "click"
                with mock.patch.object(draft, "_semantic_step", side_effect=OperationError("missing target", "picker_not_found")):
                    draft.recorded([event], warning_codes=[warning])
                chosen = draft.remember_image(self.program, image_choice())
                draft.repair(0, chosen, action="click")
                self.assertEqual(draft.recording_warnings, [warning])
                self.assertFalse(draft.recording_acknowledged)
                with self.assertRaises(OperationError) as failed: draft.validate()
                self.assertEqual(failed.exception.code, "recording_review_required")

    def test_target_repair_does_not_resolve_unrelated_unverified_value_or_deleted_issue(self):
        for mode in ("value", "deleted", "missing_native"):
            with self.subTest(mode=mode):
                draft = ProcessDraft([self.program]); event, _ = self.event("set_checked", True)
                event.pop("template_png"); event["requested_operation"] = "click"
                with mock.patch.object(draft, "_semantic_step", side_effect=OperationError("missing target", "picker_not_found")):
                    draft.recorded([event])
                if mode == "value":
                    with mock.patch.object(draft, "_semantic_step", side_effect=OperationError("unknown value", "recording_final_value_unconfirmed")):
                        draft.recorded([event])
                elif mode == "missing_native":
                    other = copy.deepcopy(event); other.pop("native_target"); draft.recorded([other])
                else:
                    with mock.patch.object(draft, "_semantic_step", side_effect=OperationError("missing target", "picker_not_found")):
                        draft.recorded([event])
                    draft.change("remove_step", {"index": 2})
                chosen = draft.remember_image(self.program, image_choice())
                draft.repair(0, chosen, action="click")
                self.assertIn("semantic_target_unverified", draft.recording_warnings)

    def test_historical_value_without_owned_after_input_evidence_is_still_unverified(self):
        event, snapshots = self.event("set_checked", True)
        snapshots[("editor", "main")]["elements"][0]["selected"] = False
        later = {**image_choice(), "program_id": "editor", "operation": "click"}
        self.draft.recorded([event, later], snapshots=snapshots)
        self.assertEqual(self.draft.steps[0]["operation"], "manual_entry")

    def test_typed_combo_final_value_does_not_invent_the_successful_input_method(self):
        event, snapshots = self.event("select_option", "typed result")
        event["input_method"] = "keyboard_unverified"
        self.draft.recorded([event], snapshots=snapshots)
        self.assertEqual(self.draft.steps[0]["operation"], "manual_entry")
        self.assertEqual(self.draft.steps[0]["manual_reason"], "recording_method_unverified")
        self.assertNotIn("typed result", json.dumps(self.draft.steps))
        self.assertEqual(self.draft.recording_warnings, ["recording_method_unverified"])
        self.draft.recording_acknowledged = True
        with self.assertRaises(OperationError): self.draft.validate()

    def test_partial_recording_warnings_require_explicit_review_and_survive_reopen(self):
        self.draft.recorded([{**image_choice(), "program_id": "editor", "operation": "click"}],
                            warning_codes=["outside_target_not_recorded", "uia_text_unavailable"])
        with self.assertRaises(OperationError) as failure: self.draft.validate()
        self.assertEqual(failure.exception.code, "recording_review_required")
        self.draft.recording_acknowledged = True; self.draft.validate()
        review = {k: self.draft.recording_review()[k] for k in ("partial", "warning_codes", "acknowledged")}
        reopened = ProcessDraft([self.program], {"steps": self.draft.steps, "recording_review": review})
        self.assertEqual(reopened.recording_warnings, self.draft.recording_warnings)
        self.assertTrue(reopened.recording_review()["partial"])

    def test_zero_actions_is_not_successful_hover_recording(self):
        with self.assertRaises(OperationError) as failure: self.draft.recorded([])
        self.assertEqual(failure.exception.code, "recording_invalid_events")
        self.assertIn("마우스", str(failure.exception))

    def test_wait_for_state_uses_an_explicit_selected_expectation_without_action_target(self):
        event, snapshots = self.event()
        selection = self.draft.remember(self.program, snapshots[("editor", "main")], event["native_target"])
        self.draft.add({**TARGET, "program_id": "editor", "action": "wait_for_state", "timeout_seconds": 2.5,
                        "expect": {"selection_id": selection["selection_id"], "property": "value", "equals": "done"}})
        self.assertEqual(self.draft.steps[0]["timeout_ms"], 2500)
        self.assertEqual(self.draft.steps[0]["operation"], "wait_for_state")
        self.assertNotIn("selector", self.draft.steps[0])

    def test_progress_is_owned_bounded_and_redacts_unknown_values(self):
        progress = {"helper_pid": 888, "state": "recording", "event_count": 2, "manual_count": 1, "max_events": 15,
                    "warning_codes": [], "probe": {"hooks": "available", "uia": "available"},
                    "last_event": {"operation": "click", "recognition": "uia_candidate", "value": "SECRET"}, "value": "SECRET"}
        clean = ProcessEditors._recording_progress(progress, 888)
        self.assertFalse(clean["hover_is_action"])
        self.assertNotIn("SECRET", json.dumps(clean))
        for invalid in ({"helper_pid": 777}, {"state": {}}, {"event_count": 35}, {"manual_count": 3},
                        {"warning_codes": ["untrusted_warning"]}, {"probe": {"hooks": "available"}}):
            with self.subTest(invalid=invalid), self.assertRaises(OperationError):
                ProcessEditors._recording_progress({**progress, **invalid}, 888)

    def test_recording_progress_accepts_three_hundred_events_but_rejects_overflow(self):
        progress = {"helper_pid": 888, "state": "review", "event_count": MAX_RECORDING_EVENTS,
            "manual_count": MAX_RECORDING_EVENTS, "max_events": MAX_RECORDING_EVENTS,
            "warning_codes": [], "probe": {"hooks": "available", "uia": "available"}}
        self.assertEqual(ProcessEditors._recording_progress(progress, 888)["event_count"], 300)
        for field in ("event_count", "manual_count", "max_events"):
            with self.subTest(field=field), self.assertRaises(OperationError):
                ProcessEditors._recording_progress({**progress, field: MAX_RECORDING_EVENTS + 1}, 888)

    def test_failed_start_preserves_capture_cause_and_bounded_scope_without_content(self):
        diagnostic = {"code": "target_capture_too_large", "target_window_id": 123, "target_pid": 456,
            "target_valid": True, "target_visible": True, "target_minimized": False,
            "foreground_window_id": 123, "foreground_pid": 456, "foreground_root_id": 123,
            "foreground_matches_target": True, "activation_requested": True, "activation_returned": True,
            "toolbar_minimized": True}
        progress = {"helper_pid": 888, "state": "paused", "event_count": 0, "manual_count": 0,
            "max_events": 15, "warning_codes": [], "probe": {"hooks": "available", "uia": "available"},
            "start_diagnostic": diagnostic}
        clean = ProcessEditors._recording_progress(progress, 888)
        self.assertEqual(clean["start_diagnostic"], diagnostic)
        diagnostic["target_pid"] = 999
        self.assertEqual(clean["start_diagnostic"]["target_pid"], 456)
        for invalid in ({"target_pid": True}, {"target_window_id": -1}, {"foreground_root_id": 2**63},
                        {"target_visible": 1}, {"code": "private title: SECRET"}, {"code": "x" * 81},
                        {"code": ""}, {"window_title": "SECRET"}):
            with self.subTest(invalid=invalid), self.assertRaises(OperationError) as caught:
                ProcessEditors._recording_progress({**progress, "start_diagnostic": {**diagnostic, **invalid}}, 888)
            self.assertEqual(caught.exception.code, "recording_invalid_progress")

    @staticmethod
    def start_progress_with(**extra):
        diagnostic = {"code": "target_render_unavailable", "target_window_id": 123, "target_pid": 456,
            "target_valid": True, "target_visible": True, "target_minimized": False,
            "foreground_window_id": 123, "foreground_pid": 456, "foreground_root_id": 123,
            "foreground_matches_target": True, "activation_requested": True, "activation_returned": True,
            "toolbar_minimized": False, **extra}
        return {"helper_pid": 888, "state": "paused", "event_count": 0, "manual_count": 0,
            "max_events": 15, "warning_codes": [], "probe": {"hooks": "available", "uia": "available"},
            "start_diagnostic": diagnostic}

    @staticmethod
    def occlusion_window(**extra):
        return {"window_id": 987, "pid": 654, "class_name": "NativeOverlayWindow", "kind": "external_window",
                "bounds": {"x": -1920, "y": 0, "width": 3840, "height": 2160}, "layered": True, **extra}

    def test_failed_start_accepts_bounded_occlusion_and_capture_diagnostics(self):
        window = self.occlusion_window()
        cases = [{}, {"occlusion_details_unavailable": True},
            {"occlusion_details": [], "occlusion_count_reported": 0, "layered_occlusion_count_reported": 0,
             "occlusion_limit_reached": False},
            {"occlusion_details": [window], "occlusion_count_reported": 1, "layered_occlusion_count_reported": 1,
             "occlusion_limit_reached": False},
            {"occlusion_details": [{**window, "window_id": 987 + i} for i in range(12)],
             "occlusion_count_reported": 12, "layered_occlusion_count_reported": 12, "occlusion_limit_reached": True}]
        for extra in cases:
            for capture in ({}, {"capture_backend_stage": "wait_frame", "capture_backend_hresult": "80004005",
                                 "capture_backend_pending": True}):
                progress = self.start_progress_with(**extra, **capture)
                clean = ProcessEditors._recording_progress(progress, 888)
                with self.subTest(extra=extra, capture=capture):
                    self.assertEqual(clean["start_diagnostic"], progress["start_diagnostic"])
                    self.assertFalse(clean["task_verified"])
                    if extra.get("occlusion_details"):
                        progress["start_diagnostic"]["occlusion_details"][0]["bounds"]["x"] = 0
                        self.assertEqual(clean["start_diagnostic"]["occlusion_details"][0]["bounds"]["x"], -1920)
                        progress["start_diagnostic"]["occlusion_details"][0]["bounds"]["x"] = -1920

    def test_failed_start_rejects_unbounded_or_inconsistent_occlusion_metadata(self):
        original = {"occlusion_details": [self.occlusion_window()], "occlusion_count_reported": 1,
                    "layered_occlusion_count_reported": 1, "occlusion_limit_reached": False}
        bad = [{"occlusion_count_reported": True}, {"occlusion_count_reported": 2},
               {"layered_occlusion_count_reported": False}, {"layered_occlusion_count_reported": 0},
               {"occlusion_limit_reached": True}, {"occlusion_limit_reached": 0},
               {"occlusion_details": {}}, {"occlusion_details": [self.occlusion_window()] * 2, "occlusion_count_reported": 2},
               {"occlusion_details": [self.occlusion_window(window_id=i+1) for i in range(13)], "occlusion_count_reported": 13,
                "layered_occlusion_count_reported": 13, "occlusion_limit_reached": True},
               {"occlusion_details_unavailable": True}, {"arbitrary_diagnostic": "SECRET"}]
        for change in ({"window_id": True}, {"window_id": 0}, {"window_id": 2**63}, {"pid": -1}, {"pid": 2**32},
                       {"class_name": "x" * 257}, {"class_name": "bad\nclass"}, {"kind": "untrusted"}, {"kind": []},
                       {"layered": 1}, {"title": "SECRET"}, {"text": "SECRET"}, {"bounds": {}},
                       {"bounds": {"x": False, "y": 0, "width": 1, "height": 1}},
                       {"bounds": {"x": 0, "y": -2**31-1, "width": 1, "height": 1}},
                       {"bounds": {"x": 0, "y": 0, "width": 0, "height": 1}},
                       {"bounds": {"x": 0, "y": 0, "width": 1, "height": 1, "title": "SECRET"}}):
            bad.append({"occlusion_details": [self.occlusion_window(**change)]})
        for change in bad:
            with self.subTest(change=change), self.assertRaises(OperationError) as caught:
                ProcessEditors._recording_progress(self.start_progress_with(**{**original, **change}), 888)
            self.assertEqual(caught.exception.code, "recording_invalid_progress")
        for extra in ({"occlusion_details_unavailable": False}, {"occlusion_details_unavailable": 1},
                      {"occlusion_details": []}, {"occlusion_count_reported": 0}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                ProcessEditors._recording_progress(self.start_progress_with(**extra), 888)

    def test_failed_start_rejects_unknown_or_partial_capture_diagnostics(self):
        capture = {"capture_backend_stage": "create_item", "capture_backend_hresult": "00000000", "capture_backend_pending": False}
        for key in capture:
            incomplete = {name: value for name, value in capture.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(OperationError):
                ProcessEditors._recording_progress(self.start_progress_with(**incomplete), 888)
        for change in ({"capture_backend_stage": "private title SECRET"}, {"capture_backend_stage": []},
                       {"capture_backend_hresult": "0x80004005"}, {"capture_backend_hresult": "8000abcd"},
                       {"capture_backend_hresult": 0}, {"capture_backend_pending": 1}, {"capture_message": "SECRET"}):
            with self.subTest(change=change), self.assertRaises(OperationError) as caught:
                ProcessEditors._recording_progress(self.start_progress_with(**{**capture, **change}), 888)
            self.assertEqual(caught.exception.code, "recording_invalid_progress")

    def test_failed_start_accepts_bounded_frame_diagnostics_without_verifying_task(self):
        backend = {"capture_backend_stage": "wait_frame", "capture_backend_hresult": "00000000", "capture_backend_pending": True}
        for count, age in ((0, -1), (3, 15.25), (2**31-1, 60000)):
            fields = {"capture_frame_count": count, "capture_empty_polls": count, "capture_refresh_count": count,
                      "capture_latest_content_age_ms": age}
            progress = self.start_progress_with(**backend, **fields)
            result = ProcessEditors._recording_progress(progress, 888)
            self.assertEqual(result["start_diagnostic"], progress["start_diagnostic"])
            self.assertFalse(result["task_verified"])
            progress["start_diagnostic"]["capture_frame_count"] = 99
            self.assertEqual(result["start_diagnostic"]["capture_frame_count"], count)

    def test_failed_start_rejects_partial_unbounded_or_unbound_frame_diagnostics(self):
        backend = {"capture_backend_stage": "complete", "capture_backend_hresult": "00000000", "capture_backend_pending": False}
        fields = {"capture_frame_count": 1, "capture_empty_polls": 0, "capture_refresh_count": 0,
                  "capture_latest_content_age_ms": 0.0}
        bad = [{**backend, **{k: v for k, v in fields.items() if k != missing}} for missing in fields]
        bad.append(fields)
        for key in ("capture_frame_count", "capture_empty_polls", "capture_refresh_count"):
            bad.extend({**backend, **fields, key: value} for value in (True, -1, 2**31, 1.0, "1"))
        bad.extend({**backend, **fields, "capture_latest_content_age_ms": value}
                   for value in (False, -1.1, 60000.1, float("nan"), float("inf"), float("-inf"), "0", None))
        for diagnostic in bad:
            with self.subTest(diagnostic=diagnostic), self.assertRaises(OperationError) as caught:
                ProcessEditors._recording_progress(self.start_progress_with(**diagnostic), 888)
            self.assertEqual(caught.exception.code, "recording_invalid_progress")


    def test_previous_capture_failure_requires_complete_bounded_diagnostic_groups(self):
        backend = {"capture_backend_stage": "wait_frame", "capture_backend_hresult": "00000000", "capture_backend_pending": True}
        frames = {"capture_frame_count": 1, "capture_empty_polls": 2, "capture_refresh_count": 3, "capture_latest_content_age_ms": -1}
        previous = {"capture_previous_failure": "capture_time_future", "capture_previous_future_lead_ms": 0.25}
        codes = ("none", "target_changed", "target_requires_foreground", "target_unavailable", "target_minimized",
                 "target_hidden", "target_cloaked", "target_offscreen", "target_capture_size_invalid", "recording_start_failed",
                 "capture_time_invalid", "capture_time_future", "capture_time_stale", "capture_content_size_changed",
                 "capture_raw_bounds_changed", "capture_visible_bounds_changed")
        for code in codes:
            for age in (0, 0.25, 60000):
                progress = self.start_progress_with(**backend, **frames, capture_previous_failure=code, capture_previous_future_lead_ms=age)
                result = ProcessEditors._recording_progress(progress, 888)
                self.assertEqual(result["start_diagnostic"], progress["start_diagnostic"])
                self.assertFalse(result["task_verified"])
        bad = [previous, {**backend, **previous}, {**frames, **previous}]
        full = {**backend, **frames, **previous}
        bad.extend({k: v for k, v in full.items() if k != missing} for missing in (*backend, *frames, *previous))
        bad.extend({**full, "capture_previous_failure": value} for value in (None, [], "private_title", "Capture_Time_Future", "x" * 81))
        bad.extend({**full, "capture_previous_future_lead_ms": value}
                   for value in (True, -0.01, 60000.01, float("nan"), float("inf"), float("-inf"), "0", None))
        for diagnostic in bad:
            with self.subTest(diagnostic=diagnostic), self.assertRaises(OperationError) as caught:
                ProcessEditors._recording_progress(self.start_progress_with(**diagnostic), 888)
            self.assertEqual(caught.exception.code, "recording_invalid_progress")


class ImageDraftTests(unittest.TestCase):
    def setUp(self):
        DraftTests.setUp(self)
        self.image = self.draft.remember_image(self.program, image_choice())
        self.image_base = {**TARGET, "program_id": "editor", "selection_id": self.image["selection_id"]}

    def test_image_selection_requires_human_and_same_window(self):
        for values in ({"human_confirmed": False}, {"pid": 900}, {"window_id": 900}):
            with self.subTest(values=values), self.assertRaises(OperationError):
                self.draft.remember_image(self.program, image_choice(**values))

    def test_image_click_adds_review_and_never_stores_desktop_coordinates(self):
        self.draft.add({**self.image_base, "action": "click"})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "checkpoint"])
        self.assertEqual(self.draft.steps[0]["image_target"]["template_png"], image_choice()["template_png"])
        encoded = json.dumps(self.draft.steps)
        for forbidden in ('"pid"', '"window_id"', '"selection_id"', '"thumbnail_png"'):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn("template_png", json.dumps(self.draft.summaries()))

    def test_image_automatic_completion_requires_selected_changed_state_and_no_checkpoint(self):
        self.draft.add({**self.image_base, "action": "click", "completion_mode": "automatic",
            "expect": {"selection_id": self.selection["selection_id"], "property": "value", "equals": "done"}})
        self.assertEqual(len(self.draft.steps), 1)
        self.assertTrue(self.draft.steps[0]["expect"][0]["require_change"])
        self.draft.add({**self.base, "action": "delay", "seconds": .1})
        self.draft.change("move_step", {"index": 1, "direction": -1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["delay", "image_click"])
        self.draft.change("remove_step", {"index": 1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["delay"])

    def test_image_automatic_completion_without_evidence_is_rejected(self):
        with self.assertRaises(OperationError):
            self.draft.add({**self.image_base, "action": "click", "completion_mode": "automatic"})
        self.assertEqual(self.draft.steps, [])

    def test_image_wait_has_no_input_or_checkpoint_and_can_be_reordered(self):
        self.draft.add({**self.image_base, "action": "wait_for_element", "timeout_seconds": 3})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["wait_for_image"])
        self.draft.add({**self.image_base, "action": "click"})
        self.draft.change("move_step", {"index": 2, "direction": -1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "checkpoint", "wait_for_image"])
        self.draft.change("remove_step", {"index": 1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["wait_for_image"])

    def test_unknown_recorded_input_requires_explicit_resolution_and_leaks_no_typed_keys(self):
        self.draft.recorded([{**image_choice(), "operation": "manual_entry", "program_id": "editor", "value": "PRIVATE", "keys": ["SECRET"]}])
        self.assertEqual(self.draft.steps[0]["operation"], "manual_entry")
        self.assertTrue(self.draft.summaries()[0]["requires_input"])
        self.assertNotIn("PRIVATE", json.dumps(self.draft.steps)); self.assertNotIn("SECRET", json.dumps(self.draft.steps))
        with self.assertRaises(OperationError) as caught: self.draft.validate()
        self.assertEqual(caught.exception.code, "recording_input_required")
        self.draft.resolve_input({"index": 0, "value": "user typed intended value"})
        self.draft.validate()
        self.assertEqual(self.draft.steps[0]["operation"], "image_type_text")
        self.assertEqual(self.draft.steps[0]["value"], "user typed intended value")

    def canvas_candidate(self, **extra):
        return {**image_choice(), "operation": "manual_entry", "program_id": "editor", "reason": "unknown_input",
                "input_target_candidate": {"source": "preceding_recorded_click", "verified": False,
                    "requires_user_confirmation": True, "typed_text_recorded": False},
                "recorded_preparation": "CTRL+A", **extra}

    def test_canvas_candidate_import_is_manual_and_does_not_retain_raw_text_or_proof(self):
        event = self.canvas_candidate(value="PRIVATE", keys=["SECRET"], observed_selection="PRIVATE_SELECTION")
        self.draft.recorded([event])
        self.assertEqual([s["operation"] for s in self.draft.steps], ["manual_entry", "checkpoint"])
        self.assertEqual(self.draft.steps[0]["manual_reason"], "unknown_input")
        row = self.draft.summaries()[0]
        self.assertEqual(row["label"], "입력 위치 후보 · 값 확인 필요")
        self.assertEqual(row["recognition"], "image_candidate")
        self.assertTrue(row["requires_input"]); self.assertTrue(row["editable_input"])
        self.assertEqual(row["input_target_candidate"], event["input_target_candidate"])
        self.assertIn("두 번 클릭", row["detail"])
        serialized = json.dumps({"steps": self.draft.steps, "draft": self.draft.draft_metadata()})
        for forbidden in ("PRIVATE", "SECRET", "PRIVATE_SELECTION", "input_target_candidate", "recorded_preparation", "CTRL+A"):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(OperationError): self.draft.validate()

    def test_canvas_candidate_reopen_and_explicit_value_resolution_preserve_image_and_checkpoint(self):
        self.draft.recorded([self.canvas_candidate()])
        image = copy.deepcopy(self.draft.steps[0]["image_target"])
        checkpoint = copy.deepcopy(self.draft.steps[1])
        task = {"steps": copy.deepcopy(self.draft.steps), "draft": self.draft.draft_metadata(), "variables": {}}
        reopened = ProcessDraft([self.program], task)
        self.assertEqual(reopened.summaries()[0]["recognition"], "image_candidate")
        reopened.resolve_input({"index": 0, "value": "user reviewed value"})
        reopened.validate()
        self.assertEqual(reopened.steps[0]["operation"], "image_type_text")
        self.assertTrue(reopened.steps[0]["replace_all"])
        self.assertEqual(reopened.steps[0]["value"], "user reviewed value")
        self.assertEqual(reopened.steps[0]["image_target"], image)
        self.assertEqual(reopened.steps[1], checkpoint)
        self.assertNotIn("input_target_candidate", reopened.summaries()[0])
        self.assertNotIn("manual_reason", reopened.steps[0])

    def test_canvas_candidate_can_retarget_once_with_user_value_without_changing_other_steps(self):
        self.draft.recorded([self.canvas_candidate()])
        checkpoint = copy.deepcopy(self.draft.steps[1])
        replacement = self.draft.remember_image(self.program, image_choice(anchor={"x": .8, "y": .3}))
        self.draft.repair(0, replacement, action="set_value", value="chosen value")
        self.draft.validate()
        self.assertEqual(self.draft.steps[0]["image_target"]["anchor"], {"x": .8, "y": .3})
        self.assertEqual(self.draft.steps[0]["value"], "chosen value")
        self.assertTrue(self.draft.steps[0]["replace_all"])
        self.assertEqual(self.draft.steps[1], checkpoint)
        self.assertNotIn("input_target_candidate", self.draft.summaries()[0])

    def test_unverified_candidate_metadata_cannot_promote_protected_or_other_unknown_actions(self):
        for change in ({"verified": True}, {"verified": 0}, {"typed_text_recorded": True},
                       {"requires_user_confirmation": False}, {"source": "hover"}, {"extra": "untrusted"}):
            event = self.canvas_candidate()
            event["input_target_candidate"].update(change)
            draft = ProcessDraft([self.program]); draft.recorded([event])
            with self.subTest(change=change):
                self.assertNotIn("input_target_candidate", draft.summaries()[0])
                self.assertEqual(draft.steps[0]["operation"], "manual_entry")
        for reason in ("protected_input", "shortcut_requires_manual_setup", "recording_method_unverified"):
            draft = ProcessDraft([self.program]); draft.recorded([self.canvas_candidate(reason=reason)])
            with self.subTest(reason=reason):
                self.assertNotIn("input_target_candidate", draft.summaries()[0])
                with self.assertRaises(OperationError): draft.resolve_input({"index": 0, "value": "not accepted"})
        event = self.canvas_candidate(); event.pop("template_png")
        draft = ProcessDraft([self.program]); draft.recorded([event])
        self.assertNotIn("input_target_candidate", draft.summaries()[0])
        self.assertFalse(draft.summaries()[0]["editable_input"])

    def test_recording_scope_and_invalid_event_are_atomic(self):
        before = copy.deepcopy(self.draft.steps)
        for extra in ({"program_id": "unbound"}, {"operation": "execute"}, {"width": 999}):
            with self.subTest(extra=extra), self.assertRaises((OperationError, ValueError)):
                self.draft.recorded([{**image_choice(), "operation": "click", "program_id": "editor", **extra}])
            self.assertEqual(before, self.draft.steps)

    def test_recording_capacity_includes_each_review_checkpoint(self):
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        self.draft.recorded([event] * MAX_RECORDING_EVENTS)
        self.draft.recorded([event] * (MAX_RECIPE_STEPS // 2 - MAX_RECORDING_EVENTS))
        self.assertEqual(len(self.draft.steps), MAX_RECIPE_STEPS)
        with self.assertRaises((OperationError, ValueError)):
            self.draft.recorded([event])
        self.assertEqual(len(self.draft.steps), MAX_RECIPE_STEPS)

    def test_recording_packet_and_popup_boundaries_preserve_existing_draft(self):
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        with self.assertRaises(OperationError):
            self.draft.recorded([event] * (MAX_RECORDING_EVENTS + 1))
        self.assertEqual(self.draft.steps, [])
        windows = [{"program_id": "editor", "window_ref": "popup" + str(index + 1), "owner_ref": "main", "pid": TARGET["pid"],
            "window_id": TARGET["window_id"] + index + 1, "owner_window_id": TARGET["window_id"],
            "owner_verified": True, "closed": True, "title": "", "class_name": "ComboLBox"} for index in range(MAX_RECORDED_WINDOWS)]
        self.assertEqual(len(self.draft.recorded_windows(windows)), MAX_RECORDED_WINDOWS)
        with self.assertRaises(OperationError):
            self.draft.recorded_windows(windows + [windows[-1]])
        self.assertEqual(self.draft.steps, [])

    def test_recording_payload_overflow_preserves_prior_image_and_marks_only_suffix_for_repair(self):
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        pending = {"operation": "manual_entry", "requested_operation": "click", "program_id": "editor",
            "reason": "recording_payload_limit", "capture_issue": "recording_payload_limit"}
        self.draft.recorded([event, pending], warning_codes=["recording_payload_limit"])
        self.assertEqual(len(self.draft.steps), 4)
        self.assertEqual(self.draft.steps[0]["operation"], "image_click")
        self.assertIn("image_target", self.draft.steps[0])
        self.assertEqual(self.draft.steps[2]["operation"], "manual_entry")
        self.assertEqual(self.draft.steps[2]["manual_reason"], "recording_payload_limit")
        self.assertEqual(self.draft.steps[2]["repair_action"], "click")
        self.assertFalse(self.draft.readiness()["runnable"])
        self.assertIn("이미지 기록 용량", self.draft.summaries()[2]["detail"])
        self.draft.validate(allow_pending=True)

    def popup_recording(self, *, append=False, hover_parent=False):
        popup = {"program_id": "editor", "window_ref": "popup_1", "owner_ref": "main", "pid": TARGET["pid"],
            "window_id": TARGET["window_id"]+1, "owner_window_id": TARGET["window_id"],
            "owner_verified": True, "closed": True, "title": "", "class_name": "ComboLBox"}
        windows = [popup]
        if hover_parent:
            windows.append({**popup, "window_ref":"popup_2", "owner_ref":"popup_1",
                "window_id":TARGET["window_id"]+2, "owner_window_id":TARGET["window_id"]+1})
        events = [] if append else [{**image_choice(), "operation":"click", "program_id":"editor"}]
        events.append({**image_choice(), "operation":"click", "program_id":"editor",
            "window_ref":"popup_2" if hover_parent else "popup_1"})
        self.draft.recorded(events, windows=windows, review_mode="visual", auto_popup=True)

    def test_appended_recording_does_not_replace_existing_explicit_human_review(self):
        self.draft.add({**self.image_base, "action":"click"})
        self.draft.edit({"index":1,"review_mode":"human"})
        original = copy.deepcopy(self.draft.steps)
        self.popup_recording(append=True)
        self.assertEqual(self.draft.steps[:2], original)
        self.assertEqual(self.draft.steps[1]["review_origin"], "explicit")
        self.assertEqual(self.draft.steps[1]["review_mode"], "human")
        self.draft.validate()

    def test_explicit_review_choice_survives_automatic_completion_edit(self):
        self.draft.add({**self.image_base, "action":"click"})
        self.draft.edit({"index":1,"review_mode":"human"})
        original = copy.deepcopy(self.draft.steps[1])
        self.draft.completion({"index":0,"selection_id":self.selection["selection_id"],
            "property":"value","equals":"done"})
        self.assertEqual(self.draft.steps[1], original)

    def test_authored_review_message_survives_later_automatic_completion(self):
        self.draft.add({**self.image_base, "action":"click"})
        self.draft.edit({"index":1,"value":"사용자가 최종 결과를 직접 확인해야 합니다."})
        original = copy.deepcopy(self.draft.steps[1])
        self.assertEqual(original["review_origin"], "explicit")
        self.draft.completion({"index":0,"selection_id":self.selection["selection_id"],
            "property":"value","equals":"done"})
        self.assertEqual(self.draft.steps[1], original)

    def test_popup_image_retarget_updates_linked_condition_atomically(self):
        self.popup_recording()
        new = image_choice(window_id=TARGET["window_id"]+1, anchor={"x":.25,"y":.75})
        self.draft.retarget(3, new)
        self.assertEqual(self.draft.steps[2]["image_target"], self.draft.steps[3]["image_target"])
        self.assertEqual(self.draft.steps[2]["image_target"]["anchor"], {"x":.25,"y":.75})
        self.assertEqual(self.draft.steps[2]["review_mode"], "popup_image")
        self.draft.validate()

    def test_legacy_popup_image_retarget_keeps_condition_in_sync(self):
        self.popup_recording()
        self.draft.steps[2]["review_origin"] = "user_grouped"
        new = image_choice(window_id=TARGET["window_id"]+1, anchor={"x":.25,"y":.75})
        self.draft.retarget(3, new)
        self.assertEqual(self.draft.steps[2]["image_target"], self.draft.steps[3]["image_target"])
        self.assertEqual(self.draft.steps[2]["review_origin"], "user_grouped")
        self.draft.validate()

    def test_popup_image_repair_updates_condition_and_native_repair_retains_review(self):
        self.popup_recording()
        target = self.draft.targets[("editor","popup_1")]
        selected = self.draft.remember_image(target, image_choice(window_id=target["window_id"], anchor={"x":.25,"y":.75}))
        self.draft.repair(3, selected)
        self.assertEqual(self.draft.steps[2]["image_target"], self.draft.steps[3]["image_target"])
        native_id="native-popup-button"
        self.draft.selections[native_id] = {"target":("editor","popup_1"),"selector":{"role":"Button","name":"Apply"},
            "recognition":"uia","actions":["click"],"label":"Apply"}
        self.draft.repair(3, {"selection_id":native_id})
        self.assertEqual(self.draft.steps[2]["review_mode"], "human")
        self.assertNotIn("image_target", self.draft.steps[2])
        self.assertEqual(self.draft.steps[3]["operation"], "click")
        self.draft.validate()

    def test_hover_only_intermediate_owner_does_not_create_missing_reference(self):
        self.popup_recording(hover_parent=True)
        waits = [item for item in self.draft.steps if item["operation"] == "wait_for_window"]
        self.assertEqual([(item["window_ref"],item["owner_ref"]) for item in waits], [("popup_2","main")])
        self.assertEqual(self.draft.steps[2]["opened_from"], "main")
        self.assertEqual(self.draft.steps[-1]["window_ref"], "main")
        self.assertEqual(self.draft.steps[-1]["return_from"], "popup_2")
        self.draft.validate()
        reopened = ProcessDraft([self.program], {"steps":self.draft.steps})
        self.assertEqual(set(reopened.targets), {("editor","main"),("editor","popup_2")})

    def test_seven_closed_popup_episodes_import_and_reopen_without_live_targets(self):
        windows, events = [], []
        for index in range(7):
            ref = "popup" + str(index + 1)
            windows.append({"program_id": "editor", "window_ref": ref, "owner_ref": "main", "pid": TARGET["pid"],
                "window_id": TARGET["window_id"] + index + 1, "owner_window_id": TARGET["window_id"],
                "owner_verified": True, "closed": True, "title": "", "class_name": "ComboLBox"})
            events.append({**image_choice(), "operation": "click", "program_id": "editor", "window_ref": ref})
        self.draft.recorded(events, windows=windows)
        self.assertEqual(len(self.draft.steps), 21)
        self.assertEqual(sum(step["operation"] == "wait_for_window" for step in self.draft.steps), 7)
        reopened = ProcessDraft([self.program], {"steps": self.draft.steps})
        self.assertEqual(len(reopened.targets), 8)
        self.assertTrue(all(row["window_id"] == 0 for key, row in reopened.targets.items() if key[1] != "main"))

    def test_unsupported_drag_and_missing_image_remain_reviewable_and_cannot_become_text(self):
        self.draft.recorded([{**image_choice(), "program_id": "editor", "operation": "manual_entry", "reason": "drag_requires_manual_setup"},
                             {"program_id": "editor", "operation": "press_key", "key": "ENTER", "reason": "image_capture_required"}])
        self.assertEqual(len(self.draft.steps), 4)
        for index in (0, 2):
            self.assertEqual(self.draft.steps[index]["operation"], "manual_entry")
            self.assertFalse(self.draft.summaries()[index]["editable_input"])
            with self.assertRaises(OperationError): self.draft.resolve_input({"index": index, "value": "text"})
        self.assertNotIn("key", self.draft.steps[2])
        with self.assertRaises(OperationError): self.draft.validate()
        self.draft.change("remove_step", {"index": 3})
        self.assertEqual(len(self.draft.steps), 2)

    def test_task_text_view_redacts_pixels_without_mutating_storage(self):
        self.draft.add({**self.image_base, "action": "click"})
        task = {"steps": self.draft.steps}
        view = task_view(task)
        self.assertNotIn("template_png", json.dumps(view))
        self.assertTrue(view["steps"][0]["image_target"]["template_stored_locally"])
        self.assertIn("template_png", task["steps"][0]["image_target"])

    def test_protected_recording_placeholder_stays_noneditable_without_image(self):
        self.draft.recorded([{"operation": "manual_entry", "program_id": "editor", "reason": "protected_input", "value": "NEVER_STORE"}])
        self.assertEqual(self.draft.steps[0]["manual_reason"], "protected_input")
        self.assertFalse(self.draft.summaries()[0]["editable_input"])
        self.assertIn("보호된 입력칸", self.draft.summaries()[0]["detail"])
        self.assertNotIn("NEVER_STORE", json.dumps(self.draft.steps))
        with self.assertRaises(OperationError): self.draft.resolve_input({"index": 0, "value": "not allowed"})

    def test_retarget_preserves_action_order_and_same_window_review_checkpoint(self):
        self.draft.add({**self.image_base, "action": "click"})
        checkpoint = copy.deepcopy(self.draft.steps[1])
        self.draft.retarget(0, image_choice(anchor={"x": .8, "y": .4}))
        self.assertEqual([step["operation"] for step in self.draft.steps], ["image_click", "checkpoint"])
        self.assertEqual(self.draft.steps[0]["image_target"]["anchor"], {"x": .8, "y": .4})
        self.assertEqual(self.draft.steps[1], checkpoint)
        original = copy.deepcopy(self.draft.steps)
        for index, chosen in ((1, image_choice()), (0, image_choice(window_id=88)), (0, image_choice(human_confirmed=False))):
            with self.subTest(index=index), self.assertRaises(OperationError): self.draft.retarget(index, chosen)
            self.assertEqual(self.draft.steps, original)


class Child:
    pid = 999
    returncode = None
    terminated = False
    def poll(self): return self.returncode
    def terminate(self): self.returncode = 0; self.terminated = True
    def wait(self, timeout): return self.returncode
    def kill(self): self.returncode = -1


class EditorIPCTests(unittest.TestCase):
    def setUp(self):
        gc.collect()  # Dispose earlier hidden Tk fixtures on their owning thread.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config_at(self.temporary.name)
        self.manager = ComputerManager(self.config)
        self.runtime = MutableRuntime(self.config)
        self.runtime.run_dir = Path(self.temporary.name) / "run"
        self.manager.session = self.runtime
        self.editors = self.manager.process_editors
        self.addCleanup(self.editors.close)
        self.child = Child()
        self.args = {"targets": [{"program_id": "editor", **TARGET}], "name": "시험 프로세스"}
        self.spawn_count = 0
        self.seq = 0
        self.paths = {}

    def spawn(self, args, **kwargs):
        self.spawn_count += 1
        self.assertEqual(args[1], "--edit")
        request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        self.nonce = request["nonce"]
        self.paths = {key: Path(args[3] + suffix) for key, suffix in
                      (("response", ""), ("ready", ".ready.json"), ("command", ".command.json"), ("event", ".event.json"))}
        atomic_json(self.paths["ready"], {"nonce": self.nonce, "status": "ready", "helper_pid": self.child.pid, "helper_window_id": 1000})
        return self.child

    def start(self, **extra):
        answer = self.manager.call("computer_process_editor", {**self.args, **extra})
        return answer["structuredContent"]

    def status(self, job, **extra):
        return self.manager.call("computer_process_status", {"editor_id": job["editor_id"], **extra})["structuredContent"]

    def terminal(self, job):
        limit = time.monotonic()+5
        while time.monotonic()<limit:
            answer = self.status(job, wait_ms=100)
            if not self.editors.jobs[job["editor_id"]]["done"].is_set(): continue
            return answer
        self.fail("editor worker did not terminate: " + str(answer))

    def command(self, action, payload, *, sequence=None, nonce=None):
        self.seq = self.seq+1 if sequence is None else sequence
        atomic_json(self.paths["command"], {"nonce": nonce or self.nonce, "seq": self.seq, "action": action, "payload": payload})
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            if self.paths["event"].exists():
                answer = ProcessEditors._read(self.paths["event"], self.nonce)
                if answer["seq"] == self.seq: return answer
            time.sleep(.01)
        self.fail("command did not return an event")

    def context(self):
        stack = mock.patch("process_editor.subprocess.Popen", side_effect=self.spawn)
        self.addCleanup(stack.stop); stack.start()
        visible = mock.patch("process_editor._helper_visible", return_value=True)
        self.addCleanup(visible.stop); visible.start()
        exists = mock.patch("process_editor.Path.is_file", return_value=True)
        self.addCleanup(exists.stop); exists.start()

    def test_ready_duplicate_cancel_and_owned_helper_cleanup(self):
        self.context()
        job = self.start()
        self.assertEqual(job["status"], "editing")
        self.assertTrue(job["editor_visible"])
        self.assertTrue(job["pending"])
        again = self.start()
        self.assertEqual(again["editor_id"], job["editor_id"])
        self.assertEqual(self.spawn_count, 1)
        self.status(job, cancel=True)
        done = self.terminal(job)
        self.assertEqual(done["status"], "cancelled")
        self.assertFalse(done["pending"])
        self.assertTrue(self.child.terminated)
        self.assertEqual(self.manager.tasks.all(), [])
        self.assertEqual(self.runtime.calls, [])
        self.assertFalse(any(path.exists() for path in self.paths.values()))

    def test_hosted_identity_is_pinned_for_selection_and_child_replacement_is_rejected(self):
        identity = {"app_pid": 12, "app_started": 50, "app_window_id": 23}
        self.runtime.guard.hosted_target = mock.Mock(side_effect=lambda target: copy.deepcopy(identity))
        self.context()
        opened = self.start()
        job = self.editors.jobs[opened["editor_id"]]
        self.assertEqual(job["hosted_targets"][(TARGET["pid"], TARGET["window_id"])], identity)
        identity["app_started"] = 60
        with mock.patch.object(self.editors, "_visual") as visual:
            answer = self.command("pick_target", {**self.args["targets"][0], "purpose": "action"})
        self.assertEqual(answer["status"], "error")
        self.assertEqual(answer["code"], "editor_target_changed")
        visual.assert_not_called()
        self.status(opened, cancel=True)
        self.terminal(opened)

    def test_unified_target_selection_uses_same_choice_without_f8_fallback(self):
        self.context(); self.start()
        with mock.patch.object(self.editors, "_visual", return_value=image_choice()) as visual, mock.patch("process_editor._run_helper") as old_picker:
            answer = self.command("pick_target", {**self.args["targets"][0], "purpose": "action"})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(answer["selection"]["recognition"], "image")
        old_picker.assert_not_called()
        self.assertTrue(visual.call_args.args[2]["auto_capture"])
        self.assertTrue(visual.call_args.args[2]["include_native"])

    def test_unified_target_uses_native_candidate_only_after_driver_match(self):
        self.context(); self.start()
        with mock.patch.object(self.editors, "_visual", return_value=image_choice(native_target=native_choice())), mock.patch("process_editor._run_helper") as old_picker:
            answer = self.command("pick_target", {**self.args["targets"][0], "purpose": "action"})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(answer["selection"]["recognition"], "uia")
        old_picker.assert_not_called()

    def test_native_candidate_succeeds_when_small_image_quality_check_failed(self):
        self.context(); self.start()
        chosen = {**TARGET, "human_confirmed": True, "status": "selected", "native_target": native_choice(), "capture_issue": "template_low_detail"}
        with mock.patch.object(self.editors, "_visual", return_value=chosen):
            answer = self.command("pick_target", {**self.args["targets"][0], "purpose": "action"})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(answer["selection"]["recognition"], "uia")
        self.assertNotIn("thumbnail_png", answer["selection"])
        self.assertFalse(answer["recording_review"]["partial"])

    def test_unmatched_native_without_image_returns_recoverable_diagnostic(self):
        self.context(); self.start()
        chosen = {**TARGET, "human_confirmed": True, "status": "selected", "native_target": native_choice(), "capture_issue": "image_ambiguous"}
        with mock.patch.object(self.editors, "_visual", return_value=chosen), \
                mock.patch.object(ProcessDraft, "remember", side_effect=OperationError("missing", "picker_not_found")):
            answer = self.command("pick_target", {**self.args["targets"][0], "purpose": "action"})
        self.assertEqual(answer["status"], "error", answer)
        self.assertEqual(answer["code"], "image_ambiguous")
        self.assertIn("같은 모양", answer["message"])
        self.assertEqual(answer["steps"], [])

    def test_trial_uses_isolated_draft_does_not_save_or_claim_unknown_success(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        before = copy.deepcopy(self.editors.jobs[job["editor_id"]]["draft"].steps)
        def trial(runtime, task, targets):
            self.assertIs(runtime, self.runtime)
            self.assertEqual(targets, self.args["targets"])
            task["steps"][0]["duration_ms"] = 990
            return {"status": "awaiting_checkpoint", "task_verified": False, "run_id": "test-run", "content": [{"type": "image", "data": "private"}]}
        self.editors.test_runner = trial
        answer = self.command("test_run", {"name": "trial", "description": ""})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertFalse(answer["test_result"]["task_verified"])
        self.assertNotIn("content", answer["test_result"])
        self.assertEqual(self.manager.tasks.all(), [])
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps, before)
        self.assertEqual(self.status(job)["last_test"]["run_id"], "test-run")

    def test_trial_resume_retains_original_draft_and_requires_native_ready(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        self.editors.test_runner = mock.Mock(return_value={"status": "needs_review", "task_verified": False, "run_id": "trial-one"})
        self.command("test_run", {"name": "trial", "description": ""})
        self.command("edit_step", {"index": 0, "seconds": .9})
        self.editors.test_runner.return_value = {"status": "verified", "task_verified": True, "run_id": "trial-one"}
        review = {"token": "observed"}
        with mock.patch.object(self.editors, "_trial_signal") as signal:
            result = self.editors.resume_test(job["editor_id"], self.runtime, observation_review=review)
        call = self.editors.test_runner.call_args
        self.assertEqual(call.args[1]["steps"][0]["duration_ms"], 100)
        self.assertEqual(call.kwargs["resume_run_id"], "trial-one")
        self.assertEqual(call.kwargs["observation_review"], review)
        self.assertEqual([call.args[1] for call in signal.call_args_list], ["running", "finished"])
        self.assertTrue(result["last_test"]["task_verified"])
        self.assertEqual(self.manager.tasks.all(), [])
        count = self.editors.test_runner.call_count
        with mock.patch.object(self.editors, "_trial_signal") as signal, self.assertRaises(OperationError):
            self.editors.resume_test(job["editor_id"], self.runtime, observation_review=review)
        signal.assert_not_called()
        self.assertEqual(self.editors.test_runner.call_count, count)

    def test_trial_chunk_can_resume_same_frozen_draft_without_a_review(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        continuation = {"reason": "workflow_chunk_complete", "session_id": "trial-session"}
        paused = {"status": "interrupted", "task_verified": False, "run_id": "trial-chunk", "pending_step": None, "continuation": continuation}
        self.editors.test_runner = mock.Mock(return_value=paused)
        answer = self.command("test_run", {"name": "chunk", "description": ""})
        self.assertEqual(answer["test_result"]["continuation"], continuation)
        with mock.patch.object(self.editors, "_trial_signal"), self.assertRaises(OperationError):
            self.editors.resume_test(job["editor_id"], self.runtime, observation_review={"token": "unexpected"})
        self.editors.test_runner.return_value = {"status": "verified", "task_verified": True, "run_id": "trial-chunk"}
        with mock.patch.object(self.editors, "_trial_signal"):
            result = self.editors.resume_test(job["editor_id"], self.runtime)
        self.assertTrue(result["last_test"]["task_verified"])
        self.assertEqual(self.editors.test_runner.call_args.kwargs["resume_run_id"], "trial-chunk")
        self.assertIsNone(self.editors.test_runner.call_args.kwargs["observation_review"])
        self.assertEqual(self.manager.tasks.all(), [])

    def test_trial_unknown_or_session_budget_interrupt_is_not_chunk_resume(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        for reason, pending in (("session_budget_exhausted", None), ("workflow_chunk_complete", 3)):
            self.editors.test_runner = mock.Mock(return_value={"status": "interrupted", "task_verified": False, "run_id": "trial-chunk",
                "pending_step": pending, "continuation": {"reason": reason, "session_id": "trial-session"}})
            self.command("test_run", {"name": "chunk", "description": ""})
            with self.subTest(reason=reason, pending=pending), mock.patch.object(self.editors, "_trial_signal") as signal, self.assertRaises(OperationError):
                self.editors.resume_test(job["editor_id"], self.runtime)
            signal.assert_not_called()
            self.assertEqual(self.editors.test_runner.call_count, 1)

    def test_trial_resume_hidden_ack_failure_does_not_dispatch_or_mask_error(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        self.editors.test_runner = mock.Mock(return_value={"status": "needs_review", "task_verified": False, "run_id": "trial-one"})
        self.command("test_run", {"name": "trial", "description": ""})
        count = self.editors.test_runner.call_count
        with mock.patch.object(self.editors, "_trial_signal", side_effect=[OperationError("not hidden", "editor_test_not_ready"), OSError("helper already closed")]):
            with self.assertRaises(OperationError) as caught:
                self.editors.resume_test(job["editor_id"], self.runtime, observation_review={"token": "observed"})
        self.assertEqual(caught.exception.code, "editor_test_not_ready")
        self.assertEqual(self.editors.test_runner.call_count, count)
        lock = self.editors.jobs[job["editor_id"]]["interaction_lock"]
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_visual_recording_rechecks_capability_after_editor_opened(self):
        self.context()
        self.editors.visual_review_enabled = lambda runtime: True
        job = self.start()
        self.assertTrue(self.editors.jobs[job["editor_id"]]["visual_review_enabled"])
        self.editors.visual_review_enabled = lambda runtime: False
        with mock.patch("replay_preflight.image_replay_preflight") as preflight, mock.patch.object(self.editors, "_visual") as recorder:
            response = self.command("record", {"review_mode":"visual"})
            self.assertNotEqual(response["status"], "ok")
            preflight.assert_not_called()
            recorder.assert_not_called()
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps, [])

    def test_trial_status_never_sends_screenshots_to_unverified_client(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        self.editors.test_runner = mock.Mock(return_value={"status": "needs_review", "task_verified": False, "run_id": "trial-one",
            "visual_review": {"id": "review"}, "observation_content": [{"type": "image", "data": "localpixels"}]})
        self.command("test_run", {"name": "trial", "description": ""})
        self.assertNotIn("observation_content", self.editors.status(job["editor_id"]))
        self.editors.visual_review_enabled = lambda runtime: True
        self.assertEqual(self.editors.status(job["editor_id"])["observation_content"][0]["data"], "localpixels")
        self.editors.visual_review_enabled = lambda runtime: False
        self.assertNotIn("observation_content", self.editors.status(job["editor_id"]))

    def test_trial_human_review_retains_private_capture_for_facade(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        content = [{"type": "image", "data": "local-review-only"}]
        self.editors.test_runner = mock.Mock(return_value={"status": "needs_review", "task_verified": False, "run_id": "trial-one",
            "checkpoint_id": "human-review", "checkpoint_content": content})
        reply = self.command("test_run", {"name": "trial", "description": ""})
        self.assertNotIn("checkpoint_content", reply["test_result"])
        response = self.editors.status(job["editor_id"])
        self.assertEqual(response["checkpoint_content"], content)
        response["checkpoint_content"][0]["data"] = "mutated"
        self.assertEqual(self.editors.status(job["editor_id"])["checkpoint_content"], content)

    def test_different_duplicate_reports_busy_without_opening_a_second_editor(self):
        self.context(); job = self.start()
        same = self.start()
        different = self.start(name="another process")
        self.assertTrue(same["reused"])
        self.assertFalse(same["busy"])
        self.assertTrue(different["busy"])
        self.assertFalse(different["reused"])
        self.assertEqual(different["editor_id"], job["editor_id"])
        self.assertEqual(self.spawn_count, 1)
        self.status(job, cancel=True); self.terminal(job)

    def test_cleanup_failure_does_not_claim_closed_or_allow_another_helper(self):
        self.context(); job = self.start()
        with mock.patch("process_editor._end_helper", side_effect=OSError("owned helper still running")):
            self.status(job, cancel=True)
            done = self.terminal(job)
        self.assertTrue(done["cleanup_pending"])
        self.assertIsNone(done["editor_visible"])
        again = self.start()
        self.assertEqual(again["editor_id"], job["editor_id"])
        self.assertEqual(self.spawn_count, 1)
        self.assertIsNotNone(self.editors.pending())

    def test_hardlinked_command_is_rejected_without_reading_or_deleting_original(self):
        self.context(); job = self.start()
        original = Path(self.temporary.name) / "original.json"
        original.write_text(json.dumps({"nonce": self.nonce, "seq": 1, "action": "add_step",
            "payload": {"program_id": "editor", **TARGET, "action": "delay", "seconds": 1}}), encoding="utf-8")
        self.addCleanup(lambda: self.paths["command"].unlink(missing_ok=True))
        try:
            os.link(original, self.paths["command"])
        except (OSError, NotImplementedError) as error:
            self.skipTest("hardlinks not supported by test filesystem: " + str(error))
        done = self.terminal(job)
        self.assertEqual(done["status"], "failed")
        self.assertTrue(original.exists())
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps, [])
        self.assertEqual(self.manager.tasks.all(), [])
        self.assertEqual(self.runtime.calls, [])

    def test_native_request_write_in_long_install_path_remains_atomic_and_under_max_path(self):
        folder = Path(self.temporary.name) / ("p" * (195-len(self.temporary.name)-1))
        folder.mkdir(parents=True)
        target = folder / "0123456789abcdef.response.event.json"
        self.assertLess(len(str(target)+".tmp"), 260)
        ProcessEditors._write(target, {"nonce": "n"*64, "status": "old"})
        ProcessEditors._write(target, {"nonce": "n"*64, "status": "new"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "new")
        self.assertEqual(list(folder.iterdir()), [target])

    def test_transient_sharing_read_retries_then_validates_nonce(self):
        target = Path(self.temporary.name) / "read-response.json"
        target.write_text(json.dumps({"nonce": "owned", "seq": 1}), encoding="utf-8")
        for error in (PermissionError(13, "sharing busy"), FileNotFoundError(2, "replace gap")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("process_editor.Path.read_text", side_effect=[error,
                        json.dumps({"nonce": "owned", "seq": 1})]) as read, mock.patch("process_editor.time.sleep"):
                    answer = ProcessEditors._read(target, "owned")
                self.assertEqual(answer["seq"], 1)
                self.assertEqual(read.call_count, 2)
                with mock.patch("process_editor.Path.read_text", side_effect=[error,
                        json.dumps({"nonce": "wrong", "seq": 1})]), mock.patch("process_editor.time.sleep"):
                    with self.assertRaises(OperationError) as failure:
                        ProcessEditors._read(target, "owned")
                self.assertEqual(failure.exception.code, "editor_invalid_response")

    def test_persistent_sharing_read_fails_with_bounded_retry(self):
        target = Path(self.temporary.name) / "read-response.json"
        target.write_text(json.dumps({"nonce": "owned"}), encoding="utf-8")
        for error in (PermissionError(13, "still busy"), FileNotFoundError(2, "still absent")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch("process_editor.Path.read_text", side_effect=error) as read, \
                    mock.patch("process_editor.time.monotonic", side_effect=range(10)), mock.patch("process_editor.time.sleep"):
                with self.assertRaises((OSError, OperationError)):
                    ProcessEditors._read(target, "owned")
            self.assertLessEqual(read.call_count, 3)
        self.assertTrue(target.exists())

    def test_transient_sharing_replace_retries_then_commits_without_partial_file(self):
        target = Path(self.temporary.name) / "write-response.json"
        target.write_text('{"status":"old"}', encoding="utf-8")
        original_replace = os.replace
        attempts = []
        def replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) == 1:
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "old")
                raise PermissionError(13, "sharing busy")
            return original_replace(source, destination)
        with mock.patch("process_editor.os.replace", side_effect=replace), mock.patch("process_editor.time.sleep"):
            ProcessEditors._write(target, {"status": "new"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "new")
        self.assertFalse(target.with_name(target.name+".tmp").exists())

    def test_transient_lstat_sharing_error_rechecks_path_before_read_or_replace(self):
        target = Path(self.temporary.name) / "metadata-response.json"
        original_lstat = Path.lstat
        for operation in ("read", "replace"):
            target.write_text(json.dumps({"nonce": "owned", "status": "old"}), encoding="utf-8")
            raised = []
            def lstat(path, *args, **kwargs):
                replacing = target.with_name(target.name+".tmp").exists()
                if path == target and not raised and (operation == "read" or replacing):
                    raised.append(True)
                    raise PermissionError(13, "metadata sharing busy")
                return original_lstat(path, *args, **kwargs)
            with self.subTest(operation=operation), mock.patch("process_editor.Path.lstat", autospec=True, side_effect=lstat), \
                    mock.patch("process_editor.time.sleep"):
                if operation == "read":
                    self.assertEqual(ProcessEditors._read(target, "owned")["status"], "old")
                else:
                    ProcessEditors._write(target, {"nonce": "owned", "status": "new"})
            self.assertEqual(raised, [True])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "old" if operation == "read" else "new")
            self.assertFalse(target.with_name(target.name+".tmp").exists())

    def test_persistent_sharing_replace_keeps_original_and_cleans_owned_temporary(self):
        target = Path(self.temporary.name) / "write-response.json"
        original = '{"status":"old"}'
        target.write_text(original, encoding="utf-8")
        with mock.patch("process_editor.os.replace", side_effect=PermissionError(13, "still busy")) as replace, \
                mock.patch("process_editor.time.monotonic", side_effect=range(10)), mock.patch("process_editor.time.sleep"):
            with self.assertRaises((OSError, OperationError)):
                ProcessEditors._write(target, {"status": "new"})
        self.assertLessEqual(replace.call_count, 3)
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertFalse(target.with_name(target.name+".tmp").exists())

    def test_transient_command_read_and_event_replace_do_not_repeat_authoring_or_dispatch_input(self):
        self.context(); job = self.start()
        original_read, original_replace = Path.read_text, os.replace
        read_attempts, replace_attempts = [], []
        def read(path, *args, **kwargs):
            if path == self.paths["command"]:
                read_attempts.append(path)
                if len(read_attempts) == 1: raise PermissionError(13, "command temporarily busy")
            return original_read(path, *args, **kwargs)
        def replace(source, destination):
            if Path(destination) == self.paths["event"]:
                replace_attempts.append(destination)
                if len(replace_attempts) == 1: raise PermissionError(13, "event temporarily busy")
            return original_replace(source, destination)
        with mock.patch("process_editor.Path.read_text", autospec=True, side_effect=read), \
                mock.patch("process_editor.os.replace", side_effect=replace):
            answer = self.command("add_step", {"program_id": "editor", **TARGET, "action": "delay", "seconds": 1})
        self.assertEqual(answer["status"], "ok")
        self.assertGreaterEqual(len(read_attempts), 2)
        self.assertEqual(len(replace_attempts), 2)
        self.assertEqual(len(self.editors.jobs[job["editor_id"]]["draft"].steps), 1)
        self.assertEqual(self.runtime.calls, [])
        self.status(job, cancel=True); self.terminal(job)

    def test_nested_picker_cleanup_failure_blocks_more_authoring_and_business_input(self):
        self.context(); job = self.start()
        failure = OperationError("picker still running", "picker_close_failed")
        failure.helper_cleanup_pending = True
        with mock.patch("process_editor._run_helper", side_effect=failure):
            atomic_json(self.paths["command"], {"nonce": self.nonce, "seq": 1, "action": "pick_element",
                "payload": {"program_id": "editor", **TARGET, "purpose": "action"}})
            done = self.terminal(job)
        self.assertTrue(done["cleanup_pending"])
        self.assertEqual(done["diagnostic"]["code"], "picker_close_failed")
        self.assertEqual(self.start()["editor_id"], job["editor_id"])
        self.assertEqual(self.spawn_count, 1)
        operation = self.manager.call("get_window_state", TARGET)
        self.assertTrue(operation["isError"])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.manager.tasks.all(), [])

    def test_authoring_blocks_business_operations(self):
        self.context(); job = self.start()
        for name in ("computer_launch", "computer_perform", "get_window_state", "computer_teach_element"):
            answer = self.manager.call(name, {})
            self.assertTrue(answer["isError"])
            self.assertEqual(answer["structuredContent"]["editor_id"], job["editor_id"])
        self.assertEqual(self.runtime.calls, [])
        self.status(job, cancel=True); self.terminal(job)

    def test_missing_helper_and_forged_ready_never_claim_visible(self):
        with mock.patch("process_editor.Path.is_file", return_value=False):
            failed = self.start()
        self.assertEqual(failed["diagnostic"]["code"], "editor_missing")
        self.assertFalse(failed["editor_visible"])
        self.context()
        with mock.patch("process_editor._helper_visible", return_value=False):
            failed = self.start()
        self.assertEqual(failed["diagnostic"]["code"], "editor_not_visible")
        self.assertTrue(self.child.terminated)

    def test_bad_nonce_or_skipped_sequence_stops_before_authoring(self):
        self.context(); job = self.start()
        atomic_json(self.paths["command"], {"nonce": "wrong", "seq": 1, "action": "add_step", "payload": {}})
        failed = self.terminal(job)
        self.assertEqual(failed["diagnostic"]["code"], "editor_invalid_response")
        self.child = Child(); job = self.start()
        atomic_json(self.paths["command"], {"nonce": self.nonce, "seq": 2, "action": "add_step", "payload": {}})
        failed = self.terminal(job)
        self.assertEqual(failed["diagnostic"]["code"], "editor_invalid_sequence")
        self.assertEqual(self.manager.tasks.all(), [])

    def test_repeated_sequence_is_not_replayed(self):
        self.context(); job = self.start()
        payload = {"program_id": "editor", **TARGET, "action": "delay", "seconds": 1}
        answer = self.command("add_step", payload)
        self.assertEqual(len(answer["steps"]), 1)
        answer = self.command("add_step", payload, sequence=1)
        self.assertEqual(len(answer["steps"]), 1)
        self.assertEqual(len(self.editors.jobs[job["editor_id"]]["draft"].steps), 1)
        self.status(job, cancel=True); self.terminal(job)

    def test_save_failure_preserves_editable_draft_then_saves_without_execution(self):
        self.context(); job = self.start()
        self.command("add_step", {"program_id": "editor", **TARGET, "action": "delay", "seconds": .5})
        with mock.patch.object(self.manager.tasks, "save", side_effect=OperationError("disk unavailable", "store_error")):
            failed = self.command("save", {"name": "retry", "description": ""})
        self.assertEqual(failed["status"], "error")
        self.assertEqual(len(failed["steps"]), 1)
        self.assertEqual(self.status(job)["status"], "editing")
        self.assertEqual(self.manager.tasks.all(), [])
        saved = self.command("save", {"name": "retry", "description": ""})
        task_id = saved["saved_task"]["id"]
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "closed"})
        done = self.terminal(job)
        self.assertEqual(done["status"], "saved")
        self.assertEqual(self.manager.tasks.get(task_id)["steps"][0]["duration_ms"], 500)
        self.assertFalse(done["input_dispatched"])
        self.assertEqual(self.runtime.calls, [])

    def test_unsupported_native_projection_offers_image_without_save_or_execution(self):
        self.context(); job = self.start()
        self.runtime.answer["structuredContent"] = {**TARGET, "elements": [], "elements_complete": False,
            "tree_markdown": '- Window "Synthetic rendered screen"\n'}
        with mock.patch("process_editor._run_helper", return_value=native_choice()), \
                mock.patch.object(self.editors, "_visual", return_value=image_choice()) as visual:
            answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["selection"]["recognition"], "image")
        self.assertEqual(visual.call_count, 1)
        self.assertEqual(answer["steps"], [])
        self.assertEqual(self.status(job)["status"], "editing")
        self.assertEqual(self.manager.tasks.all(), [])
        self.assertEqual(self.runtime.mutations, [])
        self.assertEqual([name for name, _ in self.runtime.calls], ["get_window_state"])
        self.status(job, cancel=True); self.terminal(job)

    def test_picker_cancel_protection_mismatch_and_cleanup_never_fall_back(self):
        self.context(); job = self.start()
        for code in ("picker_cancelled", "protected_element", "target_mismatch", "picker_not_visible", "picker_read_timeout"):
            with self.subTest(code=code), mock.patch("process_editor._run_helper", side_effect=OperationError("blocked", code)), \
                    mock.patch.object(self.editors, "_visual") as visual:
                answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
                self.assertEqual(answer["status"], "error"); self.assertEqual(answer["code"], code)
                visual.assert_not_called()
        self.status(job, cancel=True); self.terminal(job)

    def test_selected_but_unmatched_falls_back_once_to_visible_image_picker(self):
        self.context(); job = self.start()
        with mock.patch("process_editor._run_helper", side_effect=OperationError("unmatched", "picker_not_found")), \
                mock.patch.object(self.editors, "_visual", return_value=image_choice()) as visual:
            answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.assertEqual(answer["status"], "ok"); self.assertEqual(answer["selection"]["recognition"], "image")
        self.assertEqual(visual.call_count, 1); self.assertEqual(self.runtime.mutations, [])
        self.assertNotIn("thumbnail_png", json.dumps(self.status(job)))
        self.status(job, cancel=True); self.terminal(job)

    def test_native_button_missing_from_driver_explains_distinction_and_keeps_diagnostic(self):
        self.context(); job = self.start()
        native = native_choice()
        native["element"].update(role="Button", automation_id="RunButton", name="private button caption")
        with mock.patch("process_editor._run_helper", return_value=native), \
                mock.patch.object(self.editors, "_visual", return_value=image_choice()) as visual:
            answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["recognition_diagnostic"]["reason"], "role_not_projected")
        self.assertEqual(answer["recognition_diagnostic"]["role_candidates"], 0)
        label = visual.call_args.args[2]["label"]
        self.assertIn("요소 유형을 인식했지만", label)
        self.assertNotIn("버튼 정보가 없어", label)
        self.assertEqual(visual.call_count, 1)
        state = self.status(job)
        self.assertEqual(state["last_recognition"]["reason"], "role_not_projected")
        self.assertNotIn("private button caption", json.dumps(state))
        self.assertNotIn("RunButton", json.dumps(state))
        self.assertEqual(self.runtime.mutations, [])
        self.assertEqual([name for name, _ in self.runtime.calls], ["get_window_state"])
        self.status(job, cancel=True); self.terminal(job)

    def test_image_capture_failure_retains_matching_reason_and_success_clears_it(self):
        self.context(); job = self.start()
        failure = OperationError("not found", "picker_not_found")
        failure.picker_diagnostic = {"reason": "geometry_mismatch", "geometry_rejected": 1,
                                     "point": {"x": 123, "y": 456}, "name": "private"}
        with mock.patch("process_editor._run_helper", side_effect=failure), \
                mock.patch.object(self.editors, "_visual", side_effect=OperationError("capture cancelled", "visual_cancelled")):
            answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.assertEqual(answer["status"], "error")
        self.assertEqual(answer["code"], "visual_cancelled")
        info = self.status(job)["last_recognition"]
        self.assertEqual(info["reason"], "geometry_mismatch")
        self.assertEqual(info["geometry_rejected"], 1)
        self.assertNotIn("point", info); self.assertNotIn("name", info)
        with mock.patch("process_editor._run_helper", return_value=native_choice()), \
                mock.patch.object(self.editors, "_visual") as visual:
            answer = self.command("pick_element", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.assertEqual(answer["selection"]["recognition"], "uia")
        self.assertNotIn("last_recognition", self.status(job))
        visual.assert_not_called()
        self.assertEqual(self.runtime.mutations, [])
        self.status(job, cancel=True); self.terminal(job)

    def test_recording_modes_replace_exact_group_and_preserve_other_steps(self):
        self.context(); job = self.start()
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", side_effect=[{"events": [event], "nonce": str(i)} for i in range(4)]):
            for mode in ("append", "append"):
                self.assertEqual(self.command("record", {"mode": mode})["status"], "ok")
            before = copy.deepcopy(self.editors.jobs[job["editor_id"]]["draft"].steps)
            self.assertEqual(len(before), 4)
            replaced = self.command("record", {"mode": "replace_selected", "index": 0})
            self.assertEqual(replaced["status"], "ok", replaced)
            after = self.editors.jobs[job["editor_id"]]["draft"].steps
            self.assertEqual(len(after), 4)
            self.assertEqual([{k: v for k, v in s.items() if k != "step_id"} for s in after[2:]], before[2:])
            self.assertEqual(self.command("record", {"mode": "replace_all"})["status"], "ok")
            self.assertEqual(len(self.editors.jobs[job["editor_id"]]["draft"].steps), 2)
        self.status(job, cancel=True); self.terminal(job)

    def test_failed_recording_replacement_keeps_old_draft(self):
        self.context(); job = self.start()
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event]}):
            self.assertEqual(self.command("record", {})["status"], "ok")
        before = copy.deepcopy(self.editors.jobs[job["editor_id"]]["draft"].steps)
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", side_effect=OperationError("cancelled")):
            self.assertNotEqual(self.command("record", {"mode": "replace_all"})["status"], "ok")
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps, before)
        self.status(job, cancel=True); self.terminal(job)

    def test_save_new_task_keeps_existing_task_and_revision_unchanged(self):
        self.context(); job = self.start()
        draft = self.editors.jobs[job["editor_id"]]["draft"]
        draft.steps = [{"operation": "delay", "program_id": "editor", "duration_ms": 0}]
        draft.labels = ["현재 창"]
        first = self.command("save", {"name": "original", "description": ""})
        self.assertEqual(first["status"], "ok", first)
        original = copy.deepcopy(self.manager.tasks.get(first["saved_task"]["id"]))
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)
        self.child = Child(); self.seq = 0
        job = self.start(task_id=original["id"])
        copied = self.command("save", {"name": "copy", "description": "", "save_mode": "copy"})
        self.assertEqual(copied["status"], "ok", copied)
        self.assertNotEqual(copied["saved_task"]["id"], original["id"])
        self.assertEqual(self.manager.tasks.get(original["id"]), original)
        self.status(job, cancel=True); self.terminal(job)

    def test_recording_reserves_remaining_step_capacity_without_changing_configuration(self):
        self.context(); job = self.start()
        draft = self.editors.jobs[job["editor_id"]]["draft"]
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        for count, expected in ((970, 10), (997, 1), (998, 0)):
            draft = self.editors.jobs[job["editor_id"]]["draft"]
            draft.steps = [{"operation": "delay", "program_id": "editor", "duration_ms": 0} for _ in range(count)]
            draft.labels = ["현재 창"] * count
            with self.subTest(count=count), mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                    mock.patch.object(self.editors, "_visual", return_value={"events": [event], "nonce": str(count)}) as visual:
                result = self.command("record", {})
                if expected:
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual(visual.call_args.args[2]["max_events"], expected)
                    self.assertEqual(len(self.editors.jobs[job["editor_id"]]["draft"].steps), count + 2)
                else:
                    self.assertNotEqual(result["status"], "ok")
                    visual.assert_not_called()
                    self.assertEqual(len(draft.steps), count)
        self.status(job, cancel=True); self.terminal(job)

    def test_missing_recorded_captures_report_incomplete_but_preserve_receipt_and_draft_save(self):
        self.context(); job = self.start()
        events = [{"operation": "manual_entry", "program_id": "editor", "reason": reason}
                  for reason in ("image_capture_required", "image_occluded_requires_selection")]
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": events}):
            recorded = self.command("record", {})
            reanalyzed = self.command("reimport_recording", {})
        for result in (recorded, reanalyzed):
            self.assertEqual(result["status"], "ok", result)
            self.assertIn("캡처 누락 2개", result["message"])
            self.assertIn("녹화 원본은 보존", result["message"])
            self.assertIn("아직 재생할 수 없습니다", result["message"])
            self.assertNotIn("다른 창", result["message"])
            self.assertFalse(result["readiness"]["runnable"])
            self.assertTrue(result["recording_receipt_available"])
        saved = self.command("save", {"name": "incomplete capture", "description": ""})
        self.assertEqual(saved["status"], "ok", saved)
        self.assertFalse(saved["saved_task"]["runnable"])
        self.assertEqual(saved["saved_task"]["unresolved_steps"], 2)
        self.assertEqual(self.runtime.mutations, [])
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)

    def test_recording_pending_input_saves_reopens_and_repairs_same_task(self):
        self.context(); job = self.start()
        event = {**image_choice(), "operation": "manual_entry", "program_id": "editor"}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event]}) as visual:
            recorded = self.command("record", {})
        self.assertEqual(recorded["status"], "ok"); self.assertTrue(recorded["steps"][0]["requires_input"])
        self.assertEqual(visual.call_args.args[2]["max_events"], MAX_RECORDING_EVENTS)  # Each event reserves input, review and popup wait.
        self.assertEqual(self.manager.tasks.all(), [])
        drafted = self.command("save", {"name": "recorded", "description": ""})
        self.assertEqual(drafted["status"], "ok", drafted)
        self.assertFalse(drafted["saved_task"]["runnable"])
        self.assertEqual(drafted["saved_task"]["unresolved_steps"], 1)
        task_id = drafted["saved_task"]["id"]
        original = self.manager.tasks.get(task_id)
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)
        self.child = Child(); self.seq = 0
        job = self.start(task_id=task_id)
        self.assertEqual(job["status"], "editing", job)
        self.assertTrue(job["steps"][0]["requires_input"])
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps, original["steps"])
        with mock.patch.object(self.editors, "test_runner") as run:
            blocked = self.command("test_run", {"name": "recorded", "description": ""})
        self.assertEqual(blocked["code"], "recording_input_required")
        run.assert_not_called()
        resolved = self.command("resolve_input", {"index": 0, "value": "intended"})
        self.assertEqual(resolved["status"], "ok")
        saved = self.command("save", {"name": "recorded", "description": ""})
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["saved_task"]["id"], task_id)
        self.assertTrue(saved["saved_task"]["runnable"])
        final = self.manager.tasks.get(task_id)
        self.assertNotIn("draft", final)
        self.assertEqual(final["steps"][0]["step_id"], original["steps"][0]["step_id"])
        self.assertEqual(final["steps"][1:], original["steps"][1:])
        value = self.manager.call("computer_get_task", {"id": saved["saved_task"]["id"]})
        self.assertNotIn("template_png", json.dumps(value))
        self.assertEqual(self.runtime.mutations, [])
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)

    def test_refused_recording_preflight_never_opens_unusable_recorder_and_keeps_draft(self):
        self.context(); job = self.start()
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        draft = self.editors.jobs[job["editor_id"]]["draft"]
        previous = copy.deepcopy(draft.steps)
        blocked = {"status": "blocked", "ready_for_input": False, "capture_verified": False,
            "diagnostic": {"code": "image_capture_refused", "message": "Driver session expired"}}
        with mock.patch("replay_preflight.image_replay_preflight", return_value=blocked), \
                mock.patch.object(self.editors, "_visual") as visual:
            answer = self.command("record", {})
        self.assertEqual(answer["code"], "image_capture_refused")
        self.assertIn("Driver session expired", answer["message"])
        self.assertIn("아직 녹화를 시작하지 않았습니다", answer["message"])
        visual.assert_not_called()
        self.assertEqual(draft.steps, previous)
        self.assertEqual(self.status(job)["recording_preflight"][0]["diagnostic"], blocked["diagnostic"])

    def test_dpi_unaware_app_records_verified_uia_without_image_or_scale_change(self):
        self.context(); job = self.start()
        event, snapshots = RecordingSemanticTests().event(value="final")
        event = {key: event[key] for key in ("program_id", "operation", "native_target", "after", "value")}
        snapshot = snapshots[("editor", "main")]
        blocked = {"status": "blocked", "ready_for_input": False, "ready_for_capture": True,
                   "capture_verified": False, "diagnostic": {"code": "image_dpi_unsupported", "message": "scale mismatch"}}
        with mock.patch("replay_preflight.image_replay_preflight", return_value=blocked), \
                mock.patch.object(self.editors.library, "_observe", return_value=(snapshot, "binding")), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event], "warnings": []}) as visual:
            answer = self.command("record", {})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertFalse(visual.call_args.args[2]["targets"][0]["image_allowed"])
        preflight = self.status(job)["recording_preflight"][0]
        self.assertEqual(preflight["recording_mode"], "uia_only")
        self.assertFalse(preflight["ready_for_input"])
        self.assertFalse(preflight["capture_verified"])
        draft = self.editors.jobs[job["editor_id"]]["draft"]
        self.assertEqual([step["operation"] for step in draft.steps], ["set_value"])
        self.assertEqual(draft.steps[0]["expect"][0]["equals"], "final")
        self.assertNotIn("template_png", json.dumps(draft.steps))
        self.assertEqual(self.runtime.mutations, [])
        self.status(job, cancel=True); self.terminal(job)

    def test_semantic_only_preflight_requires_fresh_actionable_nonpassword_uia(self):
        self.context(); job = self.start()
        blocked = {"ready_for_input": False, "ready_for_capture": True,
                   "diagnostic": {"code": "image_dpi_unsupported", "message": "scale mismatch"}}
        snapshots = [{**TARGET, "elements": []}, {**TARGET, "elements": [
            {"role": "Edit", "actions": ["set_value"], "is_password": True}]}]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot), mock.patch("replay_preflight.image_replay_preflight", return_value=blocked), \
                    mock.patch.object(self.editors.library, "_observe", return_value=(snapshot, "binding")), \
                    mock.patch.object(self.editors, "_visual") as visual:
                answer = self.command("record", {})
                self.assertEqual(answer["code"], "image_dpi_unsupported")
                visual.assert_not_called()
        with mock.patch("replay_preflight.image_replay_preflight", return_value=blocked), \
                mock.patch.object(self.editors.library, "_observe", side_effect=OperationError("Driver refused", "observation_failed")), \
                mock.patch.object(self.editors, "_visual") as visual:
            answer = self.command("record", {})
        self.assertEqual(answer["code"], "observation_failed")
        visual.assert_not_called()
        self.status(job, cancel=True); self.terminal(job)

    def test_semantic_only_fallback_never_bypasses_other_capture_or_identity_failures(self):
        self.context(); job = self.start()
        for code in ("image_target_changed", "image_coordinate_mismatch", "image_geometry_unavailable", "image_capture_refused"):
            blocked = {"ready_for_input": False, "ready_for_capture": True, "diagnostic": {"code": code, "message": code}}
            with self.subTest(code=code), mock.patch("replay_preflight.image_replay_preflight", return_value=blocked), \
                    mock.patch.object(self.editors.library, "_observe") as observe, mock.patch.object(self.editors, "_visual") as visual:
                answer = self.command("record", {})
                self.assertEqual(answer["code"], code)
                observe.assert_not_called(); visual.assert_not_called()
        self.status(job, cancel=True); self.terminal(job)

    def test_capture_backlog_partial_recording_saves_and_reopens_reviewable_draft(self):
        self.context(); job = self.start()
        receipt = {"events": [{**image_choice(), "operation": "click", "program_id": "editor"}],
                   "warnings": ["recording_capture_backlog"]}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value=receipt):
            recorded = self.command("record", {})
        self.assertEqual(recorded["status"], "ok", recorded)
        self.assertEqual(recorded["recording_review"]["warning_codes"], ["recording_capture_backlog"])
        self.assertIn("캡처 처리 대기", recorded["recording_review"]["messages"][0])
        self.assertEqual(len(recorded["steps"]), 2)
        saved = self.command("save", {"name": "partial backlog", "description": ""})
        self.assertEqual(saved["status"], "ok", saved)
        self.assertFalse(saved["saved_task"]["runnable"])
        self.assertTrue(saved["saved_task"]["review_required"])
        task = self.manager.tasks.get(saved["saved_task"]["id"])
        self.assertEqual(task["recording_review"]["warning_codes"], ["recording_capture_backlog"])
        self.assertFalse(task["recording_review"]["acknowledged"])
        self.assertEqual(task["steps"][0]["image_target"]["template_png"], receipt["events"][0]["template_png"])
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)
        self.child = Child(); self.seq = 0
        reopened = self.start(task_id=task["id"])
        self.assertEqual(reopened["status"], "editing", reopened)
        with mock.patch.object(self.editors, "test_runner") as run:
            blocked = self.command("test_run", {"name": "partial backlog", "description": ""})
        self.assertEqual(blocked["code"], "recording_review_required")
        run.assert_not_called()
        self.status(reopened, cancel=True); self.terminal(reopened)
        self.assertEqual(self.manager.tasks.get(task["id"]), task)

    def test_recording_receipt_reanalysis_is_readonly_and_never_duplicates_or_overwrites_edits(self):
        self.context(); opened = self.start(); job = self.editors.jobs[opened["editor_id"]]
        event = {**image_choice(), "program_id": "editor", "operation": "click"}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event]}):
            result = self.command("record", {})
        self.assertEqual(result["status"], "ok")
        original = copy.deepcopy(job["draft"].steps)
        receipt = job["last_recording"]
        raw = receipt["path"].read_bytes()
        with mock.patch.object(self.editors, "_visual") as visual:
            reimported = self.command("reimport_recording", {})
        self.assertEqual(reimported["status"], "ok"); visual.assert_not_called()
        self.assertEqual(job["draft"].steps, original)
        self.assertEqual(receipt["path"].read_bytes(), raw)
        self.assertNotIn("template_png", json.dumps(self.status(opened)))
        self.assertFalse(self.status(opened)["recording_import"]["input_dispatched"])
        self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        edited = copy.deepcopy(job["draft"].steps)
        self.assertEqual(self.command("reimport_recording", {})["code"], "recording_draft_changed")
        self.assertEqual(job["draft"].steps, edited)
        self.assertEqual(self.runtime.mutations, [])
        self.status(opened, cancel=True); self.terminal(opened)
        self.assertTrue(receipt["path"].is_file())

    def test_failed_normalization_preserves_original_receipt_and_draft_for_retry(self):
        self.context(); opened = self.start(); job = self.editors.jobs[opened["editor_id"]]
        event = {**image_choice(), "program_id": "editor", "operation": "click"}
        before = copy.deepcopy(job["draft"].__dict__)
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event]}), \
                mock.patch.object(ProcessDraft, "recorded", side_effect=OperationError("normalizer failed", "test_failure")):
            result = self.command("record", {})
        self.assertEqual(result["code"], "test_failure")
        self.assertTrue(result["recording_receipt_available"])
        self.assertEqual(job["draft"].__dict__, before)
        receipt = job["last_recording"]
        self.assertEqual(json.loads(receipt["path"].read_text(encoding="utf-8"))["recording"]["events"], [event])
        with mock.patch.object(self.editors, "_visual") as visual:
            recovered = self.command("reimport_recording", {})
        self.assertEqual(recovered["status"], "ok"); visual.assert_not_called()
        self.assertEqual([s["operation"] for s in job["draft"].steps], ["image_click", "checkpoint"])
        self.assertEqual(self.runtime.mutations, [])
        self.status(opened, cancel=True); self.terminal(opened)

    def test_recorded_closed_popup_updates_editor_choices_and_saves_without_raw_handles(self):
        self.context(); job = self.start()
        window = {"program_id": "editor", "window_ref": "popup_1", "owner_ref": "main", "pid": TARGET["pid"],
            "window_id": TARGET["window_id"] + 1, "owner_window_id": TARGET["window_id"], "title": "Recorded popup",
            "class_name": "OwnedDialog", "owner_verified": True}
        event = {**image_choice(), "operation": "click", "program_id": "editor", "window_ref": "popup_1"}
        with mock.patch("replay_preflight.image_replay_preflight", return_value={"ready_for_input": True}), \
                mock.patch.object(self.editors, "_visual", return_value={"events": [event], "windows": [window]}):
            answer = self.command("record", {})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(len(answer["programs"]), 2)
        self.assertEqual([s["action"] for s in answer["steps"]], ["wait_for_window", "image_click", "checkpoint"])
        saved = self.command("save", {"name": "popup recording", "description": ""})
        self.assertEqual(saved["status"], "ok", saved)
        encoded = json.dumps(self.manager.tasks.all())
        self.assertNotIn('"window_id"', encoded)
        self.assertNotIn('"pid"', encoded)
        self.assertEqual(self.runtime.mutations, [])
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)

    def test_explicit_image_repair_bypasses_uia_and_preserves_other_steps(self):
        self.context(); job = self.start()
        draft = self.editors.jobs[job["editor_id"]]["draft"]
        draft.recorded([{"program_id": "editor", "operation": "manual_entry", "reason": "image_capture_required", "requested_operation": "click"}])
        self.editors.jobs[job["editor_id"]]["result"]["recording_import"] = {"unresolved_steps": 1, "diagnostics": [{"code": "image_capture_required"}]}
        old_checkpoint = copy.deepcopy(draft.steps[1])
        with mock.patch("process_editor._run_helper") as native, mock.patch.object(self.editors, "_visual", return_value=image_choice()) as visual:
            for invalid in ("wrong", None, {"method": "image"}):
                answer = self.command("retarget_step", {"index": 0, "action": "click", "method": invalid})
                self.assertEqual(answer["status"], "error")
            visual.assert_not_called(); native.assert_not_called()
            answer = self.command("retarget_step", {"index": 0, "action": "click", "method": "image"})
            self.assertEqual(answer["status"], "ok", answer)
            native.assert_not_called(); self.assertEqual(visual.call_count, 1)
        self.assertEqual(draft.steps[0]["operation"], "image_click")
        self.assertEqual(draft.steps[1], old_checkpoint)
        imported = self.status(job)["recording_import"]
        self.assertEqual(imported["unresolved_steps"], 0)
        self.assertEqual(imported["unresolved_at_import"], 1)
        self.assertEqual(imported["diagnostics_phase"], "original_import")
        self.assertEqual(imported["diagnostics"], [{"code": "image_capture_required"}])
        self.assertEqual(self.runtime.mutations, [])
        self.status(job, cancel=True); self.terminal(job)

    def test_step_thumbnail_is_only_returned_to_native_preview_not_mcp_status(self):
        self.context(); job = self.start()
        anchor = {"x": .125, "y": .875}
        with mock.patch.object(self.editors, "_visual", return_value=image_choice(anchor=anchor)):
            picked = self.command("pick_image", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.command("add_step", {"program_id": "editor", **TARGET, "action": "click", "selection_id": picked["selection"]["selection_id"]})
        preview = self.command("preview_step", {"index": 0})
        self.assertEqual(preview["preview"]["thumbnail_png"], image_choice()["template_png"])
        self.assertEqual(preview["preview"]["preview_anchor"], anchor)
        preview["preview"]["preview_anchor"]["x"] = .9
        self.assertEqual(self.editors.jobs[job["editor_id"]]["draft"].steps[0]["image_target"]["anchor"], anchor)
        self.assertNotIn("steps", preview)
        self.assertNotIn(image_choice()["template_png"], json.dumps(self.status(job)))
        self.assertNotIn("preview_anchor", json.dumps(self.status(job)))
        self.assertEqual(self.runtime.mutations, [])
        self.status(job, cancel=True); self.terminal(job)

    def visual_spawn(self, child, *, nonce_override=None, ready_pid=None, confirmed=True):
        def spawn(args, **kwargs):
            self.assertIn(args[1], {"--pick", "--record"})
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            response = Path(args[3])
            atomic_json(Path(str(response) + ".ready.json"), {"nonce": request["nonce"], "status": "ready",
                "helper_pid": child.pid if ready_pid is None else ready_pid, "helper_window_id": 9998})
            atomic_json(response, {**image_choice(), "nonce": nonce_override or request["nonce"], "human_confirmed": confirmed})
            return child
        return spawn

    def test_visual_wrapper_validates_nonce_ready_and_confirmation_and_cleans_owned_files(self):
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        for override, expected in (({}, None), ({"nonce_override": "foreign"}, "editor_invalid_response"),
                ({"ready_pid": 99901}, "visual_helper_not_visible"), ({"confirmed": False}, "visual_invalid_response")):
            with self.subTest(override=override):
                child = Child(); child.pid = 888
                with mock.patch("process_editor.subprocess.Popen", side_effect=self.visual_spawn(child, **override)):
                    if expected:
                        with self.assertRaises(OperationError) as caught:
                            self.editors._visual(job, "pick", TARGET, 30)
                        self.assertEqual(caught.exception.code, expected)
                    else:
                        value = self.editors._visual(job, "pick", TARGET, 30)
                        self.assertTrue(value["human_confirmed"])
                self.assertTrue(child.terminated)
                self.assertEqual(list((self.runtime.run_dir / "visual").iterdir()), [])
        self.status(answer, cancel=True); self.terminal(answer)

    def test_visual_ready_can_reappear_after_capture_without_accepting_invisible_window(self):
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        child = Child(); child.pid = 888; exchanged = {}; checks = []
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            exchanged.update(nonce=request["nonce"], response=Path(args[3]))
            atomic_json(Path(args[3]+".ready.json"), {"nonce": request["nonce"], "status": "ready", "helper_pid": child.pid, "helper_window_id": 9998})
            return child
        def visible(pid, hwnd):
            checks.append((pid, hwnd))
            if len(checks) == 1: return False
            atomic_json(exchanged["response"], {**image_choice(), "nonce": exchanged["nonce"]})
            return True
        with mock.patch("process_editor.subprocess.Popen", side_effect=spawn), mock.patch("process_editor._helper_visible", side_effect=visible):
            result = self.editors._visual(job, "pick", TARGET, 30)
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue(result["human_confirmed"]); self.assertTrue(child.terminated)
        self.status(answer, cancel=True); self.terminal(answer)

    def test_visual_receives_only_guard_pinned_hosted_identity_and_rejects_midflight_replacement(self):
        identity = {"app_pid": 12, "app_started": 50, "app_window_id": 23}
        self.runtime.guard.hosted_target = mock.Mock(side_effect=lambda target: copy.deepcopy(identity))
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        child = Child(); child.pid = 888
        expected = copy.deepcopy(identity)
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            self.assertEqual(request["hosted_target"], expected)
            identity["app_started"] = 60
            return child
        supplied = {**TARGET, "hosted_target": {"app_pid": 999, "app_window_id": 999}}
        with mock.patch("process_editor.subprocess.Popen", side_effect=spawn), self.assertRaises(OperationError) as caught:
            self.editors._visual(job, "pick", supplied, 30)
        self.assertEqual(caught.exception.code, "editor_target_changed")
        self.assertEqual(supplied["hosted_target"]["app_pid"], 999)
        self.assertTrue(child.terminated)
        self.status(answer, cancel=True); self.terminal(answer)

    def test_recording_payload_does_not_grant_injected_or_other_target_hosted_identity(self):
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        child = Child(); child.pid = 888
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            self.assertNotIn("hosted_target", request["targets"][0])
            atomic_json(Path(args[3]+".ready.json"), {"nonce": request["nonce"], "status": "ready", "helper_pid": 888, "helper_window_id": 9998})
            atomic_json(Path(args[3]+".progress.json"), {"nonce": request["nonce"], "helper_pid": 888,
                "state": "review", "event_count": 0, "manual_count": 0, "max_events": 15,
                "warning_codes": [], "probe": {"hooks": "available", "uia": "available"}})
            atomic_json(Path(args[3]), {"nonce": request["nonce"], "status": "recorded", "human_confirmed": True, "events": [], "warnings": []})
            return child
        with mock.patch("process_editor.subprocess.Popen", side_effect=spawn):
            result = self.editors._visual(job, "record", {"targets": [{**TARGET, "hosted_target": {"app_pid": 999}}]}, 30)
        self.assertEqual(result["status"], "recorded")
        self.status(answer, cancel=True); self.terminal(answer)

    def test_recording_progress_is_reported_and_owned_files_are_cleaned(self):
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        child = Child(); child.pid = 888
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            atomic_json(Path(args[3]+".ready.json"), {"nonce": request["nonce"], "status": "ready", "helper_pid": 888, "helper_window_id": 9998})
            atomic_json(Path(args[3]+".progress.json"), {"nonce": request["nonce"], "helper_pid": 888,
                "state": "review", "event_count": 1, "manual_count": 0, "max_events": 15,
                "warning_codes": ["outside_target_not_recorded"], "probe": {"hooks": "available", "uia": "available"}})
            atomic_json(Path(args[3]), {"nonce": request["nonce"], "status": "recorded", "human_confirmed": True,
                "events": [{**image_choice(), "program_id": "editor", "operation": "click"}], "warnings": ["outside_target_not_recorded"]})
            return child
        with mock.patch("process_editor.subprocess.Popen", side_effect=spawn):
            result = self.editors._visual(job, "record", {"targets": [TARGET]}, 30)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(job["result"]["recording"]["event_count"], 1)
        self.assertFalse(job["result"]["recording"]["task_verified"])
        self.assertEqual(list((self.runtime.run_dir / "visual").iterdir()), [])
        self.assertTrue(child.terminated)
        self.status(answer, cancel=True); self.terminal(answer)

    def test_recording_result_without_heartbeat_is_not_accepted(self):
        self.context(); answer = self.start(); job = self.editors.jobs[answer["editor_id"]]
        child = Child(); child.pid = 888
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            atomic_json(Path(args[3]+".ready.json"), {"nonce": request["nonce"], "status": "ready", "helper_pid": 888, "helper_window_id": 9998})
            atomic_json(Path(args[3]), {"nonce": request["nonce"], "status": "recorded", "human_confirmed": True, "events": []})
            return child
        with mock.patch("process_editor.subprocess.Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            self.editors._visual(job, "record", {"targets": [TARGET]}, 30)
        self.assertEqual(failure.exception.code, "recording_progress_missing")
        self.assertEqual(job["result"]["recording"]["state"], "failed")
        self.assertEqual(list((self.runtime.run_dir / "visual").iterdir()), [])
        self.assertTrue(child.terminated)
        self.status(answer, cancel=True); self.terminal(answer)

    def test_cancel_during_accepted_store_write_is_responsive_and_reports_saved_result(self):
        self.context(); job = self.start()
        self.command("add_step", {"program_id": "editor", **TARGET, "action": "delay", "seconds": .5})
        entered, release = threading.Event(), threading.Event()
        original_save = self.manager.tasks.save
        def blocked_save(task):
            entered.set()
            if not release.wait(5): raise AssertionError("store gate was not released")
            return original_save(task)
        with mock.patch.object(self.manager.tasks, "save", side_effect=blocked_save):
            atomic_json(self.paths["command"], {"nonce": self.nonce, "seq": 2, "action": "save",
                                               "payload": {"name": "accepted save", "description": ""}})
            try:
                self.assertTrue(entered.wait(3), "store write was not reached")
                started = time.monotonic()
                answer = self.status(job, cancel=True)
                self.assertLess(time.monotonic()-started, .5, answer)
                self.assertTrue(answer["save_already_started"])
                self.assertEqual(answer["status"], "cancelling")
                self.assertEqual(self.manager.tasks.all(), [])
            finally:
                release.set()
            done = self.terminal(job)
        self.assertEqual(done["status"], "saved")
        self.assertEqual(len(self.manager.tasks.all()), 1)
        self.assertFalse(done["input_dispatched"])
        self.assertTrue(self.child.terminated)

    def test_vision_connection_cannot_forward_unverified_human_acknowledgement(self):
        self.manager.image_delivery.check(delivery_mode="vision")
        task = self.manager.tasks.save({"name": "review", "instructions": "review", "expected": "human review",
            "program_ids": ["editor"], "steps": [{"operation": "checkpoint", "program_id": "editor", "message": "Review"}],
            "variables": {}})
        content = {"type": "image", "mimeType": "image/png", "data": "c3ludGhldGlj"}
        with mock.patch.object(self.manager.workflows, "run", return_value={"status": "needs_review", "task_verified": False,
                "checkpoint": {"id": "review-id"}, "checkpoint_content": [content]}) as run:
            response = self.manager.call("computer_run_task", {"task_id": task["id"], "targets": self.args["targets"],
                "acknowledge_checkpoint": "earlier-reviewed-id"})
        self.assertTrue(response.get("isError", False), response)
        self.assertFalse(response["structuredContent"]["task_verified"])
        self.assertFalse(response["structuredContent"]["input_dispatched"])
        self.assertNotIn("checkpoint_content", response["structuredContent"])
        run.assert_not_called()

    def test_loading_and_saving_preserves_identity_as_new_revision(self):
        original = self.manager.tasks.save({"name": "original", "instructions": "old", "expected": "old",
            "program_ids": ["editor"], "steps": [{"operation": "delay", "program_id": "editor", "duration_ms": 100}], "variables": {}})
        self.context(); job = self.start(task_id=original["id"])
        saved = self.command("save", {"name": "copy", "description": "new"})
        self.assertEqual(saved["status"], "ok", saved)
        self.assertEqual(saved["saved_task"]["id"], original["id"], saved)
        self.assertEqual(saved["saved_task"]["revision"], original.get("revision", 1) + 1)
        self.status(job, cancel=True)
        done = self.terminal(job)
        self.assertEqual(done["status"], "saved")
        self.assertEqual(self.manager.tasks.get(original["id"])["name"], "copy")
        self.assertEqual(len(self.manager.tasks.all()), 1)


@unittest.skipUnless(os.name == "nt" and Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Windows .NET compiler required")
class NativePreviewAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-preview-anchor-")
        folder = Path(cls.directory.name)
        source = folder / "probe.cs"
        source.write_text(r'''
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Web.Script.Serialization;
internal static class PreviewAnchorProbe {
 static Dictionary<string,object> Anchor(object x, object y) { return new Dictionary<string,object>{{"x",x},{"y",y}}; }
 static int Main(string[] args) {
  var json=new JavaScriptSerializer(); PointF p;
  if(args[0]=="invalid") {
   bool accepted=false;
   foreach(object bad in new object[]{true,"0.5",Double.NaN,Double.PositiveInfinity,-.01,1.01,null})
    accepted |= ProcessEditor.PreviewAnchorPoint(Anchor(bad,.5),new Size(100,100),new Size(492,150),out p);
   accepted |= ProcessEditor.PreviewAnchorPoint(null,new Size(100,100),new Size(492,150),out p);
   accepted |= ProcessEditor.PreviewAnchorPoint(Anchor(.5,.5),Size.Empty,new Size(492,150),out p);
   accepted |= ProcessEditor.PreviewAnchorPoint(Anchor(.5,.5),new Size(100,100),Size.Empty,out p);
   Console.WriteLine(json.Serialize(new {accepted=accepted}));return 0;
  }
  if(args[0]=="paint") {
   using(var image=new Bitmap(300,100)) using(var preview=new Bitmap(492,150)) {
    using(var g=Graphics.FromImage(image))g.Clear(Color.Black);
    using(var g=Graphics.FromImage(preview)) {
     g.Clear(Color.White);g.DrawImage(image,new Rectangle(21,0,450,150));
     ProcessEditor.PreviewAnchorPoint(Anchor(.25,.75),image.Size,preview.Size,out p);
     ProcessEditor.PaintPreviewAnchor(g,p,1);
    }
    Color marker=preview.GetPixel(134,113);
    Console.WriteLine(json.Serialize(new {blue=marker.B>180&&marker.R<40,source_unchanged=image.GetPixel(75,75).ToArgb()==Color.Black.ToArgb()}));return 0;
   }
  }
  var results=new List<object>();
  foreach(var sample in new[]{new[]{300,100,492,150},new[]{100,300,492,150},new[]{300,100,984,300},new[]{4,4,8,8}}) {
   bool edge=sample[0]==4;
   bool valid=ProcessEditor.PreviewAnchorPoint(Anchor(edge?1:.25,edge?1:.75),new Size(sample[0],sample[1]),new Size(sample[2],sample[3]),out p);
   results.Add(new {valid=valid,x=p.X,y=p.Y});
  }
  Console.WriteLine(json.Serialize(results));return 0;
 }
}''', encoding="utf-8")
        cls.helper = folder / "preview-anchor.exe"
        result = subprocess.run([r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "/nologo", "/target:exe",
            "/main:PreviewAnchorProbe", "/codepage:65001", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
            "/reference:System.Web.Extensions.dll", "/out:" + str(cls.helper), str(Path(__file__).with_name("ProcessEditor.cs")), str(source)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, mode):
        result = subprocess.run([str(self.helper), mode], capture_output=True, text=True, check=True,
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)

    def test_zoom_letterboxing_dpi_and_last_pixel_anchor_are_preserved(self):
        expected = [(134.25, 113.25), (233.75, 112.75), (268.5, 226.5), (7, 7)]
        for actual, (x, y) in zip(self.call("points"), expected):
            self.assertTrue(actual["valid"])
            self.assertAlmostEqual(actual["x"], x)
            self.assertAlmostEqual(actual["y"], y)

    def test_invalid_or_missing_anchor_is_not_guessed(self):
        self.assertFalse(self.call("invalid")["accepted"])

    def test_marker_is_visible_on_preview_without_changing_source_pixels(self):
        self.assertEqual(self.call("paint"), {"blue": True, "source_unchanged": True})


if __name__ == "__main__":
    unittest.main()

