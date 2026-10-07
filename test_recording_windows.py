"""Popup recording contracts: scope, stable recipes, ambiguity, and no replay."""
import copy
import threading
import unittest
from unittest import mock

from operations import OperationError
from process_editor import ProcessDraft
from recording_windows import dynamic_window_keys, execute_window_wait, validate_window_wait
from test_learning import TARGET
from test_process_editor import image_choice


def step(**extra):
    return {"operation": "wait_for_window", "program_id": "editor", "window_ref": "popup_1", "owner_ref": "main",
            "title": "Choose a value", "class_name": "OwnedDialog", "timeout_ms": 0, **extra}


def row(hwnd, owner=10, **extra):
    return {"pid": 42, "window_id": hwnd, "owner_window_id": owner, "root_owner_window_id": owner,
            "visible": True, "title": "Choose a value", "class_name": "OwnedDialog", **extra}


class Probe:
    def __init__(self, windows, **extra):
        parent = row(10, owner=0, title="Parent", class_name="ParentClass", thread_id=9)
        self.state = {"process_exited": False, "target_present": True, "creation_time": 123, "executable": "test.exe", "windows": [parent, *windows], **extra}
        self.closed = False
    def capture(self): return {**self.state, "creation_time": 123, "executable": "test.exe"}
    def snapshot(self): return copy.deepcopy(self.state)
    def close(self): self.closed = True


class Runtime:
    def __init__(self, probe): self.probe = probe; self.stop_event = threading.Event(); self.calls = []
    def create_transition_probe(self, target): self.calls.append(target); return self.probe
    def check_active(self):
        if self.stop_event.is_set(): raise OperationError("stopped", "session_stopped")


class WindowBindingTests(unittest.TestCase):
    def run_wait(self, windows, **extra):
        probe = Probe(windows, **extra); runtime = Runtime(probe)
        result = execute_window_wait(runtime, step(), {("editor", "main"): {"pid": 42, "window_id": 10}})
        self.assertTrue(probe.closed)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["metrics"]["mutations"], 0)
        return result

    def test_unique_owned_popup_binds_without_input(self):
        result = self.run_wait([row(20)])
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["target"], {"pid": 42, "window_id": 20})

    def test_owner_chain_and_hidden_then_visible_popup_are_supported(self):
        self.assertTrue(self.run_wait([row(20, owner=30), row(30, owner=10, title="Parent")])["task_verified"])
        self.assertFalse(self.run_wait([row(20, visible=False)])["task_verified"])

    def test_foreign_unowned_wrong_class_title_and_self_are_excluded(self):
        for candidate in (row(20, owner=99), row(20, pid=43), row(20, title="Other"), row(20, class_name="Other"), row(10)):
            with self.subTest(candidate=candidate):
                result = self.run_wait([candidate])
                self.assertFalse(result["task_verified"])
                self.assertEqual(result["diagnostic"]["code"], "recording_window_missing")

    def test_duplicate_dialogs_stop_without_choosing_first(self):
        result = self.run_wait([row(20), row(21)])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "recording_window_ambiguous")

    def test_ownerless_dropdown_requires_exact_combo_list_relationship(self):
        for confirmed in (False, True):
            runtime=Runtime(Probe([row(20, owner=0, class_name="ComboLBox", title="")]))
            with mock.patch("recording_windows.combo_list_owned_by", return_value=confirmed) as relation:
                result=execute_window_wait(runtime,step(title="",class_name="ComboLBox"),{("editor","main"):{"pid":42,"window_id":10}})
            self.assertEqual(result["task_verified"],confirmed)
            relation.assert_called_once_with(10,20,42)
            self.assertFalse(result["input_dispatched"])

    def test_combo_relationship_never_expands_to_a_foreign_process(self):
        runtime=Runtime(Probe([row(20, owner=0, pid=43, class_name="ComboLBox", title="")]))
        with mock.patch("recording_windows.combo_list_owned_by", return_value=True) as relation:
            result=execute_window_wait(runtime,step(title="",class_name="ComboLBox"),{("editor","main"):{"pid":42,"window_id":10}})
        self.assertFalse(result["task_verified"])
        relation.assert_not_called()

    def test_process_exit_owner_close_and_pid_reuse_stop(self):
        for extra in ({"process_exited": True}, {"target_present": False}, {"creation_time": 456}):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_wait([row(20)], **extra)["diagnostic"]["code"], "window_owner_unavailable")

    def test_wait_specs_and_binding_order_are_validated(self):
        self.assertEqual(dynamic_window_keys([step(), {"operation": "checkpoint", "program_id": "editor", "window_ref": "popup_1"}]), {("editor", "popup_1")})
        for extra in ({"owner_ref": "popup_1"}, {"class_name": ""}, {"timeout_ms": True}, {"timeout_ms": 10001}, {"window_id": 123}):
            with self.subTest(extra=extra), self.assertRaises(OperationError): validate_window_wait(step(**extra))
        for steps in ([step(), step()], [{"program_id": "editor", "window_ref": "popup_1"}, step()], [step(owner_ref="popup_2"), step(window_ref="popup_2")]):
            with self.assertRaises(OperationError): dynamic_window_keys(steps)


