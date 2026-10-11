"""Large finite input expansion and saved-process continuation, no desktop I/O."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from server import ComputerManager
from session_runtime import SessionError, SessionRuntime
from task_inputs import expand, InputError, validate_spec
from test_easy_api import Runtime
from test_server import config_at, FakeLease, FakeEmergency, FakeTransport, TestGuard


class CapacityRuntime(Runtime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_cause = ""
        self.guard.window_resolver = lambda hwnd: 100

    def stop(self, reason, *, cause="requested_or_error"):
        if cause == "requested_or_error" or self.stop_cause != "requested_or_error":
            self.stop_cause = cause
        super().stop(reason)


class InputCapacityTests(unittest.TestCase):
    def test_large_group_rejected_before_payload_expansion(self):
        specs = {"rows": {"type": "list", "items": {"type": "text"}}}
        body = [{"operation": "set_value", "value": {"input_ref": "row"}} for _ in range(11)]
        with mock.patch("task_inputs.substitute_step") as render:
            with self.assertRaisesRegex(InputError, "10,000"):
                expand([{"operation": "foreach", "input": "rows", "item": "row", "steps": body}], specs,
                       {"rows": ["value"] * 1000})
        render.assert_not_called()

    def test_expansion_budget_counts_all_groups(self):
        specs = {"rows": {"type": "list", "items": {"type": "text"}}}
        group = {"operation": "foreach", "input": "rows", "item": "row",
                 "steps": [{"operation": "delay", "duration_ms": 0} for _ in range(5)]}
        steps = expand([group, copy.deepcopy(group)], specs, {"rows": ["x"] * 1000})
        self.assertEqual(len(steps), 10000)
        self.assertEqual(steps[-1]["iteration_id"], "group-2:1000")
        with self.assertRaises(InputError):
            expand([group, copy.deepcopy(group), {"operation": "delay", "duration_ms": 0}], specs,
                   {"rows": ["x"] * 1000})

    def test_explicit_smaller_list_bound_is_preserved(self):
        spec = {"type": "list", "items": {"type": "text"}, "max_items": 20}
        self.assertEqual(validate_spec(spec, "rows")["max_items"], 20)
        with self.assertRaises(InputError): validate_spec({**spec, "max_items": 1001}, "rows")


class SavedCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        chunks = mock.patch("workflows.RUN_CHUNK_STEPS", 2)
        chunks.start()
        self.addCleanup(chunks.stop)
        self.config = config_at(self.temp.name, tool_profile="simple", approval="client")
        self.manager = ComputerManager(self.config, runtime_factory=CapacityRuntime)
        self.addCleanup(self.manager.close)
        self.task = self.manager.tasks.save({"id": "long", "name": "Long process", "instructions": "Run all finite steps",
            "expected": "Completed", "program_ids": ["editor"],
            "steps": [{"program_id": "editor", "operation": "delay", "duration_ms": 0} for _ in range(5)]})

    def call(self, run_id=None):
        args = {"task_id": "long"}
        if run_id: args["resume_run_id"] = run_id
        return self.manager.call("computer_run_task", args)["structuredContent"]

    def test_public_saved_run_continues_three_chunks_with_one_id(self):
        first = self.call()
        self.assertEqual(first["completed_steps"], 2)
        current = self.manager.session
        second = self.call(first["run_id"])
        final = self.call(first["run_id"])
        self.assertIs(self.manager.session, current)
        self.assertEqual([second["completed_steps"], final["completed_steps"]], [4, 5])
        self.assertTrue(final["task_verified"])
        self.assertEqual(final["run_id"], first["run_id"])
        self.assertEqual(len(final["step_receipts"]), 5)

    def test_action_budget_renews_only_connection_and_retains_run(self):
        first = self.call()
        original = self.manager.session
        original.guard.action_count = original.max_actions
        answer = self.call(first["run_id"])
        self.assertIsNot(self.manager.session, original)
        self.assertEqual(original.stop_cause, "workflow_budget")
        self.assertEqual(self.manager.session.max_actions, self.config["max_actions"])
        self.assertEqual(answer["run_id"], first["run_id"])
        self.assertEqual(answer["completed_steps"], 4)

    def test_actual_timed_runtime_stop_can_renew_at_known_boundary(self):
        first = self.call()
        original = self.manager.session
        original.deadline = time.monotonic() - 1
        original.stop("Budget ended", cause="time_budget")
        answer = self.call(first["run_id"])
        self.assertEqual(answer["completed_steps"], 4)
        self.assertIsNot(self.manager.session, original)

    def test_explicit_stop_after_timeout_prevents_renewal(self):
        first = self.call()
        original = self.manager.session
        original.deadline = time.monotonic() - 1
        original.stop("Budget ended", cause="time_budget")
        original.stop("User stopped")
        with self.assertRaises(SessionError): self.call(first["run_id"])
        self.assertIs(self.manager.session, original)

    def test_cancellation_between_boundary_and_connection_is_not_restarted(self):
        first = self.call()
        original = self.manager.session
        original.guard.action_count = original.max_actions
        renewal = self.manager.easy._renew_workflow_budget(first["run_id"], None)
        self.manager.stop("User stopped during renewal")
        with self.assertRaises(SessionError):
            self.manager.easy._session(["editor"], "Continue existing work", None, renewal=renewal)
        self.assertIs(self.manager.session, original)

    def test_replacement_session_cannot_bypass_renewal_token(self):
        first = self.call()
        original = self.manager.session
        original.guard.action_count = original.max_actions
        renewal = self.manager.easy._renew_workflow_budget(first["run_id"], None)
        self.manager.begin({"program_ids": ["editor"], "task_description": "Another explicit session"})
        replacement = self.manager.session
        with self.assertRaises(SessionError):
            self.manager.easy._session(["editor"], "Continue existing work", None, renewal=renewal)
        self.assertIs(self.manager.session, replacement)

    def test_editor_continuation_uses_existing_trial_and_never_unknown_input(self):
        answer = {"status": "editing", "editor_id": "editor-1", "last_test": {
            "status": "interrupted", "run_id": "trial-1", "pending_step": None,
            "continuation": {"reason": "workflow_chunk_complete", "session_id": "session"}}}
        response = self.manager.easy._observation_response(answer)["structuredContent"]
        self.assertEqual(response["next_tool"], "computer_process_editor")
        self.assertEqual(response["next_arguments"], {"action": "resume_test", "editor_id": "editor-1"})
        self.assertEqual(response["last_test"]["run_id"], "trial-1")
        for change in ({"pending_step": 2}, {"status": "needs_review"},
                       {"continuation": {"reason": "session_budget_exhausted"}}):
            blocked = copy.deepcopy(answer)
            blocked["last_test"].update(change)
            result = self.manager.easy._observation_response(blocked)["structuredContent"]
            self.assertNotIn("next_arguments", result)


class RuntimeStopCauseTests(unittest.TestCase):
    def test_real_runtime_timeout_stop_and_later_explicit_stop_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            config = config_at(folder, approval="client")
            runtime = SessionRuntime(config, [config["programs"][0]], "uia", {"instructions": "Synthetic finite work"},
                lease_factory=FakeLease, emergency_factory=FakeEmergency, transport_factory=FakeTransport,
                guard_factory=TestGuard, registry_register=lambda path: None, registry_unregister=lambda path: None)
            try:
                runtime.deadline = time.monotonic() - 1
                with self.assertRaises(SessionError): runtime._check_not_stopped()
                self.assertEqual(runtime.stop_cause, "time_budget")
                self.assertTrue((runtime.run_dir / "stop.flag").exists())
                runtime.stop("User stopped")
                runtime.stop("Attempted renewal", cause="workflow_budget")
                self.assertEqual(runtime.stop_cause, "requested_or_error")
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
