"""Deterministic transition lifecycle/dispatch tests; no desktop access."""
import copy
import tempfile
import time
import unittest
from unittest import mock

from operations import OperationError, Operations, validate_step, verification_step
from test_operations import Runtime, TARGET, FIELD, element, snapshot
from test_workflows import FakeRuntime, ScriptedOperations
from workflows import WorkflowRunner


NEW = {"pid": 7, "window_id": 22}


def row(hwnd=12, pid=7, title="Before", owner=0, visible=True):
    return {"pid": pid, "window_id": hwnd, "title": title, "owner_window_id": owner,
            "root_owner_window_id": owner or hwnd, "visible": visible, "class_name": "Fixture"}


def state(*rows, present=False, exited=False, creation=100):
    return {"process_exited": exited, "target_present": present, "creation_time": creation,
            "executable": "C:/fixture.exe", "windows": list(rows)}


class Probe:
    def __init__(self, initial=None, current=None):
        self.initial = initial or state(row(), present=True)
        self.current = current or state(row(22, title="After"))
        self.closed = False
        self.reads = 0

    def capture(self):
        return copy.deepcopy(self.initial)

    def snapshot(self):
        self.reads += 1
        if isinstance(self.current, Exception):
            raise self.current
        if callable(self.current):
            return self.current(self.reads)
        return copy.deepcopy(self.current)

    def close(self):
        self.closed = True


