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
    def test_complex_form_search_scope_and_paging_keep_global_uniqueness(self):
        runtime = Runtime([
            {"element_index": 1, "label": "검색 조건", "role": "Group"},
            {"element_index": 2, "label": "다른 조건", "role": "Group"},
            *[{"element_index": 10+i, "parent_index": 1, "label": "조건 " + str(i),
               "role": "Edit", "actions": ["set_value"]} for i in range(6)],
            {"element_index": 30, "parent_index": 2, "label": "조건 2", "role": "Edit", "actions": ["set_value"]},
        ])
        data = self.inspect(runtime, search="조건", within={"name": "검색 조건"},
                            actionable_only=True, offset=2, max_controls=2)["structuredContent"]["inspection"]
        self.assertEqual(data["control_count"], 6)
        self.assertEqual(data["next_offset"], 4)
        self.assertEqual([c["element_index"] for c in data["controls"]], [12, 13])
        self.assertEqual(data["controls"][0]["selector"]["within"], {"name": "검색 조건", "role": "Group"})
        self.assertEqual(runtime.calls[0][0], "get_window_state")
        self.assertFalse(runtime.calls[0][1]["include_screenshot"])
        self.assertEqual(data["observation_scope"], {"kind": "driver_window_tree", "max_depth": 12, "max_elements": 600})
        self.assertEqual(data["filter_scope"], "returned_controls_after_window_observation")
        self.assertFalse(data["filters_reduce_uia_traversal"])
        self.assertFalse(data["execution_verified"])
        self.assertFalse(data["controls"][0]["execution_verified"])
    def test_search_does_not_match_current_input_values_or_guess_duplicate_scope(self):
        runtime = Runtime([{"element_index": 1, "label": "입력", "role": "Edit", "value": "사내 비공개 검색값"}])
        self.assertEqual(self.inspect(runtime, search="비공개")["structuredContent"]["inspection"]["controls"], [])
        runtime = Runtime([{"element_index": i, "label": "같은 영역", "role": "Group"} for i in (1, 2)])
        with self.assertRaises(OperationError):
            self.inspect(runtime, within={"name": "같은 영역"})
    def test_bad_filters_and_visual_filtering_fail_before_observation(self):
        for options in ({"offset": True}, {"offset": -1}, {"search": "x"*201}, {"actionable_only": 1}, {"within": {"role": "Group"}}):
            runtime = Runtime()
            with self.assertRaises(OperationError):
                self.inspect(runtime, **options)
            self.assertEqual(runtime.calls, [])
        runtime = Runtime(mode="visual")
        with self.assertRaises(OperationError):
            self.inspect(runtime, search="조회")
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


