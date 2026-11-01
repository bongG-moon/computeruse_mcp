"""Explicit grid intent edits and menu conditions, without corporate app data."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from checkpoint_groups import popup_condition, use_popup_image_conditions, validate_pair_links
from operations import OperationError
from process_editor import ProcessDraft, ProcessEditors
from test_learning import TARGET
from test_menu_semantic_authoring import semantic_recording


MENU = {"path": [{"name": "Export to excel", "role": "MenuItem"}]}


def intent():
    return {"format": "computer-grid-intent/v1", "grid_selector": {"role": "DataGrid", "automation_id": "result_grid"},
        "export_scope": "whole_active_sheet", "active_sheet": {"selector": {"role": "TabItem", "name": "Results"},
            "property": "selected", "equals": True},
        "ready_when": [{"selector": {"role": "Text", "automation_id": "query_state"}, "property": "value", "equals": "ready"}],
        "view_conditions": [], "allow_empty": False}


def observed_grid():
    return {"target": dict(TARGET), "read_only": True, "authorizes_input": False,
        "elements": [
            {"element_index": 0, "role": "DataGrid", "automation_id": "result_grid", "enabled": True, "offscreen": False,
             "bounds": {"x": 100, "y": 200, "width": 600, "height": 200},
             "patterns": {"Grid": {"status": "supported", "row_count": 4, "column_count": 2}}},
            {"element_index": 1, "parent_index": 0, "role": "TabItem", "name": "Results", "selected": True},
            {"element_index": 2, "parent_index": 0, "role": "DataItem", "name": "VARIABLE-CELL-DATA", "enabled": True,
             "offscreen": False, "bounds": {"x": 110, "y": 230, "width": 80, "height": 20},
             "grid_item": {"status": "supported", "containing_grid": 0, "row": 1, "column": 0, "row_span": 1, "column_span": 1}},
            {"element_index": 3, "role": "Text", "automation_id": "query_state", "value": "ready"}],
        "grid_selection": {"status": "supported", "complete": True, "grid_element_index": 0, "selected_items": [2]}}


def recorded_draft():
    draft = ProcessDraft([{"program_id": "editor", **TARGET, "label": "Fixture"}])
    events, windows = semantic_recording()
    draft.recorded(events, windows=windows, auto_popup=True)
    draft.steps[0]["step_id"] = "grid-context-step"
    query = {"step_id": "query-step", "operation": "click", "program_id": "editor", "window_ref": "main",
        "selector": {"role": "Button", "automation_id": "query_button"},
        "expect": [{"selector": {"role": "Text", "automation_id": "query_state"}, "property": "value",
            "equals": "ready", "require_change": True}]}
    draft.steps.insert(0, query)
    draft.labels.insert(0, "Query")
    draft.validate(allow_pending=True)
    return draft, events, windows


def convert(draft, **overrides):
    args = {"query_step_id": "query-step", "observation": observed_grid()}
    args.update(overrides)
    return draft.define_grid_export(1, intent(), MENU, **args)


class GridIntentAuthoringTests(unittest.TestCase):
    def test_generic_recorded_right_click_is_not_implicitly_export(self):
        draft, _, _ = recorded_draft()
        self.assertEqual(draft.steps[1]["operation"], "image_right_click")
        self.assertNotIn("grid_intent", draft.steps[1])

    def test_explicit_edit_preserves_recording_and_references_without_cell_content(self):
        draft, events, windows = recorded_draft()
        source = copy.deepcopy((events, windows, draft.steps, draft.labels))
        draft.task_id, draft.revision = "saved-task", 4
        result = convert(draft)
        self.assertEqual((events, windows), source[:2])
        self.assertEqual(draft.steps[2:], source[2][2:])
        self.assertEqual((draft.task_id, draft.revision), ("saved-task", 4))
        step = draft.steps[1]
        self.assertEqual(step["step_id"], "grid-context-step")
        self.assertEqual(step["query_step_id"], "query-step")
        self.assertNotIn("image_target", step)
        self.assertFalse(result["input_dispatched"])
        self.assertFalse(result["query_verified"])
        serialized = json.dumps(step)
        for forbidden in ("VARIABLE-CELL-DATA", "element_index", "bounds", "window_id", '"pid"', "template_png"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(draft.summaries()[1]["recognition"], "grid_intent")
        self.assertFalse(draft.summaries()[1]["repairable"])
        self.assertTrue(draft.summaries()[1]["grid_intent_editable"])

    def test_changed_result_values_do_not_change_authored_intent(self):
        outputs = []
        for value, count in (("before", 4), ("after", 20)):
            draft, _, _ = recorded_draft()
            snapshot = observed_grid()
            snapshot["elements"][2]["name"] = value
            snapshot["elements"][0]["patterns"]["Grid"]["row_count"] = count
            convert(draft, observation=snapshot)
            outputs.append(draft.steps[1])
        self.assertEqual(outputs[0], outputs[1])

    def test_missing_grid_evidence_leaves_original_image_steps_unchanged(self):
        draft, _, _ = recorded_draft()
        original = copy.deepcopy(draft.steps)
        snapshot = observed_grid(); snapshot["grid_selection"]["complete"] = False
        with self.assertRaises(OperationError) as error:
            convert(draft, observation=snapshot)
        self.assertEqual(error.exception.code, "grid_selection_unavailable")
        self.assertEqual(draft.steps, original)

    def test_wrong_window_and_caller_input_authorization_are_not_observations(self):
        for mutate in (lambda s: s["target"].update(window_id=999), lambda s: s.update(authorizes_input=True)):
            draft, _, _ = recorded_draft(); original = copy.deepcopy(draft.steps)
            snapshot = observed_grid(); mutate(snapshot)
            with self.assertRaises(OperationError) as error:
                convert(draft, observation=snapshot)
            self.assertEqual(error.exception.code, "grid_observation_invalid")
            self.assertEqual(draft.steps, original)

    def test_query_requires_preceding_same_window_fresh_result_condition(self):
        for mutation in (lambda step: step["expect"][0].pop("require_change"),
                         lambda step: step.update(window_ref="other"),
                         lambda step: step.update(operation="set_value", value="anything")):
            draft, _, _ = recorded_draft(); mutation(draft.steps[0])
            before = copy.deepcopy(draft.steps)
            with self.assertRaises(OperationError) as error:
                convert(draft)
            self.assertEqual(error.exception.code, "grid_query_step_required")
            self.assertEqual(draft.steps, before)

    def test_unobserved_parent_name_or_wrong_popup_relation_is_not_inferred(self):
        for mutation in (lambda steps: steps[4].pop("menu_target"),
                         lambda steps: steps[2].update(owner_ref="another")):
            draft, _, _ = recorded_draft(); mutation(draft.steps)
            before = copy.deepcopy(draft.steps)
            with self.assertRaises(OperationError) as error:
                convert(draft)
            self.assertIn(error.exception.code, {"grid_menu_semantics_required", "grid_menu_relation_unverified"})
            self.assertEqual(draft.steps, before)

    def test_row_edit_is_not_automatically_replaced_and_explicit_human_review_stays(self):
        draft, _, _ = recorded_draft()
        draft.steps[1]["operation"] = "image_click"
        with self.assertRaises(OperationError) as error:
            convert(draft)
        self.assertEqual(error.exception.code, "grid_intent_step_required")
        draft, _, _ = recorded_draft()
        draft.edit({"index": 3, "review_mode": "human"})
        before = copy.deepcopy(draft.steps[3])
        convert(draft)
        self.assertEqual(draft.steps[3], before)

    def test_generic_retarget_cannot_drop_output_scope(self):
        draft, _, _ = recorded_draft(); convert(draft)
        before = copy.deepcopy(draft.steps)
        with self.assertRaises(OperationError) as error:
            draft.repair(1, {"selection_id": "not-a-scope"})
        self.assertEqual(error.exception.code, "grid_intent_edit_required")
        self.assertEqual(draft.steps, before)


class MenuConditionIntentTests(unittest.TestCase):
    def test_leaf_condition_uses_current_command_semantics_not_hover_pixels(self):
        action = {"operation": "menu_command", "program_id": "editor", "window_ref": "menu",
            "menu_target": {"path": [*MENU["path"], {"name": "Active sheet", "role": "MenuItem"}]},
            "completion_mode": "human"}
        check = {"operation": "checkpoint", "program_id": "editor", "window_ref": "menu",
            "review_origin": "recording_generated", "review_mode": "visual", "opened_from": "main", "step_id": "check-step"}
        popup_condition(check, action)
        self.assertEqual(check["review_mode"], "menu_semantic")
        self.assertEqual(check["menu_purpose"], "command")
        self.assertNotIn("image_target", check)
        self.assertTrue(validate_pair_links([check, action]))

    def test_explicit_human_condition_is_not_replaced_by_leaf_upgrade(self):
        check = {"operation": "checkpoint", "step_id": "check-step", "program_id": "editor", "window_ref": "menu",
            "review_mode": "human", "review_origin": "explicit", "opened_from": "main", "message": "Approve"}
        command = {"operation": "menu_command", "program_id": "editor", "window_ref": "menu",
            "menu_target": MENU, "completion_mode": "human"}
        before = copy.deepcopy([check, command])
        with self.assertRaises(ValueError):
            use_popup_image_conditions([check, command], ["check-step"])
        self.assertEqual([check, command], before)


class MenuCompletionAuthoringTests(unittest.TestCase):
    def setup_draft(self):
        draft, events, windows = recorded_draft()
        index = next(i for i, step in enumerate(draft.steps) if step["operation"] == "menu_command")
        draft.steps[index]["step_id"] = "saved-menu-command"
        draft.selections["output"] = {"recognition": "uia", "target": ("editor", "main"),
            "selector": {"role": "Text", "automation_id": "export_state"}}
        draft.task_id, draft.revision = "existing-recording", 4
        return draft, index, events, windows

    def completion_payload(self, index, **changes):
        return {"index": index, "selection_id": "output", "property": "value", "equals": "exported",
            "completion_window_ref": "main", **changes}

    def test_explicit_menu_result_edit_preserves_source_and_removes_only_paired_generated_return(self):
        draft, index, events, windows = self.setup_draft()
        source = copy.deepcopy((events, windows, draft.steps))
        step_id = draft.steps[index]["step_id"]
        self.assertEqual(draft.steps[index+1]["return_from"], draft.steps[index]["window_ref"])
        draft.completion(self.completion_payload(index, require_change=False))
        command = draft.steps[index]
        self.assertEqual(command["completion_mode"], "conditions")
        self.assertEqual(command["completion_window_ref"], "main")
        self.assertTrue(command["expect"][0]["require_change"])
        self.assertEqual(command["step_id"], step_id)
        self.assertEqual(len(draft.steps), len(source[2])-1)
        self.assertEqual(draft.steps[:index], source[2][:index])
        self.assertEqual((draft.task_id, draft.revision), ("existing-recording", 4))
        self.assertEqual((events, windows), source[:2])
        for private in ('"pid"', '"window_id"', '"selection_id"'):
            self.assertNotIn(private, json.dumps(command))
        self.assertIn("이번 명령 이후 결과 변화 확인", draft.summaries()[index]["detail"])

    def test_explicit_user_checkpoint_is_preserved(self):
        draft, index, _, _ = self.setup_draft()
        draft.edit({"index": index+1, "review_mode": "human"})
        explicit = copy.deepcopy(draft.steps[index+1])
        draft.completion(self.completion_payload(index))
        self.assertEqual(draft.steps[index+1], explicit)
        self.assertEqual(explicit["review_origin"], "explicit")

    def test_unrelated_generated_checkpoint_is_not_removed(self):
        draft, index, _, _ = self.setup_draft()
        # This separate observation of the parent menu is not the command's
        # recorded return edge. The new output condition must not consume it.
        draft.steps[index+1].update(window_ref="menu", return_from="submenu")
        original = copy.deepcopy(draft.steps[index+1])
        with mock.patch.object(draft, "validate"):
            draft.completion(self.completion_payload(index))
        self.assertEqual(draft.steps[index+1], original)

    def test_result_choices_are_same_program_earlier_live_logical_refs_only(self):
        draft, index, _, _ = self.setup_draft()
        main = draft.targets[("editor", "main")]
        draft.targets[("editor", "future")] = {**main, "window_ref": "future", "window_id": 777}
        draft.targets[("other", "foreign")] = {**main, "program_id": "other", "window_ref": "foreign", "window_id": 888}
        draft.steps.append({"operation": "wait_for_window", "program_id": "editor", "window_ref": "future"})
        draft.targets[("editor", "submenu")]["window_id"] = 0
        choices = draft.completion_targets(index)
        self.assertEqual({row["window_ref"] for row in choices}, {"main", "menu"})
        self.assertNotIn('"pid"', json.dumps(choices))
        self.assertNotIn('"window_id"', json.dumps(choices))
        for ref in ("future", "foreign", "submenu", "new", None, {}):
            with self.subTest(ref=ref):
                previous = copy.deepcopy(draft.steps)
                with self.assertRaises(OperationError) as error:
                    draft.completion(self.completion_payload(index, completion_window_ref=ref))
                self.assertEqual(error.exception.code, "completion_window_unavailable")
                self.assertEqual(draft.steps, previous)

    def test_picker_evidence_from_popup_cannot_be_used_as_main_result(self):
        draft, index, _, _ = self.setup_draft()
        draft.selections["output"]["target"] = ("editor", "submenu")
        previous = copy.deepcopy(draft.steps)
        with self.assertRaises(OperationError):
            draft.completion(self.completion_payload(index))
        self.assertEqual(draft.steps, previous)

    def test_completion_edit_cannot_move_other_conditions_to_a_different_window(self):
        draft, index, _, _ = self.setup_draft()
        draft.completion(self.completion_payload(index))
        draft.steps[index]["expect"].append({"selector": {"role": "Edit", "name": "Output scope"},
            "property": "value", "equals": "whole_active_sheet"})
        draft.selections["menu-output"] = {"recognition": "uia", "target": ("editor", "menu"),
            "selector": {"role": "Text", "automation_id": "export_state"}}
        previous = copy.deepcopy(draft.steps)
        for condition_index in (-1, 0, 1):
            with self.subTest(condition_index=condition_index), self.assertRaises(OperationError) as error:
                draft.completion(self.completion_payload(index, selection_id="menu-output",
                    completion_window_ref="menu", condition_index=condition_index))
            self.assertEqual(error.exception.code, "completion_window_in_use")
            self.assertEqual(draft.steps, previous)

    def test_added_static_menu_scope_condition_keeps_existing_change_evidence(self):
        draft, index, _, _ = self.setup_draft()
        draft.completion(self.completion_payload(index))
        original = copy.deepcopy(draft.steps[index]["expect"][0])
        draft.selections["scope"] = {"recognition": "uia", "target": ("editor", "main"),
            "selector": {"role": "Edit", "name": "Output scope"}}
        draft.completion(self.completion_payload(index, selection_id="scope", condition_index=-1,
            equals="whole_active_sheet", require_change=False))
        self.assertEqual(draft.steps[index]["expect"][0], original)
        self.assertNotIn("require_change", draft.steps[index]["expect"][1])

    def test_menu_completion_command_is_explicit_undoable_and_preview_offers_result_window(self):
        draft, index, _, _ = self.setup_draft()
        previous = copy.deepcopy(draft.steps)
        editors = ProcessEditors(None, None)
        job = {"draft": draft, "runtime": SimpleNamespace(), "result": {}, "dirty": False}
        with mock.patch.object(editors, "_check"):
            preview = editors._command(job, {"action": "preview_step", "payload": {"index": index}})["preview"]
            self.assertTrue(preview["completion_editable"])
            self.assertEqual(preview["completion_window_ref"], "main")
            self.assertIn("main", {row["window_ref"] for row in preview["completion_targets"]})
            self.assertEqual(draft.steps, previous)
            result = editors._command(job, {"action": "set_completion", "payload": self.completion_payload(index)})
            self.assertTrue(result["can_undo"])
            editors._command(job, {"action": "undo", "payload": {}})
        self.assertEqual(draft.steps, previous)
        self.assertEqual((draft.task_id, draft.revision), ("existing-recording", 4))

    def test_result_picker_reobserves_selected_owner_not_closed_menu(self):
        draft, index, _, _ = self.setup_draft()
        native = {"native_target": {"pid": TARGET["pid"], "window_id": TARGET["window_id"], "element": {}}}
        library = SimpleNamespace(_observe=mock.Mock(return_value=({"elements": []}, None)))
        editors = ProcessEditors(library, None)
        runtime = SimpleNamespace(execution_lock=threading.RLock())
        job = {"draft": draft, "runtime": runtime, "result": {}}
        chosen = {"selection_id": "output", "recognition": "uia", "role": "Text", "label": "Export state"}
        with mock.patch.object(editors, "_check"), mock.patch.object(editors, "_program"), \
             mock.patch.object(editors, "_visual", return_value=native) as pick, \
             mock.patch.object(draft, "remember", return_value=chosen) as remember:
            result = editors._command(job, {"action": "pick_completion", "payload": {
                "index": index, "completion_window_ref": "main"}})
        self.assertEqual(pick.call_args.args[2]["window_id"], TARGET["window_id"])
        self.assertEqual(library._observe.call_args.args[1], TARGET)
        self.assertEqual(remember.call_args.args[0].get("window_ref", "main"), "main")
        self.assertEqual(result["selection"]["completion_window_ref"], "main")
        self.assertTrue(result["selection"]["completion_for_menu"])
        self.assertEqual(draft.steps[index]["completion_mode"], "human")

    def test_preview_and_picker_keep_existing_condition_for_item_editor(self):
        draft, index, _, _ = self.setup_draft()
        draft.completion(self.completion_payload(index))
        check = copy.deepcopy(draft.steps[index]["expect"][0])
        native = {"native_target": {"pid": TARGET["pid"], "window_id": TARGET["window_id"], "element": {}}}
        library = SimpleNamespace(_observe=mock.Mock(return_value=({"elements": []}, None)))
        editors = ProcessEditors(library, None)
        job = {"draft": draft, "runtime": SimpleNamespace(execution_lock=threading.RLock()), "result": {}}
        chosen = {"selection_id": "output", "recognition": "uia", "role": "Text", "label": "Export state"}
        with mock.patch.object(editors, "_check"), mock.patch.object(editors, "_program"), \
             mock.patch.object(editors, "_visual", return_value=native), \
             mock.patch.object(draft, "remember", return_value=chosen):
            preview = editors._command(job, {"action": "preview_step", "payload": {"index": index}})["preview"]
            self.assertEqual(preview["completion_conditions"], [check])
            result = editors._command(job, {"action": "pick_completion", "payload": {
                "index": index, "completion_window_ref": "main", "condition_index": 0}})
        self.assertEqual(result["selection"]["condition_index"], 0)
        self.assertEqual(result["selection"]["completion_condition"], check)
        self.assertEqual(draft.steps[index]["expect"], [check])


class GridIntentEditorTransportTests(unittest.TestCase):
    def selection_payload(self, draft):
        for identity, selector in (("grid", intent()["grid_selector"]), ("sheet", intent()["active_sheet"]["selector"])):
            draft.selections[identity] = {"recognition": "uia", "target": ("editor", "main"), "selector": copy.deepcopy(selector)}
        return {"index": 1, "grid_selection_id": "grid", "sheet_selection_id": "sheet", "query_step_id": "query-step",
            "export_scope": "whole_active_sheet", "sheet_condition": {"property": "selected", "equals": True},
            "view_conditions": [], "view_conditions_confirmed": True, "allow_empty": False}

    def test_local_picker_ids_build_contract_without_user_json_or_cell_identity(self):
        draft, _, _ = recorded_draft()
        runtime = SimpleNamespace(observe_grid_context=mock.Mock(return_value=observed_grid()), execution_lock=threading.RLock())
        job = {"draft": draft, "runtime": runtime, "result": {}, "dirty": False}
        editors = ProcessEditors(None, None)
        with mock.patch.object(editors, "_check"), mock.patch.object(editors, "_program"):
            result = editors._command(job, {"action": "define_grid_export_selection", "payload": self.selection_payload(draft)})
        self.assertEqual(draft.steps[1]["grid_intent"], intent())
        self.assertTrue(result["can_undo"])
        self.assertEqual(len(job["undo"]), 1)
        self.assertEqual(runtime.observe_grid_context.call_count, 1)
        self.assertNotIn("selection_id", json.dumps(draft.steps[1]))

    def test_scope_is_explicit_and_unknown_picker_id_or_view_confirmation_blocks(self):
        for mutate in (lambda p: p.update(export_scope=""), lambda p: p.update(grid_selection_id="missing"),
                       lambda p: p.update(view_conditions_confirmed=False)):
            draft, _, _ = recorded_draft(); before = copy.deepcopy(draft.steps)
            payload = self.selection_payload(draft); mutate(payload)
            runtime = SimpleNamespace(observe_grid_context=mock.Mock(return_value=observed_grid()), execution_lock=threading.RLock())
            job = {"draft": draft, "runtime": runtime, "result": {}}
            editors = ProcessEditors(None, None)
            with mock.patch.object(editors, "_check"), mock.patch.object(editors, "_program"), self.assertRaises(OperationError):
                editors._command(job, {"action": "define_grid_export_selection", "payload": payload})
            self.assertEqual(draft.steps, before)
            self.assertFalse(job.get("undo"))

    def test_sheet_field_or_cell_value_is_rejected_before_provider_read(self):
        cases = (("Edit", "value", "Results"), ("DataItem", "name", "Results"),
                 ("TabItem", "value", "Results"), ("TabItem", "selected", False))
        for role, property_name, expected in cases:
            with self.subTest(role=role, property=property_name):
                draft, _, _ = recorded_draft(); before = copy.deepcopy(draft.steps)
                payload = self.selection_payload(draft)
                draft.selections["sheet"]["selector"]["role"] = role
                payload["sheet_condition"] = {"property": property_name, "equals": expected}
                callback = mock.Mock(return_value=observed_grid())
                job = {"draft": draft, "runtime": SimpleNamespace(observe_grid_context=callback), "result": {}}
                editors = ProcessEditors(None, None)
                with mock.patch.object(editors, "_check"), self.assertRaises(OperationError) as error:
                    editors._command(job, {"action": "define_grid_export_selection", "payload": payload})
                self.assertEqual(error.exception.code, "grid_active_sheet_unavailable")
                callback.assert_not_called()
                self.assertEqual(draft.steps, before)

    def test_setup_only_offers_same_window_changed_result_queries(self):
        draft, _, _ = recorded_draft()
        setup = draft.grid_export_setup(1)
        self.assertEqual(setup["query_steps"], [{"step_id": "query-step", "index": 0, "label": "Query"}])
        self.assertEqual(setup["menu_target"], MENU)
        draft.steps[0]["expect"][0].pop("require_change")
        self.assertEqual(draft.grid_export_setup(1)["query_steps"], [])

    def test_native_query_completion_can_explicitly_require_change(self):
        draft, _, _ = recorded_draft()
        draft.steps[0]["expect"][0].pop("require_change")
        draft.selections["ready"] = {"recognition": "uia", "target": ("editor", "main"),
            "selector": {"role": "Text", "automation_id": "query_state"}}
        draft.completion({"index": 0, "selection_id": "ready", "property": "value", "equals": "ready", "require_change": True})
        self.assertTrue(draft.steps[0]["expect"][0]["require_change"])
        self.assertEqual(len(draft.grid_export_setup(1)["query_steps"]), 1)

    def test_only_runtime_read_only_snapshot_is_used_and_edit_can_be_undone(self):
        draft, _, _ = recorded_draft(); original = copy.deepcopy(draft.steps)
        runtime = SimpleNamespace(observe_grid_context=mock.Mock(return_value=observed_grid()),
            execution_lock=threading.RLock())
        job = {"draft": draft, "runtime": runtime, "result": {}, "dirty": False}
        editors = ProcessEditors(None, None)
        command = {"action": "define_grid_export", "payload": {
            "index": 1, "grid_intent": intent(), "menu_target": MENU, "query_step_id": "query-step"}}
        with mock.patch.object(editors, "_check"), mock.patch.object(editors, "_program"):
            result = editors._command(job, command)
            self.assertEqual(runtime.observe_grid_context.call_count, 1)
            self.assertTrue(result["can_undo"])
            self.assertFalse(result["input_dispatched"])
            editors._command(job, {"action": "undo", "payload": {}})
        self.assertEqual(draft.steps, original)

    def test_client_cannot_supply_provider_proof_in_edit_payload(self):
        draft, _, _ = recorded_draft(); original = copy.deepcopy(draft.steps)
        job = {"draft": draft, "runtime": SimpleNamespace(), "result": {}}
        editors = ProcessEditors(None, None)
        with mock.patch.object(editors, "_check"), self.assertRaises(OperationError) as error:
            editors._command(job, {"action": "define_grid_export", "payload": {
                "index": 1, "grid_intent": intent(), "menu_target": MENU, "query_step_id": "query-step",
                "observation": observed_grid()}})
        self.assertEqual(error.exception.code, "invalid_grid_intent_edit")
        self.assertEqual(draft.steps, original)


@unittest.skipUnless(os.name == "nt" and Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Windows .NET required")
class NativeIntentDetailsTests(unittest.TestCase):
    def test_native_result_details_show_semantic_source_and_business_limit(self):
        with tempfile.TemporaryDirectory(prefix="cua-intent-details-") as folder:
            root = Path(folder); source = root / "probe.cs"; executable = root / "probe.exe"
            source.write_text('''using System;using System.Collections.Generic;using System.Web.Script.Serialization;
internal static class IntentDetailsProbe {static int Main(string[] args){
 Console.OutputEncoding=new System.Text.UTF8Encoding(false);
 var json=new JavaScriptSerializer();var data=json.Deserialize<Dictionary<string,object>>(args[0]);
 Console.WriteLine(json.Serialize(ProcessEditor.LocalExecutionDetails(data)));return 0;}}
''', encoding="utf-8")
            compilation = subprocess.run([r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "/nologo", "/target:exe",
                "/main:IntentDetailsProbe", "/codepage:65001", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                "/reference:System.Web.Extensions.dll", "/out:" + str(executable), str(Path(__file__).with_name("ProcessEditor.cs")), str(source)],
                capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(compilation.returncode, 0, compilation.stdout + compilation.stderr)
            result = {"menu_recognition": {"selected_source": "direct_msaa", "outcome": "matched", "fallback_reason": "uia_missing"},
                "grid_recognition": {"export_scope": "whole_active_sheet", "query_verified": False,
                    "grid_context_verified": True, "data_values_compared": False, "outcome": "ready"}}
            completed = subprocess.run([str(executable), json.dumps(result)], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(completed.returncode, 0)
            lines = json.loads(completed.stdout.decode("utf-8", errors="strict"))
            self.assertTrue(any("MSAA" in item for item in lines))
            self.assertIn("이번 조회 완료: 미확인", lines)
            self.assertIn("표와 선택 상태: 확인됨", lines)
            self.assertIn("표 또는 메뉴 확인은 출력 파일 생성 완료를 뜻하지 않습니다.", lines)

if __name__ == "__main__":
    unittest.main()
