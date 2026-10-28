"""Observed row keys and dynamic editors, without fabricated Driver abilities."""
import copy
import tempfile
import unittest
from unittest import mock

from operations import (OperationError, Operations, SELECTOR_SCHEMA, _unique,
                        supports_set_value, validate_selector, verification_step)
from test_operations import Runtime, TARGET, element, snapshot


CELL = {"name": "Family", "role": "Edit", "within": {"automation_id": "devices"},
        "row": {"key": {"name": "Device", "role": "Text"}, "property": "value", "equals": "D200"}}


def grid(*, chosen="before", offset=0, reverse=False):
    rows = [("D100", "other"), ("D200", chosen)]
    if reverse:
        rows.reverse()
    elements = [element(offset, name="Device results", role="DataGrid", automation_id="devices")]
    for n, (key, value) in enumerate(rows):
        row = offset + 10 + n * 10
        elements += [element(row, name="", role="DataItem", parent_index=offset),
                     element(row + 1, name="Device", role="Text", value=key, parent_index=row),
                     element(row + 2, name="Family", value=value, parent_index=row)]
    return snapshot(*elements)


class GridTargetingTests(unittest.TestCase):
    def test_schema_exposes_row_key_not_runtime_or_position(self):
        self.assertEqual(set(SELECTOR_SCHEMA["properties"]["row"]["properties"]), {"key", "property", "equals"})
        self.assertEqual(validate_selector(CELL), CELL)
        for invalid in ({**CELL, "row": {"row_index": 1}},
                        {k: v for k, v in CELL.items() if k != "within"},
                        {**CELL, "row": {**CELL["row"], "property": []}},
                        {**CELL, "row": {**CELL["row"], "key": {"runtime_id": "old"}}},
                        {**CELL, "row": {**CELL["row"], "key": {"role": "Text"}}},
                        {**CELL, "row": {**CELL["row"], "key": {"name": "Device", "within": {"name": "other"}}}}):
            with self.subTest(invalid=invalid), self.assertRaises(OperationError):
                validate_selector(invalid)

    def test_row_key_selects_same_business_row_after_reorder_and_new_tokens(self):
        self.assertEqual(_unique(grid(), CELL)["element_index"], 22)
        self.assertEqual(_unique(grid(offset=100, reverse=True), CELL)["element_index"], 112)

    def test_duplicate_key_is_rejected_before_input(self):
        state = grid()
        state["elements"][2]["value"] = "D200"
        runtime = Runtime([state])
        result = Operations(runtime).execute({"operation": "set_value", "selector": CELL, "value": "after"}, TARGET)
        self.assertEqual(result["diagnostic"]["code"], "ambiguous_row_key")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state"])

    def test_duplicate_column_is_rejected(self):
        state = grid()
        state["elements"].append(element(23, name="Family", parent_index=20))
        with self.assertRaises(OperationError) as found:
            _unique(state, CELL)
        self.assertEqual(found.exception.code, "ambiguous_selector")

    def test_missing_header_does_not_use_column_position(self):
        state = grid()
        state["elements"][-1]["label"] = ""
        with self.assertRaises(OperationError) as found:
            _unique(state, CELL)
        self.assertEqual(found.exception.code, "selector_not_found")

    def test_missing_row_does_not_use_screen_geometry(self):
        state = grid()
        state["elements"][-3]["role"] = "Pane"
        with self.assertRaises(OperationError) as found:
            _unique(state, CELL)
        self.assertEqual(found.exception.code, "grid_row_structure_unavailable")

    def test_duplicate_indices_do_not_invent_row_parent(self):
        state = grid()
        state["elements"][-3]["element_index"] = 10
        with self.assertRaises(OperationError) as found:
            _unique(state, CELL)
        self.assertEqual(found.exception.code, "ambiguous_row_structure")

    def test_partial_tree_rejected(self):
        for flag in ("truncated", "max_depth_reached", "max_elements_reached"):
            state = grid()
            state[flag] = True
            with self.subTest(flag=flag), self.assertRaises(OperationError) as found:
                _unique(state, CELL)
            self.assertEqual(found.exception.code, "incomplete_observation")

    def test_protected_or_synthetic_key_is_not_business_value_evidence(self):
        for flag in ("protected", "is_password", "synthetic_ancestor"):
            state = grid()
            state["elements"][-2][flag] = True
            with self.subTest(flag=flag), self.assertRaises(OperationError) as found:
                _unique(state, CELL)
            self.assertEqual(found.exception.code, "selector_not_found")

    def test_full_observation_expands_once_before_resolving_key(self):
        partial = grid()
        partial["truncated"] = True
        runtime = Runtime([partial, grid(), grid(chosen="after", offset=100, reverse=True)])
        result = Operations(runtime).execute({"operation": "set_value", "selector": CELL,
            "value": "after", "verification_timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual([name for name, _ in runtime.calls],
                         ["get_window_state", "get_window_state", "set_value", "get_window_state"])

    def test_row_key_persists_in_postconditions_and_resume_is_read_only(self):
        step = {"operation": "set_value", "selector": CELL, "value": "after", "verification_timeout_ms": 0}
        check = verification_step(step)
        self.assertEqual(check["expect"][0]["selector"], CELL)
        runtime = Runtime([grid(chosen="after", offset=200, reverse=True)])
        result = Operations(runtime).execute(check, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_row_selector_does_not_call_unsupported_scoped_helper(self):
        runtime = Runtime([grid(), grid(chosen="after")])
        runtime.observe_controls = mock.Mock(side_effect=AssertionError("helper cannot represent row keys"))
        result = Operations(runtime).execute({"operation": "set_value", "selector": CELL,
            "value": "after", "verification_timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])
        runtime.observe_controls.assert_not_called()

    def test_ten_fresh_editor_instances_keep_two_reads_and_one_input(self):
        for number in range(10):
            before = grid(offset=number * 100, reverse=number % 2 == 0)
            after = grid(chosen="after", offset=number * 100 + 50, reverse=number % 2 != 0)
            runtime = Runtime([before, after])
            result = Operations(runtime).execute({"operation": "set_value", "selector": CELL,
                "value": "after", "verification_timeout_ms": 0}, TARGET)
            with self.subTest(number=number):
                self.assertTrue(result["task_verified"])
                self.assertEqual([name for name, _ in runtime.calls],
                                 ["get_window_state", "set_value", "get_window_state"])
                args = next(args for name, args in runtime.calls if name == "set_value")
                self.assertEqual(args["element_token"], _unique(before, CELL)["element_token"])


class DynamicValueTests(unittest.TestCase):
    def editable(self, **extra):
        return element(name="Property", role="Custom", is_read_only=False,
                       patterns={"Value": {"status": "supported"}}, **extra)

    def test_explicit_writable_value_works_for_custom_editor_and_verifies(self):
        runtime = Runtime([snapshot(self.editable()), snapshot(self.editable(value="after"))])
        result = Operations(runtime).execute({"operation": "set_value", "selector": {"name": "Property"},
            "value": "after", "verification_timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(sum(name == "set_value" for name, _ in runtime.calls), 1)

    def test_unknown_pattern_is_not_promoted_to_write_capability(self):
        for pattern in (None, {}, {"Value": {"status": "unknown"}}, {"Value": {"status": "unsupported"}}, {"Value": True}):
            data = self.editable()
            data["patterns"] = pattern
            self.assertFalse(supports_set_value(data))

    def test_readonly_unknown_or_contradictory_evidence_is_rejected(self):
        for readonly in (None, True, "false", 0):
            data = self.editable()
            data["is_read_only"] = readonly
            self.assertFalse(supports_set_value(data))
        data = self.editable()
        data["patterns"]["Value"]["read_only"] = True
        self.assertFalse(supports_set_value(data))

    def test_actions_alone_do_not_prove_writable_dynamic_field(self):
        data = self.editable()
        data.pop("is_read_only")
        self.assertFalse(supports_set_value(data))
        data = self.editable()
        data["actions"] = ["invoke"]
        self.assertFalse(supports_set_value(data))

    def test_selection_roles_are_not_reclassified_as_value_editors(self):
        for role in ("ComboBox", "ListItem", "TreeItem", "TabItem", "Button", "CheckBox", "Password", "Hyperlink"):
            data = self.editable()
            data["role"] = role
            self.assertFalse(supports_set_value(data), role)

    def test_readonly_diagnostic_snapshot_cannot_authorize_input(self):
        runtime = Runtime([snapshot(self.editable(), read_only=True)])
        result = Operations(runtime).execute({"operation": "set_value", "selector": {"name": "Property"},
            "value": "after", "verification_timeout_ms": 0}, TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "read_only_observation")

    def test_drawn_cell_is_not_assumed_to_be_a_combobox(self):
        data = self.editable()
        data["role"] = "DataItem"
        runtime = Runtime([snapshot(data)])
        result = Operations(runtime).execute({"operation": "select_option", "selector": {"name": "Property"},
            "value": "after", "verification_timeout_ms": 0}, TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "unsupported_control")


class GridComboTests(unittest.TestCase):
    def test_observed_selectable_data_row_in_combo_is_selected_then_parent_value_verified(self):
        combo = element(name="Family", role="ComboBox", value="All")
        row = element(2, name="Hybrid", role="DataItem", parent_index=1, selected=False)
        row["actions"] = ["select"]
        table = element(1, name="Family options", role="DataGrid", parent_index=0)
        after = dict(combo, value="Hybrid", element_token="new:0")
        runtime = Runtime([snapshot(combo, table, row), snapshot(after)])
        result = Operations(runtime).execute({"operation": "select_option", "selector": {"name": "Family", "role": "ComboBox"},
            "value": "Hybrid", "verification_timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "click", "get_window_state"])
        self.assertEqual(runtime.calls[1][1]["element_token"], "s1:2")

    def test_data_row_without_selection_evidence_is_not_an_option(self):
        from operations import _Execution
        for changes in ({"actions": ["set_value"]}, {"selected": None}, {"role": "Custom"}):
            item = element(2, name="Hybrid", role="DataItem", selected=False)
            item["actions"] = ["select"]
            item.update(changes)
            self.assertFalse(_Execution._option_role(item))

    def test_explicit_multiselect_combo_or_table_does_not_send_single_choice(self):
        for where in ("combo", "table"):
            combo = element(name="Family", role="ComboBox", value="All")
            table = element(1, name="Family options", role="DataGrid", parent_index=0)
            row = element(2, name="Hybrid", role="DataItem", parent_index=1, selected=False)
            row["actions"] = ["select"]
            (combo if where == "combo" else table)["can_select_multiple"] = True
            runtime = Runtime([snapshot(combo, table, row)])
            result = Operations(runtime).execute({"operation": "select_option", "selector": {"name": "Family", "role": "ComboBox"},
                "value": "Hybrid", "verification_timeout_ms": 0}, TARGET)
            self.assertFalse(result["input_dispatched"])
            self.assertEqual(result["diagnostic"]["code"], "multi_selection_requires_explicit_state")

    def test_table_row_select_item_requires_boolean_selected_and_action(self):
        before = element(name="D200", role="DataItem", selected=False)
        before["actions"] = ["select"]
        after = dict(before, selected=True, element_token="new:0")
        runtime = Runtime([snapshot(before), snapshot(after)])
        result = Operations(runtime).execute({"operation": "select_item", "selector": {"name": "D200", "role": "DataItem"},
            "verification_timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])


class PersistedSemanticSelectorTests(unittest.TestCase):
    def entry(self, selector):
        return {"id": "device-family", "program_id": "fixture", "label": "Family", "screen": "",
                "instructions": "", "updated_at": "2026-10-28", "program_binding": "a"*64,
                "selector": selector, "fingerprint": {"role": "Edit", "actions": ["set_value"]}, "revision": 1}

    def test_nested_row_and_existing_label_are_valid_without_flattening(self):
        from learning import ElementLibrary
        selectors = [CELL, {"role": "Edit", "within": {"name": "Conditions"},
                           "label": {"text": "Family", "relation": "rendered_preceding"}}]
        for selector in selectors:
            entry = self.entry(copy.deepcopy(selector))
            ElementLibrary._validate_entry(entry)
            self.assertEqual(entry["selector"], selector)

    def test_nested_strings_still_reject_nulls_and_invalid_unicode(self):
        from learning import ElementLibrary, LearningError
        for bad in ("D\x00", "D\ud800"):
            selector = copy.deepcopy(CELL)
            selector["row"]["equals"] = bad
            with self.assertRaises(LearningError):
                ElementLibrary._validate_entry(self.entry(selector))

    def test_task_disk_roundtrip_editor_and_replay_keep_row_contract(self):
        from server import TaskStore
        from process_editor import ProcessDraft
        from workflows import render_steps
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(folder, {"programs": [{"id": "fixture"}]})
            task = store.save({"name": "Row edit", "instructions": "Synthetic row", "expected": "Family after",
                "program_ids": ["fixture"], "variables": {}, "steps": [{"program_id": "fixture",
                "operation": "set_value", "selector": CELL, "value": "after", "verification_timeout_ms": 0}]})
            saved = store.get(task["id"])
            draft = ProcessDraft([{"program_id": "fixture", **TARGET}], saved)
            draft.validate()
            steps, _ = render_steps(saved, {})
            self.assertEqual(steps[0]["selector"], CELL)
            runtime = Runtime([grid(offset=300), grid(chosen="after", offset=400, reverse=True)])
            step = {k: v for k, v in steps[0].items() if k not in {"program_id", "step_id", "window_ref"}}
            self.assertTrue(Operations(runtime).execute(step, TARGET)["task_verified"])


if __name__ == "__main__":
    unittest.main()
