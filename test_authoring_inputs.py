import copy
import unittest
from unittest.mock import patch

from authoring_inputs import describe_inputs, edit_inputs, input_name
from task_inputs import InputError, expand, resolve_values


FIELD = {"role": "Edit", "automation_id": "condition"}
OTHER = {"role": "Edit", "automation_id": "other"}


def value_step(value="N", operation="set_value"):
    return {"step_id": "saved-step", "operation": operation, "program_id": "fixture",
            "selector": copy.deepcopy(FIELD), "value": value,
            "expect": [{"selector": copy.deepcopy(FIELD), "property": "value", "equals": value}]}


class LocalInputInventoryTests(unittest.TestCase):
    def test_exact_input_formats_and_interpolation_are_distinct(self):
        self.assertEqual(input_name("${family}"), "family")
        self.assertEqual(input_name({"input_ref": "family"}), "family")
        for value in ("prefix ${family}", "${family}${other}", {"input_ref": "family", "extra": 1},
                      {"input_ref": 1}, "${bad-key}", True):
            self.assertIsNone(input_name(value))

    def test_inventory_includes_expectations_and_exact_paths_without_mutation(self):
        steps = [value_step("${family}")]
        steps[0]["expect"][0]["equals"] = {"input_ref": "family"}
        variables = {"family": {"type": "enum", "values": ["N", "P"], "default": "N"},
                     "device": {"type": "text", "description": "장비"}}
        before = copy.deepcopy((steps, variables))
        rows = describe_inputs(steps, variables)
        self.assertEqual(rows[0]["use_count"], 2)
        self.assertEqual([use["path"] for use in rows[0]["uses"]], [["value"], ["expect", 0, "equals"]])
        self.assertEqual(rows[0]["uses"][0]["step_id"], "saved-step")
        self.assertFalse(rows[0]["required"])
        self.assertTrue(rows[1]["required"])
        rows[0]["spec"]["values"].append("X")
        self.assertEqual((steps, variables), before)

    def test_foreach_source_and_globals_reported_local_item_excluded(self):
        child = value_step("${device}/${family}")
        steps = [{"operation": "foreach", "input": "devices", "item": "device", "steps": [child]}]
        variables = {"devices": {"type": "list", "items": {"type": "text"}},
                     "family": {"type": "text"}}
        rows = describe_inputs(steps, variables)
        self.assertEqual(rows[0]["uses"][0]["path"], ["input"])
        self.assertEqual(rows[1]["uses"][0]["path"], ["steps", 0, "value"])
        self.assertEqual([row["name"] for row in rows], ["devices", "family"])


