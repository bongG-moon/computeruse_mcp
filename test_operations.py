import copy
import json
import threading
import unittest
from unittest import mock

from operations import OperationError, Operations, _payload, _unique, validate_step, validate_selector, verification_step


TARGET = {"pid": 7, "window_id": 12}
FIELD = {"name": "시험 문구", "role": "Edit"}
COMBO = {"name": "신청 상태", "role": "ComboBox"}


def element(index=0, name="시험 문구", role="Edit", value="before", **extra):
    return dict(element_index=index, element_token=f"s1:{index}", label=name, role=role,
                value=value, enabled=True, actions=["set_value"], **extra)


def snapshot(*elements, **extra):
    return dict(TARGET, snapshot_id="s1", elements=list(elements), **extra)


class Runtime:
    mode = "uia"
    state = "active"

    def __init__(self, observations, actions=None, discoveries=None):
        self.observations = list(observations)
        self.actions = list(actions or [])
        self.discoveries = list(discoveries or [])
        self.calls = []
        self.stop_event = threading.Event()

    def check_active(self):
        if self.stop_event.is_set() or self.state != "active":
            raise RuntimeError("stopped")

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        rows = self.observations if name == "get_window_state" else self.discoveries if name == "list_windows" else self.actions
        answer = rows.pop(0) if rows else {"windows": []} if name == "list_windows" else {"ok": True}
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(name, args)
        if "isError" in answer:
            return answer
        return {"structuredContent": copy.deepcopy(answer)}


class GuardedRuntime(Runtime):
    def __init__(self, observations, actions=None, discoveries=None):
        super().__init__(observations, actions, discoveries)
        self.guard = type("Guard", (), {"observed_targets": set()})()

    def call(self, name, args):
        key = args.get("pid"), args.get("window_id")
        if name != "list_windows":
            self.guard.observed_targets.discard(key)
        answer = super().call(name, args)
        if name == "get_window_state" and not answer.get("isError"):
            self.guard.observed_targets.add(key)
        return answer


