"""Semantic menu recording and atomic editor pairs; no native app input."""
import copy
import json
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from checkpoint_groups import popup_condition, use_popup_image_conditions, validate_pair_links
from operations import OperationError
from process_editor import ProcessDraft, ProcessEditors
from recorded_menus import recorded_menu_target
from test_learning import TARGET
from test_process_editor import image_choice
from test_recorded_menu_expansion import recording
from workflows import WorkflowError, WorkflowRunner, validate_recipe


def semantic_recording():
    events, windows = recording()
    events[-1]["native_target"] = {"pid": windows[1]["pid"], "window_id": windows[1]["window_id"],
        "element": {"name": "Active sheet", "role": "MenuItem", "is_password": False,
                    "automation_id": "REA_INTERNAL_COMMAND_13"}}
    return events, windows


def draft():
    return ProcessDraft([{"program_id": "editor", **TARGET, "label": "Fixture"}])


class SemanticRecordingTests(unittest.TestCase):
    def test_new_parent_condition_uses_observed_name_and_leaf_uses_separate_fresh_command(self):
        value = draft(); events, windows = semantic_recording()
        value.recorded(events, windows=windows, auto_popup=True)
        parent = next(i for i, row in enumerate(value.steps) if row["operation"] == "expand_menu")
        check, expansion = value.steps[parent-1:parent+1]
        self.assertEqual(check["review_mode"], "menu_semantic")
        self.assertNotIn("image_target", check)
        self.assertEqual(check["menu_target"], expansion["menu_target"])
        self.assertEqual(expansion["menu_target"], {"path": [{"name": "Export to excel", "role": "MenuItem"}]})
        self.assertEqual(expansion["selector"]["role"], "MenuItem")
        self.assertEqual(check["linked_step_id"], expansion["step_id"])
        self.assertEqual(expansion["precondition_step_id"], check["step_id"])
        command, completion = value.steps[-2:]
        self.assertEqual(command["operation"], "menu_command")
        self.assertNotIn("image_target", command)
        self.assertEqual(command["menu_target"], expansion["child_menu_target"])
        self.assertEqual(completion["review_mode"], "human")
        self.assertEqual(completion["return_from"], "submenu")
        stored = json.dumps(value.steps)
        for secret in ("REA_INTERNAL", "parent_identity", "native_target", '"pid"', '"window_id"'):
            self.assertNotIn(secret, stored)
        value.validate()

    def test_parent_target_and_condition_are_independent_of_saved_hover_pixels(self):
        variants = []
        for anchor in (.25, .75):
            value = draft(); events, windows = semantic_recording()
            events[1]["anchor"] = {"x": anchor, "y": .5}
            value.recorded(events, windows=windows, auto_popup=True)
            variants.append(next(row for row in value.steps if row.get("review_mode") == "menu_semantic"))
        self.assertEqual(variants[0]["menu_target"], variants[1]["menu_target"])
        self.assertNotIn("image_target", variants[0])

    def test_does_not_invent_leaf_from_uncorroborated_native_label(self):
        value = draft(); events, windows = semantic_recording()
        events[-1]["native_target"]["element"]["name"] = "All sheet"
        value.recorded(events, windows=windows, auto_popup=True)
        self.assertNotIn("menu_command", [row["operation"] for row in value.steps])

    def test_hierarchy_uses_actual_observed_labels_not_internal_identifiers(self):
        target = recorded_menu_target({"element": {"role": "MenuItem", "name": "Actual command", "automation_id": "REA_123"},
            "ancestors": [{"role": "MenuItem", "name": "Actual parent", "automation_id": "REA_456"},
                          {"role": "Pane", "name": "Container"}]})
        self.assertEqual([row["name"] for row in target["path"]], ["Actual parent", "Actual command"])
        self.assertNotIn("REA_", json.dumps(target))

    def test_closed_driver_snapshot_does_not_replace_native_menu_identity(self):
        value = draft(); events, windows = semantic_recording()
        events[1]["menu_evidence"]["child_representation"] = "logical"
        events[1]["menu_evidence"].pop("child_window_ref")
        events[-1]["window_ref"] = "menu"
        events[-1]["native_target"]["window_id"] = windows[0]["window_id"]
        snapshot = {"elements": [{"role": "Pane", "name": "Closed menu", "automation_id": "REA_PRIVATE_123"}]}
        value.recorded(events, windows=windows[:1], auto_popup=True, snapshots={("editor", "menu"): snapshot})
        expansion = next(row for row in value.steps if row["operation"] == "expand_menu")
        self.assertEqual(expansion["child_selector"]["name"], "Active sheet")
        self.assertNotIn("REA_", json.dumps(value.steps))

    def test_opening_legacy_recipe_does_not_migrate_its_image_condition(self):
        value = draft(); events, windows = semantic_recording()
        value.recorded(events, windows=windows, auto_popup=True)
        check = next(row for row in value.steps if row.get("review_mode") == "menu_semantic")
        action = next(row for row in value.steps if row.get("operation") == "expand_menu")
        check.update(review_mode="popup_image", image_target=copy.deepcopy(action["image_target"]))
        check.pop("menu_target")
        old = copy.deepcopy(value.steps)
        reopened = ProcessDraft(value.programs[:1], {"steps": old})
        self.assertEqual(reopened.steps, old)
        changed = use_popup_image_conditions(old, [check["step_id"]])
        self.assertEqual(next(row for row in changed if row.get("step_id") == check["step_id"])["review_mode"], "menu_semantic")
        self.assertEqual(old, value.steps)

    def test_explicit_human_checks_cannot_be_upgraded_by_group_edit(self):
        value = draft(); events, windows = semantic_recording()
        value.recorded(events, windows=windows, auto_popup=True)
        index = next(i for i, row in enumerate(value.steps) if row.get("review_mode") == "menu_semantic")
        value.edit({"index": index, "review_mode": "human"})
        old = copy.deepcopy(value.steps)
        with self.assertRaises(ValueError):
            use_popup_image_conditions(value.steps, [value.steps[index]["step_id"]])
        self.assertEqual(old, value.steps)