class LocalInputEditingTests(unittest.TestCase):
    def test_each_supported_type_can_be_defined_required_and_with_default(self):
        samples = [({"type": "text"}, "text"), ({"type": "date"}, "2026-10-25"),
                   ({"type": "enum", "values": ["N", "P"]}, "P"),
                   ({"type": "number", "minimum": 0, "maximum": 3}, 1.5),
                   ({"type": "integer", "minimum": 0, "maximum": 3}, 2),
                   ({"type": "boolean"}, False),
                   ({"type": "list", "items": {"type": "date"}, "max_items": 3}, ["2026-10-25"])]
        for spec, default in samples:
            with self.subTest(kind=spec["type"]):
                _, variables = edit_inputs([], {}, {"action": "upsert", "name": "value", "spec": spec})
                self.assertTrue(describe_inputs([], variables)[0]["required"])
                _, variables = edit_inputs([], variables, {"action": "upsert", "name": "value", "spec": {**spec, "default": default}})
                self.assertEqual(resolve_values(variables, {})["value"], default)

    def test_bind_value_updates_only_self_check_preserves_step_id_and_other_values(self):
        step = value_step()
        step["expect"].append({"selector": copy.deepcopy(OTHER), "property": "value", "equals": "N"})
        original = [step, value_step("N")]
        original[1]["step_id"] = "other-step"
        before = copy.deepcopy(original)
        steps, variables = edit_inputs(original, {}, {"action": "bind", "index": 0, "name": "family",
            "spec": {"type": "enum", "values": ["N", "P"], "default": "N"}})
        self.assertEqual(steps[0]["value"], {"input_ref": "family"})
        self.assertEqual(steps[0]["expect"][0]["equals"], {"input_ref": "family"})
        self.assertEqual(steps[0]["expect"][1]["equals"], "N")
        self.assertEqual(steps[1], before[1])
        self.assertEqual(original, before)
        rendered = expand(steps, variables, resolve_values(variables, {"family": "P"}))
        self.assertEqual(rendered[0]["value"], "P")
        self.assertEqual(rendered[0]["expect"][0]["equals"], "P")
        self.assertEqual(rendered[0]["step_id"], "saved-step")

    def test_bind_existing_typed_value_preserves_definition_and_can_be_shared_explicitly(self):
        variables = {"count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}
        original = [value_step("10"), value_step("10")]
        original[1]["step_id"] = "second"
        steps, specs = edit_inputs(original, variables, {"action": "bind", "index": 0, "name": "count"})
        steps, specs = edit_inputs(steps, specs, {"action": "bind", "index": 1, "name": "count"})
        self.assertEqual(specs, variables)
        self.assertEqual(describe_inputs(steps, specs)[0]["use_count"], 4)

    def test_boolean_binding_and_unbinding_keep_boolean_expectation(self):
        step = {"operation": "set_checked", "step_id": "check", "checked": False, "selector": FIELD,
                "expect": [{"selector": FIELD, "property": "selected", "equals": False}]}
        steps, variables = edit_inputs([step], {}, {"action": "bind", "index": 0, "name": "selected",
            "spec": {"type": "boolean", "default": False}})
        self.assertEqual(expand(steps, variables, {"selected": True})[0]["checked"], True)
        fixed, specs = edit_inputs(steps, variables, {"action": "unbind", "index": 0, "value": True})
        self.assertIs(fixed[0]["checked"], True)
        self.assertIs(fixed[0]["expect"][0]["equals"], True)
        self.assertEqual(specs, variables)

    def test_unbind_understands_both_reference_spellings(self):
        step = value_step("${family}")
        step["expect"][0]["equals"] = {"input_ref": "family"}
        variables = {"family": {"type": "enum", "values": ["N", "P"]}}
        steps, specs = edit_inputs([step], variables, {"action": "unbind", "index": 0, "value": "P"})
        self.assertEqual(steps[0]["value"], "P")
        self.assertEqual(steps[0]["expect"][0]["equals"], "P")
        self.assertEqual(specs, variables)  # Other stages may still use it.

    def test_image_text_is_real_input_but_image_click_cannot_be_parameterized(self):
        steps, specs = edit_inputs([{"operation": "image_type_text", "value": "old"}], {},
            {"action": "bind", "index": 0, "name": "text", "spec": {"type": "text"}})
        self.assertEqual(steps[0]["value"], {"input_ref": "text"})
        for operation in ("image_click", "image_right_click", "click", "checkpoint", "delay", "select_item"):
            with self.subTest(operation=operation), self.assertRaises(InputError):
                edit_inputs([{"operation": operation, "value": "N"}], specs,
                            {"action": "bind", "index": 0, "name": "text"})

    def test_rename_rewrites_all_uses_not_equal_literals_and_preserves_style(self):
        step = value_step("${family}")
        step["expect"][0]["equals"] = {"input_ref": "family"}
        step["selector"]["name"] = "Group ${family}"
        step["expect"][0]["selector"]["name"] = "Group ${family}"
        step["literal"] = "family"
        variables = {"family": {"type": "text", "default": "N"}}
        steps, specs = edit_inputs([step], variables, {"action": "rename", "name": "family", "new_name": "choice"})
        self.assertEqual(steps[0]["value"], "${choice}")
        self.assertEqual(steps[0]["expect"][0]["equals"], {"input_ref": "choice"})
        self.assertEqual(steps[0]["selector"]["name"], "Group ${choice}")
        self.assertEqual(steps[0]["literal"], "family")
        self.assertEqual(specs, {"choice": variables["family"]})

    def test_foreach_source_rename_and_global_in_body_preserve_item(self):
        steps = [{"operation": "foreach", "step_id": "group", "input": "devices", "item": "device",
                  "steps": [value_step("${device}/${family}")]}]
        variables = {"devices": {"type": "list", "items": {"type": "text"}, "default": ["D1", "D2"]},
                     "family": {"type": "text", "default": "N"}}
        changed, specs = edit_inputs(steps, variables, {"action": "rename", "name": "devices", "new_name": "items"})
        changed, specs = edit_inputs(changed, specs, {"action": "rename", "name": "family", "new_name": "choice"})
        self.assertEqual(changed[0]["input"], "items")
        self.assertEqual(changed[0]["item"], "device")
        rendered = expand(changed, specs, resolve_values(specs, {}))
        self.assertEqual([row["value"] for row in rendered], ["D1/N", "D2/N"])

    def test_unbound_definition_can_be_removed_without_changing_steps(self):
        original = [value_step()]
        steps, specs = edit_inputs(original, {"unused": {"type": "text"}}, {"action": "remove", "name": "unused"})
        self.assertEqual(specs, {})
        self.assertEqual(steps, original)
        self.assertIsNot(steps, original)

    def test_used_definition_requires_explicit_fixed_value(self):
        variables = {"count": {"type": "integer", "minimum": 1, "maximum": 20}}
        original = [value_step({"input_ref": "count"})]
        with self.assertRaises(InputError):
            edit_inputs(original, variables, {"action": "remove", "name": "count"})
        steps, specs = edit_inputs(original, variables, {"action": "remove", "name": "count", "value": 12})
        self.assertEqual(steps[0]["value"], "12")
        self.assertEqual(steps[0]["expect"][0]["equals"], "12")
        self.assertEqual(specs, {})

    def test_unused_foreach_list_definition_removal_is_allowed_used_one_not(self):
        spec = {"type": "list", "items": {"type": "text"}}
        steps = [{"operation": "foreach", "input": "items", "item": "item", "steps": [value_step("${item}")]}]
        with self.assertRaises(InputError):
            edit_inputs(steps, {"items": spec}, {"action": "remove", "name": "items", "value": ["X"]})

    def test_invalid_edits_are_atomic(self):
        original = [value_step("${family}")]
        variables = {"family": {"type": "enum", "values": ["N", "P"], "default": "N"},
                     "other": {"type": "text"}}
        before = copy.deepcopy((original, variables))
        commands = [
            {"action": "upsert", "name": "family", "spec": {"type": "date", "default": "N"}},
            {"action": "rename", "name": "family", "new_name": "other"},
            {"action": "rename", "name": "family", "new_name": "not valid"},
            {"action": "unbind", "index": 0, "value": "X"},
            {"action": "remove", "name": "family", "value": "X"},
            {"action": "bind", "index": 0, "name": "unknown"},
            {"action": "bind", "index": 0, "name": "family", "field": "operation"},
            {"action": "bind", "index": True, "name": "family"},
            {"action": "bind", "index": -1, "name": "family"},
            {"action": "remove", "name": "family", "force": True},
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(InputError):
                edit_inputs(original, variables, command)
            self.assertEqual((original, variables), before)

    def test_boolean_input_type_change_and_list_binding_rejected(self):
        step = {"operation": "set_checked", "checked": {"input_ref": "checked"}}
        with self.assertRaises(InputError):
            edit_inputs([step], {"checked": {"type": "boolean"}},
                        {"action": "upsert", "name": "checked", "spec": {"type": "text"}})
        with self.assertRaises(InputError):
            edit_inputs([value_step()], {}, {"action": "bind", "index": 0, "name": "list",
                        "spec": {"type": "list", "items": {"type": "text"}}})

    def test_rename_to_foreach_item_name_is_rejected_without_mutation(self):
        steps = [{"operation": "foreach", "input": "items", "item": "item", "steps": [value_step("${item}")]}]
        variables = {"items": {"type": "list", "items": {"type": "text"}}, "family": {"type": "text"}}
        with self.assertRaises(InputError):
            edit_inputs(steps, variables, {"action": "rename", "name": "family", "new_name": "item"})
        self.assertEqual(steps[0]["item"], "item")

    def test_authoring_does_not_expand_runtime_list_defaults(self):
        import authoring_inputs
        original_expand = authoring_inputs.expand
        values_seen = []
        def capture(steps, specs, values):
            values_seen.append(copy.deepcopy(values))
            return original_expand(steps, specs, values)
        variables = {"items": {"type": "list", "items": {"type": "text"}, "default": ["D"] * 1000}}
        steps = [{"operation": "foreach", "input": "items", "item": "item", "steps": [value_step("${item}")]}]
        with patch.object(authoring_inputs, "expand", capture):
            edit_inputs(steps, variables, {"action": "upsert", "name": "label", "spec": {"type": "text"}})
        self.assertEqual(len(values_seen[0]["items"]), 1)
        self.assertEqual(len(variables["items"]["default"]), 1000)


if __name__ == "__main__":
    unittest.main()
