"""Projection diagnostics explain loss without inventing native capabilities."""
import copy
import json
import unittest
from unittest import mock

from accessibility_tree import normalize_snapshot, projection_evidence
from inspection import inspect_window, projection_diagnostics
from operations import OperationError


TARGET = {"pid": 11, "window_id": 22}


class Runtime:
    mode = "uia"

    def __init__(self, snapshot, error=False, text_only=False):
        self.snapshot = copy.deepcopy(snapshot)
        self.error, self.text_only = error, text_only
        self.calls = []

    def check_active(self):
        pass

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        answer = {"isError": self.error}
        if self.text_only:
            answer["content"] = [{"type": "text", "text": json.dumps(self.snapshot)}]
        else:
            answer["structuredContent"] = copy.deepcopy(self.snapshot)
        return answer


def inspect(snapshot, **kwargs):
    runtime = Runtime({**TARGET, **snapshot})
    answer = inspect_window(runtime, TARGET, observation="uia", diagnostics=True, **kwargs)
    return runtime, answer["structuredContent"]["inspection"]


class ProjectionDiagnosticsTests(unittest.TestCase):
    def test_anonymous_inert_nodes_remain_in_diagnostics_with_filter_reason(self):
        runtime, data = inspect({"elements": [
            {"element_index": 0, "role": "Pane", "class_name": "WindowsFormsHost"},
            {"element_index": 1, "role": "Custom", "actions": []},
            {"element_index": 2, "role": "Button", "name": "조회", "actions": ["invoke"]},
        ]})
        diag = data["diagnostics"]
        self.assertEqual(diag["counts"]["driver_elements"], 3)
        self.assertEqual(diag["counts"]["mcp_controls_returned"], 1)
        self.assertEqual(diag["filter_reasons"], {"no_identity_or_actions": 2, "returned_control": 1})
        self.assertEqual(len(diag["nodes"]), 3)
        self.assertEqual(runtime.calls[0][0], "get_window_state")
        self.assertEqual(len(runtime.calls), 1)
        self.assertFalse(diag["authorizes_input"])
        self.assertIsNone(diag["counts"]["provider_original"])

    def test_absent_pattern_is_unknown_and_explicit_false_is_unsupported(self):
        _, data = inspect({"elements": [{"role": "Custom", "name": "grid", "actions": ["invoke"],
            "patterns": {"Grid": True, "VirtualizedItem": False,
                         "ScrollItem": {"status": "unknown", "hresult": "0x80040201", "timeout": True}}}]})
        patterns = data["diagnostics"]["nodes"][0]["patterns"]
        self.assertEqual(patterns["Grid"]["status"], "supported")
        self.assertEqual(patterns["VirtualizedItem"]["status"], "unsupported")
        self.assertEqual(patterns["ScrollItem"]["hresult"], "0x80040201")
        self.assertEqual(patterns["Invoke"]["status"], "unknown")
        self.assertEqual(patterns["Selection"]["status"], "unknown")

    def test_metadata_is_observed_only_and_handles_are_not_selectors(self):
        _, data = inspect({"elements": [{"role": "Edit", "name": "Family", "actions": ["set_value"],
            "FrameworkId": "WinForm", "ClassName": "UltraCombo", "ProviderDescription": "vendor stub",
            "NativeWindowHandle": 123, "RuntimeId": [42, 7, 9]}]})
        node = data["diagnostics"]["nodes"][0]
        self.assertEqual(node["metadata"]["framework_id"]["value"], "WinForm")
        self.assertEqual(node["metadata"]["runtime_id"]["value"], [42, 7, 9])
        self.assertEqual(node["metadata_unreported"], [])
        self.assertEqual(node["identifiers_lifetime"], "this_observation_only")
        self.assertNotIn("runtime_id", data["controls"][0]["selector"])
        self.assertNotIn("native_window_handle", data["controls"][0]["selector"])

    def test_custom_editor_requires_explicit_writable_value_pattern(self):
        _, data = inspect({"elements": [
            {"name": "writable", "role": "Custom", "actions": ["set_value"],
             "patterns": {"Value": {"status": "supported", "read_only": False}}},
            {"name": "unknown", "role": "Custom", "actions": ["set_value"]},
            {"name": "readonly", "role": "Custom", "actions": ["set_value"],
             "patterns": {"Value": {"status": "supported", "read_only": True}}},
            {"name": "selection", "role": "ComboBox", "actions": ["set_value"],
             "patterns": {"Value": {"status": "supported", "read_only": False}}},
        ]})
        controls = {row["name"]: row for row in data["controls"]}
        self.assertIn("set_value", controls["writable"]["suggested_operations"])
        for name in ("unknown", "readonly", "selection"):
            self.assertNotIn("set_value", controls[name]["suggested_operations"])

    def test_unknown_provider_not_misreported_as_no_accessibility(self):
        _, data = inspect({"elements": [{"name": "CustomWindowAutomationPeer", "role": "Window"}]})
        diag = data["diagnostics"]
        self.assertEqual(diag["provider_original"]["status"], "not_observed")
        self.assertEqual(diag["completeness"], "unknown")
        self.assertEqual(diag["view_membership"], {"raw": "unknown", "control": "unknown", "content": "unknown"})

    def test_empty_truncated_missing_and_provider_error_are_distinct(self):
        for payload, status in (({"elements": []}, "empty"), ({"elements": [], "truncated": True}, "truncated"),
                                ({}, "missing"), ({"elements": [], "error_code": "uia_provider_error"}, "provider_error"),
                                ({"diagnostic": {"code": "driver_timeout"}}, "timeout"),
                                ({"elements": [], "error_code": "driver_timeout"}, "timeout")):
            with self.subTest(status=status):
                report = projection_diagnostics(payload, {})
                self.assertEqual(report["status"], status)
                self.assertEqual(report["provider_original"]["status"], "not_observed")

    def test_tree_rows_and_normalized_nodes_are_separate_counts(self):
        raw = {"elements": [{"element_index": 0, "role": "Edit", "name": "input", "depth": 2,
                             "actions": ["set_value"]}],
               "tree_markdown": '- Window "fixture"\n  - Group "conditions"\n    - [0] Edit "input"\n',
               "elements_complete": False}
        _, data = inspect(raw)
        diag = data["diagnostics"]
        self.assertEqual(diag["counts"]["driver_elements"], 1)
        self.assertEqual(diag["counts"]["tree_markdown_rows"], 3)
        self.assertEqual(diag["counts"]["normalized_elements"], 3)
        self.assertFalse(diag["tree_markdown"]["native_elements_proven"])
        for node in diag["nodes"]:
            self.assertFalse(node["authorizes_input"])
            if node["source"].endswith("synthetic_ancestor"):
                self.assertEqual(node["actions"], [])
                self.assertEqual(node["patterns"]["Invoke"]["status"], "unknown")

    def test_protected_metadata_and_values_remain_redacted(self):
        secret = "NOT-FOR-DIAGNOSTICS"
        _, data = inspect({"elements": [{"role": "Password", "name": secret, "value": secret,
            "actions": ["set_value"], "provider_description": secret, "runtime_id": [9],
            "patterns": {"Value": {"status": "supported", "error_code": secret}}}]})
        self.assertNotIn(secret, json.dumps(data["diagnostics"]))
        self.assertEqual(data["diagnostics"]["nodes"][0]["metadata_status"], "redacted_protected")

    def test_search_action_and_page_exclusions_have_separate_reasons(self):
        _, data = inspect({"elements": [
            {"name": "item A", "role": "Edit", "actions": ["set_value"]},
            {"name": "item B", "role": "Edit", "actions": ["set_value"]},
            {"name": "item C", "role": "Edit", "actions": ["set_value"]},
            {"name": "unrelated", "role": "Button", "actions": ["invoke"]},
            {"name": "item label", "role": "Text"},
        ]}, search="item", actionable_only=True, offset=1, max_controls=1)
        self.assertEqual(data["diagnostics"]["filter_reasons"], {"before_page_offset": 1,
            "returned_control": 1, "after_page_limit": 1, "search_filter": 1, "actionable_filter": 1})

    def test_explicit_scope_exclusion_is_not_provider_loss(self):
        _, data = inspect({"elements": [
            {"element_index": 0, "name": "conditions", "role": "Group"},
            {"element_index": 1, "name": "Family", "role": "Edit", "parent_index": 0},
            {"element_index": 2, "name": "unrelated", "role": "Button"},
        ]}, within={"name": "conditions"})
        self.assertEqual(data["diagnostics"]["filter_reasons"], {"outside_requested_scope": 2, "returned_control": 1})

    def test_default_inspection_does_not_run_diagnostic_projection(self):
        runtime = Runtime({**TARGET, "elements": [{"role": "Button", "name": "조회", "actions": ["invoke"]}]})
        with mock.patch("inspection.projection_diagnostics", side_effect=AssertionError("unexpected diagnostic")):
            answer = inspect_window(runtime, TARGET, observation="uia")
        self.assertNotIn("diagnostics", answer["structuredContent"]["inspection"])
        self.assertEqual(len(runtime.calls), 1)

    def test_diagnostics_preserves_normal_controls_and_call_budget(self):
        raw = {**TARGET, "elements": [{"role": "Edit", "name": "Family", "actions": ["set_value"]}]}
        ordinary, diagnostic = Runtime(raw), Runtime(raw)
        before = inspect_window(ordinary, TARGET, observation="uia")["structuredContent"]["inspection"]
        after = inspect_window(diagnostic, TARGET, observation="uia", diagnostics=True)["structuredContent"]["inspection"]
        self.assertEqual(before["controls"], after["controls"])
        self.assertEqual(ordinary.calls, diagnostic.calls)

    def test_timeout_response_stays_error_with_diagnostics_not_empty_success(self):
        runtime = Runtime({"error_code": "driver_timeout"}, error=True)
        answer = inspect_window(runtime, TARGET, observation="uia", diagnostics=True)
        self.assertTrue(answer["isError"])
        self.assertEqual(answer["structuredContent"]["accessibility_diagnostics"]["status"], "timeout")
        self.assertEqual(len(runtime.calls), 1)

    def test_older_text_only_driver_retains_original_count(self):
        runtime = Runtime({**TARGET, "elements": [{"role": "Pane", "element_index": 0}]}, text_only=True)
        answer = inspect_window(runtime, TARGET, observation="uia", diagnostics=True)
        self.assertEqual(answer["structuredContent"]["inspection"]["diagnostics"]["counts"]["driver_elements"], 1)

    def test_malformed_patterns_do_not_crash_or_become_supported(self):
        _, data = inspect({"elements": [{"name": "grid", "role": "Custom", "patterns": {
            "Grid": {"status": {"untrusted": True}}, "Table": "maybe", "Invoke": 1}}]})
        patterns = data["diagnostics"]["nodes"][0]["patterns"]
        self.assertEqual(patterns["Grid"]["status"], "unknown")
        self.assertEqual(patterns["Table"]["status"], "unknown")
        self.assertEqual(patterns["Invoke"]["status"], "unknown")

    def test_node_budget_reports_omissions_without_changing_counts(self):
        report = projection_diagnostics({"elements": [{"role": "Pane"}] * 3}, {}, max_nodes=1)
        self.assertEqual(len(report["nodes"]), 1)
        self.assertEqual(report["diagnostic_nodes_omitted"], 2)
        self.assertEqual(report["counts"]["driver_elements"], 3)
        self.assertEqual(report["filter_reasons"]["no_identity_or_actions"], 3)

    def test_diagnostics_type_is_strict_and_invalid_tree_not_counted_as_zero(self):
        with self.assertRaises(OperationError):
            inspect_window(Runtime({}), TARGET, diagnostics=1)
        result = projection_evidence({"elements": [], "tree_markdown": "not a UIA tree"})
        self.assertEqual(result["tree_markdown"]["status"], "invalid")
        self.assertIsNone(result["tree_markdown"]["count"])


if __name__ == "__main__":
    unittest.main()
