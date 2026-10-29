"""Recipe-level menu transition tests; no corporate data or live app input."""
import copy
import json
import tempfile
import threading
import unittest
from unittest import mock

from process_editor import ProcessDraft, ProcessEditors
from test_process_editor import image_choice
from test_learning import TARGET
from workflows import WorkflowRunner
from test_workflows import FakeRuntime, ScriptedOperations


def image_event(ref, operation="click"):
    fields = image_choice()
    return {"program_id": "editor", "window_ref": ref, "operation": operation,
            **{key: fields[key] for key in ("template_png", "width", "height", "anchor", "capture_window")}}


def recording():
    root = TARGET["window_id"] + 10
    child = root + 1
    windows = [{"program_id": "editor", "window_ref": ref, "owner_ref": "main", "pid": TARGET["pid"],
                "window_id": hwnd, "owner_window_id": TARGET["window_id"], "title": "",
                "class_name": "WindowsForms10.Window.20808.app.0.abc_r10_ad1", "owner_verified": True,
                "closed": True} for ref, hwnd in (("menu", root), ("submenu", child))]
    parent = image_event("menu", "expand_menu")
    parent.update(expansion_method="hover", native_target={"pid": TARGET["pid"], "window_id": root,
        "element": {"role": "MenuItem", "name": "Export to excel", "is_password": False}},
        menu_evidence={"source": "uia_menu_transition", "before_state": "collapsed", "after_state": "expanded",
            "input_version_unchanged": True, "parent_identity": "transient-only-7", "child_representation": "hwnd",
            "child_window_ref": "submenu", "child_items": [{"name": "Active sheet", "role": "MenuItem", "enabled": True}]})
    return [image_event("main", "right_click"), parent, image_event("submenu")], windows


