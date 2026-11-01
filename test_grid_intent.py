"""Contract/provider-model cases; these are not SmartMMDM acceptance runs."""
import copy
import unittest

from grid_intent import (FORMAT, compare_grid_context, inspect_grid_intent,
                         resolve_grid_intent, validate_grid_intent)
from operations import OperationError


def contract(scope="whole_active_sheet"):
    return {"format": FORMAT, "grid_selector": {"automation_id": "results", "role": "DataGrid"},
            "export_scope": scope, "active_sheet": {"selector": {"automation_id": "sheet", "role": "TabItem"},
                "property": "selected", "equals": True},
            "ready_when": [{"selector": {"automation_id": "status"}, "property": "name", "equals": "Ready"}],
            "view_conditions": [{"selector": {"automation_id": "filter"}, "property": "value", "equals": "All"}],
            "allow_empty": False}


def snapshot():
    def item(index, parent, role, aid, x, y, width, height, **extra):
        return {"element_index": index, "parent_index": parent, "role": role, "automation_id": aid,
                "name": aid, "enabled": True, "offscreen": False,
                "bounds": {"x": x, "y": y, "width": width, "height": height},
                "coordinate_space": "desktop_physical", "verification_only": True, **extra}
    values = [item(0, -1, "DataGrid", "results", 100, 200, 500, 400,
                  patterns={"Grid": {"status": "supported", "row_count": 200, "column_count": 5}}),
              item(1, 0, "TabItem", "sheet", 100, 550, 80, 25, selected=True),
              item(2, 0, "Cell", "", 100, 250, 100, 25, name="DEV-001",
                  grid_item={"status": "supported", "containing_grid": 0, "row": 0, "column": 0, "row_span": 1, "column_span": 1}),
              item(3, 0, "Cell", "", 100, 275, 100, 25, name="DEV-002",
                  grid_item={"status": "supported", "containing_grid": 0, "row": 1, "column": 0, "row_span": 1, "column_span": 1}),
              item(4, 0, "Text", "status", 100, 580, 80, 20, name="Ready"),
              item(5, 0, "Edit", "filter", 200, 580, 80, 20, value="All")]
    return {"target": {"pid": 10, "window_id": 20}, "elements": values, "truncated": False,
            "grid_selection": {"status": "supported", "complete": True, "grid_element_index": 0,
                               "selected_items": [2]}}


def proof(method="require_change"):
    return {"run_id": "run", "query_step_id": "query", "target": {"pid": 10, "window_id": 20},
            "input_dispatched": True, "freshness_verified": True, "failed": False,
            "verification_method": method, "completion_verified": True, "require_change_verified": True,
            "busy_observed": True, "ready_observed": True}


