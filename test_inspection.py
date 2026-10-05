"""Observed capabilities and exact targets, independent of browser/app names."""
import copy
import unittest
import tempfile
from types import SimpleNamespace
from inspection import inspect_window
from operations import OperationError, Operations


class Runtime:
    def __init__(self, elements=None, mode="uia", answer=None):
        self.mode = mode
        self.calls = []
        self.answer = answer or {"structuredContent": {"pid": 11, "window_id": 22, "elements": elements or []}}
    def check_active(self):
        pass
    def call(self, name, args):
        self.calls.append((name, args))
        return copy.deepcopy(self.answer)


class InspectionTests(unittest.TestCase):
    def inspect(self, runtime, **kwargs):
        return inspect_window(runtime, {"pid": 11, "window_id": 22}, **kwargs)
    def test_native_patterns_and_unsupported_state_are_distinguished(self):
        runtime = Runtime([
            {"label": "문구", "role": "Edit", "actions": ["set_value"]},
            {"label": "완료", "role": "CheckBox", "actions": ["toggle"], "selected": False},
            {"label": "불명확", "role": "CheckBox", "actions": ["toggle"]},
            {"label": "세부", "role": "TabItem", "actions": ["select"], "selected": False},
            {"label": "읽기만", "role": "Edit", "actions": ["set_value"], "read_only": True},
        ])
        result = self.inspect(runtime)["structuredContent"]["inspection"]
        controls = {v["name"]: v for v in result["controls"]}
        self.assertIn("set_value", controls["문구"]["suggested_operations"])
        self.assertIn("set_checked", controls["완료"]["suggested_operations"])
        self.assertNotIn("set_checked", controls["불명확"]["suggested_operations"])
        self.assertIn("select_item", controls["세부"]["suggested_operations"])
        self.assertNotIn("set_value", controls["읽기만"]["suggested_operations"])
        self.assertFalse(result["task_verified"])
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state"])
    def test_scoped_duplicate_buttons_are_reusable_without_raw_handles(self):
        runtime = Runtime([
            {"element_index": 1, "label": "왼쪽", "role": "Group"},
            {"element_index": 2, "label": "오른쪽", "role": "Group"},
            {"element_index": 3, "parent_index": 1, "label": "확인", "role": "Button", "actions": ["invoke"], "element_token": "private-fresh"},
            {"element_index": 4, "parent_index": 2, "label": "확인", "role": "Button", "actions": ["invoke"]},
        ])
        data = self.inspect(runtime)["structuredContent"]["inspection"]
        controls = [c for c in data["controls"] if c["name"] == "확인"]
        self.assertEqual([c["selector"]["within"]["name"] for c in controls], ["왼쪽", "오른쪽"])
        self.assertNotIn("private-fresh", str(data))
    def test_unscoped_duplicates_are_not_recommended(self):
        data = self.inspect(Runtime([{"label": "확인", "role": "Button"}] * 2))["structuredContent"]["inspection"]
        self.assertTrue(all(c["selector"] is None and c["suggested_operations"] == [] for c in data["controls"]))
    def test_automation_id_can_select_unnamed_native_control(self):
        data = self.inspect(Runtime([{"automation_id": "entryBox", "role": "Edit", "actions": ["set_value"]}]))
        self.assertEqual(data["structuredContent"]["inspection"]["controls"][0]["selector"], {"automation_id": "entryBox"})
    def test_duplicate_automation_id_falls_back_to_unique_label(self):
        runtime = Runtime([{"automation_id": "reused", "label": name, "role": "Edit", "actions": ["set_value"]} for name in ("시작일", "종료일")])
        controls = self.inspect(runtime)["structuredContent"]["inspection"]["controls"]
        self.assertEqual([c["selector"] for c in controls], [{"name": "시작일", "role": "Edit"}, {"name": "종료일", "role": "Edit"}])
    def test_empty_accessibility_does_not_switch_or_input(self):
        runtime = Runtime()
        info = self.inspect(runtime)["structuredContent"]["inspection"]
        self.assertEqual(info["status"], "no_accessible_controls")
        self.assertEqual(len(runtime.calls), 1)
        self.assertFalse(runtime.calls[0][1]["include_screenshot"])
    def test_visual_keeps_image_and_has_no_uia_capability_claim(self):
        runtime = Runtime(mode="visual", answer={"structuredContent": {"pid": 11, "window_id": 22, "screenshot_width": 400}, "content": [{"type": "image", "data": "synthetic", "mimeType": "image/png"}]})
        data = self.inspect(runtime)
        self.assertEqual(data["structuredContent"]["inspection"]["status"], "visual_observation")
        self.assertEqual(data["content"][1]["data"], "synthetic")
        self.assertFalse(runtime.calls[0][1]["include_accessibility_tree"])
    def test_errors_preserved_and_wrong_window_rejected(self):
        runtime = Runtime(answer={"isError": True, "structuredContent": {"error_code": "driver_timeout"}})
        self.assertEqual(self.inspect(runtime), runtime.answer)
        with self.assertRaises(OperationError):
            self.inspect(Runtime(answer={"structuredContent": {"pid": 99, "window_id": 22, "elements": []}}))
    def test_bounds_count_and_large_value_truncation_are_explicit(self):
        runtime = Runtime([{"label": str(i), "role": "Edit", "value": "a" * 16001} for i in range(3)])
        data = self.inspect(runtime, max_controls=1, max_elements=3)["structuredContent"]["inspection"]
        self.assertEqual((data["control_count"], data["controls_omitted"]), (3, 2))
        self.assertTrue(data["controls"][0]["value_truncated"])
        self.assertTrue(data["traversal_may_be_limited"])
        for kwargs in ({"max_controls": 0}, {"max_depth": 33}, {"max_elements": True}):
            with self.assertRaises(OperationError):
                self.inspect(runtime, **kwargs)
    def test_inactive_session_cannot_be_observed(self):
        runtime = Runtime()
        runtime.check_active = lambda: (_ for _ in ()).throw(RuntimeError("stopped"))
        with self.assertRaises(RuntimeError):
            self.inspect(runtime)
        self.assertEqual(runtime.calls, [])
    def test_mcp_tool_requires_session_and_validates_bounds_before_reading(self):
        from server import ComputerManager, SessionError, MANAGEMENT
        from test_server import config_at
        with tempfile.TemporaryDirectory() as folder:
            manager = ComputerManager(config_at(folder))
            with self.assertRaises(SessionError):
                manager.call("computer_inspect", {"pid": 11, "window_id": 22})
            runtime = Runtime([{"label": "이름", "role": "Edit", "actions": ["set_value"]}])
            manager.session = runtime
            for args in ({"pid": 11, "window_id": 22, "max_controls": 201}, {"pid": 11, "window_id": 22, "force": True}):
                with self.assertRaises(SessionError):
                    manager.call("computer_inspect", args)
            self.assertEqual(runtime.calls, [])
            result = manager.call("computer_inspect", {"pid": 11, "window_id": 22})
            self.assertEqual(result["structuredContent"]["inspection"]["control_count"], 1)
            self.assertTrue(MANAGEMENT["computer_inspect"]["annotations"]["readOnlyHint"])
    def test_reconstructed_ancestors_can_never_be_mutation_targets(self):
        for extra in ({"element_index": -1}, {"element_index": 3, "synthetic_ancestor": True}):
            runtime = Runtime([{"label": "상위", "role": "Group", "element_token": "even-a-token", **extra}])
            result = Operations(runtime).execute({"operation": "click", "selector": {"name": "상위"},
                "expect": [{"selector": {"name": "상위"}, "property": "name", "equals": "상위"}]}, {"pid": 11, "window_id": 22})
            self.assertFalse(result["input_dispatched"])
            self.assertEqual(result["diagnostic"]["code"], "read_only_ancestor")
            self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))


if __name__ == "__main__":
    unittest.main()