class RecordedMenuExpansionTests(unittest.TestCase):
    def draft(self):
        return ProcessDraft([{"program_id": "editor", **TARGET, "label": "Fixture"}])

    def _unfinished_physical_menu(self, *, cancel_to_main=False):
        draft = self.draft()
        draft.recorded([image_event("main")])
        existing_steps, existing_labels = copy.deepcopy((draft.steps, draft.labels))
        events, windows = recording()
        events = events[:2]
        if cancel_to_main:
            events.append(image_event("main"))
        original = copy.deepcopy((events, windows))

        draft.recorded(events, windows=windows, auto_popup=True)

        self.assertEqual((events, windows), original)
        self.assertEqual(draft.steps[:len(existing_steps)], existing_steps)
        self.assertEqual(draft.labels[:len(existing_labels)], existing_labels)
        imported = draft.steps[len(existing_steps):]
        repairs = [row for row in imported if row["operation"] == "manual_entry"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["manual_reason"], "menu_expansion_unverified")
        self.assertEqual(repairs[0]["image_target"]["template_png"], events[1]["template_png"])
        self.assertEqual(sum(row["operation"] == "image_right_click" for row in imported), 1)
        self.assertEqual(sum(row["operation"] == "image_click" for row in imported), int(cancel_to_main))
        self.assertFalse(any(row["operation"] in {"expand_menu", "menu_command"}
                             or row.get("window_ref") == "submenu" for row in imported))
        self.assertIn("menu_expansion_unverified", draft.recording_warnings)
        self.assertEqual(draft.readiness()["unresolved_steps"], 1)
        self.assertFalse(draft.readiness()["runnable"])
        draft.validate(allow_pending=True)
        return draft

    def test_physical_menu_recording_can_end_without_a_child_selection(self):
        self._unfinished_physical_menu()

    def test_cancelled_physical_menu_keeps_later_main_click_and_existing_steps(self):
        self._unfinished_physical_menu(cancel_to_main=True)

    def test_unfinished_physical_menu_saves_and_reopens_as_a_nonexecutable_draft(self):
        from server import TaskStore
        from workflows import WorkflowError, render_steps
        draft = self._unfinished_physical_menu()
        with tempfile.TemporaryDirectory() as directory:
            config = {"programs": [{"id": "editor"}]}
            store = TaskStore(directory, config)
            review = {key: draft.recording_review()[key]
                      for key in ("partial", "warning_codes", "acknowledged")}
            saved = store.save({"name": "Unfinished menu", "instructions": "Complete the missing selection",
                "expected": "Explicit output condition", "program_ids": ["editor"],
                "steps": draft.steps, "variables": {}, "recording_review": review,
                "draft": draft.draft_metadata()})
            reopened = TaskStore(directory, config).get(saved["id"])
            self.assertEqual(reopened, saved)
            restored = ProcessDraft([{"program_id": "editor", **TARGET, "label": "Fixture"}], reopened)
            self.assertEqual(restored.steps, saved["steps"])
            self.assertEqual(restored.labels, draft.labels)
            self.assertEqual(restored.recording_review(), draft.recording_review())
            self.assertFalse(restored.recording_review()["acknowledged"])
            self.assertEqual(restored.readiness()["unresolved_steps"], 1)
            self.assertFalse(restored.readiness()["runnable"])
            with self.assertRaises(WorkflowError):
                render_steps(reopened, {})

    def test_hover_parent_is_kept_before_child_binding_and_leaf(self):
        draft = self.draft(); events, windows = recording()
        draft.recorded(events, windows=windows, auto_popup=True)
        kinds = [row["operation"] for row in draft.steps]
        index = kinds.index("expand_menu")
        self.assertEqual(kinds[index+1:index+3], ["wait_for_window", "image_click"])
        self.assertEqual(draft.steps[index+1]["window_ref"], "submenu")
        self.assertEqual(draft.steps[index]["expansion_method"], "hover")
        self.assertEqual(draft.steps[index]["image_target"]["min_score"], .94)
        self.assertEqual(kinds[-1], "checkpoint")
        self.assertEqual(draft.steps[-1]["return_from"], "submenu")
        self.assertEqual(draft.steps[-1]["review_mode"], "human")
        self.assertIn("최종 명령과 파일 생성은 별도 확인", draft.summaries()[index]["detail"])
        draft.validate()

    def test_no_native_identity_or_instance_handle_in_saved_step(self):
        draft = self.draft(); events, windows = recording()
        draft.recorded(events, windows=windows)
        serialized = json.dumps(draft.steps)
        for token in ('"pid"', '"window_id"', '"native_target"', '"parent_identity"', 'transient-only-7'):
            self.assertNotIn(token, serialized)

    def test_missing_transition_or_native_evidence_preserves_manual_step(self):
        for field in ("menu_evidence", "native_target"):
            with self.subTest(field=field):
                draft = self.draft(); events, windows = recording()
                events[1].pop(field)
                draft.recorded(events, windows=windows)
                repairs = [row for row in draft.steps if row["operation"] == "manual_entry"]
                self.assertEqual(repairs[0]["manual_reason"], "menu_expansion_unverified")
                self.assertNotIn("expand_menu", [row["operation"] for row in draft.steps])
                self.assertIn("menu_expansion_unverified", draft.recording_warnings)
                row = next(row for row in draft.summaries() if row["requires_input"])
                self.assertFalse(row["repairable"])

    def test_observed_semantic_parent_does_not_require_hover_template(self):
        draft = self.draft(); events, windows = recording()
        events[1].pop("template_png")
        draft.recorded(events, windows=windows)
        expansion = next(row for row in draft.steps if row["operation"] == "expand_menu")
        self.assertIn("menu_target", expansion)
        self.assertNotIn("image_target", expansion)

    def test_unproven_child_and_changed_input_epoch_are_not_guessed(self):
        for change in ("child_ref", "input_epoch", "foreign_native", "child_image"):
            draft = self.draft(); events, windows = recording()
            if change == "child_ref": events[1]["menu_evidence"]["child_window_ref"] = "not_recorded"
            if change == "input_epoch": events[1]["menu_evidence"]["input_version_unchanged"] = False
            if change == "foreign_native": events[1]["native_target"]["pid"] += 1
            if change == "child_image": events[2].pop("template_png")
            draft.recorded(events, windows=windows)
            self.assertTrue(any(row.get("manual_reason") == "menu_expansion_unverified" for row in draft.steps), change)

    def test_malformed_native_evidence_becomes_repair_instead_of_lost_recording(self):
        for value in (None, [], "not-an-object"):
            for field in ("element", "menu_evidence"):
                draft = self.draft(); events, windows = recording()
                if field == "element": events[1]["native_target"][field] = value
                else: events[1][field] = value
                draft.recorded(events, windows=windows)
                self.assertTrue(any(row.get("manual_reason") == "menu_expansion_unverified" for row in draft.steps))

    def test_existing_receipt_is_not_migrated_to_guessed_hover(self):
        draft = self.draft(); events, windows = recording()
        original = copy.deepcopy(events)
        draft.recorded([events[0], events[2]], windows=windows)
        self.assertNotIn("expand_menu", [row["operation"] for row in draft.steps])
        self.assertEqual(events, original)

    def test_closed_logical_menu_retains_names_and_prehover_image_not_old_handles(self):
        draft = self.draft(); events, windows = recording()
        windows = windows[:1]
        events[1]["menu_evidence"].update(child_representation="logical")
        events[1]["menu_evidence"].pop("child_window_ref")
        events[2]["window_ref"] = "menu"
        events[2]["native_target"] = {"pid": TARGET["pid"], "window_id": windows[0]["window_id"],
                                      "element": {"role": "MenuItem", "name": "Active sheet"}}
        draft.recorded(events, windows=windows, snapshots={}, auto_popup=True)
        expansion = next(row for row in draft.steps if row["operation"] == "expand_menu")
        self.assertIn("image_target", expansion)
        self.assertEqual(expansion["selector"], {"role": "MenuItem", "name": "Export to excel"})
        self.assertEqual(expansion["child_selector"]["within"], expansion["selector"])
        self.assertNotIn("child_window", expansion)
        self.assertEqual(sum(row["operation"] == "wait_for_window" for row in draft.steps), 1)
        self.assertEqual(draft.steps[-2]["operation"], "menu_command")

    def test_logical_transition_does_not_guess_unobserved_active_sheet(self):
        draft = self.draft(); events, windows = recording()
        events[1]["menu_evidence"]["child_representation"] = "logical"
        events[2]["window_ref"] = "menu"
        events[2]["native_target"] = {"pid": TARGET["pid"], "window_id": windows[0]["window_id"],
                                      "element": {"role": "MenuItem", "name": "All sheet"}}
        draft.recorded(events, windows=windows[:1])
        self.assertTrue(any(row.get("manual_reason") == "menu_expansion_unverified" for row in draft.steps))

    def test_logical_parent_image_repair_keeps_semantic_path(self):
        draft = self.draft(); events, windows = recording()
        events[1]["menu_evidence"]["child_representation"] = "logical"
        events[2]["window_ref"] = "menu"
        events[2]["native_target"] = {"pid": TARGET["pid"], "window_id": windows[0]["window_id"],
                                      "element": {"role": "MenuItem", "name": "Active sheet"}}
        draft.recorded(events, windows=windows[:1])
        index = next(i for i, row in enumerate(draft.steps) if row["operation"] == "expand_menu")
        target = draft.targets[("editor", "menu")]
        before = copy.deepcopy(draft.steps[index])
        selected = draft.remember_image(target, image_choice(pid=target["pid"], window_id=target["window_id"],
                                                           anchor={"x": .25, "y": .5}))
        draft.repair(index, selected)
        self.assertEqual(draft.steps[index]["selector"], before["selector"])
        self.assertEqual(draft.steps[index]["child_selector"], before["child_selector"])
        self.assertEqual(draft.steps[index]["operation"], "expand_menu")
        self.assertEqual(draft.steps[index]["image_target"]["anchor"]["x"], .25)

    def test_explicit_manual_expansion_is_not_relabelled_missing_image(self):
        draft = self.draft()
        draft.recorded([{"program_id": "editor", "operation": "manual_entry", "reason": "menu_expansion_unverified"}])
        self.assertEqual(draft.steps[0]["manual_reason"], "menu_expansion_unverified")

    def test_local_trial_exposes_reason_and_delivery_without_export_success(self):
        answer = ProcessEditors._trial_summary({"status": "needs_review", "pending_step": 4,
            "last_result": {"input_dispatched": None, "menu_expansion": {"child_representation": "unknown"},
                            "diagnostic": {"code": "menu_child_timeout"}}}, {"id": "fixture"})
        self.assertEqual(answer["pending_operation"], "expand_menu")
        self.assertIsNone(answer["input_dispatched"])
        self.assertEqual(answer["diagnostic"]["code"], "menu_child_timeout")
        self.assertFalse(answer["business_result_verified"])

    def test_retargeted_leaf_updates_its_generated_child_image_condition(self):
        draft = self.draft(); events, windows = recording()
        draft.recorded(events, windows=windows, auto_popup=True)
        leaf = next(i for i, row in enumerate(draft.steps) if row["operation"] == "image_click")
        previous = copy.deepcopy(draft.steps)
        draft.steps[leaf]["image_target"]["anchor"]["x"] = .25
        draft._sync_popup_condition(draft.steps, leaf)
        self.assertEqual(draft.steps[leaf-2]["child_image_target"], draft.steps[leaf]["image_target"])
        self.assertNotEqual(previous[leaf-2]["child_image_target"], draft.steps[leaf-2]["child_image_target"])
        draft.validate()

    def test_local_delivery_does_not_release_menu_path_or_nested_observation(self):
        from local_delivery import receipt
        original = {"status": "needs_review", "menu_expansion": {
            "menu_path": ["PRIVATE_PARENT", "PRIVATE_COMMAND"], "observation": {"title": "PRIVATE_WINDOW"}}}
        self.assertNotIn("PRIVATE", json.dumps(receipt(original)))