class AtomicPairTests(unittest.TestCase):
    def setUp(self):
        self.value = draft()
        events, windows = recording()
        self.value.recorded([events[0], events[2]], windows=windows, auto_popup=True)
        self.check_index = next(i for i, row in enumerate(self.value.steps) if row.get("review_mode") == "popup_image")

    def selected_image(self, anchor=.2):
        check = self.value.steps[self.check_index]
        target = self.value.targets[(check["program_id"], check["window_ref"])]
        return image_choice(pid=target["pid"], window_id=target["window_id"], anchor={"x": anchor, "y": .5})

    def test_retarget_in_either_direction_updates_both_and_reports_affected_steps(self):
        for index in (self.check_index, self.check_index+1):
            affected = self.value.retarget(index, self.selected_image(.2 if index == self.check_index else .8))
            self.assertEqual([row["index"] for row in affected], [self.check_index, self.check_index+1])
            check, action = self.value.steps[self.check_index:self.check_index+2]
            self.assertEqual(check["image_target"], action["image_target"])
            validate_pair_links(self.value.steps)

    def test_bad_explicit_link_rejected_without_partial_edit(self):
        self.value.steps[self.check_index]["linked_step_id"] = "unrelated-step"
        old, labels = copy.deepcopy(self.value.steps), list(self.value.labels)
        with self.assertRaises(OperationError) as error:
            self.value.retarget(self.check_index, self.selected_image())
        self.assertEqual(error.exception.code, "popup_condition_link_mismatch")
        self.assertEqual(self.value.steps, old)
        self.assertEqual(self.value.labels, labels)

    def test_legacy_adjacency_requires_exact_original_image(self):
        check, action = self.value.steps[self.check_index:self.check_index+2]
        check.pop("linked_step_id"); action.pop("precondition_step_id")
        check["image_target"]["anchor"]["x"] = .1
        old = copy.deepcopy(self.value.steps)
        with self.assertRaises(OperationError):
            self.value.retarget(self.check_index+1, self.selected_image())
        self.assertEqual(self.value.steps, old)

    def test_semantic_checkpoint_retarget_also_changes_linked_parent(self):
        value = draft(); events, windows = semantic_recording()
        value.recorded(events, windows=windows, auto_popup=True)
        index = next(i for i, row in enumerate(value.steps) if row.get("review_mode") == "menu_semantic")
        value.selections["picked"] = {"target": ("editor", "menu"), "recognition": "uia",
            "selector": {"name": "Another observed menu", "role": "MenuItem"}, "actions": ["click"], "label": "Another observed menu"}
        affected = value.repair(index, {"selection_id": "picked"})
        self.assertEqual([row["index"] for row in affected], [index, index+1])
        self.assertEqual(value.steps[index]["menu_target"], value.steps[index+1]["menu_target"])
        self.assertEqual(value.steps[index+1]["selector"]["name"], "Another observed menu")


