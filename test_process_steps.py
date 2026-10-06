"""Process waits/checkpoints are cancellable, bounded, and never imply image verification."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from operations import OperationError
from process_steps import execute_process_step, validate_process_step
from session_runtime import SessionError
from test_workflows import FakeRuntime, GuardedUIRuntime, ScriptedOperations
from workflows import WorkflowError, WorkflowRunner, render_steps, validate_recipe


TARGET = {"pid": 100, "window_id": 200}
SELECTOR = {"name": "준비", "role": "Button"}


class StepRuntime:
    mode = "uia"
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []
        self.stop_event = threading.Event()
        self.captures = []
        self.capture_result = {"structuredContent": TARGET, "content": [{"type": "image", "data": "c3ludGhldGlj", "mimeType": "image/png"}]}
    def check_active(self):
        if self.stop_event.is_set():
            raise SessionError("stopped")
    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        self.check_active()
        result = self.rows.pop(0) if self.rows else []
        if callable(result):
            return result()
        if isinstance(result, dict):
            return result
        return {"structuredContent": {**TARGET, "elements": copy.deepcopy(result)}}
    def capture_checkpoint(self, target):
        self.check_active()
        self.captures.append(copy.deepcopy(target))
        return copy.deepcopy(self.capture_result)


class StepTests(unittest.TestCase):
    def wait(self, runtime, **kwargs):
        return execute_process_step(runtime, {"operation": "wait_for_element", "selector": SELECTOR,
            "timeout_ms": 300, "poll_interval_ms": 100, **kwargs}, TARGET)

    def test_missing_element_can_appear_but_no_input_is_sent(self):
        runtime = StepRuntime([[], [{"name": "준비", "role": "Button"}]])
        result = self.wait(runtime)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["metrics"]["observations"], 2)
        self.assertFalse(result["input_dispatched"])
        self.assertTrue(all(name == "get_window_state" and not args["include_screenshot"] for name, args in runtime.calls))

    def test_timeout_zero_observes_once_and_does_not_treat_absence_as_success(self):
        runtime = StepRuntime()
        result = self.wait(runtime, timeout_ms=0)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "element_wait_timeout")
        self.assertEqual(len(runtime.calls), 1)

    def test_access_error_or_ambiguity_incomplete_or_wrong_target_is_not_polled(self):
        good = {"name": "준비", "role": "Button"}
        failures = [({"isError": True, "structuredContent": {"error_code": "access_denied"}}, "observation_failed"),
                    ({"structuredContent": {**TARGET, "elements": [good, good]}}, "ambiguous_selector"),
                    ({"structuredContent": {**TARGET, "elements": [], "truncated": True}}, "incomplete_observation"),
                    ({"structuredContent": {**TARGET, "window_id": 201, "elements": []}}, "target_mismatch"),
                    ({"structuredContent": {**TARGET}}, "missing_accessibility")]
        for answer, code in failures:
            runtime = StepRuntime([answer, [good]])
            with self.subTest(code=code):
                result = self.wait(runtime)
                self.assertEqual(result["diagnostic"]["code"], code)
                self.assertEqual(len(runtime.calls), 1)
                self.assertFalse(result["task_verified"])

    def test_wait_cancellation_interrupts_before_next_poll(self):
        runtime = StepRuntime([[]])
        with mock.patch.object(runtime.stop_event, "wait", side_effect=lambda seconds: runtime.stop_event.set()):
            with self.assertRaises(SessionError):
                self.wait(runtime, timeout_ms=60000)
        self.assertEqual(len(runtime.calls), 1)

    def test_fixed_delay_is_cancellable_and_never_accesses_a_screen(self):
        runtime = StepRuntime()
        result = execute_process_step(runtime, {"operation": "delay", "duration_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(runtime.calls, [])
        with mock.patch.object(runtime.stop_event, "wait", side_effect=lambda seconds: runtime.stop_event.set()):
            with self.assertRaises(SessionError):
                execute_process_step(runtime, {"operation": "delay", "duration_ms": 60000}, TARGET)
        self.assertEqual(runtime.calls, [])

    def test_checkpoint_requires_explicit_capture_and_remains_unverified(self):
        runtime = StepRuntime()
        result = execute_process_step(runtime, {"operation": "checkpoint", "message": "완료 상태를 확인하세요."}, TARGET)
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["image_verified"])
        self.assertTrue(result["checkpoint_ready"])
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.captures, [TARGET])
        self.assertEqual(runtime.mode, "uia")
        self.assertEqual(runtime.calls, [])
        self.assertEqual(result["checkpoint_content"], runtime.capture_result["content"])

    def test_checkpoint_missing_image_error_and_foreign_target_cannot_be_approved(self):
        for answer, code in (({"isError": True}, "checkpoint_capture_failed"),
                             ({"isError": True, "structuredContent": {"error_code": "checkpoint_requires_foreground"}}, "checkpoint_requires_foreground"),
                             ({"content": []}, "checkpoint_image_missing"),
                             ({"structuredContent": {"pid": 9}}, "target_mismatch")):
            runtime = StepRuntime()
            runtime.capture_result = answer
            with self.subTest(code=code):
                result = execute_process_step(runtime, {"operation": "checkpoint", "message": "확인"}, TARGET)
                self.assertEqual(result["diagnostic"]["code"], code)
                self.assertFalse(result.get("checkpoint_ready", False))
                self.assertNotIn("checkpoint_content", result)

    def test_process_schema_rejects_unbounded_waits_boolean_times_code_and_handles(self):
        bad = [{"operation": "delay", "duration_ms": True}, {"operation": "delay", "duration_ms": 60001},
               {"operation": []},
               {"operation": "delay", "duration_ms": 1, "script": "x"},
               {"operation": "wait_for_element", "selector": SELECTOR, "timeout_ms": -1},
               {"operation": "wait_for_element", "selector": SELECTOR, "timeout_ms": 2, "poll_interval_ms": 0},
               {"operation": "wait_for_element", "selector": {"element_index": 1}, "timeout_ms": 0},
               {"operation": "checkpoint", "message": ""},
               {"operation": "checkpoint", "message": "ok", "approved": True},
               {"operation": "checkpoint", "message": "ok", "image": "stored"}]
        for step in bad:
            with self.subTest(step=step), self.assertRaises(OperationError):
                validate_process_step(step)


class ProcessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = FakeRuntime(self.temporary.name)
        self.runtime.guard.window_resolver = lambda hwnd: 100
        self.runtime.stop_event = threading.Event()
        self.runtime.capture_checkpoint = mock.Mock(return_value={"structuredContent": TARGET,
            "content": [{"type": "image", "data": "PRIVATE_IMAGE", "mimeType": "image/png"}]})
        self.runner = WorkflowRunner(self.temporary.name)
        self.targets = [{"program_id": "editor", **TARGET}]
        self.task = {"id": "process", "revision": 1, "name": "프로세스", "instructions": "순서대로 실행",
                     "expected": "확인", "program_ids": ["editor"], "variables": {}, "steps": []}

    def step(self, operation, **fields):
        return {"program_id": "editor", "operation": operation, **fields}

    def action(self, label="입력칸"):
        return self.step("set_value", selector={"name": label, "role": "Edit"}, value="값")

    def run_task(self, engine=None, **kwargs):
        with mock.patch("operations.Operations", return_value=engine or ScriptedOperations()):
            return self.runner.run(self.runtime, self.task, {}, self.targets, **kwargs)

    def test_all_steps_validate_and_checkpoint_message_variables_are_literal(self):
        self.task["steps"] = [self.action(), self.step("delay", duration_ms=0),
                              self.step("wait_for_element", selector=SELECTOR, timeout_ms=100),
                              self.step("checkpoint", message="${review}")]
        self.task["variables"] = {"review": {"description": "확인 내용"}}
        rendered, _ = render_steps(self.task, {"review": "문구 ${not_reexecuted}"})
        self.assertEqual(rendered[-1]["message"], "문구 ${not_reexecuted}")
        self.assertEqual(len(validate_recipe(self.task["steps"], self.task["variables"], ["editor"])[0]), 4)

    def test_checkpoint_pauses_before_later_action_and_persists_no_image_or_message(self):
        self.task["steps"] = [self.action(), self.step("checkpoint", message="PRIVATE_PROMPT"), self.action("다음")]
        engine = ScriptedOperations()
        answer = self.run_task(engine)
        self.assertEqual(answer["status"], "needs_review")
        self.assertEqual(answer["completed_steps"], 1)
        self.assertEqual(answer["pending_step"], 1)
        self.assertFalse(answer["task_verified"])
        self.assertTrue(answer["checkpoint"]["human_review_required"])
        self.assertFalse(answer["checkpoint"]["image_verified"])
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["set_value"])
        self.runtime.capture_checkpoint.assert_called_once_with(TARGET)
        record = self.runner.progress(answer["run_id"])
        self.assertEqual(record["checkpoint"]["id"], answer["checkpoint"]["id"])
        saved = (self.runner.root / (answer["run_id"]+".json")).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", saved)
        self.assertNotIn("checkpoint_content", saved)

    def test_resume_without_ack_recaptures_but_never_runs_next_action(self):
        self.task["steps"] = [self.action(), self.step("checkpoint", message="확인"), self.action("다음")]
        initial = self.run_task()
        engine = ScriptedOperations()
        later = self.run_task(engine, resume_run_id=initial["run_id"])
        self.assertNotEqual(initial["checkpoint"]["id"], later["checkpoint"]["id"])
        self.assertFalse(later["task_verified"])
        self.assertEqual(later["completed_steps"], 1)
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["assert"])
        self.assertEqual(self.runtime.capture_checkpoint.call_count, 2)

    def test_matching_ack_rechecks_prior_result_and_advances_without_replaying(self):
        self.task["steps"] = [self.action(), self.step("checkpoint", message="확인"), self.action("다음")]
        initial = self.run_task()
        engine = ScriptedOperations()
        answer = self.run_task(engine, resume_run_id=initial["run_id"], acknowledge_checkpoint=initial["checkpoint"]["id"])
        self.assertTrue(answer["task_verified"])
        self.assertFalse(answer["checkpoint_images_verified"])
        self.assertEqual(answer["completed_steps"], 3)
        self.assertNotIn("checkpoint", self.runner.progress(answer["run_id"]))
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["assert", "set_value"])
        self.assertEqual(self.runtime.capture_checkpoint.call_count, 1)

    def test_wrong_or_stale_ack_never_dispatches_and_last_checkpoint_requires_ack(self):
        self.task["steps"] = [self.step("checkpoint", message="마지막 확인")]
        initial = self.run_task()
        engine = ScriptedOperations()
        for kwargs in ({"acknowledge_checkpoint": initial["checkpoint"]["id"]},
                       {"resume_run_id": initial["run_id"], "acknowledge_checkpoint": "0"*32}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkflowError):
                self.run_task(engine, **kwargs)
        refreshed = self.run_task(resume_run_id=initial["run_id"])
        with self.assertRaises(WorkflowError):
            self.run_task(engine, resume_run_id=initial["run_id"], acknowledge_checkpoint=initial["checkpoint"]["id"])
        final = self.run_task(engine, resume_run_id=initial["run_id"], acknowledge_checkpoint=refreshed["checkpoint"]["id"])
        self.assertTrue(final["task_verified"])
        self.assertEqual(engine.calls, [])

    def test_ack_cannot_continue_when_prior_observed_result_changed(self):
        self.task["steps"] = [self.action(), self.step("checkpoint", message="확인"), self.action("다음")]
        initial = self.run_task()
        engine = ScriptedOperations([{"task_verified": False}])
        answer = self.run_task(engine, resume_run_id=initial["run_id"], acknowledge_checkpoint=initial["checkpoint"]["id"])
        self.assertFalse(answer["task_verified"])
        self.assertEqual(answer["completed_steps"], 1)
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["assert"])

    def test_failed_capture_cannot_be_acknowledged(self):
        self.task["steps"] = [self.step("checkpoint", message="확인"), self.action()]
        self.runtime.capture_checkpoint.return_value = {"isError": True}
        initial = self.run_task()
        self.assertFalse(initial["checkpoint"]["capture_available"])
        self.assertEqual(initial["checkpoint_content"], [])
        self.assertIn("승인 ID를 보내지 마세요", initial["next_step"])
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=initial["run_id"], acknowledge_checkpoint=initial["checkpoint"]["id"])

    def test_ack_is_bound_to_captured_session_and_exact_live_window(self):
        self.task["steps"] = [self.step("checkpoint", message="확인"), self.action()]
        initial = self.run_task()
        previous_session = self.runtime.id
        engine = ScriptedOperations()
        for changed in ("session", "target", "owner"):
            self.runtime.id = "new-session" if changed == "session" else previous_session
            self.targets[0]["window_id"] = 201 if changed == "target" else 200
            self.runtime.guard.window_resolver = lambda hwnd: 101 if changed == "owner" else 100
            with self.subTest(changed=changed), self.assertRaises(WorkflowError):
                self.run_task(engine, resume_run_id=initial["run_id"], acknowledge_checkpoint=initial["checkpoint"]["id"])
        self.assertEqual(engine.calls, [])
        self.assertEqual(self.runtime.capture_checkpoint.call_count, 1)
        stored = self.runner.progress(initial["run_id"])
        self.assertFalse(stored["task_verified"])
        self.assertEqual(stored["pending_step"], 0)
        self.assertNotIn("pid", stored["checkpoint"])
        self.assertNotIn("window_id", stored["checkpoint"])

    def test_pending_wait_resume_rechecks_previous_action_then_waits_without_input_replay(self):
        self.task["steps"] = [self.action(), self.step("wait_for_element", selector=SELECTOR, timeout_ms=0), self.action("다음")]
        self.runtime.call = mock.Mock(return_value={"structuredContent": {**TARGET, "elements": []}})
        first = self.run_task()
        self.assertEqual(first["completed_steps"], 1)
        self.runtime.call.return_value = {"structuredContent": {**TARGET, "elements": [{"name": "준비", "role": "Button"}]}}
        engine = ScriptedOperations()
        answer = self.run_task(engine, resume_run_id=first["run_id"])
        self.assertTrue(answer["task_verified"])
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["assert", "set_value"])

    def test_delay_breaks_cached_observation_reuse_between_real_actions(self):
        self.runtime = GuardedUIRuntime(self.temporary.name)
        self.task["steps"] = [self.action("첫 입력칸"), self.step("delay", duration_ms=0), self.action("둘째 입력칸")]
        answer = self.runner.run(self.runtime, self.task, {}, self.targets)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([name for name, _ in self.runtime.calls],
                         ["get_window_state", "set_value", "get_window_state", "get_window_state", "set_value", "get_window_state"])
        self.assertEqual(answer["last_result"]["metrics"]["reused_observations"], 0)

    def test_cancelled_delay_remains_pending_without_running_later_action(self):
        self.task["steps"] = [self.step("delay", duration_ms=60000), self.action()]
        def cancel(seconds):
            self.runtime.active = False
            self.runtime.stop_event.set()
        engine = ScriptedOperations()
        with mock.patch.object(self.runtime.stop_event, "wait", side_effect=cancel):
            answer = self.run_task(engine)
        self.assertEqual(answer["status"], "interrupted")
        self.assertEqual(answer["pending_step"], 0)
        self.assertEqual(engine.calls, [])

    def test_delay_does_not_start_when_bound_window_owner_changed(self):
        self.task["steps"] = [self.step("delay", duration_ms=60000), self.action()]
        self.runtime.guard.window_resolver = lambda hwnd: 101
        engine = ScriptedOperations()
        answer = self.run_task(engine)
        self.assertEqual(answer["status"], "needs_binding")
        self.assertIsNone(answer["pending_step"])
        self.assertEqual(engine.calls, [])
        self.runtime.capture_checkpoint.assert_not_called()

    def test_program_exit_before_delay_preserves_prior_action_without_replay(self):
        self.task["steps"] = [self.action(), self.step("delay", duration_ms=60000)]
        def first_action():
            self.runtime.guard.process_resolver = mock.Mock(side_effect=OSError("process gone"))
            return {"task_verified": True}
        engine = ScriptedOperations([first_action])
        answer = self.run_task(engine)
        self.assertEqual(answer["status"], "needs_binding")
        self.assertEqual(answer["completed_steps"], 1)
        self.assertIsNone(answer["pending_step"])
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["set_value"])

    def test_corrupt_checkpoint_metadata_refused_without_overwriting(self):
        self.task["steps"] = [self.step("checkpoint", message="확인")]
        answer = self.run_task()
        path = self.runner.root / (answer["run_id"]+".json")
        original = json.loads(path.read_text(encoding="utf-8"))
        original["checkpoint"]["image"] = "PRIVATE"
        raw = json.dumps(original)
        path.write_text(raw, encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.runner.progress(answer["run_id"])
        self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_checkpoint_record_write_failure_does_not_leave_invalid_pending_metadata(self):
        from vendor.guard import atomic_json
        self.task["steps"] = [self.step("checkpoint", message="확인")]
        initial = self.run_task()
        calls = []
        def fail_once(path, record):
            calls.append(record["status"])
            if len(calls) == 1:
                raise OSError("synthetic checkpoint write failure")
            atomic_json(path, record)
        with mock.patch("workflows.atomic_json", side_effect=fail_once):
            answer = self.run_task(resume_run_id=initial["run_id"])
        self.assertEqual(answer["status"], "interrupted")
        progress = self.runner.progress(answer["run_id"])
        self.assertIsNone(progress["pending_step"])
        self.assertNotIn("checkpoint", progress)
        self.assertEqual(progress["completed_steps"], 0)

    def test_cancellation_during_capture_does_not_publish_approval_or_run_later_action(self):
        self.task["steps"] = [self.step("checkpoint", message="확인"), self.action()]
        image = copy.deepcopy(self.runtime.capture_checkpoint.return_value)
        def cancel_capture(target):
            self.runtime.active = False
            return image
        self.runtime.capture_checkpoint.side_effect = cancel_capture
        engine = ScriptedOperations()
        answer = self.run_task(engine)
        self.assertEqual(answer["status"], "interrupted")
        self.assertFalse(answer["checkpoint"]["capture_available"])
        self.assertNotIn("checkpoint_content", answer)
        self.assertEqual(engine.calls, [])


if __name__ == "__main__":
    unittest.main()