class OperationsTests(unittest.TestCase):
    def write(self, **extra):
        return dict(operation="set_value", selector=FIELD, value="after", verification_timeout_ms=0, **extra)

    def test_explicit_window_key_has_fresh_window_but_no_element_handle(self):
        for operation, extra in (("press_key", {"key": "enter"}), ("hotkey", {"keys": ["ctrl", "s"]})):
            runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
            step = {"operation": operation, "key_target": "window", **extra,
                    "expect": [{"selector": FIELD, "property": "value", "equals": "after"}]}
            result = Operations(runtime).execute(step, TARGET, delivery_mode="foreground")
            self.assertTrue(result["task_verified"])
            self.assertEqual(result["diagnostic"]["selection_strategy"], "explicit_window_keyboard")
            name, args = runtime.calls[1]
            self.assertEqual(name, operation)
            self.assertEqual(args, {**TARGET, **extra, "delivery_mode": "foreground"})
            self.assertEqual(verification_step(step)["operation"], "assert")
    def test_window_keys_are_explicit_and_require_postconditions(self):
        good = {"operation": "press_key", "key": "enter", "key_target": "window",
                "expect": [{"selector": FIELD, "property": "value", "equals": "after"}]}
        for bad in ({**good, "selector": FIELD}, {**good, "key_target": "guessed"},
                    {k: v for k, v in good.items() if k == "operation" or k == "key"},
                    {k: v for k, v in good.items() if k != "expect"},
                    {**self.write(), "key_target": "window"}):
            with self.assertRaises(OperationError):
                validate_step(bad)
    def test_window_key_failure_never_switches_delivery_or_replays(self):
        runtime = Runtime([snapshot(element())], actions=[{"isError": True, "structuredContent": {
            "input_sent": False, "error_code": "background_unavailable"}}])
        step = {"operation": "press_key", "key_target": "window", "key": "enter",
                "expect": [{"selector": FIELD, "property": "value", "equals": "after"}]}
        answer = Operations(runtime).execute(step, TARGET)
        self.assertFalse(answer["task_verified"])
        self.assertEqual([c[0] for c in runtime.calls].count("press_key"), 1)
        self.assertNotIn("bring_to_front", [c[0] for c in runtime.calls])
    def test_window_key_never_sends_after_failed_observation(self):
        runtime = Runtime([RuntimeError("no observation")])
        answer = Operations(runtime).execute({"operation": "press_key", "key_target": "window", "key": "enter",
            "expect": [{"selector": FIELD, "property": "value", "equals": "after"}]}, TARGET)
        self.assertFalse(answer["input_dispatched"])
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state"])

    def test_schema_validation_rejects_arbitrary_code_and_coordinates(self):
        for selector in ({"role": "Edit"}, {"x": 1}, {"name": ""}, {"name": "a", "script": "eval"}):
            with self.assertRaises(OperationError):
                validate_selector(selector)
        with self.assertRaises(OperationError):
            validate_step(dict(self.write(), script="anything"))
        for operation in ("click", "assert"):
            with self.assertRaises(OperationError):
                validate_step({"operation": operation, "selector": FIELD})

    def test_legacy_text_snapshot_with_structured_metrics(self):
        observed = snapshot(element())
        answer = {"structuredContent": {"metrics": {"duration_ms": 1}},
                  "content": [{"type": "text", "text": json.dumps(observed)}]}
        parsed = _payload(answer)
        self.assertEqual(parsed["elements"], observed["elements"])
        self.assertEqual(parsed["metrics"]["duration_ms"], 1)

    def test_structured_snapshot_is_authoritative_over_text_copy(self):
        answer = {"structuredContent": snapshot(element(value="new")),
                  "content": [{"type": "text", "text": json.dumps(snapshot(element(value="old")))}]}
        self.assertEqual(_payload(answer)["elements"][0]["value"], "new")

    def test_success_uses_fresh_token_and_verifies_value(self):
        before = element()
        before["element_token"] = "fresh:9"
        runtime = Runtime([snapshot(before), snapshot(element(value="after"))])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["input_dispatched"])
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state", "set_value", "get_window_state"])
        self.assertEqual(runtime.calls[1][1]["element_token"], "fresh:9")
        self.assertFalse(runtime.calls[0][1]["include_screenshot"])
        self.assertEqual(runtime.calls[0][1]["max_elements"], 600)
        self.assertNotIn("elements", result)

    def test_duplicate_selector_never_mutates(self):
        runtime = Runtime([snapshot(element(), element(1))])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "ambiguous_selector")
        self.assertFalse(result["input_dispatched"])

    def test_failed_verification_not_delivery_success(self):
        runtime = Runtime([snapshot(element()), snapshot(element())])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["task_verified"])
        self.assertTrue(result["input_dispatched"])

    def test_set_value_rejects_combo_hyperlink_readonly_and_unsupported(self):
        samples = [element(role="ComboBox"), element(role="Hyperlink"), element(read_only=True), element(is_read_only=True)]
        unsupported = element()
        unsupported["actions"] = ["text"]
        samples.append(unsupported)
        for item in samples:
            with self.subTest(item=item):
                selector = {"name": item["label"], "role": item["role"]}
                runtime = Runtime([snapshot(item)])
                result = Operations(runtime).execute(dict(self.write(), selector=selector), TARGET)
                self.assertEqual(result["diagnostic"]["code"], "unsupported_control")
                self.assertFalse(result["input_dispatched"])

    def test_missing_false_is_not_false(self):
        item = element()
        step = {"operation": "assert", "expect": [{"selector": FIELD, "property": "selected", "equals": False}], "verification_timeout_ms": 0}
        result = Operations(Runtime([snapshot(item)])).execute(step, TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"][0]["reason"], "property_unavailable")

    def test_missing_null_is_not_null(self):
        item = element()
        del item["value"]
        step = {"operation": "assert", "expect": [{"selector": FIELD, "property": "value", "equals": None}], "verification_timeout_ms": 0}
        result = Operations(Runtime([snapshot(item)])).execute(step, TARGET)
        self.assertEqual(result["status"], "failed")

    def test_expansion_finds_deep_target_and_uses_new_token(self):
        item = element()
        item["element_token"] = "expanded:0"
        runtime = Runtime([snapshot(), snapshot(item), snapshot(element(value="after"))])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(runtime.calls[1][1]["max_elements"], 5000)
        self.assertEqual(runtime.calls[2][1]["element_token"], "expanded:0")

    def test_truncation_expands_and_cannot_hide_duplicate(self):
        runtime = Runtime([snapshot(element(), truncated=True), snapshot(element(), element(1))])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "ambiguous_selector")
        self.assertFalse(result["input_dispatched"])

    def test_persistent_truncation_refuses_action(self):
        runtime = Runtime([snapshot(element(), truncated=True), snapshot(element(), truncated=True)])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "incomplete_observation")
        self.assertFalse(result["input_dispatched"])

    def test_projection_completeness_alone_is_not_walk_truncation(self):
        runtime = Runtime([snapshot(element(), elements_complete=False), snapshot(element(value="after"), elements_complete=False)])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["expanded_observations"], 0)

    def test_select_option_never_set_value_and_uses_new_child_token(self):
        combo = element(name="신청 상태", role="ComboBox", value="전체 상태")
        option = element(1, "신청", "ListItem", "", parent_index=0)
        option["element_token"] = "open:1"
        after = element(name="신청 상태", role="ComboBox", value="신청")
        runtime = Runtime([snapshot(combo), snapshot(combo, option), snapshot(after)])
        result = Operations(runtime).execute({"operation": "select_option", "selector": COMBO, "value": "신청", "verification_timeout_ms": 0}, TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state", "list_windows", "click", "list_windows", "get_window_state", "click", "get_window_state"])
        self.assertEqual(runtime.calls[5][1]["element_token"], "open:1")

    def test_unrelated_option_is_never_clicked(self):
        combo = element(name="신청 상태", role="ComboBox", value="전체 상태")
        unrelated = element(1, "신청", "ListItem", "", parent_index=99)
        observed = snapshot(combo, unrelated)
        runtime = Runtime([snapshot(combo), observed, observed, observed])
        result = Operations(runtime).execute({"operation": "select_option", "selector": COMBO, "value": "신청", "verification_timeout_ms": 0}, TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostic"]["code"], "unsupported_option_structure")
        self.assertEqual(sum(n == "click" for n, _ in runtime.calls), 1)

    def test_duplicate_child_options_refused(self):
        combo = element(name="신청 상태", role="ComboBox", value="전체 상태")
        options = [element(i, "신청", "ListItem", "", parent_index=0) for i in (1, 2)]
        runtime = Runtime([snapshot(combo, *options)])
        result = Operations(runtime).execute({"operation": "select_option", "selector": COMBO, "value": "신청"}, TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["input_dispatched"])

    def test_already_correct_value_is_read_only(self):
        runtime = Runtime([snapshot(element(value="after"))])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(runtime.calls), 1)

    def test_explicit_foreground_only(self):
        checks = [{"selector": FIELD, "property": "value", "equals": "after"}]
        for mode in ("background", "foreground"):
            runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
            result = Operations(runtime).execute({"operation": "click", "selector": FIELD, "expect": checks}, TARGET, mode)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(runtime.calls[1][1]["delivery_mode"], mode)
            self.assertNotIn("bring_to_front", [c[0] for c in runtime.calls])

    def test_mutation_error_reads_but_never_replays_or_claims_success(self):
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))], [RuntimeError("lost response")])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(sum(n == "set_value" for n, _ in runtime.calls), 1)
        self.assertTrue(result["checks"][0]["passed"])
        self.assertFalse(result["task_verified"])

    def test_post_action_read_error_never_replays(self):
        runtime = Runtime([snapshot(element()), RuntimeError("timed out")])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(runtime.calls), 3)
        self.assertEqual(sum(n == "set_value" for n, _ in runtime.calls), 1)

    def test_driver_specific_read_timeout_also_stops_without_duplicate_read(self):
        timeout = {"isError": True, "structuredContent": {"computer_use_guidance": {
            "diagnostic_code": "accessibility_timeout", "next_step": "inspect recovery"}},
            "content": [{"type": "text", "text": "UIA did not respond"}]}
        runtime = Runtime([snapshot(element()), timeout])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostic"]["code"], "accessibility_timeout")
        self.assertEqual(len(runtime.calls), 3)

    def test_stale_action_is_reobserved_without_reusing_handle(self):
        stale = {"isError": True, "structuredContent": {"computer_use_guidance": {
            "diagnostic_code": "stale_observation", "next_step": "reobserve"}},
            "content": [{"type": "text", "text": "stale element token"}]}
        refreshed = element()
        refreshed["element_token"] = "next:4"
        runtime = Runtime([snapshot(element()), snapshot(refreshed)], [stale])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "set_value", "get_window_state"])
        self.assertEqual(result["diagnostic"]["code"], "stale_observation")

    def test_disabled_field_refused_before_dispatch(self):
        item = element()
        item["enabled"] = False
        runtime = Runtime([snapshot(item)])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "disabled_element")
        self.assertFalse(result["input_dispatched"])

    def test_multiple_conditions_all_must_pass(self):
        expected = [{"selector": FIELD, "property": "enabled", "equals": False}]
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
        result = Operations(runtime).execute(self.write(expect=expected), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["checks"][0]["passed"])
        self.assertFalse(result["checks"][1]["passed"])

    def test_polling_is_bounded_and_never_repeats_mutation(self):
        runtime = Runtime([snapshot(element()) for _ in range(8)])
        now = [1.0]
        def advance(seconds):
            now[0] += seconds
        with mock.patch("operations.time.monotonic", side_effect=lambda: now[0]), mock.patch.object(runtime.stop_event, "wait", advance):
            result = Operations(runtime).execute(dict(self.write(), verification_timeout_ms=500), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sum(n == "get_window_state" for n, _ in runtime.calls), 5)
        self.assertEqual(sum(n == "set_value" for n, _ in runtime.calls), 1)

    def test_pre_dispatch_refusal_is_not_input_sent(self):
        refusal = {"isError": True, "structuredContent": {"input_sent": False, "error_code": "background_unavailable"}, "content": [{"type": "text", "text": "unsupported"}]}
        runtime = Runtime([snapshot(element())], [refusal])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(runtime.calls), 2)

    def test_stopped_session_cannot_observe_or_mutate(self):
        runtime = Runtime([])
        runtime.stop_event.set()
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runtime.calls, [])

    def test_stop_after_mutation_prevents_post_read(self):
        runtime = Runtime([snapshot(element())])
        def stop(name, args):
            runtime.stop_event.set()
            return {"ok": True}
        runtime.actions = [stop]
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(runtime.calls), 2)

    def test_wrong_window_snapshot_refused(self):
        runtime = Runtime([dict(snapshot(element()), window_id=14)])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "target_mismatch")

    def test_read_only_resume_includes_all_postconditions(self):
        expected = [{"selector": {"name": "완료", "role": "Text"}, "property": "name", "equals": "완료"}]
        check = verification_step(self.write(expect=expected))
        self.assertEqual(check["operation"], "assert")
        self.assertEqual(len(check["expect"]), 2)
        runtime = Runtime([snapshot(element(value="after"), element(1, "완료", "Text"))])
        result = Operations(runtime).execute(check, TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state"])

    def selection(self, **extra):
        return dict(operation="select_option", selector=COMBO, value="신청", verification_timeout_ms=0, **extra)

    def combo(self, value="전체 상태"):
        return element(name="신청 상태", role="ComboBox", value=value)

    def popup(self, window_id=99, **extra):
        return dict(TARGET, window_id=window_id, owner_window_id=TARGET["window_id"], is_on_screen=True, **extra)

    def test_known_option_order_uses_verified_keys_without_popup(self):
        second = self.combo("대기")
        second["element_token"] = "second:0"
        runtime = Runtime([snapshot(self.combo()), snapshot(second), snapshot(self.combo("신청"))])
        result = Operations(runtime).execute(self.selection(option_order=["전체 상태", "대기", "신청"]), TARGET, "foreground")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["diagnostic"]["selection_strategy"], "confirmed_option_order")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "press_key", "get_window_state", "press_key", "get_window_state"])
        self.assertEqual(runtime.calls[1][1]["key"], "down")
        self.assertEqual(runtime.calls[3][1]["element_token"], "second:0")
        self.assertEqual(runtime.calls[3][1]["delivery_mode"], "foreground")

    def test_known_option_order_up_and_background_no_auto_foreground(self):
        runtime = Runtime([snapshot(self.combo()), snapshot(self.combo("신청"))])
        result = Operations(runtime).execute(self.selection(option_order=["신청", "전체 상태"]), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(runtime.calls[1][1]["key"], "up")
        self.assertEqual(runtime.calls[1][1]["delivery_mode"], "background")

    def test_known_option_order_no_effect_or_unexpected_stops_immediately(self):
        for after in ("전체 상태", "취소", "신청"):
            # Even reaching the final goal by an unexpected two-item jump is
            # not accepted as a correctly verified transition.
            runtime = Runtime([snapshot(self.combo()), snapshot(self.combo(after))])
            result = Operations(runtime).execute(self.selection(option_order=["전체 상태", "대기", "신청"]), TARGET)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["diagnostic"]["code"], "unexpected_selection")
            self.assertEqual(len(runtime.calls), 3)

    def test_known_option_order_missing_current_or_too_far_refuses(self):
        for order, code in ((["대기", "신청"], "option_order_mismatch"),
                            (["전체 상태"] + [str(i) for i in range(12)] + ["신청"], "selection_distance_exceeded")):
            runtime = Runtime([snapshot(self.combo())])
            result = Operations(runtime).execute(self.selection(option_order=order), TARGET)
            self.assertEqual(result["diagnostic"]["code"], code)
            self.assertFalse(result["input_dispatched"])

    def test_known_option_order_validation(self):
        for order in (["신청", "신청"], ["신청", ""], ["신청", None], ["취소"], [], [str(i) for i in range(31)]):
            with self.assertRaises(OperationError):
                validate_step(self.selection(option_order=order))
        with self.assertRaises(OperationError):
            validate_step(dict(self.write(), option_order=["after"]))

    def test_owned_popup_uses_own_fresh_target_then_verifies_parent(self):
        menu = element(0, "선택 목록", "List", "")
        option = element(1, "신청", "ListItem", "", parent_index=0)
        option["element_token"] = "popup:1"
        popup_snapshot = dict(snapshot(menu, option), window_id=99)
        runtime = Runtime([snapshot(self.combo()), popup_snapshot, snapshot(self.combo("신청"))],
                          discoveries=[{"windows": [dict(TARGET, is_on_screen=True)]}, {"windows": [self.popup()]}])
        result = Operations(runtime).execute(self.selection(), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["diagnostic"]["selection_strategy"], "owned_popup")
        self.assertEqual(runtime.calls[4][1]["window_id"], 99)
        self.assertEqual(runtime.calls[5][1]["window_id"], 99)
        self.assertEqual(runtime.calls[5][1]["element_token"], "popup:1")
        self.assertEqual(runtime.calls[6][1]["window_id"], TARGET["window_id"])

    def test_popup_must_be_new_same_pid_visible_and_owned(self):
        scenarios = [([self.popup()], [self.popup()]),
                     ([], [dict(self.popup(), owner_window_id=55)]),
                     ([], [dict(self.popup(), pid=123)]),
                     ([], [dict(self.popup(), is_on_screen=False)])]
        for before, after in scenarios:
            combo = self.combo()
            child = element(1, "신청", "ListItem", "", parent_index=0)
            runtime = Runtime([snapshot(combo), snapshot(combo, child), snapshot(self.combo("신청"))],
                              discoveries=[{"windows": before}, {"windows": after}])
            result = Operations(runtime).execute(self.selection(), TARGET)
            self.assertEqual(result["status"], "verified")
            self.assertTrue(all(args.get("window_id", TARGET["window_id"]) == TARGET["window_id"] for _, args in runtime.calls))

    def test_multiple_new_owned_popups_never_choose_one(self):
        runtime = Runtime([snapshot(self.combo())], discoveries=[{"windows": []}, {"windows": [self.popup(98), self.popup(99)]}])
        result = Operations(runtime).execute(self.selection(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostic"]["code"], "ambiguous_popup")
        self.assertEqual(sum(n == "click" for n, _ in runtime.calls), 1)
        self.assertEqual(sum(n == "get_window_state" for n, _ in runtime.calls), 1)

    def test_owned_popup_observation_failure_does_not_read_blocked_parent(self):
        runtime = Runtime([snapshot(self.combo()), RuntimeError("No window exists")],
                          discoveries=[{"windows": []}, {"windows": [self.popup()]}])
        result = Operations(runtime).execute(self.selection(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(sum(n == "get_window_state" for n, _ in runtime.calls), 2)
        self.assertEqual(runtime.calls[-1][1]["window_id"], 99)

    def test_popup_duplicate_or_unscoped_options_refused(self):
        menu = element(0, "목록", "List", "")
        option = element(1, "신청", "ListItem", "", parent_index=0)
        variants = ((option,), (menu, option, element(2, "신청", "ListItem", "", parent_index=0)))
        for elements in variants:
            runtime = Runtime([snapshot(self.combo()), dict(snapshot(*elements), window_id=99)],
                              discoveries=[{"windows": []}, {"windows": [self.popup()]}])
            result = Operations(runtime).execute(self.selection(), TARGET)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["diagnostic"]["code"], "unsupported_option_structure")
            self.assertEqual(sum(n == "click" for n, _ in runtime.calls), 1)

    def test_adjacent_verified_recipe_reuses_postcondition_observation(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")), snapshot(element(value="next"))])
        engine = Operations(runtime)
        self.assertEqual(engine.execute(self.write(), TARGET)["status"], "verified")
        result = engine.execute(dict(self.write(), value="next"), TARGET, reuse_verified=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["reused_observations"], 1)
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "set_value", "get_window_state", "set_value", "get_window_state"])

    def test_default_standalone_call_always_reads_again(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="next"))])
        engine = Operations(runtime)
        engine.execute(self.write(), TARGET)
        result = engine.execute(dict(self.write(), value="next"), TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["reused_observations"], 0)
        self.assertEqual(len(runtime.calls), 6)

    def test_expired_or_consumed_guard_observation_cannot_be_reused(self):
        for invalidate in ("expired", "consumed"):
            runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="next"))])
            engine = Operations(runtime)
            engine.execute(self.write(), TARGET)
            if invalidate == "expired":
                engine._verified_observation["observed_at"] -= 1
            else:
                runtime.guard.observed_targets.clear()
            result = engine.execute(dict(self.write(), value="next"), TARGET, reuse_verified=True)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["metrics"]["reused_observations"], 0)
            self.assertEqual(len(runtime.calls), 6)

    def test_assert_resume_always_fresh_and_cannot_seed_cache(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="next"))])
        engine = Operations(runtime)
        engine.execute(self.write(), TARGET)
        checked = engine.execute(verification_step(self.write()), TARGET, reuse_verified=True)
        self.assertEqual(checked["metrics"]["reused_observations"], 0)
        result = engine.execute(dict(self.write(), value="next"), TARGET, reuse_verified=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["reused_observations"], 0)

    def test_failed_step_clears_reuse_cache(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="after")), snapshot(element(value="next"))])
        engine = Operations(runtime)
        engine.execute(self.write(), TARGET)
        failed = engine.execute(dict(self.write(), value="not applied"), TARGET, reuse_verified=True)
        self.assertEqual(failed["status"], "failed")
        result = engine.execute(dict(self.write(), value="next"), TARGET, reuse_verified=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["reused_observations"], 0)

    def test_explicit_foreground_missing_selector_activates_once_then_reads(self):
        ready = element()
        ready["element_token"] = "after-focus:1"
        runtime = Runtime([snapshot(), snapshot(ready), snapshot(element(value="after"))])
        result = Operations(runtime).execute(self.write(), TARGET, "foreground")
        self.assertEqual(result["status"], "verified")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "bring_to_front", "get_window_state", "set_value", "get_window_state"])
        self.assertEqual(runtime.calls[1][1], TARGET)
        self.assertEqual(runtime.calls[3][1]["element_token"], "after-focus:1")
        self.assertEqual(result["metrics"]["focus_actions"], 1)
        self.assertEqual(result["metrics"]["expanded_observations"], 0)
        self.assertTrue(result["focus_dispatched"])

    def test_foreground_still_missing_fronts_only_once_before_expansion(self):
        runtime = Runtime([snapshot(), snapshot(), snapshot()])
        result = Operations(runtime).execute(self.write(), TARGET, "foreground")
        self.assertEqual(result["status"], "failed")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "bring_to_front", "get_window_state", "get_window_state"])
        self.assertEqual(runtime.calls[2][1]["max_elements"], 600)
        self.assertEqual(runtime.calls[3][1]["max_elements"], 5000)
        self.assertTrue(result["focus_dispatched"])
        self.assertFalse(result["input_dispatched"])

    def test_background_missing_selector_never_activates(self):
        runtime = Runtime([snapshot(), snapshot()])
        result = Operations(runtime).execute(self.write(), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "get_window_state"])
        self.assertFalse(result["focus_dispatched"])

    def test_foreground_read_error_never_activates(self):
        runtime = Runtime([RuntimeError("UIA timeout")])
        result = Operations(runtime).execute(self.write(), TARGET, "foreground")
        self.assertEqual(result["status"], "failed")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state"])
        self.assertFalse(result["focus_dispatched"])

    def test_foreground_after_business_input_never_activates(self):
        runtime = Runtime([snapshot(element()), snapshot(), snapshot()])
        result = Operations(runtime).execute(self.write(), TARGET, "foreground")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["focus_dispatched"])
        self.assertNotIn("bring_to_front", [n for n, _ in runtime.calls])

    def test_foreground_assert_stays_read_only_when_missing(self):
        runtime = Runtime([snapshot(), snapshot()])
        result = Operations(runtime).execute(verification_step(self.write()), TARGET, "foreground")
        self.assertEqual(result["status"], "failed")
        self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "get_window_state"])
        self.assertFalse(result["focus_dispatched"])

    def test_within_disambiguates_duplicate_buttons_and_checks(self):
        left = element(0, "왼쪽", "Group", "")
        right = element(1, "오른쪽", "Group", "")
        first = element(2, "적용", "Button", "before", parent_index=0)
        second = element(3, "적용", "Button", "before", parent_index=1)
        selector = {"name": "적용", "role": "Button", "within": {"name": "오른쪽", "role": "Group"}}
        after = dict(second, value="done")
        runtime = Runtime([snapshot(left, right, first, second), snapshot(left, right, first, after)])
        step = {"operation": "click", "selector": selector, "expect": [{"selector": selector, "property": "value", "equals": "done"}], "verification_timeout_ms": 0}
        result = Operations(runtime).execute(step, TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(runtime.calls[1][1]["element_token"], "s1:3")
        self.assertEqual(result["checks"][0]["observed"], "done")

    def test_within_can_match_distant_indexed_ancestor(self):
        group = element(0, "상세", "Group", "")
        pane = element(1, "입력 영역", "Pane", "", parent_index=0)
        field = element(2, "시험 문구", parent_index=1)
        selector = dict(FIELD, within={"name": "상세", "role": "Group"})
        self.assertEqual(_unique(snapshot(group, pane, field), selector)["element_index"], 2)

    def test_within_missing_or_ambiguous_never_falls_back_to_global_match(self):
        field = element(2, parent_index=0)
        selector = dict(FIELD, within={"name": "영역", "role": "Group"})
        for elements, code in (((field,), "selector_not_found"),
                               ((element(0, "영역", "Group"), element(1, "영역", "Group"), field), "ambiguous_scope")):
            runtime = Runtime([snapshot(*elements), snapshot(*elements)])
            result = Operations(runtime).execute(dict(self.write(), selector=selector), TARGET)
            self.assertEqual(result["diagnostic"]["code"], code)
            self.assertFalse(result["input_dispatched"])

    def test_within_requires_observed_parent_relationship(self):
        group = element(0, "영역", "Group")
        field = element(1)
        selector = dict(FIELD, within={"name": "영역"})
        with self.assertRaises(OperationError):
            _unique(snapshot(group, field), selector)

    def test_within_rejects_nested_scopes_and_role_only_ancestor(self):
        for within in ({"name": "a", "within": {"name": "b"}}, {"role": "Group"}, "parent"):
            with self.assertRaises(OperationError):
                validate_selector(dict(FIELD, within=within))

    def test_mouse_and_key_operations_require_expected_outcome(self):
        for operation, extra in (("double_click", {}), ("right_click", {}),
                                 ("press_key", {"key": "return"}), ("hotkey", {"keys": ["ctrl", "s"]})):
            with self.subTest(operation=operation), self.assertRaises(OperationError):
                validate_step(dict(operation=operation, selector=FIELD, **extra))

    def test_mouse_and_key_operations_forward_exact_fresh_handle_and_delivery(self):
        expected = [{"selector": FIELD, "property": "value", "equals": "after"}]
        for operation, extra in (("double_click", {}), ("right_click", {}),
                                 ("press_key", {"key": "return", "modifiers": ["ctrl"]}),
                                 ("hotkey", {"keys": ["ctrl", "s"]})):
            with self.subTest(operation=operation):
                runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
                step = dict(operation=operation, selector=FIELD, expect=expected, **extra)
                result = Operations(runtime).execute(step, TARGET, "foreground")
                self.assertEqual(result["status"], "verified")
                self.assertEqual(runtime.calls[1], (operation, dict(TARGET, element_token="s1:0", delivery_mode="foreground", **extra)))

    def test_key_operation_argument_validation(self):
        expect = [{"selector": FIELD, "property": "value", "equals": "after"}]
        invalid = [dict(operation="press_key", key=""), dict(operation="press_key", key="a", modifiers=["ctrl", "ctrl"]),
                   dict(operation="press_key", key="a", modifiers=[None]), dict(operation="hotkey", keys=[]),
                   dict(operation="hotkey", keys=["ctrl", 2]), dict(operation="hotkey", keys=["ctrl", "ctrl", "s"]), dict(operation="click", keys=["x"]),
                   dict(operation="hotkey", keys=["s"], key="s"), dict(operation="click", checked=True)]
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                validate_step(dict(selector=FIELD, expect=expect, **extra))

    def test_key_guard_refusal_is_preserved_without_retry_or_foreground_switch(self):
        refused = {"isError": True, "structuredContent": {"input_sent": False, "error_code": "background_unavailable"},
                   "content": [{"type": "text", "text": "background modifiers refused"}]}
        runtime = Runtime([snapshot(element())], [refused])
        step = {"operation": "hotkey", "selector": FIELD, "keys": ["ctrl", "s"],
                "expect": [{"selector": FIELD, "property": "value", "equals": "after"}]}
        result = Operations(runtime).execute(step, TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "background_unavailable")
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(runtime.calls[1][1]["delivery_mode"], "background")

    def checked_element(self, selected=False, role="CheckBox", actions=None, **extra):
        item = element(name="자동 저장", role=role, selected=selected, **extra)
        item["actions"] = ["toggle"] if actions is None else actions
        return item

    def test_set_checked_verifies_both_on_and_off(self):
        selector = {"name": "자동 저장", "role": "CheckBox"}
        for desired in (True, False):
            runtime = Runtime([snapshot(self.checked_element(not desired)), snapshot(self.checked_element(desired))])
            step = {"operation": "set_checked", "selector": selector, "checked": desired, "verification_timeout_ms": 0}
            result = Operations(runtime).execute(step, TARGET)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["checks"][0]["property"], "selected")
            self.assertIs(result["checks"][0]["observed"], desired)
            self.assertEqual([n for n, _ in runtime.calls], ["get_window_state", "click", "get_window_state"])

    def test_set_checked_already_desired_is_read_only(self):
        runtime = Runtime([snapshot(self.checked_element(True))])
        step = {"operation": "set_checked", "selector": {"name": "자동 저장"}, "checked": True}
        result = Operations(runtime).execute(step, TARGET)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(runtime.calls), 1)

    def test_set_checked_requires_boolean_state_and_toggle_capability(self):
        missing = self.checked_element()
        del missing["selected"]
        invalid = [missing, self.checked_element(None), self.checked_element("off"),
                   self.checked_element(actions=["invoke"]), self.checked_element(role="RadioButton")]
        for item in invalid:
            runtime = Runtime([snapshot(item)])
            step = {"operation": "set_checked", "selector": {"name": "자동 저장"}, "checked": True}
            result = Operations(runtime).execute(step, TARGET)
            self.assertEqual(result["diagnostic"]["code"], "unsupported_control")
            self.assertFalse(result["input_dispatched"])

    def test_set_checked_does_not_repeat_toggle_after_unchanged_value(self):
        runtime = Runtime([snapshot(self.checked_element(False)), snapshot(self.checked_element(False))])
        step = {"operation": "set_checked", "selector": {"name": "자동 저장"}, "checked": True, "verification_timeout_ms": 0}
        result = Operations(runtime).execute(step, TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sum(n == "click" for n, _ in runtime.calls), 1)

    def test_select_item_list_tab_tree_and_radio_observed_capability(self):
        for role in ("ListItem", "TabItem", "TreeItem", "RadioButton"):
            before = self.checked_element(False, role, ["select"])
            after = self.checked_element(True, role, ["select"])
            runtime = Runtime([snapshot(before), snapshot(after)])
            result = Operations(runtime).execute({"operation": "select_item", "selector": {"name": "자동 저장", "role": role}}, TARGET)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["checks"][0]["property"], "selected")

    def test_select_item_missing_selected_is_not_assumed_false(self):
        item = self.checked_element(False, "TabItem", ["select"])
        del item["selected"]
        runtime = Runtime([snapshot(item)])
        result = Operations(runtime).execute({"operation": "select_item", "selector": {"name": "자동 저장"}}, TARGET)
        self.assertEqual(result["diagnostic"]["code"], "unsupported_control")
        self.assertFalse(result["input_dispatched"])

    def test_check_and_item_resume_steps_are_read_only_state_checks(self):
        for operation, extra, value in (("set_checked", {"checked": False}, False), ("select_item", {}, True)):
            step = verification_step(dict(operation=operation, selector=FIELD, **extra))
            self.assertEqual(step["operation"], "assert")
            self.assertEqual(step["expect"], [{"selector": FIELD, "property": "selected", "equals": value}])


if __name__ == "__main__":
    unittest.main()