class SemanticPresenceTests(unittest.TestCase):
    def runtime(self, highlighted=False):
        target = {"pid": 5, "window_id": 7}
        item = {"index": 0, "parent_index": None, "name": "Export to excel", "role": "MenuItem",
            "visible": True, "enabled": True, "has_children": True, "expansion_state": "collapsed",
            "bounds": {"x": 10, "y": 10, "width": 100, "height": 25}, "supported_actions": ["expand"],
            "locator": {"provider": "uia_raw", "runtime_id": [1, 3]}, "evidence": {"observed": True},
            "highlighted": highlighted}
        observation = {"target": target, "read_only": True, "authorizes_input": False,
            "sources": {"uia_raw": {"status": "ok", "complete": True, "truncated": False, "elements": [item]}}}
        runtime = SimpleNamespace(check_active=mock.Mock(), observe_menu=mock.Mock(return_value=observation),
                                  image_action=mock.Mock(side_effect=AssertionError("Pixels cannot gate this check")))
        return runtime, target, observation

    def test_white_and_highlighted_parent_pass_without_image_gate_or_input(self):
        from semantic_menus import verify_menu_precondition
        step = {"operation": "checkpoint", "review_mode": "menu_semantic",
                "menu_target": {"path": [{"name": "Export to excel", "role": "MenuItem"}]}}
        for highlighted in (False, True):
            runtime, target, _ = self.runtime(highlighted)
            answer = verify_menu_precondition(runtime, step, target)
            self.assertTrue(answer["task_verified"])
            self.assertFalse(answer["input_dispatched"])
            self.assertFalse(answer["business_result_verified"])
            runtime.image_action.assert_not_called()


    def test_wrong_label_or_disabled_parent_stops_without_image_substitution(self):
        from semantic_menus import verify_menu_precondition
        step = {"menu_target": {"path": [{"name": "Export to excel", "role": "MenuItem"}]}}
        for field, value in (("name", "Other menu"), ("enabled", False), ("visible", False)):
            runtime, target, observation = self.runtime()
            observation["sources"]["uia_raw"]["elements"][0][field] = value
            answer = verify_menu_precondition(runtime, step, target)
            self.assertFalse(answer["task_verified"])
            self.assertFalse(answer["input_dispatched"])
            runtime.image_action.assert_not_called()


