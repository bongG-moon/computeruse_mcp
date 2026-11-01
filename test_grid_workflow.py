"""Runner/persistence integration using explicit provider/operation doubles.

The right-click crosses the actual Python Guard with a fake transport. Query,
menu expansion, leaf delivery and final capture are scripted model results.
This proves continuation contracts, not a live SmartMMDM export or workbook.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_grid_context as grid_runtime
import test_grid_intent as grid_model
import test_semantic_menus as menu_model
from workflows import WorkflowError, WorkflowRunner, validate_recipe


TARGET = {"pid": 10, "window_id": 20}


def recipe(scope="whole_active_sheet"):
    intent = grid_model.contract(scope)
    parent = menu_model.target("Export to excel")
    child = menu_model.target("Export to excel", "Active sheet")
    steps = [
        {"step_id": "query", "operation": "click", "selector": {"name": "Search", "role": "Button"},
         "expect": [{**copy.deepcopy(intent["ready_when"][0]), "require_change": True}]},
        {"step_id": "grid-open", **grid_runtime.step(scope)},
        {"step_id": "parent", "operation": "expand_menu", "menu_target": parent,
         "child_menu_target": child, "expansion_method": "hover"},
        {"step_id": "command", "operation": "menu_command", "menu_target": child, "completion_mode": "human"},
        {"step_id": "output", "operation": "checkpoint", "message": "Verify this run's actual workbook output"},
    ]
    for item in steps: item["program_id"] = "editor"
    return {"id": "grid-export", "revision": 1, "name": "Explicit grid export", "instructions": "Model integration only",
            "expected": "Output confirmation required", "program_ids": ["editor"], "variables": {}, "steps": steps}


class Runtime(grid_runtime.Runtime):
    def __init__(self, folder):
        super().__init__(folder)
        self.programs = [{"id": "editor", "exe": self.policy["allowed_apps"][0], "control_exes": []}]
        self.submenu_visible = False

    def observe_menu(self, target, **kwargs):
        rows = [menu_model.row(0, "Export to excel", is_parent=True)] if self.menu_visible else []
        if self.submenu_visible: rows.append(menu_model.row(1, "Active sheet", parent=0))
        return menu_model.observation(rows, actual=target,
            msaa=menu_model.source() if not self.menu_visible else None)


class OperationsModel:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []
        self.query_result = {"task_verified": True, "input_dispatched": True}
        self.expand_result = {"task_verified": True, "input_dispatched": True}
        self.command_result = {"task_verified": False, "input_dispatched": True, "verification_deferred": True}
        self.after_expand = None

    def execute(self, step, target, **kwargs):
        self.calls.append(copy.deepcopy(step))
        operation = step["operation"]
        if operation == "click": return copy.deepcopy(self.query_result)
        if operation == "assert":
            checks = step["expect"]
            verified = all(any(row.get("automation_id") == check["selector"].get("automation_id")
                and row.get(check["property"]) == check["equals"] for row in self.runtime.scene["elements"]) for check in checks)
            return {"task_verified": verified, "input_dispatched": False}
        if operation == "expand_menu":
            if self.expand_result.get("task_verified"):
                self.runtime.submenu_visible = True
                if self.after_expand: self.after_expand()
            return copy.deepcopy(self.expand_result)
        if operation == "menu_command":
            # Reproduce the private Guard callback boundary after approval;
            # no fake success is substituted for the real grid scope check.
            callback = getattr(self.runtime.guard, "_grid_export_revalidate", None)
            if not callable(callback): raise AssertionError("export lacks grid continuity capability")
            callback()
            self.runtime.menu_visible = self.runtime.submenu_visible = False
            return copy.deepcopy(self.command_result)
        raise AssertionError("Unexpected scripted operation " + operation)


class GridWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name)
        self.engine = OperationsModel(self.runtime)
        self.runner = WorkflowRunner(self.temp.name)
        self.task = recipe()
        self.targets = [{"program_id": "editor", **TARGET}]

    def run_task(self, *, resume=None, task=None):
        with mock.patch("operations.Operations", return_value=self.engine), mock.patch("workflows.execute_process_step",
                return_value={"task_verified": False, "input_dispatched": False, "checkpoint_ready": True, "checkpoint_content": []}):
            return self.runner.run(self.runtime, self.task if task is None else task, {}, self.targets, resume_run_id=resume)

    def count(self, operation): return sum(row["operation"] == operation for row in self.engine.calls)
    def right_clicks(self): return sum(call["name"] == "right_click" for call in self.runtime.transport.calls)

    def edit_record(self, run_id, edit):
        path = self.runner._path(run_id)
        value = json.loads(path.read_text(encoding="utf-8")); edit(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_verified_query_receipt_and_grid_proof_persist_before_leaf(self):
        result = self.run_task()
        self.assertEqual(result["completed_steps"], 4, result)
        self.assertEqual(result["pending_step"], 4)
        self.assertFalse(result["task_verified"])
        self.assertTrue(result["checkpoint"]["human_review_required"])
        self.assertTrue(result["step_receipts"][0]["query_input_verified"])
        self.assertIn("target_hash", result["step_receipts"][0])
        self.assertIn("context_hash", result["grid_verification"]["1"])
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (1, 1, 1))

    def test_query_completion_without_dispatched_query_cannot_export_old_results(self):
        for dispatched in (False, None):
            self.engine.query_result = {"task_verified": True, "input_dispatched": dispatched}
            result = self.run_task()
            self.assertEqual(result["completed_steps"], 1, result)
            self.assertEqual(result["last_result"]["diagnostic"]["code"], "grid_query_freshness_unverified")
            self.assertNotIn("query_input_verified", result["step_receipts"][0])
            self.assertEqual(self.right_clicks(), 0)
            self.assertEqual(self.count("menu_command"), 0)

    def test_query_incomplete_never_reaches_grid(self):
        self.engine.query_result = {"task_verified": False, "input_dispatched": True,
                                    "diagnostic": {"code": "query_still_processing"}}
        result = self.run_task()
        self.assertEqual(result["completed_steps"], 0)
        self.assertEqual(self.right_clicks(), 0)
        self.assertEqual(self.runtime.native_reads, 0)

    def test_selected_ranges_changed_after_parent_blocks_final_command(self):
        self.task = recipe("selected_ranges")
        self.runtime.scene["grid_selection"]["selected_items"] = [2, 3]
        self.engine.after_expand = lambda: self.runtime.scene["grid_selection"].update(selected_items=[2])
        result = self.run_task()
        self.assertEqual(result["completed_steps"], 3, result)
        self.assertEqual(result["step_delivery"], {"step_index": 3, "state": "not_sent"})
        self.assertEqual(result["last_result"]["diagnostic"]["code"], "grid_selection_scope_mismatch")
        self.assertEqual(self.count("menu_command"), 0)
        self.assertEqual(self.right_clicks(), 1)

    def test_changed_filter_before_leaf_is_not_ignored_as_visual_noise(self):
        self.engine.after_expand = lambda: self.runtime.scene["elements"][5].update(value="Filtered")
        result = self.run_task()
        self.assertEqual(result["completed_steps"], 3, result)
        self.assertEqual(result["last_result"]["diagnostic"]["code"], "grid_view_state_unavailable")
        self.assertEqual(self.count("menu_command"), 0)

    def test_unknown_grid_click_without_menu_episode_stays_readonly_after_runner_restart(self):
        self.runtime.input_result = {"isError": True, "structuredContent": {"input_sent": None, "error_code": "driver_timeout"}}
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 1, first)
        self.assertEqual(first["step_delivery"], {"step_index": 1, "state": "unknown"})
        self.runner = WorkflowRunner(self.temp.name)
        self.runtime.menu_visible = True
        second = self.run_task(resume=first["run_id"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["completed_steps"], 1, second)
        self.assertEqual(second["last_result"]["diagnostic"]["code"], "grid_menu_episode_changed")
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (1, 1, 0))

    def test_changed_app_lifetime_does_not_adopt_old_query_or_repeat_click(self):
        self.runtime.input_result = {"isError": True, "structuredContent": {"input_sent": None, "error_code": "driver_timeout"}}
        first = self.run_task()
        self.runtime.id = "replacement-app-session"
        self.runtime.menu_visible = True
        second = self.run_task(resume=first["run_id"])
        self.assertEqual(second["completed_steps"], 1, second)
        self.assertFalse(second["task_verified"])
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (1, 1, 0))

    def test_unknown_leaf_is_never_repeated_on_same_run(self):
        self.engine.command_result = {"task_verified": False, "input_dispatched": None,
                                      "diagnostic": {"code": "export_delivery_unknown"}}
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 3, first)
        self.assertEqual(first["step_delivery"], {"step_index": 3, "state": "unknown"})
        self.runner = WorkflowRunner(self.temp.name)
        second = self.run_task(resume=first["run_id"])
        self.assertFalse(second["task_verified"])
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (1, 1, 1))

    def test_resume_uses_frozen_recipe_when_saved_task_is_edited(self):
        self.engine.expand_result = {"task_verified": False, "input_dispatched": False,
                                     "diagnostic": {"code": "parent_disabled"}}
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 2, first)
        self.engine.expand_result = {"task_verified": True, "input_dispatched": True}
        changed = copy.deepcopy(self.task)
        changed["revision"] = 2
        changed["steps"][1]["grid_intent"]["export_scope"] = "selected_ranges"
        changed["steps"][0]["selector"]["name"] = "Do not click this replacement"
        second = self.run_task(resume=first["run_id"], task=changed)
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["recipe_hash"], first["recipe_hash"])
        self.assertEqual(second["completed_steps"], 3, second)
        self.assertEqual(second["last_result"]["diagnostic"]["code"], "grid_source_continuity_unverified")
        self.assertEqual(self.count("click"), 1)
        self.assertEqual(self.right_clicks(), 1)
        self.assertEqual(self.count("menu_command"), 0)

    def test_proven_grid_menu_same_run_restarts_without_repeating_but_unproven_source_blocks_leaf(self):
        self.engine.expand_result = {"task_verified": False, "input_dispatched": False,
                                     "diagnostic": {"code": "parent_disabled"}}
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 2)
        self.assertIn("menu_episode", first["grid_verification"]["1"])
        self.runner = WorkflowRunner(self.temp.name)
        self.engine.expand_result = {"task_verified": True, "input_dispatched": True}
        second = self.run_task(resume=first["run_id"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["completed_steps"], 3, second)
        self.assertEqual(second["last_result"]["diagnostic"]["code"], "grid_source_continuity_unverified")
        self.assertEqual(second["step_delivery"], {"step_index": 3, "state": "not_sent"})
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (1, 1, 0))

    def test_reused_menu_identity_from_other_grid_cannot_authorize_resumed_leaf(self):
        self.engine.expand_result = {"task_verified": False, "input_dispatched": False,
                                     "diagnostic": {"code": "parent_disabled"}}
        first = self.run_task()
        # ToolStrip identity, HWND, current grid values and menu path can all
        # be unchanged after unobserved closing/reopening for another grid.
        # A stable identity hash is deliberately not a visibility episode.
        self.runtime.menu_visible = False
        self.runtime.menu_visible = True
        self.runtime.scene["grid_selection"]["selected_items"] = [3]
        self.engine.expand_result = {"task_verified": True, "input_dispatched": True}
        second = self.run_task(resume=first["run_id"])
        self.assertEqual(second["completed_steps"], 3, second)
        self.assertEqual(second["last_result"]["diagnostic"]["code"], "grid_source_continuity_unverified")
        self.assertEqual(self.count("menu_command"), 0)
        self.assertEqual(self.right_clicks(), 1)

    def test_missing_old_query_receipt_is_not_manufactured_on_resume(self):
        self.runtime.scene["elements"][4]["name"] = "Processing"
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 1)
        self.edit_record(first["run_id"], lambda value: value["step_receipts"][0].pop("query_input_verified"))
        self.runtime.scene["elements"][4]["name"] = "Ready"
        second = self.run_task(resume=first["run_id"])
        self.assertFalse(second["task_verified"])
        self.assertEqual(second["completed_steps"], 1, second)
        self.assertEqual(self.right_clicks(), 0)
        self.assertEqual(self.count("click"), 1)

    def test_unknown_click_without_persisted_context_cannot_adopt_current_scope(self):
        self.runtime.input_result = {"isError": True, "structuredContent": {"input_sent": None, "error_code": "driver_timeout"}}
        first = self.run_task()
        self.edit_record(first["run_id"], lambda value: value.pop("grid_verification"))
        self.runtime.menu_visible = True
        second = self.run_task(resume=first["run_id"])
        self.assertEqual(second["completed_steps"], 1, second)
        self.assertEqual(self.right_clicks(), 1)
        self.assertEqual(self.count("menu_command"), 0)

    def test_readiness_must_be_query_change_condition_not_unrelated_toggle(self):
        task = recipe()
        task["steps"][0]["expect"] = [{"selector": {"name": "Unrelated toggle"}, "property": "selected", "equals": True, "require_change": True}]
        with self.assertRaises(WorkflowError): validate_recipe(task["steps"], {}, ["editor"])
        self.assertEqual(self.engine.calls, [])

    def test_arbitrary_row_commands_cannot_use_whole_sheet_cell_choice(self):
        for parent, child in (("Edit", "Delete"), ("Export to excel", "All sheet")):
            task = recipe()
            parent_path, child_path = menu_model.target(parent), menu_model.target(parent, child)
            task["steps"][1]["menu_target"] = copy.deepcopy(parent_path)
            task["steps"][2].update(menu_target=parent_path, child_menu_target=child_path)
            task["steps"][3]["menu_target"] = copy.deepcopy(child_path)
            with self.assertRaises(WorkflowError): validate_recipe(task["steps"], {}, ["editor"])
        self.assertEqual(self.engine.calls, [])

    def test_ten_model_runs_with_new_values_row_counts_and_single_cell_choices(self):
        original = copy.deepcopy(self.task)
        hashes = set()
        for iteration in range(10):
            self.runtime.scene["elements"][0]["patterns"]["Grid"]["row_count"] = 20 + iteration * 50
            self.runtime.scene["elements"][2]["name"] = "new-query-device-" + str(iteration)
            self.runtime.scene["elements"][3]["name"] = "reordered-device-" + str(10 - iteration)
            self.runtime.scene["grid_selection"]["selected_items"] = [2 + iteration % 2]
            result = self.run_task()
            self.assertEqual(result["completed_steps"], 4, result)
            self.assertFalse(result["task_verified"])
            self.assertTrue(result["checkpoint"]["human_review_required"])
            hashes.add(result["recipe_hash"])
        self.assertEqual(self.task, original)
        self.assertEqual(len(hashes), 1)
        self.assertEqual((self.count("click"), self.right_clicks(), self.count("menu_command")), (10, 10, 10))


if __name__ == "__main__": unittest.main()