class AdaptiveInspectionTests(unittest.TestCase):
    target = {"pid": 11, "window_id": 22}
    image = {"type": "image", "mimeType": "image/png", "data": "synthetic"}

    def runtime(self, elements=None):
        runtime = Runtime(elements=elements)
        runtime.captures = []
        def capture(target):
            runtime.captures.append(copy.deepcopy(target))
            return {"structuredContent": dict(target), "content": [copy.deepcopy(self.image)]}
        runtime.capture_checkpoint = capture
        return runtime

    def test_default_auto_adds_readonly_image_for_empty_accessibility(self):
        runtime = self.runtime()
        result = inspect_window(runtime, self.target)
        info = result["structuredContent"]["inspection"]
        self.assertEqual(info["status"], "combined_observation")
        self.assertEqual(info["observed_modalities"], ["uia", "visual"])
        self.assertEqual(result["content"][1], self.image)
        self.assertEqual(runtime.captures, [self.target])
        self.assertEqual(runtime.mode, "uia")
        self.assertFalse(info["input_dispatched"])
        self.assertFalse(info["image_delivery"]["client_rendering_verified"])
        self.assertFalse(info["image_delivery"]["model_image_understanding_verified"])

    def test_structure_only_can_be_complemented_without_calling_it_actionable(self):
        runtime = self.runtime([{"label": "부모", "role": "Pane"}, {"label": "안내", "role": "Text"}])
        result = inspect_window(runtime, self.target)["structuredContent"]["inspection"]
        self.assertEqual(result["uia_evidence"], "structure_only")
        self.assertEqual(result["image_capture"]["reason"], "weak_accessibility")
        self.assertTrue(result["input_mode_unchanged"])

    def test_system_titlebar_buttons_do_not_count_as_accessible_app_content(self):
        runtime = self.runtime([{"element_index": 1, "name": "Any app title", "role": "TitleBar"},
                                {"element_index": 2, "parent_index": 1, "name": "Localized close", "role": "Button", "actions": ["invoke"]}])
        info = inspect_window(runtime, self.target)["structuredContent"]["inspection"]
        self.assertEqual(info["uia_evidence"], "structure_only")
        self.assertEqual(info["status"], "combined_observation")
        self.assertEqual(runtime.captures, [self.target])

    def test_healthy_uia_does_not_capture_unnecessarily_even_search_has_no_match(self):
        runtime = self.runtime([{"name": "조회", "role": "Button", "actions": ["invoke"]}])
        result = inspect_window(runtime, self.target, search="다른 버튼")["structuredContent"]["inspection"]
        self.assertEqual(runtime.captures, [])
        self.assertEqual(result["control_count"], 0)
        self.assertEqual(result["uia_evidence"], "actionable_controls_observed")

    def test_explicit_uia_never_captures_and_both_keeps_both(self):
        runtime = self.runtime([{"label": "조회", "role": "Button", "actions": ["invoke"]}])
        self.assertEqual(inspect_window(runtime, self.target, observation="both")["content"][1], self.image)
        self.assertEqual(runtime.captures, [self.target])
        runtime = self.runtime()
        result = inspect_window(runtime, self.target, observation="uia")["structuredContent"]["inspection"]
        self.assertEqual(result["status"], "no_accessible_controls")
        self.assertEqual(runtime.captures, [])

    def test_combined_read_does_not_return_input_handles_invalidated_by_checkpoint(self):
        runtime = self.runtime([{"element_index": 4, "parent_index": 0, "name": "Query", "role": "Button", "actions": ["invoke"]}])
        runtime.answer["structuredContent"]["snapshot_id"] = "before-checkpoint"
        result = inspect_window(runtime, self.target, observation="both")["structuredContent"]
        self.assertNotIn("snapshot_id", result)
        self.assertTrue(result["inspection"]["input_requires_fresh_observation"])
        control = result["inspection"]["controls"][0]
        self.assertNotIn("element_index", control)
        self.assertTrue(control["input_requires_fresh_observation"])
        self.assertEqual(control["selector"], {"name": "Query", "role": "Button"})

    def test_explicit_visual_on_uia_reads_only_image_without_mode_change(self):
        runtime = self.runtime()
        answer = inspect_window(runtime, self.target, observation="visual")
        self.assertEqual(runtime.calls, [])
        self.assertEqual(runtime.captures, [self.target])
        self.assertEqual(runtime.mode, "uia")
        self.assertEqual(answer["structuredContent"]["inspection"]["status"], "visual_observation")
        self.assertFalse(answer["isError"])

    def test_uia_error_never_triggers_image_fallback(self):
        for code in ("driver_timeout", "target_denied", "target_unavailable"):
            runtime = self.runtime()
            runtime.answer = {"isError": True, "structuredContent": {"error_code": code}}
            self.assertEqual(inspect_window(runtime, self.target, observation="both"), runtime.answer)
            self.assertEqual(runtime.captures, [])

    def test_different_capture_window_is_rejected_and_no_pixels_returned(self):
        runtime = self.runtime()
        runtime.capture_checkpoint = lambda target: {"structuredContent": {**target, "window_id": 99}, "content": [self.image]}
        with self.assertRaises(OperationError) as raised:
            inspect_window(runtime, self.target)
        self.assertEqual(raised.exception.code, "target_mismatch")

    def test_minimized_or_occluded_capture_has_actionable_diagnostic_without_refocus(self):
        runtime = self.runtime()
        runtime.capture_checkpoint = lambda target: {"isError": True, "structuredContent": {"error_code": "checkpoint_requires_foreground"}}
        result = inspect_window(runtime, self.target)
        info = result["structuredContent"]["inspection"]
        self.assertEqual(info["image_capture"]["diagnostic_code"], "checkpoint_requires_foreground")
        self.assertIn("맨 앞으로", info["next_step"])
        self.assertEqual([name for name, args in runtime.calls], ["get_window_state"])
        self.assertFalse(result["isError"])
        self.assertFalse(any(item["type"] == "image" for item in result["content"]))
        self.assertTrue(inspect_window(runtime, self.target, observation="visual")["isError"])

    def test_stop_during_capture_does_not_return_image(self):
        runtime = self.runtime()
        def capture(target):
            runtime.check_active = lambda: (_ for _ in ()).throw(RuntimeError("stopped"))
            return {"structuredContent": dict(target), "content": [self.image]}
        runtime.capture_checkpoint = capture
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            inspect_window(runtime, self.target)

    def test_visual_session_never_reads_uia_by_adding_an_option(self):
        for observation in ("both", "uia", "not-a-mode"):
            runtime = Runtime(mode="visual")
            with self.assertRaises(OperationError):
                inspect_window(runtime, self.target, observation=observation)
            self.assertEqual(runtime.calls, [])