class LegacyMenuRepairTests(unittest.TestCase):
    def legacy(self):
        value = draft(); events, windows = recording()
        value.recorded(events, windows=windows, auto_popup=True)
        parent = next(i for i, row in enumerate(value.steps) if row["operation"] == "expand_menu")
        check, expansion = value.steps[parent-1:parent+1]
        expansion.pop("menu_target"); expansion.pop("selector")
        expansion.pop("precondition_step_id")
        check.pop("menu_target"); check.pop("linked_step_id")
        check.update(review_mode="popup_image", image_target=copy.deepcopy(expansion["image_target"]))
        value.validate()
        return value, parent

    def select(self, value, name="Export to excel"):
        value.selections["native-parent"] = {"target": ("editor", "menu"), "recognition": "uia",
            "selector": {"name": name, "role": "MenuItem"}, "actions": ["click"], "label": name}
        return {"selection_id": "native-parent"}

    def test_legacy_parent_or_checkpoint_native_repick_updates_only_proven_pair(self):
        for side in ("parent", "checkpoint"):
            value, index = self.legacy()
            old = copy.deepcopy(value.steps)
            selected = self.select(value)
            affected = value.repair(index if side == "parent" else index-1, selected)
            check, expansion = value.steps[index-1:index+1]
            self.assertEqual([row["index"] for row in affected], [index-1, index])
            self.assertEqual(check["review_mode"], "menu_semantic")
            self.assertEqual(check["menu_target"], expansion["menu_target"])
            self.assertEqual(check["linked_step_id"], expansion["step_id"])
            self.assertEqual(expansion["precondition_step_id"], check["step_id"])
            self.assertEqual(expansion["child_window"], old[index]["child_window"])
            self.assertEqual(expansion["child_image_target"], old[index]["child_image_target"])
            self.assertNotIn("child_menu_target", expansion)
            self.assertEqual(value.steps[index+1:], old[index+1:])

    def test_mismatch_is_atomic_and_human_check_is_never_promoted(self):
        value, index = self.legacy()
        value.steps[index-1]["image_target"]["anchor"]["x"] = .1
        old = copy.deepcopy(value.steps)
        with self.assertRaises(OperationError):
            value.repair(index-1, self.select(value))
        self.assertEqual(value.steps, old)
        value, index = self.legacy()
        value.edit({"index": index-1, "review_mode": "human"})
        human = copy.deepcopy(value.steps[index-1])
        value.repair(index, self.select(value))
        self.assertEqual(value.steps[index-1], human)
        self.assertNotIn("precondition_step_id", value.steps[index])

    def test_legacy_logical_child_requires_saved_name_and_preserves_relation(self):
        from test_menu_expansion import step as logical_step
        for named in (True, False):
            value = draft()
            expansion = logical_step(program_id="editor", window_ref="main")
            if not named: expansion["child_selector"] = {"role": "MenuItem", "automation_id": "REA_UNOBSERVED"}
            value.steps, value.labels = [expansion], ["Old parent"]
            value.selections["picked"] = {"target": ("editor", "main"), "recognition": "uia",
                "selector": {"role": "MenuItem", "name": "Observed new parent"}, "label": "Observed new parent", "actions": ["click"]}
            old = copy.deepcopy(value.steps)
            if not named:
                with self.assertRaises(OperationError): value.repair(0, {"selection_id": "picked"})
                self.assertEqual(value.steps, old)
            else:
                value.repair(0, {"selection_id": "picked"})
                self.assertEqual([row["name"] for row in value.steps[0]["child_menu_target"]["path"]], ["Observed new parent", "Active sheet"])
                self.assertEqual(value.steps[0]["child_selector"]["within"], value.steps[0]["selector"])

    def test_reopen_explicit_repick_saves_new_revision_and_keeps_old_run_snapshot(self):
        from server import TaskStore
        from task_revision import TaskRevisions
        from test_workflows import FakeRuntime, ScriptedOperations
        value, index = self.legacy()
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(directory, {"programs": [{"id": "editor"}]})
            args = {"id": "legacy-menu", "name": "Legacy menu", "instructions": "Saved recipe", "expected": "Separate result check",
                    "program_ids": ["editor"], "variables": {}, "steps": copy.deepcopy(value.steps)}
            original = store.save(args)
            reopened = ProcessDraft(value.programs[:1], original)
            self.assertEqual(reopened.steps, original["steps"])
            runtime = FakeRuntime(directory)
            runtime.guard.window_resolver = lambda hwnd: 100
            runtime.image_action = mock.Mock(side_effect=lambda step, target: (
                {"task_verified": False, "input_dispatched": False, "diagnostic": {"code": "image_not_found"}}
                if step["operation"] == "wait_for_image" else {"task_verified": False, "input_dispatched": True, "verification_deferred": True}))
            runner = WorkflowRunner(directory); self.addCleanup(runner.close)
            with mock.patch("operations.Operations", return_value=ScriptedOperations()), \
                 mock.patch("workflows.execute_window_wait", return_value={"task_verified": True, "input_dispatched": False, "target": {"pid": 100, "window_id": 11}}):
                first = runner.run(runtime, original, {}, [{"program_id": "editor", "pid": 100, "window_id": 10}])
            self.assertEqual(first["pending_step"], index-1)
            snapshot_path = runner._snapshot_path(first["run_id"])
            snapshot_bytes = snapshot_path.read_bytes()
            reopened.repair(index, self.select(reopened))
            revised = store.save({**args, "steps": reopened.steps})
            self.assertEqual(revised["revision"], 2)
            self.assertEqual(TaskRevisions(store).get("legacy-menu", 1)["steps"], original["steps"])
            self.assertEqual(snapshot_path.read_bytes(), snapshot_bytes)
            frozen, unused = runner.resume_snapshot(first["run_id"], "legacy-menu", {})
            self.assertEqual(frozen["steps"][index-1]["review_mode"], "popup_image")
            self.assertNotIn("menu_target", frozen["steps"][index])

    def native_menu(self, target):
        bounds = {"x": 10, "y": 10, "width": 100, "height": 25}
        native = {**target, "status": "selected", "human_confirmed": True,
            "element": {"role": "MenuItem", "name": "Export to excel", "is_password": False},
            "bounds": bounds, "point": {"x": 50, "y": 20}}
        row = {"index": 0, "parent_index": None, "role": "MenuItem", "name": "Export to excel",
            "visible": True, "enabled": True, "has_children": True, "expansion_state": "collapsed",
            "bounds": copy.deepcopy(bounds), "supported_actions": ["expand"],
            "locator": {"provider": "direct_msaa", "child_id": 17}, "evidence": {"source": "fresh"}}
        observation = {"target": target, "read_only": True, "authorizes_input": False, "sources": {
            "uia_raw": {"status": "error", "complete": False, "truncated": False, "elements": []},
            "direct_msaa": {"status": "ok", "complete": True, "truncated": False, "elements": [row]}}}
        return native, observation

    def test_editor_native_menu_repick_uses_complete_msaa_without_driver_or_input(self):
        value, index = self.legacy()
        target = {key: value.targets[("editor", "menu")][key] for key in ("pid", "window_id")}
        native, observation = self.native_menu(target)
        library = SimpleNamespace(_observe=mock.Mock(side_effect=AssertionError("Driver has no menu")))
        editor = ProcessEditors(library, mock.Mock())
        runtime = SimpleNamespace(execution_lock=threading.RLock(), check_active=mock.Mock(),
            observe_menu=mock.Mock(return_value=observation), menu_action=mock.Mock(side_effect=AssertionError("No authoring input")))
        job = {"draft": value, "runtime": runtime, "result": {}}
        with mock.patch.object(editor, "_check"), mock.patch.object(editor, "_program"), \
             mock.patch.object(editor, "_visual", return_value={"native_target": native}) as picker:
            result = editor._command(job, {"action": "retarget_step", "payload": {"index": index}})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(picker.call_args.args[2]["include_native"])
        runtime.observe_menu.assert_called_once_with(target, timeout_ms=2000)
        library._observe.assert_not_called(); runtime.menu_action.assert_not_called()
        self.assertEqual(value.steps[index-1]["review_mode"], "menu_semantic")
        for key in ("child_id", "locator", "evidence", "direct_msaa"):
            self.assertNotIn(key, json.dumps(value.steps))

    def test_editor_native_menu_repick_failure_never_falls_back_or_mutates_draft(self):
        for failure in ("ambiguous", "source_error", "hidden", "disabled", "bounds", "wrong_target"):
            value, index = self.legacy()
            target = {key: value.targets[("editor", "menu")][key] for key in ("pid", "window_id")}
            native, observation = self.native_menu(target)
            source = observation["sources"]["direct_msaa"]
            if failure == "ambiguous": source["elements"].append({**copy.deepcopy(source["elements"][0]), "index": 1})
            if failure == "source_error": source.update(status="error", complete=False, elements=[])
            if failure == "hidden": source["elements"][0]["visible"] = False
            if failure == "disabled": source["elements"][0]["enabled"] = False
            if failure == "bounds": source["elements"][0]["bounds"]["x"] += 10
            if failure == "wrong_target": native["window_id"] += 1
            library = SimpleNamespace(_observe=mock.Mock(side_effect=AssertionError("Do not use Driver fallback")))
            editor = ProcessEditors(library, mock.Mock())
            runtime = SimpleNamespace(execution_lock=threading.RLock(), check_active=mock.Mock(), observe_menu=mock.Mock(return_value=observation))
            job = {"draft": value, "runtime": runtime, "result": {}}
            before = copy.deepcopy(value.__dict__)
            with self.subTest(failure=failure), mock.patch.object(editor, "_check"), mock.patch.object(editor, "_program"), \
                 mock.patch.object(editor, "_visual", return_value={**image_choice(), "native_target": native}), \
                 self.assertRaises(OperationError):
                editor._command(job, {"action": "retarget_step", "payload": {"index": index-1}})
            self.assertEqual(value.__dict__, before)
            library._observe.assert_not_called()


