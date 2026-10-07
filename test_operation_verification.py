"""Deadlines, fresh completion evidence and explicitly configured combo input."""
import copy
import unittest
from unittest import mock

from operations import OperationError, Operations, validate_step, verification_step
from process_steps import execute_process_step, validate_process_step
from test_operations import TARGET, FIELD, COMBO, Runtime, GuardedRuntime, element, snapshot


class Clock:
    def __init__(self):
        self.now = 1.0
        self.waits = []

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def check(value="after", **extra):
    return {"selector": FIELD, "property": "value", "equals": value, **extra}


def write(**extra):
    return {"operation": "set_value", "selector": FIELD, "value": "after",
            "verification_timeout_ms": 1000, "poll_interval_ms": 100, **extra}


class DeadlineTests(unittest.TestCase):
    def timed(self, runtime, step, **kwargs):
        clock = Clock()
        with mock.patch("operations.time.monotonic", clock.monotonic), mock.patch.object(runtime.stop_event, "wait", clock.wait):
            answer = Operations(runtime).execute(step, TARGET, **kwargs)
        return answer, clock

    def test_fifth_post_read_can_succeed_without_second_mutation(self):
        runtime = Runtime([snapshot(element())] * 5 + [snapshot(element(value="after"))])
        result, clock = self.timed(runtime, write())
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["metrics"]["observations"], 6)
        self.assertEqual(result["metrics"]["mutations"], 1)
        self.assertEqual(len(clock.waits), 4)

    def test_satisfied_condition_has_no_poll_delay(self):
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
        result, clock = self.timed(runtime, write(verification_timeout_ms=60000))
        self.assertTrue(result["task_verified"])
        self.assertEqual(clock.waits, [])

    def test_polling_stops_at_deadline_without_extra_read_or_input(self):
        runtime = Runtime([snapshot(element())] * 30)
        result, clock = self.timed(runtime, write(verification_timeout_ms=450))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["observations"], 6)  # initial + t=0,.1,.2,.3,.4
        self.assertAlmostEqual(sum(clock.waits), .45)
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_cancel_during_poll_prevents_next_observation(self):
        runtime = Runtime([snapshot(element())] * 5)
        with mock.patch.object(runtime.stop_event, "wait", side_effect=lambda _: runtime.stop_event.set()):
            result = Operations(runtime).execute(write(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["metrics"]["observations"], 2)
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_entire_step_deadline_prevents_late_initial_input(self):
        clock = Clock()
        def slow_read(name, args):
            clock.now += .6
            return snapshot(element())
        runtime = Runtime([slow_read])
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(write(step_timeout_ms=500), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "step_timeout")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state"])

    def test_late_input_response_is_unknown_without_recovery_or_replay(self):
        clock = Clock()
        def slow_write(name, args):
            clock.now += .6
            return {"ok": True}
        runtime = Runtime([snapshot(element())], [slow_write])
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(write(step_timeout_ms=500), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostic"]["code"], "step_timeout")
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value"])

    def test_late_approval_guard_refusal_retains_explicit_no_input_evidence(self):
        clock = Clock()
        def approval_expired(name, args):
            clock.now += .6
            return {"isError": True, "structuredContent": {"input_sent": False, "error_code": "step_timeout"}}
        runtime = Runtime([snapshot(element())], [approval_expired])
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(write(step_timeout_ms=500), TARGET)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "step_timeout")
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value"])

    def test_remaining_budget_is_forwarded_to_runtime(self):
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
        runtime.call_with_timeout = mock.Mock(side_effect=lambda name, args, timeout_ms: runtime.call(name, args))
        result, _ = self.timed(runtime, write(step_timeout_ms=1700, verification_timeout_ms=900))
        self.assertTrue(result["task_verified"])
        self.assertEqual([c.kwargs["timeout_ms"] for c in runtime.call_with_timeout.call_args_list], [1700, 1700, 1700])

    def test_late_verified_snapshot_is_not_accepted_after_verification_deadline(self):
        clock = Clock()
        def slow_verify(name, args):
            clock.now += .6
            return snapshot(element(value="after"))
        runtime = Runtime([snapshot(element()), slow_verify])
        runtime.call_with_timeout = mock.Mock(side_effect=lambda name, args, timeout_ms: runtime.call(name, args))
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(write(verification_timeout_ms=500), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "verification_timeout")
        self.assertEqual(result["metrics"]["observations"], 2)
        self.assertGreater(runtime.call_with_timeout.call_args.kwargs["timeout_ms"], 500)

    def test_normal_slow_uia_read_passes_default_without_poll_sleep(self):
        clock = Clock()
        def read(value):
            def inner(name, args):
                clock.now += 1.3
                return snapshot(element(value=value))
            return inner
        runtime = Runtime([read("before"), read("after")])
        with mock.patch("operations.time.monotonic", clock.monotonic), mock.patch.object(runtime.stop_event, "wait", clock.wait):
            result = Operations(runtime).execute({"operation": "set_value", "selector": FIELD, "value": "after"}, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(clock.waits, [])
        self.assertEqual(verification_step(write()).get("verification_timeout_ms"), 1000)
        self.assertEqual(verification_step({"operation": "set_value", "selector": FIELD, "value": "after"})["verification_timeout_ms"], 10000)

    def test_late_shallow_read_does_not_start_deep_expansion(self):
        clock = Clock()
        def slow_incomplete(name, args):
            clock.now += .6
            return snapshot(element(value="after"), truncated=True)
        runtime = Runtime([snapshot(element()), slow_incomplete])
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(write(verification_timeout_ms=500), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "verification_timeout")
        self.assertEqual(result["metrics"]["expanded_observations"], 0)

    def test_step_deadline_can_end_polling_before_longer_verification_timeout(self):
        runtime = Runtime([snapshot(element())] * 10)
        answer, clock = self.timed(runtime, write(step_timeout_ms=250, verification_timeout_ms=1000))
        self.assertEqual(answer["diagnostic"]["code"], "step_timeout")
        self.assertFalse(answer["task_verified"])
        self.assertAlmostEqual(sum(clock.waits), .25)
        self.assertEqual(answer["metrics"]["mutations"], 1)


class FreshChangeTests(unittest.TestCase):
    def click(self, **extra):
        return {"operation": "click", "selector": FIELD, "expect": [check("done", require_change=True)],
                "verification_timeout_ms": 0, **extra}

    def test_old_done_state_is_not_new_action_success(self):
        runtime = Runtime([snapshot(element(value="done"))] * 2)
        result = Operations(runtime).execute(self.click(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["checks"][0]["reason"], "change_not_observed")
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_done_loading_done_observed_transition_can_pass(self):
        runtime = Runtime([snapshot(element(value=v)) for v in ("done", "loading", "done")])
        clock = Clock()
        with mock.patch("operations.time.monotonic", clock.monotonic), mock.patch.object(runtime.stop_event, "wait", clock.wait):
            result = Operations(runtime).execute(self.click(verification_timeout_ms=1000), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertTrue(result["checks"][0]["change_observed"])
        self.assertNotIn("baseline", str(result))

    def test_missing_baseline_property_prevents_first_input(self):
        row = element()
        row.pop("value")
        runtime = Runtime([snapshot(row)])
        result = Operations(runtime).execute(self.click(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "change_baseline_unavailable")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(runtime.calls), 1)

    def test_resume_preserves_change_rule_and_never_invents_baseline(self):
        step = verification_step(self.click())
        runtime = Runtime([snapshot(element(value="done"))])
        result = Operations(runtime).execute(step, TARGET)
        self.assertTrue(step["expect"][0]["require_change"])
        self.assertEqual(result["diagnostic"]["code"], "change_baseline_required")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.calls, [])

    def test_change_assertions_disable_previous_step_snapshot_reuse(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element(value="after")),
                                  snapshot(element(value="done")), snapshot(element(value="done"))])
        engine = Operations(runtime)
        self.assertTrue(engine.execute(write(verification_timeout_ms=0), TARGET)["task_verified"])
        answer = engine.execute(self.click(), TARGET, reuse_verified=True)
        self.assertEqual(answer["metrics"]["reused_observations"], 0)
        self.assertFalse(answer["task_verified"])

    def test_already_checked_is_never_toggled_off_to_force_change(self):
        row = {**element(role="CheckBox"), "selected": True, "actions": ["toggle"]}
        selector = {"name": "시험 문구", "role": "CheckBox"}
        runtime = Runtime([snapshot(row)])
        result = Operations(runtime).execute({"operation": "set_checked", "selector": selector, "checked": True,
            "verification_timeout_ms": 0, "expect": [{"selector": selector, "property": "selected", "equals": True, "require_change": True}]}, TARGET)
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["input_dispatched"])