class GridIntentTests(unittest.TestCase):
    def rejected(self, code, scene=None, intent=None):
        with self.assertRaises(OperationError) as caught:
            inspect_grid_intent(scene or snapshot(), intent or contract())
        self.assertEqual(caught.exception.code, code)

    def test_current_cell_values_row_count_order_and_geometry_are_not_identity(self):
        before = inspect_grid_intent(snapshot(), contract())
        after = snapshot()
        after["elements"][0]["patterns"]["Grid"]["row_count"] = 12
        for element in after["elements"]:
            element["bounds"]["x"] = element["bounds"]["x"] * 1.5 - 900
            element["bounds"]["y"] *= 1.5
            element["bounds"]["width"] *= 1.5
            element["bounds"]["height"] *= 1.5
        after["elements"][2]["name"], after["elements"][3]["name"] = "NEW-Z", "NEW-A"
        after["elements"][2]["grid_item"]["row"] = 4
        after["elements"][3]["grid_item"]["row"] = 2
        result = inspect_grid_intent(after, contract())
        self.assertTrue(compare_grid_context(before["context"], result["context"]))
        self.assertFalse(result["data_values_compared"])
        self.assertFalse(result["authorizes_input"])

    def test_other_current_single_cell_preserves_whole_sheet_scope(self):
        one = inspect_grid_intent(snapshot(), contract())
        current = snapshot(); current["grid_selection"]["selected_items"] = [3]
        two = inspect_grid_intent(current, contract())
        self.assertEqual(two["cell"]["element_index"], 3)
        self.assertTrue(compare_grid_context(one["context"], two["context"]))

    def test_no_selection_is_not_inferred_from_missing_provider(self):
        for value in ({"status": "unsupported"}, {"status": "unknown"}, None):
            current = snapshot(); current["grid_selection"] = value
            self.rejected("grid_selection_unavailable", current)
        current = snapshot(); current["grid_selection"]["selected_items"] = []
        self.assertEqual(inspect_grid_intent(current, contract())["cell"]["element_index"], 2)

    def test_multiple_or_wide_selection_never_becomes_whole_sheet(self):
        current = snapshot(); current["grid_selection"]["selected_items"] = [2, 3]
        self.rejected("grid_selection_scope_mismatch", current)
        current = snapshot(); current["elements"][2]["grid_item"]["row_span"] = 2
        self.rejected("grid_selection_scope_mismatch", current)

    def test_selected_ranges_are_preserved_exactly_and_not_cleared(self):
        current = snapshot(); current["grid_selection"]["selected_items"] = [2, 3]
        original = copy.deepcopy(current)
        one = inspect_grid_intent(current, contract("selected_ranges"))
        self.assertEqual(current, original)
        current["elements"][3]["grid_item"]["row"] = 2
        two = inspect_grid_intent(current, contract("selected_ranges"))
        with self.assertRaises(OperationError) as caught:
            compare_grid_context(one["context"], two["context"])
        self.assertEqual(caught.exception.code, "grid_selection_changed")

    def test_single_cell_cannot_claim_selected_range_export_for_active_sheet_command(self):
        self.rejected("grid_selection_scope_mismatch", snapshot(), contract("selected_ranges"))
        current = snapshot(); current["elements"][2]["grid_item"]["column_span"] = 2
        result = inspect_grid_intent(current, contract("selected_ranges"))
        self.assertEqual(result["context"]["selection_ranges"], [{"row": 0, "column": 0, "row_span": 1, "column_span": 2}])

    def test_active_sheet_filter_and_hidden_state_changes_block(self):
        for index, key, value, code in ((1, "selected", False, "grid_active_sheet_unavailable"),
                                      (5, "value", "Filtered", "grid_view_state_unavailable")):
            current = snapshot(); current["elements"][index][key] = value
            self.rejected(code, current)
        intent = contract(); intent["view_conditions"].append({"selector": {"automation_id": "hiddenColumns"}, "property": "value", "equals": "none"})
        self.rejected("grid_view_state_unavailable", intent=intent)

    def test_headers_sheet_tabs_and_scrollbars_are_not_data_cells(self):
        for role in ("HeaderItem", "TabItem", "ScrollBar", "Button"):
            current = snapshot(); current["elements"][2]["role"] = role
            self.rejected("grid_selection_unavailable", current)

    def test_current_cell_must_belong_to_selected_grid(self):
        current = snapshot(); current["elements"][2]["grid_item"]["containing_grid"] = 99
        self.rejected("grid_selection_unavailable", current)
        current = snapshot(); current["elements"][2]["parent_index"] = -1
        self.rejected("grid_selection_unavailable", current)

    def test_duplicate_grid_is_not_first_candidate(self):
        current = snapshot(); second = copy.deepcopy(current["elements"][0]); second["element_index"] = 99
        current["elements"].append(second)
        with self.assertRaises(OperationError) as caught: inspect_grid_intent(current, contract())
        self.assertEqual(caught.exception.code, "ambiguous_selector")
        second["automation_id"] = "detailResults"
        self.assertEqual(inspect_grid_intent(current, contract())["grid"]["element_index"], 0)

    def test_sheet_of_other_grid_is_not_reused(self):
        current = snapshot(); current["elements"][1]["parent_index"] = -1
        self.rejected("grid_active_sheet_mismatch", current)

    def test_incomplete_or_unmapped_selection_cannot_claim_empty(self):
        for change in ({"complete": False}, {"selected_items": [99]}, {"selected_items": [2, 2]}, {"grid_element_index": 99}):
            current = snapshot(); current["grid_selection"].update(change)
            self.rejected("grid_selection_unavailable", current)
        current = snapshot(); current["truncated"] = True
        self.rejected("grid_observation_incomplete", current)

    def test_hidden_disabled_or_occluded_geometry_cells_do_not_use_grid_center(self):
        for key, value in (("offscreen", True), ("enabled", False), ("coordinate_space", "logical")):
            current = snapshot(); current["elements"][2][key] = value
            self.rejected("grid_data_cell_unavailable", current)
        current = snapshot(); current["elements"][2]["bounds"]["x"] = 10
        self.rejected("grid_data_cell_unavailable", current)

    def test_empty_result_has_separate_policy_and_no_header_fallback(self):
        current = snapshot(); current["elements"] = [e for e in current["elements"] if e["role"] != "Cell"]
        current["elements"][0]["patterns"]["Grid"]["row_count"] = 0
        current["grid_selection"]["selected_items"] = []
        self.rejected("grid_empty_forbidden", current)
        intent = contract(); intent["allow_empty"] = True
        self.rejected("grid_empty_no_data_cell", current, intent)

    def test_query_old_visible_results_are_insufficient(self):
        for value in (None, {}, {**proof(), "freshness_verified": False}, {**proof(), "verification_method": "visible_results"},
                      {**proof(), "run_id": "old"}, {**proof(), "target": {"pid": 10, "window_id": 21}},
                      {**proof(), "input_dispatched": None}, {**proof(), "failed": True}):
            with self.assertRaises(OperationError) as caught:
                resolve_grid_intent(snapshot(), contract(), query_evidence=value, run_id="run")
            self.assertEqual(caught.exception.code, "grid_query_freshness_unverified")

    def test_fresh_query_accepts_observed_transition_or_verified_change(self):
        for method in ("busy_to_ready", "require_change"):
            result = resolve_grid_intent(snapshot(), contract(), query_evidence=proof(method), run_id="run")
            self.assertTrue(result["query_verified"])
            self.assertFalse(result["authorizes_input"])
        current = snapshot(); current["elements"][4]["name"] = "Processing"
        with self.assertRaises(OperationError) as caught:
            resolve_grid_intent(current, contract(), query_evidence=proof(), run_id="run")
        self.assertEqual(caught.exception.code, "grid_not_ready")

    def test_authoring_preview_is_not_execution_evidence(self):
        self.assertFalse(inspect_grid_intent(snapshot(), contract())["query_verified"])
        with self.assertRaises(OperationError): resolve_grid_intent(snapshot(), contract())

    def test_row_edit_delete_or_unobserved_internal_name_is_not_inferred(self):
        for scope in ("delete_row", "edit_cell", "current_grid", [], None):
            intent = contract(); intent["export_scope"] = scope
            with self.assertRaises(OperationError): validate_grid_intent(intent)
        intent = contract(); intent["grid_selector"]["automation_id"] = "spdDevice"
        with self.assertRaises(OperationError): inspect_grid_intent(snapshot(), intent)

    def test_synthetic_provider_nodes_do_not_authorize_semantic_grid(self):
        current = snapshot(); current["elements"][0]["synthetic_ancestor"] = True
        self.rejected("grid_target_unavailable", current)

    def test_scope_conditions_are_explicit_and_never_data_defaults(self):
        intent = contract(); intent["view_conditions"] = []
        result = inspect_grid_intent(snapshot(), intent)
        self.assertEqual(result["context"]["scope"], "selection_and_declared_view_conditions")
        self.assertEqual(result["context"]["view_states"], [])
        intent = contract(); intent["data_values"] = ["DEV-001"]
        with self.assertRaises(OperationError): validate_grid_intent(intent)

    def test_unknown_grid_counts_or_invalid_cell_ranges_fail(self):
        current = snapshot(); current["elements"][0]["patterns"]["Grid"] = {"status": "unknown"}
        self.rejected("grid_state_unavailable", current)
        current = snapshot(); current["elements"][2]["grid_item"]["column"] = 5
        self.rejected("grid_cell_state_unavailable", current)

    def test_custom_or_pane_requires_actual_grid_selection_and_griditem(self):
        for role in ("Custom", "Pane"):
            current, intent = snapshot(), contract()
            current["elements"][0]["role"] = role; intent["grid_selector"]["role"] = role
            self.assertEqual(inspect_grid_intent(current, intent)["recognition"], "grid_semantic")
            current["elements"][0]["patterns"]["Grid"] = {"status": "unsupported"}
            self.rejected("grid_state_unavailable", current, intent)
            current = snapshot(); current["elements"][0]["role"] = role
            current["elements"][2].pop("grid_item")
            self.rejected("grid_selection_unavailable", current, intent)

    def test_unselected_tab_name_cannot_stand_for_active_sheet(self):
        current, intent = snapshot(), contract()
        current["elements"][1]["selected"] = False
        intent["active_sheet"].update(property="name", equals="sheet")
        self.rejected("grid_active_sheet_unavailable", current, intent)


    def test_selected_data_cell_or_static_label_is_not_an_active_sheet(self):
        for role, prop, expected in (("Cell", "selected", True), ("Text", "name", "sheet"),
                                     ("Edit", "value", "All"), ("Cell", "value", "Results")):
            with self.subTest(role=role):
                current = snapshot()
                current["elements"][1]["role"] = role
                current["elements"][1][prop] = expected
                intent = contract()
                intent["active_sheet"].update(selector={"automation_id": "sheet", "role": role}, property=prop, equals=expected)
                self.rejected("invalid_grid_intent", current, intent)


if __name__ == "__main__": unittest.main()