class RecordingDraftWindowTests(unittest.TestCase):
    def setUp(self):
        self.program = {"program_id": "editor", "window_ref": "main", **TARGET, "label": "Synthetic editor"}
        self.draft = ProcessDraft([self.program])
        self.window = {"program_id": "editor", "window_ref": "popup_1", "owner_ref": "main", "pid": TARGET["pid"],
                       "window_id": TARGET["window_id"] + 1, "owner_window_id": TARGET["window_id"],
                       "title": "Choose a value", "class_name": "OwnedDialog", "owner_verified": True}

    def event(self, ref):
        return {**image_choice(), "operation": "click", "program_id": "editor", "window_ref": ref}

    def test_open_popup_return_to_main_preserves_order_and_stable_wait(self):
        self.draft.recorded([self.event("main"), self.event("popup_1"), self.event("main")], windows=[self.window])
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "wait_for_window", "checkpoint", "image_click", "checkpoint", "image_click", "checkpoint"])
        self.assertEqual(self.draft.steps[1], step(timeout_ms=5000))
        self.assertEqual(self.draft.steps[2]["opened_from"], "main")
        self.assertEqual(self.draft.steps[2]["window_ref"], "popup_1")
        self.assertEqual(self.draft.steps[-1]["window_ref"], "main")
        self.assertFalse(any("pid" in s or "window_id" in s for s in self.draft.steps))
        self.draft.validate()
        reopened = ProcessDraft([self.program], {"steps": self.draft.steps, "variables": {}})
        self.assertEqual(reopened.targets[("editor", "popup_1")]["window_id"], 0)
        self.assertEqual(len(reopened.summaries()), 7)

    def test_foreign_unproven_duplicate_metadata_is_rejected_atomically(self):
        for extra in ({"pid": TARGET["pid"] + 1}, {"owner_window_id": 123}, {"owner_verified": False}, {"window_ref": "main"}, {"class_name": ""}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                self.draft.recorded([self.event("popup_1")], windows=[{**self.window, **extra}])
            self.assertEqual(self.draft.steps, [])
            self.assertEqual(len(self.draft.targets), 1)

    def test_unused_popup_does_not_add_binding_or_expand_recording_scope(self):
        self.draft.recorded([self.event("main")], windows=[self.window])
        self.assertEqual(len(self.draft.steps), 2)
        self.assertEqual(len(self.draft.targets), 1)

    def test_same_popup_wait_inserted_once(self):
        self.draft.recorded([self.event("popup_1"), self.event("popup_1")], windows=[self.window])
        self.assertEqual(sum(s["operation"] == "wait_for_window" for s in self.draft.steps), 1)

    def test_closed_popup_last_click_checks_the_owner_without_claiming_business_success(self):
        self.draft.recorded([self.event("main"), self.event("popup_1"), self.event("popup_1"), self.event("main")],
                            windows=[{**self.window, "closed": True}])
        popup_checks = [s for s in self.draft.steps if s["operation"] == "checkpoint" and s.get("return_from")]
        self.assertEqual(len(popup_checks), 1)
        self.assertEqual(popup_checks[0]["window_ref"], "main")
        self.assertEqual(popup_checks[0]["return_from"], "popup_1")
        self.assertEqual(self.draft.steps[4]["window_ref"], "popup_1")  # Earlier popup checkpoint stays there.
        self.draft.validate()

    def test_same_native_handle_reopened_as_two_episodes_has_two_owner_checkpoints(self):
        windows=[{**self.window,"closed":True}, {**self.window,"window_ref":"popup_2","closed":True}]
        events=[self.event("main"),self.event("popup_1"),self.event("main"),self.event("popup_2")]
        self.draft.recorded(events,windows=windows)
        waits=[s for s in self.draft.steps if s["operation"]=="wait_for_window"]
        returns=[s for s in self.draft.steps if s.get("return_from")]
        self.assertEqual([s["window_ref"] for s in waits],["popup_1","popup_2"])
        self.assertEqual([s["return_from"] for s in returns],["popup_1","popup_2"])
        self.assertTrue(all(s["window_ref"]=="main" for s in returns))
        openings=[s for s in self.draft.steps if s.get("opened_from")]
        self.assertEqual([s["window_ref"] for s in openings],["popup_1","popup_2"])
        self.draft.validate()

    def test_opener_wait_and_popup_checkpoint_move_as_one_group(self):
        self.draft.recorded([self.event("main"),self.event("popup_1")],windows=[self.window])
        self.draft.add({**{key:value for key,value in self.program.items() if key!="label"},"action":"delay","seconds":1})
        self.draft.change("move_step",{"index":5,"direction":-1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click","wait_for_window","checkpoint","delay","image_click","checkpoint"])
        self.draft.change("move_step",{"index":3,"direction":-1})
        self.assertEqual([s["operation"] for s in self.draft.steps[:4]], ["delay","image_click","wait_for_window","checkpoint"])
        self.draft.validate()


if __name__ == "__main__": unittest.main()