class SemanticWorkflowTests(unittest.TestCase):
    def test_local_trial_exposes_projected_image_reason_without_application_payload(self):
        raw = {"status": "needs_review", "pending_step": 2, "last_result": {
            "diagnostic": {"code": "image_not_found", "last_match": {
                "status": "not_found", "score": .961, "threshold": .94,
                "template_png": "PRIVATE_PIXELS", "path": "PRIVATE_PATH",
                "appearance_diagnostic": {"accepted": False, "reason": "missing_ink", "scope": "clicked_band",
                    "missing_ink_pixels": 12, "clicked_band": {"ink_pixels": 20, "dice": .3, "minimum_dice": .8}}}}}}
        result = ProcessEditors._trial_summary(raw, {"id": "fixture"})
        projected = result["image_comparison"]
        self.assertEqual(projected["appearance_diagnostic"]["reason"], "missing_ink")
        self.assertEqual(projected["appearance_diagnostic"]["clicked_band"]["minimum_dice"], .8)
        self.assertNotIn("PRIVATE", json.dumps(projected))
        self.assertFalse(result["automatic_retry"])

    def test_run_uses_semantic_precondition_and_preserves_frozen_snapshot_on_resume(self):
        from test_workflows import FakeRuntime, ScriptedOperations
        from test_image_steps import png
        value = draft(); events, windows = semantic_recording()
        value.recorded(events, windows=windows, auto_popup=True)
        task = {"id": "semantic-run", "revision": 1, "program_ids": ["editor"], "variables": {}, "steps": copy.deepcopy(value.steps)}
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime(directory)
            runtime.guard.window_resolver = lambda hwnd: 100
            runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True, "verification_deferred": True})
            runtime.capture_checkpoint = mock.Mock(return_value={"content": [{"type": "image", "mimeType": "image/png", "data": png()}]})
            runner = WorkflowRunner(directory)
            self.addCleanup(runner.close)
            engine = ScriptedOperations([
                {"task_verified": True, "input_dispatched": True, "business_result_verified": False},
                {"task_verified": False, "input_dispatched": True, "verification_deferred": True}])
            def window_wait(unused, definition, bindings, **kwargs):
                return {"task_verified": True, "input_dispatched": False,
                        "target": {"pid": 100, "window_id": 11 if definition["window_ref"] == "menu" else 12}}
            verified = {"task_verified": True, "input_dispatched": False, "business_result_verified": False}
            with mock.patch("operations.Operations", return_value=engine), \
                 mock.patch("workflows.execute_window_wait", side_effect=window_wait) as waits, \
                 mock.patch("menu_expansion.verify_menu_precondition", return_value=verified) as semantic:
                first = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 10}])
                self.assertEqual(first["pending_step"], len(task["steps"])-1)
                self.assertFalse(first["task_verified"])
                self.assertEqual(semantic.call_count, 1)
                self.assertEqual(runtime.image_action.call_count, 1)
                self.assertEqual([row[0]["operation"] for row in engine.calls], ["expand_menu", "menu_command"])
                self.assertTrue(any(call.kwargs.get("menu_hint") for call in waits.call_args_list))
                self.assertFalse(any(call.kwargs.get("image_hint") for call in waits.call_args_list))
                old_snapshot_hash = first["snapshot_hash"]
                # Editing the task during a paused run never replaces its
                # immutable reviewed recipe or dispatches a second command.
                task["steps"][-2]["menu_target"]["path"][-1]["name"] = "Unrelated later edit"
                final = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 10}],
                    resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
                self.assertTrue(final["task_verified"])
                self.assertEqual(final["snapshot_hash"], old_snapshot_hash)
                self.assertEqual(len(engine.calls), 2)
                self.assertEqual(runtime.image_action.call_count, 1)

if __name__ == "__main__":
    unittest.main()