class TransitionTests(unittest.TestCase):
    def step(self, **extra):
        return {"operation": "click", "selector": FIELD,
                "expect": [{"selector": FIELD, "property": "value", "equals": "after"}],
                "verification_timeout_ms": 0, **extra}

    def runtime(self, probe=None, after="after"):
        runtime = Runtime([snapshot(element()), {**snapshot(element(value=after)), **NEW}])
        runtime.probe = probe or Probe()
        runtime.create_transition_probe = lambda target: runtime.probe
        return runtime

    def test_default_recovers_replaced_window_without_old_uia_read_or_input_replay(self):
        runtime = self.runtime()
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["transition"]["state"], "verified")
        self.assertEqual(result["target"], NEW)
        self.assertEqual([name for name, args in runtime.calls], ["get_window_state", "click", "get_window_state"])
        self.assertEqual(runtime.calls[-1][1]["window_id"], 22)
        self.assertTrue(runtime.probe.closed)
        self.assertGreaterEqual(result["metrics"]["transition_probe_ms"], 0)

    def test_unique_window_does_not_substitute_for_completion_assertions(self):
        runtime = self.runtime(after="not done")
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["transition"]["state"], "unverified")
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_target_unavailable_race_recovers_once_after_failed_postread(self):
        probe = Probe()
        probe.current = lambda n: state(row(), present=True) if n == 1 else state(row(22, title="After"))
        runtime = self.runtime(probe)
        runtime.observations.insert(1, {"isError": True, "structuredContent": {"error_code": "target_unavailable"}})
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["metrics"]["mutations"], 1)
        self.assertEqual(len(runtime.calls), 4)

    def test_transition_read_beyond_verification_deadline_stops(self):
        runtime = self.runtime()
        def slow_read(name, args):
            time.sleep(.03)
            return {**snapshot(element(value="after")), **NEW}
        runtime.observations[-1] = slow_read
        step = self.step()
        step["verification_timeout_ms"] = 5
        result = Operations(runtime).execute(step, TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "transition_timeout")
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_explicit_new_window_observes_owned_popup_without_closing_parent(self):
        probe = Probe(current=state(row(), row(22, title="Dialog", owner=12), present=True))
        runtime = self.runtime(probe)
        result = Operations(runtime).execute(self.step(window_transition={"mode": "new_window", "title": "Dialog"}), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertNotIn("bring_to_front", [name for name, args in runtime.calls])

    def test_preexisting_visible_popup_is_not_a_new_window(self):
        original = state(row(), row(22, title="Dialog", owner=12), present=True)
        runtime = self.runtime(Probe(initial=original, current=original))
        result = Operations(runtime).execute(self.step(window_transition={"mode": "new_window", "title": "Dialog", "timeout_ms": 0}), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "transition_timeout")
        self.assertEqual(len(runtime.calls), 2)

    def test_exact_new_window_allows_hidden_precreated_window_becoming_visible(self):
        initial = state(row(), row(22, title="After", visible=False), present=True)
        runtime = self.runtime(Probe(initial=initial, current=state(row(22, title="After"))))
        result = Operations(runtime).execute(self.step(window_transition={"mode": "new_window", "title": "After"}), TARGET)
        self.assertTrue(result["task_verified"])

    def test_final_identity_read_after_deadline_never_passes(self):
        runtime = self.runtime()
        def native_read(n):
            if n == 5:
                time.sleep(.025)
            return state(row(22, title="After"))
        runtime.probe.current = native_read
        step = self.step()
        step["verification_timeout_ms"] = 10
        result = Operations(runtime).execute(step, TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "verification_timeout")

    def test_old_modal_can_return_to_its_existing_owner(self):
        runtime = self.runtime(Probe(initial=state(row(owner=22), row(22, title="Parent"), present=True),
                                     current=state(row(22, title="Parent"))))
        self.assertTrue(Operations(runtime).execute(self.step(), TARGET)["task_verified"])

    def test_ambiguous_windows_stop_before_candidate_uia_read(self):
        runtime = self.runtime(Probe(current=state(row(22, title="After"), row(23, title="After"))))
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertEqual(result["status"], "needs_target")
        self.assertEqual(result["diagnostic"]["code"], "transition_ambiguous")
        self.assertEqual(len(result["transition"]["candidates"]), 2)
        self.assertEqual(len(runtime.calls), 2)

    def test_wrong_process_or_unrelated_existing_window_never_used(self):
        for probe in (Probe(current=state(row(22, pid=88))),
                      Probe(initial=state(row(), row(22), present=True), current=state(row(22)))):
            with self.subTest(probe=probe):
                runtime = self.runtime(probe)
                result = Operations(runtime).execute(self.step(window_transition={"mode": "auto", "timeout_ms": 0}), TARGET)
                self.assertEqual(result["status"], "needs_target")
                self.assertEqual(len(runtime.calls), 2)

    def test_process_exit_or_reuse_is_not_success(self):
        for current, expected in ((state(exited=True), "transition_process_exited"),
                                  (state(row(22), creation=101), "transition_process_changed")):
            runtime = self.runtime(Probe(current=current))
            result = Operations(runtime).execute(self.step(), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertEqual(result["diagnostic"]["code"], expected)
            self.assertEqual(len(runtime.calls), 2)
            self.assertTrue(runtime.probe.closed)

    def test_same_window_does_not_prepare_or_recover(self):
        runtime = self.runtime()
        runtime.observations[-1] = {"isError": True, "structuredContent": {"error_code": "target_unavailable"}}
        result = Operations(runtime).execute(self.step(window_transition={"mode": "same_window"}), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "target_unavailable")
        self.assertNotIn("transition", result)
        self.assertEqual(runtime.probe.reads, 0)

    def test_no_retained_identity_new_window_refuses_before_click(self):
        runtime = Runtime([snapshot(element())])
        result = Operations(runtime).execute(self.step(window_transition={"mode": "new_window", "title": "After"}), TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "transition_identity_unavailable")

    def test_cancellation_during_transition_wait_never_replays_input(self):
        runtime = self.runtime(Probe(current=state()))
        runtime.stop_event.wait = lambda timeout: runtime.stop_event.set()
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["metrics"]["mutations"], 1)
        self.assertTrue(runtime.probe.closed)

    def test_wait_deadline_is_bounded(self):
        runtime = self.runtime(Probe(current=state()))
        result = Operations(runtime).execute(self.step(window_transition={"mode": "auto", "timeout_ms": 5}), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "transition_timeout")
        self.assertLess(result["metrics"]["elapsed_ms"], 500)

    def test_target_changes_during_fresh_driver_read(self):
        runtime = self.runtime()
        def changed(name, args):
            runtime.probe.current = state(row(23, title="Different"))
            return {**snapshot(element(value="after")), **NEW}
        runtime.observations[-1] = changed
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "transition_target_changed")

    def test_driver_delivery_facts_reported_without_pointer_inference(self):
        runtime = self.runtime()
        runtime.actions = [{"backend": "uia", "delivery_mode": "background", "input_sent": True}]
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertEqual(result["delivery"]["actions"][0]["reported_backend"], "uia")
        self.assertEqual(result["delivery"]["pointer_movement"], "not_reported")

    def test_actual_driver_foreground_acknowledgement_failure_after_click_can_verify_new_window(self):
        runtime = self.runtime()
        runtime.actions = [{"isError": True, "structuredContent": {"error_code": "target_denied",
            "computer_use_guidance": {"diagnostic_code": "target_denied", "diagnostic":
                "foreground_unavailable: exact target HWND 0x123 or a verified same-process post-action window was not foreground after the click (actual foreground HWND 0x456)"}}}]
        result = Operations(runtime).execute(self.step(), TARGET, delivery_mode="foreground")
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "verified_after_window_transition")
        self.assertEqual(result["diagnostic"]["action_acknowledgement_error"]["code"], "target_denied")
        self.assertEqual([name for name, args in runtime.calls].count("click"), 1)
        self.assertEqual(result["metrics"]["focus_actions"], 0)

    def test_explicit_no_input_or_unrelated_denial_never_claims_recovered_click(self):
        for payload in ({"input_sent": False, "error_code": "target_unavailable"},
                        {"error_code": "target_denied", "computer_use_guidance": {"diagnostic_code": "permission_denied"}},
                        {"error_code": "target_denied", "computer_use_guidance": {"diagnostic_code": "target_denied",
                            "diagnostic": "foreground_unavailable: exact target HWND 0x123 could not be activated before input"}}):
            runtime = self.runtime()
            runtime.actions = [{"isError": True, "structuredContent": payload}]
            result = Operations(runtime).execute(self.step(), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertNotIn("action_acknowledgement_error", result["diagnostic"])
            self.assertEqual([name for name, args in runtime.calls].count("click"), 1)

    def test_same_hwnd_changed_class_or_thread_is_not_reused(self):
        for field, value in (("class_name", "Replacement"), ("thread_id", 555)):
            changed = row()
            changed[field] = value
            runtime = self.runtime(Probe(current=state(changed, present=True)))
            result = Operations(runtime).execute(self.step(), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertEqual(result["diagnostic"]["code"], "transition_target_identity_changed")

    def test_delayed_replacement_during_scoped_poll_is_recovered_read_only(self):
        probe = Probe()
        probe.current = lambda n: state(row(), present=True) if n == 1 else state(row(22, title="After"))
        runtime = self.runtime(probe)
        scoped_reads = []
        def scoped(target, selectors, **kwargs):
            scoped_reads.append(target)
            return {"structuredContent": {**snapshot(element(value="before")), "read_only": True,
                                          "scoped_observation": True, "scope_complete": True}}
        runtime.observe_controls = scoped
        step = self.step()
        step["verification_timeout_ms"] = 1500
        result = Operations(runtime).execute(step, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(len(scoped_reads), 1)
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_scoped_guard_failure_during_read_requires_native_disappearance(self):
        probe = Probe()
        probe.current = lambda n: state(row(), present=True) if n == 1 else state(row(22, title="After"))
        runtime = self.runtime(probe)
        def scoped(*args, **kwargs):
            raise RuntimeError("window disappeared during scoped read")
        runtime.observe_controls = scoped
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["metrics"]["mutations"], 1)

    def test_resume_assertion_does_not_require_transition_again(self):
        step = verification_step(self.step(window_transition={"mode": "new_window", "title": "After"}))
        self.assertNotIn("window_transition", step)
        runtime = Runtime([{**snapshot(element(value="after")), **NEW}])
        result = Operations(runtime).execute(step, NEW)
        self.assertTrue(result["task_verified"])
        self.assertFalse(result["input_dispatched"])

    def test_invalid_modes_cannot_treat_disappearance_as_closed(self):
        for spec in ({"mode": "closed"}, {"mode": "new_window"}, {"mode": "new_window", "title": " "},
                     {"mode": "auto", "timeout_ms": True}, {"mode": "auto", "pid": 99}):
            with self.assertRaises(OperationError):
                validate_step(self.step(window_transition=spec))


class TransitionWorkflowTests(unittest.TestCase):
    def test_verified_transition_binding_propagates_and_resume_uses_fresh_target(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = FakeRuntime(folder)
            runner = WorkflowRunner(folder)
            first = {"program_id": "editor", "operation": "click", "selector": FIELD,
                     "expect": [{"selector": FIELD, "property": "value", "equals": "after"}],
                     "window_transition": {"mode": "new_window", "title": "After"}}
            task = {"id": "transition", "revision": 1, "program_ids": ["editor"], "variables": {},
                    "steps": [first, {"program_id": "editor", "operation": "set_value", "selector": FIELD, "value": "done"}]}
            engine = ScriptedOperations([{"task_verified": True, "transition": {"state": "verified", "target": {"pid": 100, "window_id": 201}}},
                                         {"task_verified": False}])
            with mock.patch("operations.Operations", return_value=engine):
                answer = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 200}])
            self.assertEqual(engine.calls[1][1], {"pid": 100, "window_id": 201})
            self.assertEqual(answer["completed_steps"], 1)
            continued = ScriptedOperations()
            with mock.patch("operations.Operations", return_value=continued):
                result = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 201}], resume_run_id=answer["run_id"])
            self.assertTrue(result["task_verified"])
            self.assertEqual([call[0]["operation"] for call in continued.calls], ["assert"])

    def test_uncertain_transition_resume_only_checks_new_window_assertions(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = FakeRuntime(folder)
            runner = WorkflowRunner(folder)
            first = {"program_id": "editor", "operation": "click", "selector": FIELD,
                     "expect": [{"selector": FIELD, "property": "value", "equals": "after"}],
                     "window_transition": {"mode": "new_window", "title": "After"}}
            task = {"id": "transition", "revision": 1, "program_ids": ["editor"], "variables": {}, "steps": [first]}
            uncertain = ScriptedOperations([{"task_verified": False, "status": "needs_target"}])
            with mock.patch("operations.Operations", return_value=uncertain):
                answer = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 200}])
            continued = ScriptedOperations()
            with mock.patch("operations.Operations", return_value=continued):
                result = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 201}], resume_run_id=answer["run_id"])
            self.assertTrue(result["task_verified"])
            self.assertEqual(len(continued.calls), 1)
            self.assertEqual(continued.calls[0][0]["operation"], "assert")
            self.assertNotIn("window_transition", continued.calls[0][0])


if __name__ == "__main__":
    unittest.main()
