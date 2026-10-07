"""Declarative process authoring and native IPC; business programs receive no input."""
import copy
import base64
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import struct
import zlib
from unittest import mock

from operations import OperationError
from process_editor import ProcessDraft, ProcessEditors, task_view
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

    def test_recording_scope_and_invalid_event_are_atomic(self):
        before = copy.deepcopy(self.draft.steps)
        for extra in ({"program_id": "unbound"}, {"operation": "execute"}, {"width": 999}):
            with self.subTest(extra=extra), self.assertRaises((OperationError, ValueError)):
                self.draft.recorded([{**image_choice(), "operation": "click", "program_id": "editor", **extra}])
            self.assertEqual(before, self.draft.steps)

    def test_recording_capacity_includes_each_review_checkpoint(self):
        event = {**image_choice(), "operation": "click", "program_id": "editor"}
        self.draft.recorded([event] * 15)
        self.assertEqual(len(self.draft.steps), 30)
        with self.assertRaises((OperationError, ValueError)):
            self.draft.recorded([event])
        self.assertEqual(len(self.draft.steps), 30)

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

    def test_recording_is_reviewable_not_saved_and_unknown_input_blocks_save(self):
        self.context(); job = self.start()
        event = {**image_choice(), "operation": "manual_entry", "program_id": "editor"}
        with mock.patch.object(self.editors, "_visual", return_value={"events": [event]}) as visual:
            recorded = self.command("record", {})
        self.assertEqual(recorded["status"], "ok"); self.assertTrue(recorded["steps"][0]["requires_input"])
        self.assertEqual(visual.call_args.args[2]["max_events"], 12)  # Five popup waits fit alongside review checkpoints.
        self.assertEqual(self.manager.tasks.all(), [])
        rejected = self.command("save", {"name": "recorded", "description": ""})
        self.assertEqual(rejected["code"], "recording_input_required")
        resolved = self.command("resolve_input", {"index": 0, "value": "intended"})
        self.assertEqual(resolved["status"], "ok")
        saved = self.command("save", {"name": "recorded", "description": ""})
        self.assertEqual(saved["status"], "ok")
        value = self.manager.call("computer_get_task", {"id": saved["saved_task"]["id"]})
        self.assertNotIn("template_png", json.dumps(value))
        self.assertEqual(self.runtime.mutations, [])
        atomic_json(self.paths["response"], {"nonce": self.nonce, "status": "saved"})
        self.terminal(job)

    def test_recorded_closed_popup_updates_editor_choices_and_saves_without_raw_handles(self):
        self.context(); job = self.start()
        window = {"program_id": "editor", "window_ref": "popup_1", "owner_ref": "main", "pid": TARGET["pid"],
            "window_id": TARGET["window_id"] + 1, "owner_window_id": TARGET["window_id"], "title": "Recorded popup",
            "class_name": "OwnedDialog", "owner_verified": True}
        event = {**image_choice(), "operation": "click", "program_id": "editor", "window_ref": "popup_1"}
        with mock.patch.object(self.editors, "_visual", return_value={"events": [event], "windows": [window]}):
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

    def test_step_thumbnail_is_only_returned_to_native_preview_not_mcp_status(self):
        self.context(); job = self.start()
        with mock.patch.object(self.editors, "_visual", return_value=image_choice()):
            picked = self.command("pick_image", {"program_id": "editor", **TARGET, "purpose": "action"})
        self.command("add_step", {"program_id": "editor", **TARGET, "action": "click", "selection_id": picked["selection"]["selection_id"]})
        preview = self.command("preview_step", {"index": 0})
        self.assertEqual(preview["preview"]["thumbnail_png"], image_choice()["template_png"])
        self.assertNotIn("steps", preview)
        self.assertNotIn(image_choice()["template_png"], json.dumps(self.status(job)))
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

    def test_checkpoint_image_is_forwarded_without_claiming_task_verification(self):
        task = self.manager.tasks.save({"name": "review", "instructions": "review", "expected": "human review",
            "program_ids": ["editor"], "steps": [{"operation": "checkpoint", "program_id": "editor", "message": "Review"}],
            "variables": {}})
        content = {"type": "image", "mimeType": "image/png", "data": "c3ludGhldGlj"}
        with mock.patch.object(self.manager.workflows, "run", return_value={"status": "needs_review", "task_verified": False,
                "checkpoint": {"id": "review-id"}, "checkpoint_content": [content]}) as run:
            response = self.manager.call("computer_run_task", {"task_id": task["id"], "targets": self.args["targets"],
                "acknowledge_checkpoint": "earlier-reviewed-id"})
        self.assertFalse(response.get("isError", False), response)
        self.assertIn(content, response["content"])
        self.assertFalse(response["structuredContent"]["task_verified"])
        self.assertNotIn("checkpoint_content", response["structuredContent"])
        self.assertEqual(run.call_args.kwargs["acknowledge_checkpoint"], "earlier-reviewed-id")

    def test_loading_and_saving_makes_new_copy_and_cancel_after_save_keeps_it(self):
        original = self.manager.tasks.save({"name": "original", "instructions": "old", "expected": "old",
            "program_ids": ["editor"], "steps": [{"operation": "delay", "program_id": "editor", "duration_ms": 100}], "variables": {}})
        self.context(); job = self.start(task_id=original["id"])
        saved = self.command("save", {"name": "copy", "description": "new"})
        self.assertNotEqual(saved["saved_task"]["id"], original["id"])
        self.status(job, cancel=True)
        done = self.terminal(job)
        self.assertEqual(done["status"], "saved")
        self.assertEqual(self.manager.tasks.get(original["id"]), original)
        self.assertEqual(len(self.manager.tasks.all()), 2)


if __name__ == "__main__":
    unittest.main()