class ScopedObservationTests(unittest.TestCase):
    def scoped(self, value="after", **extra):
        return {"structuredContent": {**snapshot(element(value=value)), "read_only": True,
                                      "scoped_observation": True, "scope_complete": True, **extra}}

    def test_native_property_read_is_only_post_input_and_never_cached_for_input(self):
        runtime = GuardedRuntime([snapshot(element()), snapshot(element())])
        runtime.observe_controls = mock.Mock(side_effect=[self.scoped(), self.scoped()])
        engine = Operations(runtime)
        for _ in range(2):
            result = engine.execute(write(verification_timeout_ms=0), TARGET, reuse_verified=True)
            self.assertTrue(result["task_verified"])
            self.assertEqual(result["metrics"]["scoped_observations"], 1)
            self.assertEqual(result["metrics"]["reused_observations"], 0)
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value"] * 2)
        self.assertEqual(runtime.observe_controls.call_args.args, (TARGET, [FIELD]))

    def test_disabled_hook_falls_back_to_driver(self):
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
        runtime.observe_controls = mock.Mock(side_effect=NotImplementedError)
        answer = Operations(runtime).execute(write(verification_timeout_ms=0), TARGET)
        self.assertTrue(answer["task_verified"])
        self.assertEqual(answer["metrics"]["scoped_observations"], 0)
        self.assertEqual(answer["metrics"]["observations"], 2)

    def test_failed_incomplete_or_foreign_scope_never_falls_back(self):
        for answer in (self.scoped(scope_complete=False), self.scoped(window_id=999), self.scoped(read_only=False),
                       {"isError": True, "structuredContent": {"error_code": "access_denied"}}):
            runtime = Runtime([snapshot(element())])
            runtime.observe_controls = mock.Mock(return_value=answer)
            result = Operations(runtime).execute(write(verification_timeout_ms=0), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value"])

    def test_scoped_exception_never_falls_back(self):
        runtime = Runtime([snapshot(element())])
        runtime.observe_controls = mock.Mock(side_effect=RuntimeError("access denied"))
        result = Operations(runtime).execute(write(verification_timeout_ms=0), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(result["metrics"]["scoped_observations"], 1)

    def test_malformed_or_ambiguous_scope_is_not_repolled_or_recovered(self):
        for elements in (None, [element(value="after"), element(value="after")]):
            runtime = Runtime([snapshot(element())])
            runtime.observe_controls = mock.Mock(return_value=self.scoped(elements=elements))
            result = Operations(runtime).execute(write(), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertEqual(len(runtime.calls), 2)
            self.assertEqual(runtime.observe_controls.call_count, 1)

    def test_native_read_only_tokens_cannot_dispatch_even_when_provider_sends_them(self):
        runtime = Runtime([self.scoped("before")["structuredContent"]])
        result = Operations(runtime).execute(write(verification_timeout_ms=0), TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "read_only_observation")


class EditableComboTests(unittest.TestCase):
    EDIT = {"name": "선택값 입력", "role": "Edit", "within": COMBO}

    def combo(self, parent="전체", child="전체", *, parent_index=1, **extra):
        p = element(index=1, name="신청 상태", role="ComboBox", value=parent)
        e = {**element(index=2, name="선택값 입력", value=child, parent_index=parent_index), **extra}
        return snapshot(p, e)

    def step(self, **extra):
        return {"operation": "select_option", "selector": COMBO, "value": "신청",
                "strategy": "edit_commit", "edit_selector": self.EDIT, "commit_key": "ENTER",
                "verification_timeout_ms": 0, **extra}

    def test_edit_then_commit_uses_fresh_child_handle_and_verifies_parent_value(self):
        middle = self.combo(child="신청", element_token="s2:2")
        runtime = Runtime([self.combo(), middle, self.combo(parent="신청", child="신청")])
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["diagnostic"]["selection_strategy"], "edit_commit")
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value", "get_window_state", "press_key", "get_window_state"])
        self.assertEqual(runtime.calls[1][1]["element_token"], "s1:2")
        self.assertEqual(runtime.calls[3][1]["element_token"], "s2:2")
        self.assertEqual(runtime.calls[3][1]["key"], "ENTER")

    def test_edit_only_success_is_not_parent_selection_success(self):
        runtime = Runtime([self.combo(), self.combo(child="신청"), self.combo(child="신청")])
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["checks"][0]["observed"], "전체")

    def test_unconfirmed_child_value_stops_before_commit_without_fallback(self):
        runtime = Runtime([self.combo(), self.combo(), self.combo()])
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "combo_edit_value_unconfirmed")
        self.assertEqual([name for name, _ in runtime.calls].count("set_value"), 1)
        self.assertNotIn("press_key", [name for name, _ in runtime.calls])
        self.assertNotIn("click", [name for name, _ in runtime.calls])

    def test_unrelated_edit_or_unsupported_capabilities_never_send_input(self):
        invalid = [self.combo(parent_index=9), self.combo(enabled=False), self.combo(read_only=True),
                   self.combo(is_read_only=True), self.combo(is_password=True), self.combo(actions=[]), self.combo(actions=None)]
        for row in invalid:
            runtime = Runtime([row, row])
            result = Operations(runtime).execute(self.step(), TARGET)
            self.assertFalse(result["input_dispatched"])
            self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_unscoped_edit_still_must_be_actual_descendant(self):
        runtime = Runtime([self.combo(parent_index=9)])
        result = Operations(runtime).execute(self.step(edit_selector={"name": "선택값 입력"}), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "combo_edit_not_descendant")
        self.assertFalse(result["input_dispatched"])

    def test_changed_child_relationship_prevents_commit(self):
        runtime = Runtime([self.combo(), self.combo(child="신청", parent_index=9), self.combo(child="신청", parent_index=9)])
        result = Operations(runtime).execute(self.step(edit_selector={"name": "선택값 입력"}), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertNotIn("press_key", [name for name, _ in runtime.calls])

    def test_commit_error_is_never_repeated(self):
        runtime = Runtime([self.combo(), self.combo(child="신청"), self.combo(parent="신청", child="신청")],
                          [{"ok": True}, RuntimeError("unconfirmed commit")])
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual([name for name, _ in runtime.calls].count("press_key"), 1)
        self.assertEqual([name for name, _ in runtime.calls].count("set_value"), 1)

    def test_late_commit_refusal_does_not_erase_earlier_value_input(self):
        clock = Clock()
        def approval_expired(name, args):
            clock.now += .6
            return {"isError": True, "structuredContent": {"input_sent": False, "error_code": "step_timeout"}}
        runtime = Runtime([self.combo(), self.combo(child="신청")], [{"ok": True}, approval_expired])
        with mock.patch("operations.time.monotonic", clock.monotonic):
            result = Operations(runtime).execute(self.step(step_timeout_ms=500), TARGET)
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostic"]["code"], "step_timeout")
        self.assertEqual([name for name, _ in runtime.calls], ["get_window_state", "set_value", "get_window_state", "press_key"])

    def test_schema_requires_explicit_strategy_child_and_supported_commit(self):
        good = self.step()
        for bad in ({k: v for k, v in good.items() if k != "strategy"},
                    {k: v for k, v in good.items() if k != "edit_selector"},
                    {**good, "commit_key": "SPACE"}, {**good, "option_order": ["전체", "신청"]},
                    {**good, "operation": "set_value"}):
            with self.assertRaises(OperationError):
                validate_step(bad)


class PropertyWaitTests(unittest.TestCase):
    def wait(self, runtime, **extra):
        return execute_process_step(runtime, {"operation": "wait_for_state", "expect": [check()],
            "timeout_ms": 1000, "poll_interval_ms": 100, **extra}, TARGET)

    def test_property_condition_can_complete_after_more_than_three_reads(self):
        runtime = Runtime([snapshot(element())] * 4 + [snapshot(element(value="after"))])
        clock = Clock()
        with mock.patch("operations.time.monotonic", clock.monotonic), mock.patch.object(runtime.stop_event, "wait", clock.wait):
            result = self.wait(runtime)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["operation"], "wait_for_state")
        self.assertEqual(result["metrics"]["observations"], 5)
        self.assertFalse(result["input_dispatched"])

    def test_zero_wait_reads_once_and_cannot_invent_change(self):
        runtime = Runtime([snapshot(element(value="after"))])
        result = self.wait(runtime, timeout_ms=0, expect=[check(require_change=True)])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "state_wait_timeout")
        self.assertEqual(len(runtime.calls), 1)

    def test_wait_change_is_relative_to_its_own_start(self):
        runtime = Runtime([snapshot(element()), snapshot(element(value="after"))])
        clock = Clock()
        with mock.patch("operations.time.monotonic", clock.monotonic), mock.patch.object(runtime.stop_event, "wait", clock.wait):
            result = self.wait(runtime, expect=[check(require_change=True)])
        self.assertTrue(result["task_verified"])
        self.assertFalse(result["input_dispatched"])

    def test_wait_cancel_stops_before_next_read(self):
        runtime = Runtime([snapshot(element())])
        with mock.patch.object(runtime.stop_event, "wait", side_effect=lambda _: runtime.stop_event.set()):
            result = self.wait(runtime)
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(runtime.calls), 1)

    def test_wait_foreign_snapshot_never_passes(self):
        row = snapshot(element(value="after"))
        row["window_id"] = 999
        result = self.wait(Runtime([row]))
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "target_mismatch")

    def test_wait_and_verification_schema_reject_unbounded_or_ambiguous_values(self):
        for extra in ({"timeout_ms": True}, {"timeout_ms": 60001}, {"poll_interval_ms": 99},
                      {"expect": [check(require_change=1)]}, {"expect": []}, {"selector": FIELD}):
            with self.assertRaises(OperationError):
                validate_process_step({"operation": "wait_for_state", "expect": [check()], "timeout_ms": 0, **extra})
        for extra in ({"verification_timeout_ms": 60001}, {"step_timeout_ms": True},
                      {"step_timeout_ms": 120001}, {"poll_interval_ms": 2001}):
            with self.assertRaises(OperationError):
                validate_step(write(**extra))


if __name__ == "__main__":
    unittest.main()
