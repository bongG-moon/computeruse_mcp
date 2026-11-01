"""Synthetic provider-contract tests; these are not live SmartFX acceptance.

The separate native suite creates real disposable HWND/provider instances.
Neither suite proves corporate FarPoint selection or Excel output semantics.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import menu_accessibility as adapter
import semantic_menus as menus
from operations import OperationError
import test_semantic_menus as fixture


PARENT, LEAF = fixture.PARENT_NAME, fixture.LEAF_NAME


def native_source():
    target = {"pid": 42, "window_id": 19}
    fingerprint = {**target, "thread_id": 9, "class_name": "fixture"}
    rows = []
    for index, (name, role, parent, path) in enumerate([
        ("Root", "Menu", -1, []), (PARENT, "MenuItem", 0, [0]),
        ("Logical wrapper", "Pane", 1, [0, 0]), (LEAF, "MenuItem", 2, [0, 0, 0]),
    ]):
        signature = {"name": name, "role": role, "enabled": True, "visible": True,
                     "bounds": {"x": 10, "y": index * 25, "width": 150, "height": 25,
                                "coordinate_space": "desktop_physical"},
                     "identity": str(index), "identity_kind": "uia_runtime_id", "native_window_id": 19}
        ancestry = [] if parent == -1 else copy.deepcopy(rows[parent]["expected"]["path"])
        expected = {"target": fingerprint, "path": [*ancestry, signature], "observed_at_ms": 1000}
        rows.append({**copy.deepcopy(signature), "element_index": index, "index": index,
                     "parent_index": parent, "source": "uia_raw", "verification_only": True,
                     "authorizes_input": False, "identity_lifetime": "current_menu_observation_only",
                     "locator": {"source": "uia_raw", "path": path, "root_object_id": -4},
                     "expected": expected, "evidence": copy.deepcopy(expected), "supported_actions": []})
    return {**target, "source": "uia_raw", "status": "ok", "complete": True, "truncated": False,
            "read_only": True, "authorizes_input": False, "elements": rows, "provider_errors": []}


class IntentMenuObservationTests(unittest.TestCase):
    def resolve(self, rows, *path, purpose="presence", **kwargs):
        return menus.resolve_menu(fixture.observation(rows), fixture.target(*path), purpose=purpose, **kwargs)

    def test_dynamic_wrapper_and_reordered_items_preserve_semantic_path(self):
        initial = [fixture.row(0, PARENT, is_parent=True), fixture.row(1, LEAF, 0)]
        rebuilt = [fixture.row(0, "unrelated"), fixture.row(1, "wrapper", role="Pane"),
                   fixture.row(2, PARENT, 1, is_parent=True), fixture.row(3, "wrapper2", 2, role="Pane"),
                   fixture.row(4, "All sheet", 3), fixture.row(5, LEAF, 3)]
        self.assertEqual(self.resolve(initial, PARENT, LEAF, purpose="command")["item"]["index"], 1)
        current = self.resolve(rebuilt, PARENT, LEAF, purpose="command")
        self.assertEqual(current["item"]["index"], 5)
        self.assertEqual(current["menu_target"], fixture.target(PARENT, LEAF))
        self.assertFalse(current["authorizes_input"])

    def test_parent_ready_does_not_require_hover_image_or_grid_data(self):
        runtime = fixture.Runtime()
        result = menus.verify_menu_precondition(runtime, {"menu_target": fixture.target(PARENT)}, fixture.TARGET)
        self.assertTrue(result["task_verified"])
        self.assertFalse(result["business_result_verified"])
        self.assertEqual(result["metrics"]["image_comparisons"], 0)
        self.assertEqual(runtime.semantic_actions, [])
        self.assertEqual(result["menu_recognition"]["method"], "menu_semantic")

    def test_partial_uia_uses_independent_msaa_without_promoting_read_handle(self):
        actual = fixture.row(0, PARENT, is_parent=True)
        actual["locator"]["source"] = "direct_msaa"
        diagnostic = {}
        result = menus.resolve_menu(fixture.observation(
            uia=fixture.source(complete=False, truncated=True, status="partial"),
            msaa=fixture.source([actual])), fixture.target(PARENT), purpose="parent", diagnostics=diagnostic)
        self.assertEqual(result["source"], "direct_msaa")
        self.assertFalse(result["authorizes_input"])
        self.assertEqual(diagnostic["fallback_reason"], "source_incomplete")
        self.assertEqual(diagnostic["sources"]["direct_msaa"]["path_candidates"], 1)
        self.assertEqual(diagnostic["image_fallback"], "not_attempted")

    def test_unknown_parent_capability_is_distinct_from_missing_name(self):
        unknown = fixture.row(0, PARENT, has_children=None, has_popup=None, supported_actions=[])
        with self.assertRaises(OperationError) as caught:
            self.resolve([unknown], PARENT, purpose="parent")
        self.assertEqual(caught.exception.code, "menu_parent_capability_unavailable")
        detail = caught.exception.menu_recognition["sources"]["uia_raw"]
        self.assertEqual(detail["path_candidates"], 1)
        self.assertEqual(detail["outcome"], "parent_capability_unknown")

    def test_unknown_leaf_is_not_assumed_command(self):
        unknown = fixture.row(0, LEAF, has_children=None, has_popup=None, supported_actions=[])
        with self.assertRaises(OperationError) as caught:
            self.resolve([unknown], LEAF, purpose="command")
        self.assertEqual(caught.exception.code, "menu_leaf_capability_unavailable")

    def test_disabled_or_duplicate_does_not_fall_through_to_other_provider(self):
        for rows, reason in (([fixture.row(0, PARENT, enabled=False)], "menu_item_disabled"),
                             ([fixture.row(0, PARENT), fixture.row(1, PARENT)], "menu_item_ambiguous")):
            with self.subTest(reason=reason), self.assertRaises(OperationError) as caught:
                menus.resolve_menu(fixture.observation(rows, msaa=fixture.source([
                    fixture.row(0, PARENT, is_parent=True)])), fixture.target(PARENT))
            self.assertEqual(caught.exception.code, reason)
            self.assertNotIn("direct_msaa", caught.exception.menu_recognition["sources"])

    def test_other_branch_and_all_sheet_are_not_requested_child(self):
        rows = [fixture.row(0, PARENT, is_parent=True), fixture.row(1, "All sheet", 0),
                fixture.row(2, "other parent", is_parent=True), fixture.row(3, LEAF, 2)]
        with self.assertRaises(OperationError) as caught:
            self.resolve(rows, PARENT, LEAF, purpose="command")
        self.assertEqual(caught.exception.code, "menu_item_not_found")

    def test_foreign_observation_window_rejected_before_resolution(self):
        runtime = fixture.Runtime()
        with mock.patch.object(runtime, "observe_menu", return_value=fixture.observation(
                [fixture.row(0, PARENT, is_parent=True)], actual={"pid": 99, "window_id": 88})):
            result = menus.verify_menu_precondition(runtime, {"menu_target": fixture.target(PARENT)}, fixture.TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_observation_invalid")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.semantic_actions, [])

    def test_current_left_opening_bounds_are_used_as_evidence(self):
        current = fixture.row(0, LEAF, bounds={"x": -150, "y": 40, "width": 120, "height": 30})
        result = self.resolve([current], LEAF, purpose="command")
        self.assertEqual(result["item"]["bounds"], current["bounds"])
        self.assertFalse(result["authorizes_input"])

    def test_child_absence_never_dispatches_final_command(self):
        runtime = fixture.Runtime()
        runtime.show_child = False
        result = menus.execute_semantic_expansion(runtime, fixture.step(), fixture.TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(runtime.semantic_actions), 1)
        self.assertEqual(runtime.semantic_actions[0][2], "expand")
        self.assertEqual(result["menu_expansion"]["child_command_delivery"], "not_sent")
        resumed = menus.execute_semantic_expansion(runtime, fixture.step(), fixture.TARGET, verify_only=True)
        self.assertFalse(resumed["task_verified"])
        self.assertEqual(len(runtime.semantic_actions), 1)

    def test_missing_providers_explain_local_ocr_is_not_implemented(self):
        diagnostic = {}
        with self.assertRaises(OperationError) as caught:
            menus.resolve_menu(fixture.observation(uia=fixture.source(complete=False, status="timeout")),
                               fixture.target(PARENT), diagnostics=diagnostic)
        self.assertEqual(diagnostic, caught.exception.menu_recognition)
        self.assertEqual(diagnostic["local_ocr"], "not_implemented")
        self.assertEqual(diagnostic["image_fallback"], "not_attempted")
        self.assertNotIn(PARENT, json.dumps(diagnostic))
        self.assertNotIn("locator", json.dumps(diagnostic))

    def test_diagnostics_are_per_call_and_do_not_enter_guard_identity(self):
        diagnostic = {}
        row = fixture.row(0, PARENT, is_parent=True)
        one = self.resolve([row], PARENT, purpose="parent", diagnostics=diagnostic)
        first = copy.deepcopy(diagnostic)
        two = self.resolve([row, fixture.row(1, "new command")], PARENT, purpose="parent", diagnostics=diagnostic)
        self.assertEqual(one, two)
        self.assertNotEqual(first["sources"]["uia_raw"]["menu_items"], diagnostic["sources"]["uia_raw"]["menu_items"])
        self.assertNotIn("menu_recognition", two)

    def test_complete_with_provider_errors_cannot_claim_menu_ready(self):
        invalid = fixture.source([fixture.row(0, PARENT, is_parent=True)])
        invalid["provider_errors"] = [{"code": "timeout"}]
        with self.assertRaises(OperationError) as caught:
            menus.resolve_menu(fixture.observation(uia=invalid), fixture.target(PARENT))
        self.assertEqual(caught.exception.code, "menu_observation_invalid")


class MenuTransportContractTests(unittest.TestCase):
    def test_actual_locator_and_evidence_chain_is_accepted_without_authorization(self):
        value = native_source()
        adapter._validate_source(value, 500)
        expected = copy.deepcopy(value["elements"][-1]["expected"])
        adapter._normalize_source(value)
        self.assertEqual(value["elements"][-1]["expected"], expected)
        self.assertFalse(value["elements"][-1]["authorizes_input"])

    def test_presentation_alias_and_locator_cannot_point_to_another_target(self):
        def index(row): row["index"] = 77
        def source(row): row["locator"]["source"] = "direct_msaa"
        def alias(row): row["evidence"]["target"]["window_id"] = 77
        def hwnd(row):
            row["expected"]["target"]["window_id"] = 77
            row["evidence"] = copy.deepcopy(row["expected"])
        def path(row): row["locator"]["path"] = [9, 0, 0]
        def signature(row):
            row["expected"]["path"][-1]["name"] = "All sheet"
            row["evidence"] = copy.deepcopy(row["expected"])
        for change in (index, source, alias, hwnd, path, signature):
            with self.subTest(change=change.__name__):
                value = native_source()
                change(value["elements"][-1])
                with self.assertRaises(OperationError) as caught:
                    adapter._validate_source(value, 500)
                self.assertEqual(caught.exception.code, "menu_accessibility_invalid_response")


class IntentLocalProjectionTests(unittest.TestCase):
    SECRET = "BUSINESS-SENTINEL-MENU-CELL"

    def evidence(self):
        menu = {"method": "menu_semantic", "purpose": "parent", "selected_source": "direct_msaa",
                "fallback_reason": "source_incomplete", "image_fallback": "not_attempted",
                "local_ocr": "not_implemented", "outcome": "matched", "name": self.SECRET,
                "sources": {"uia_raw": {"status": "partial", "complete": False, "truncated": True,
                    "menu_items": 0, "path_candidates": 0, "visible_candidates": 0, "outcome": "source_incomplete"},
                    "direct_msaa": {"status": "ok", "complete": True, "truncated": False,
                    "menu_items": 12, "path_candidates": 1, "visible_candidates": 1, "outcome": "matched",
                    "elements": [{"name": self.SECRET}]}}}
        return {"status": "needs_review", "last_result": {"menu_recognition": menu,
                "recognition": "grid_semantic", "grid_context_verified": True, "query_verified": True,
                "data_values_compared": False, "business_result_verified": False,
                "context": {"export_scope": "whole_active_sheet", "active_sheet": self.SECRET,
                            "selection_ranges": [{"row": 77, "column": 88}]},
                "grid": {"name": self.SECRET}, "cell": {"value": self.SECRET}, "path": self.SECRET}}

    def test_local_projection_retains_only_fixed_technical_states(self):
        from local_result_details import project_local_diagnostics
        from local_delivery import receipt, filter_response
        raw = self.evidence()
        clean = project_local_diagnostics(raw)
        self.assertEqual(clean["menu_recognition"]["selected_source"], "direct_msaa")
        self.assertEqual(clean["menu_recognition"]["fallback_reason"], "source_incomplete")
        self.assertFalse(clean["grid_recognition"]["data_values_compared"])
        self.assertTrue(clean["grid_recognition"]["query_verified"])
        self.assertEqual(clean["grid_recognition"]["export_scope"], "whole_active_sheet")
        encoded = json.dumps(clean)
        self.assertNotIn(self.SECRET, encoded)
        for name in ("selection_ranges", "elements", "active_sheet", "bounds", "locator"):
            self.assertNotIn('"' + name + '"', encoded)
        self.assertLess(len(encoded), 2500)
        for delivered in (receipt({**raw, **clean}), filter_response({"structuredContent": {**raw, **clean}})):
            self.assertNotIn("menu_recognition", json.dumps(delivered))
            self.assertNotIn("grid_recognition", json.dumps(delivered))
            self.assertNotIn(self.SECRET, json.dumps(delivered))

    def test_unknown_fields_enums_and_unbounded_counts_are_dropped(self):
        from local_result_details import project_local_diagnostics
        raw = self.evidence()
        menu = raw["last_result"]["menu_recognition"]
        menu.update(selected_source=self.SECRET, fallback_reason=self.SECRET, outcome=self.SECRET)
        menu["sources"]["direct_msaa"].update(menu_items=501, path_candidates=True,
                                             visible_candidates=float("nan"), outcome=self.SECRET)
        menu["sources"][self.SECRET] = {"status": self.SECRET}
        raw["last_result"].update(grid_context_verified=1, query_verified=self.SECRET,
                                  fallback_reason=self.SECRET, export_scope=self.SECRET)
        clean = project_local_diagnostics(raw)
        encoded = json.dumps(clean, allow_nan=False)
        self.assertNotIn(self.SECRET, encoded)
        self.assertNotIn("menu_items", clean["menu_recognition"]["sources"]["direct_msaa"])
        self.assertNotIn("grid_context_verified", clean["grid_recognition"])
        self.assertNotIn("query_verified", clean["grid_recognition"])
        self.assertNotIn("export_scope", clean["grid_recognition"])

    def test_runtime_grid_context_wrapper_projects_only_scope_and_failure_code(self):
        from local_result_details import project_local_diagnostics
        raw = {"recognition": "grid_semantic", "grid_context": {"export_scope": "selected_ranges",
               "selection_ranges": [{"row": 12, "column": 9}], "active_sheet": self.SECRET},
               "diagnostic": {"code": "grid_cell_changed", "message": self.SECRET}}
        clean = project_local_diagnostics(raw)
        self.assertEqual(clean["grid_recognition"]["export_scope"], "selected_ranges")
        self.assertEqual(clean["grid_recognition"]["outcome"], "grid_cell_changed")
        self.assertNotIn(self.SECRET, json.dumps(clean))
        self.assertNotIn("selection_ranges", json.dumps(clean))

    def test_projection_never_traverses_business_trees_or_unbounded_nesting(self):
        from local_result_details import project_local_diagnostics
        evidence = self.evidence()["last_result"]
        self.assertEqual(project_local_diagnostics({"unapproved": evidence}), {})
        self.assertEqual(project_local_diagnostics({"elements": [evidence]}), {})
        cycle = {}; cycle["last_result"] = cycle
        self.assertEqual(project_local_diagnostics(cycle), {})
        nested = evidence
        for _ in range(12): nested = {"cause": nested}
        self.assertEqual(project_local_diagnostics(nested), {})

    def test_summary_distinguishes_readiness_from_business_output(self):
        from local_result_details import project_local_diagnostics, local_recognition_summary
        summary = local_recognition_summary(project_local_diagnostics(self.evidence()))
        self.assertIn("MSAA", summary)
        self.assertIn("source_incomplete", summary)
        self.assertIn("이번 조회 완료: 확인됨", summary)
        self.assertIn("출력 파일 생성 완료를 뜻하지 않습니다", summary)
        self.assertNotIn(self.SECRET, summary)
        self.assertLessEqual(len(summary), 1800)

    def test_local_result_form_receives_summary_but_public_status_does_not(self):
        from local_runs import LocalRuns
        from local_delivery import receipt
        with tempfile.TemporaryDirectory() as folder:
            api = LocalRuns(SimpleNamespace(config={"state_dir": folder}))
            raw = self.evidence()
            job = {"id": "44" * 16, "local_result": raw, "result": receipt(raw),
                   "done": threading.Event(), "cancel": threading.Event()}
            job["done"].set(); api.jobs[job["id"]] = job
            child = mock.Mock(pid=42); child.poll.return_value = None
            with mock.patch("local_runs.subprocess.Popen", return_value=child) as launch:
                api._show_result(job)
            request = json.loads(Path(launch.call_args.args[0][2]).read_text(encoding="utf-8"))
            self.assertIn("menu_recognition", request["result"])
            self.assertIn("grid_recognition", request["result"])
            self.assertIn("이번 조회 완료: 확인됨", request["result"]["summary"])
            self.assertNotIn(self.SECRET, json.dumps(request))
            status = api.status(job["id"])
            self.assertNotIn("summary", status)
            self.assertNotIn("menu_recognition", status)
            self.assertNotIn("grid_recognition", status)


class MenuPurposeSchemaTests(unittest.TestCase):
    def checkpoint(self, **extra):
        return {"operation": "checkpoint", "review_mode": "menu_semantic", "review_origin": "recording_generated",
                "opened_from": "main", "menu_target": fixture.target(PARENT), "message": "현재 메뉴 확인", **extra}

    def test_parent_command_and_legacy_unspecified_are_distinct_valid_contracts(self):
        from process_steps import validate_process_step
        for purpose in ("parent", "command"):
            step = self.checkpoint(menu_purpose=purpose)
            self.assertEqual(validate_process_step(step), step)
        legacy = self.checkpoint()
        self.assertEqual(validate_process_step(legacy), legacy)
        self.assertNotIn("menu_purpose", legacy)

    def test_purpose_cannot_change_human_or_image_checkpoint_policy(self):
        from process_steps import validate_process_step
        for purpose in (False, None, [], "invoke", "auto"):
            with self.subTest(purpose=purpose), self.assertRaises(OperationError):
                validate_process_step(self.checkpoint(menu_purpose=purpose))
        for mode in ("human", "visual", "popup_image"):
            with self.subTest(mode=mode), self.assertRaises(OperationError):
                validate_process_step({"operation": "checkpoint", "message": "check", "review_mode": mode,
                                       "menu_purpose": "command"})


if __name__ == "__main__":
    unittest.main()
