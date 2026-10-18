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


class ReadOnlyControlContextTests(unittest.TestCase):
    @staticmethod
    def frame(x=120, y=50, width=150, height=24):
        return {"x": x, "y": y, "w": width, "h": height}

    def inspect(self, elements, **kwargs):
        runtime = Runtime(copy.deepcopy(elements))
        info = inspect_window(runtime, {"pid": 11, "window_id": 22}, observation="uia", **kwargs)["structuredContent"]["inspection"]
        self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))
        self.assertFalse(info["input_dispatched"])
        return info

    def form(self):
        return [{"element_index": 0, "name": "Conditions", "role": "Group"},
                {"element_index": 1, "parent_index": 0, "name": "Family", "role": "Text", "frame": self.frame(10, 50, 90)},
                {"element_index": 2, "parent_index": 0, "name": "[Editor] Edit Area", "role": "Edit",
                 "actions": ["set_value"], "value": "", "frame": self.frame()},
                {"element_index": 3, "parent_index": 0, "name": "[Editor] Edit Area", "role": "Edit",
                 "actions": ["set_value"], "value": "", "frame": self.frame(120, 150)}]

    def test_anonymous_duplicate_fields_are_unresolved_even_with_native_actions(self):
        elements = [{"element_index": 0, "role": "Pane"}, *[
            {"element_index": index + 1, "parent_index": 0, "name": "[Editor] Edit Area", "role": "Edit",
             "actions": ["set_value"], "value": ""} for index in range(14)]]
        info = self.inspect(elements)
        self.assertEqual(info["uia_evidence"], "actionable_controls_need_disambiguation")
        self.assertEqual(info["targetability"]["state"], "needs_disambiguation")
        self.assertEqual(info["targetability"]["ambiguous_actionable_controls"], 14)
        self.assertEqual(info["targetability"]["unique_actionable_controls"], 0)
        for field in info["controls"]:
            self.assertIsNone(field["selector"])
            self.assertEqual(field["suggested_operations"], [])
            self.assertEqual(field["value"], "")
        self.assertIn("computer_process_editor", info["next_step"])
        self.assertNotIn("computer_teach_element", info["next_step"])

    def test_counts_are_for_returned_controls_and_do_not_guess_anonymous_identity(self):
        elements = [{"role": "Edit", "actions": ["set_value"]},
                    {"name": "Search", "role": "Edit", "actions": ["set_value"]}]
        info = self.inspect(elements)
        self.assertEqual(info["targetability"]["state"], "partially_targetable")
        self.assertEqual(info["targetability"]["unidentified_actionable_controls"], 1)
        self.assertEqual(info["targetability"]["unique_actionable_controls"], 1)
        paged = self.inspect(elements, max_controls=1)
        self.assertEqual(paged["targetability"]["scope"], "returned_controls")
        self.assertEqual(paged["targetability"]["actionable_controls"], 1)
        self.assertEqual(paged["controls_omitted"], 1)

    def test_auto_adds_current_image_for_ambiguous_controls_without_guessing_target(self):
        runtime = Runtime(self.form())
        captures = []
        def capture(target):
            captures.append(target)
            return {"structuredContent": dict(target), "content": [{"type": "image", "mimeType": "image/png", "data": "synthetic"}]}
        runtime.capture_checkpoint = capture
        result = inspect_window(runtime, {"pid": 11, "window_id": 22})
        info = result["structuredContent"]["inspection"]
        self.assertEqual(len(captures), 1)
        self.assertEqual(info["status"], "combined_observation")
        self.assertEqual(info["targetability"]["state"], "needs_disambiguation")
        fields = [item for item in info["controls"] if item.get("role") == "Edit"]
        self.assertTrue(all(item["selector"] is None for item in fields))
        self.assertTrue(all("target_ref" not in item and "element_index" not in item for item in fields))
        self.assertIn("scope_region", info["next_step"])
        self.assertFalse(info["context_contract"]["authorizes_input"])
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state"])

    def test_same_parent_label_and_ancestor_are_readonly_not_promoted_to_selector(self):
        info = self.inspect(self.form())
        field = next(e for e in info["controls"] if e.get("element_index") == 2)
        context = field["context"]
        self.assertFalse(context["authorizes_input"])
        self.assertEqual(context["named_ancestors"], [{"name": "Conditions", "role": "Group", "depth": 1}])
        self.assertEqual(context["geometry"]["bounds"], {"x": 120, "y": 50, "width": 150, "height": 24})
        self.assertEqual(context["geometry"]["coordinate_space"], "desktop_physical")
        self.assertEqual(context["geometry"]["image_mapping"], "not_inferred")
        self.assertEqual(context["nearby_labels"][0]["name"], "Family")
        self.assertTrue(context["nearby_labels"][0]["inferred"])
        self.assertNotIn("label_associations", context)
        self.assertIsNone(field["selector"])
        self.assertEqual(field["suggested_operations"], [])

    def test_duplicate_nearby_labels_are_kept_as_ambiguous_candidates(self):
        elements = self.form()
        elements.insert(2, {"element_index": 4, "parent_index": 0, "name": "Family", "role": "Text", "frame": self.frame(15, 51, 90)})
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertEqual(field["context"]["nearby_label_candidate_count"], 2)
        self.assertTrue(field["context"]["nearby_labels_ambiguous"])
        self.assertEqual([e["name"] for e in field["context"]["nearby_labels"]], ["Family", "Family"])
        self.assertIsNone(field["selector"])

    def test_other_parent_and_missing_or_duplicate_parent_never_imply_label_relation(self):
        for parent in (99, None, "0"):
            elements = self.form()
            elements[1]["parent_index"] = parent
            field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
            self.assertNotIn("nearby_labels", field["context"])
        elements = self.form()
        elements.append({"element_index": 0, "name": "Other parent", "role": "Pane"})
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertNotIn("nearby_labels", field["context"])
        self.assertNotIn("named_ancestors", field["context"])

    def test_explicit_label_relation_requires_current_unique_label_reference(self):
        for reference in (1, {"element_index": 1}):
            elements = self.form(); elements[2]["labeled_by"] = reference
            field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
            association = field["context"]["label_associations"]
            self.assertEqual(association, [{"name": "Family", "role": "Text", "source": "driver.labeled_by", "inferred": False}])
            self.assertIsNone(field["selector"])
        for reference in (999, True, {"name": "Family"}, "Family"):
            elements = self.form(); elements[2]["labeled_by"] = reference
            field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
            self.assertNotIn("label_associations", field["context"])

    def test_conflicting_explicit_labels_with_same_text_remain_ambiguous(self):
        elements = self.form()
        elements.append({"element_index": 4, "parent_index": 0, "name": "Family", "role": "Text"})
        elements[2].update(labeled_by=1, labelled_by=4)
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertEqual(len(field["context"]["label_associations"]), 2)
        self.assertTrue(field["context"]["label_association_ambiguous"])

    def test_invalid_or_unknown_space_rectangles_are_never_published_or_used(self):
        for change in ({"w": 0}, {"h": -1}, {"x": True}, {"x": float("nan")}, {"h": float("inf")}, {"y": "50"}, {"x": 10 ** 1000}):
            elements = self.form(); elements[2]["frame"].update(change)
            # An invalid preferred frame must not borrow a different rectangle.
            elements[2]["bounds"] = {"x": 120, "y": 50, "width": 150, "height": 24}
            field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
            self.assertNotIn("geometry", field["context"])
            self.assertNotIn("nearby_labels", field["context"])
        elements = self.form(); elements[2]["coordinate_space"] = "image_pixels"
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertNotIn("geometry", field["context"])

    def test_legacy_bounds_and_negative_desktop_origin_preserve_observed_coordinates(self):
        elements = self.form(); elements[2].pop("frame")
        elements[2]["bounds"] = {"x": -900, "y": -40, "width": 150, "height": 24}
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertEqual(field["context"]["geometry"]["source"], "driver.bounds")
        self.assertEqual(field["context"]["geometry"]["bounds"], elements[2]["bounds"])
        self.assertNotIn("nearby_labels", field["context"])

    def test_protected_fields_labels_and_ancestors_do_not_leak_new_context_or_values(self):
        elements = self.form()
        elements[2].update(protected=True, name="SECRET_FIELD", value="SECRET_VALUE", automation_id="SECRET_ID")
        protected = next(e for e in self.inspect(elements)["controls"] if e.get("protected"))
        self.assertEqual(protected["targeting_state"], "protected")
        for key in ("context", "name", "value", "automation_id"):
            self.assertNotIn(key, protected)
        self.assertIsNone(protected["selector"])
        self.assertEqual(protected["actions"], [])
        self.assertNotIn("SECRET", str(self.inspect(elements)))
        elements = self.form()
        elements[0].update(name="SECRET_PARENT", protected=True)
        elements[1].update(name="SECRET_LABEL", protected=True)
        elements[2]["labeled_by"] = 1
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertNotIn("named_ancestors", field["context"])
        self.assertNotIn("nearby_labels", field["context"])
        self.assertNotIn("label_associations", field["context"])

    def test_input_text_is_not_reclassified_as_a_label_or_parent_name(self):
        elements = self.form()
        elements[0].update(name="PRIVATE_CONTENT", value="PRIVATE_CONTENT")
        elements[1].update(name="PRIVATE_CONTENT", value="PRIVATE_CONTENT")
        elements[2]["labeled_by"] = 1
        field = next(e for e in self.inspect(elements)["controls"] if e.get("element_index") == 2)
        self.assertNotIn("PRIVATE_CONTENT", str(field.get("context", {})))

    def projected(self, source=None):
        from test_accessibility_tree import AccessibilityTreeTests
        source = source or AccessibilityTreeTests.projected_business()
        source.update(pid=11, window_id=22)
        runtime = Runtime(answer={"structuredContent": source})
        return inspect_window(runtime, {"pid": 11, "window_id": 22}, observation="uia")["structuredContent"]["inspection"]

    def test_rendered_leaf_labels_are_context_only_with_explicit_inference(self):
        info = self.projected()
        edits = [c for c in info["controls"] if c["role"] == "Edit"]
        self.assertEqual(len(edits), 2)
        for field, group, aid in zip(edits, ("조회 조건", "보관 조건"), ("100", "200")):
            context = field["context"]
            self.assertIn(group, [a["name"] for a in context["named_ancestors"]])
            self.assertEqual(context["preceding_text_candidates"], [{"name": "Device", "inferred": True,
                              "evidence": "preceding_text_in_rendered_tree_only"}])
            self.assertEqual(context["rendered_sibling_label_count"], 1)
            self.assertNotIn("nearby_labels", context)
            self.assertNotIn("label_associations", context)
            self.assertFalse(context["authorizes_input"])
            self.assertEqual(field["selector"], {"automation_id": aid})
        self.assertTrue(edits[1]["context"]["rendered_parent_path_incomplete"])
        self.assertFalse(any(c.get("name") == "Device" for c in info["controls"]))

    def test_multiple_preceding_labels_remain_ambiguous_and_never_become_selectors(self):
        from test_accessibility_tree import AccessibilityTreeTests
        source = AccessibilityTreeTests.projected_business()
        source["tree_markdown"] = source["tree_markdown"].replace('      - Text "Device"', '      - Text "Device"\n      - Text "Family"', 1)
        source["tree_markdown"] = source["tree_markdown"].replace('id=100 ', '').replace('id=200 ', '')
        source["tree_markdown"] = source["tree_markdown"].replace('Group "보관 조건"', 'Group "조회 조건"')
        edits = [c for c in self.projected(source)["controls"] if c["role"] == "Edit"]
        self.assertTrue(edits[0]["context"]["preceding_text_ambiguous"])
        self.assertEqual([x["name"] for x in edits[0]["context"]["preceding_text_candidates"]], ["Device", "Family"])
        self.assertTrue(all(c["selector"] is None for c in edits))

    def test_malformed_tree_does_not_leak_unvalidated_label_context(self):
        from test_accessibility_tree import AccessibilityTreeTests
        source = AccessibilityTreeTests.projected_business()
        source["tree_markdown"] = source["tree_markdown"].replace('id=100', 'id=bad id=duplicate')
        source["read_only_labels"] = [{"element_index": -1, "parent_index": 0, "role": "Text", "label": "FORGED",
            "source": "driver.tree_markdown", "read_only": True, "following_element_index": 0}]
        self.assertNotIn("FORGED", str(self.projected(source)))

    def test_child_local_frames_outside_observed_window_are_not_desktop_hints(self):
        elements = self.form()
        elements[2]["frame"] = self.frame(3, 3, 111, 22)
        runtime = Runtime(answer={"structuredContent": {"pid": 11, "window_id": 22, "elements": elements,
            "window_bounds": {"x": 800, "y": 400, "width": 1000, "height": 700}}})
        info = inspect_window(runtime, {"pid": 11, "window_id": 22}, observation="uia")["structuredContent"]["inspection"]
        field = next(e for e in info["controls"] if e.get("element_index") == 2)
        self.assertNotIn("geometry", field["context"])
        self.assertNotIn("nearby_labels", field["context"])
        self.assertEqual(field["context"]["geometry_unavailable_reason"], "outside_observed_window")
        self.assertIsNone(field["selector"])


if __name__ == "__main__":
    unittest.main()