class MenuResumeTests(unittest.TestCase):
    def test_same_run_resume_only_checks_child_after_unknown_delivery(self):
        from test_menu_expansion import step as logical_step
        # Use the public runner with a frozen recipe, not a synthetic approval.
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime(directory)
            runtime.stop_event = threading.Event()
            runtime.guard.window_resolver = lambda hwnd: 42
            runner = WorkflowRunner(directory)
            spec = logical_step()
            spec["program_id"] = "editor"
            task = {"id": "menu", "program_ids": ["editor"], "variables": {}, "steps": [spec]}
            engine = ScriptedOperations([{"status": "unknown", "task_verified": False, "input_dispatched": None,
                                          "diagnostic": {"code": "menu_child_timeout"}}])
            targets = [{"program_id": "editor", "pid": 42, "window_id": 10}]
            with mock.patch("operations.Operations", return_value=engine):
                first = runner.run(runtime, task, {}, targets)
                count = len(engine.calls)
                with mock.patch("menu_expansion.verify_menu_expansion", return_value={"task_verified": True,
                    "input_dispatched": False, "verification_scope": "menu_visibility_only"}) as check:
                    final = runner.run(runtime, task, {}, targets, resume_run_id=first["run_id"])
                check.assert_called()
            self.assertEqual(len(engine.calls), count)
            self.assertEqual(final["run_id"], first["run_id"])
            self.assertTrue(final["task_verified"])


if __name__ == "__main__":
    unittest.main()