class NativeScopedInspectionTests(unittest.TestCase):
    target = {"pid": 11, "window_id": 22}
    within = {"name": "조건", "role": "Group"}

    def runtime(self, *, truncated=False):
        runtime = Runtime()
        runtime.scoped_calls = []
        snapshot = {**self.target, "read_only": True, "scoped_observation": True, "scoped_inspection": True,
                    "scope_complete": True, "within": self.within, "truncated": truncated,
                    "elements": [{"element_index": 0, "parent_index": -1, "name": "조건", "role": "Group", "verification_only": True},
                                 {"element_index": 1, "parent_index": 0, "name": "종류", "role": "Edit", "value": "W", "verification_only": True, "observed_patterns": ["set_value"]}]}
        def inspect(target, **kwargs):
            runtime.scoped_calls.append((target, kwargs))
            return {"structuredContent": copy.deepcopy(snapshot), "content": []}
        runtime.inspect_controls = inspect
        return runtime, snapshot

    def test_within_reads_native_subtree_before_control_filtering_without_driver_walk(self):
        runtime, _ = self.runtime()
        info = inspect_window(runtime, self.target, within=self.within, search="종류")["structuredContent"]["inspection"]
        self.assertEqual(runtime.calls, [])
        self.assertEqual(len(runtime.scoped_calls), 1)
        self.assertEqual(runtime.scoped_calls[0][1]["timeout_ms"], 6000)
        self.assertFalse(info["filters_reduce_uia_traversal"])
        self.assertTrue(info["filters_reduce_uia_property_reads"])
        self.assertEqual(info["scope_lookup"], "whole_window_identity_metadata")
        self.assertEqual(info["observation_scope"]["kind"], "native_selected_subtree")
        self.assertEqual(info["controls"][0]["selector"], {"name": "종류", "role": "Edit", "within": self.within})
        self.assertTrue(info["controls"][0]["input_requires_fresh_observation"])
        self.assertNotIn("element_index", info["controls"][0])
        self.assertNotIn("snapshot_id", info["controls"][0])
        self.assertEqual(info["controls"][0]["suggested_operations"], ["assert", "set_value"])

    def test_partial_subtree_never_claims_uniqueness(self):
        runtime, _ = self.runtime(truncated=True)
        info = inspect_window(runtime, self.target, within=self.within)["structuredContent"]["inspection"]
        self.assertTrue(info["traversal_may_be_limited"])
        self.assertIsNone(info["controls"][0]["selector"])
        self.assertEqual(info["controls"][0]["suggested_operations"], [])

    def test_duplicate_scope_timeout_permission_errors_do_not_expand_or_capture(self):
        for code in ("ambiguous_selector", "scoped_observation_timeout", "target_mismatch", "target_denied"):
            runtime, _ = self.runtime()
            runtime.inspect_controls = lambda *args, **kwargs: (_ for _ in ()).throw(OperationError("refused", code))
            with self.assertRaises(OperationError) as raised:
                inspect_window(runtime, self.target, within=self.within, observation="both")
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(runtime.calls, [])

    def test_helper_absent_falls_back_explicitly_to_driver(self):
        runtime = Runtime([{"name": "조건", "role": "Group", "element_index": 1}, {"name": "값", "role": "Edit", "element_index": 2, "parent_index": 1}])
        runtime.inspect_controls = lambda *args, **kwargs: (_ for _ in ()).throw(NotImplementedError())
        info = inspect_window(runtime, self.target, within=self.within)["structuredContent"]["inspection"]
        self.assertEqual(info["scope_fallback"], "scoped_helper_unavailable")
        self.assertFalse(info["filters_reduce_uia_traversal"])
        self.assertEqual(len(runtime.calls), 1)

    def test_forged_or_unscoped_native_result_refused(self):
        runtime, snapshot = self.runtime()
        snapshot["within"] = {"name": "other"}
        with self.assertRaises(OperationError) as raised:
            inspect_window(runtime, self.target, within=self.within)
        self.assertEqual(raised.exception.code, "scoped_response_invalid")


if __name__ == "__main__":
    unittest.main()
