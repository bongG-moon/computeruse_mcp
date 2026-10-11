"""Long workflow boundaries and crash recovery; no desktop interaction."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_image_steps import image_target
from test_workflows import FakeRuntime, ScriptedOperations
from workflows import WorkflowError, WorkflowRunner, render_steps


class WorkflowCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runner = WorkflowRunner(self.temp.name)
        self.targets = [{"program_id": "editor", "pid": 100, "window_id": 200}]

    def task(self, count=3):
        return {"id": "capacity", "revision": 1, "name": "Long task", "instructions": "Use saved values",
            "expected": "Values set", "program_ids": ["editor"], "variables": {}, "steps": [
                {"program_id": "editor", "step_id": "entry-" + str(index), "operation": "set_value",
                 "selector": {"name": "Input"}, "value": str(index)} for index in range(count)]}

    def run_task(self, task, engine, run_id=None):
        with mock.patch("operations.Operations", return_value=engine):
            return self.runner.run(self.runtime, task, {}, self.targets, resume_run_id=run_id)

    def test_250_steps_continue_same_run_without_repeating_completed_input(self):
        task, engine = self.task(250), ScriptedOperations()
        first = self.run_task(task, engine)
        self.assertEqual(first["completed_steps"], 100)
        self.assertEqual(first["diagnostic"]["code"], "workflow_chunk_complete")
        self.assertIsNone(first["pending_step"])
        self.assertFalse(first["input_dispatched"])
        self.assertEqual(first["next_arguments"], {"task_id": task["id"], "resume_run_id": first["run_id"]})
        self.assertEqual(self.runner.progress(first["run_id"])["continuation"], first["continuation"])
        second = self.run_task(task, engine, first["run_id"])
        self.assertEqual(second["completed_steps"], 200)
        final = self.run_task(task, engine, first["run_id"])
        self.assertTrue(final["task_verified"])
        self.assertNotIn("continuation", final)
        self.assertEqual(final["run_id"], first["run_id"])
        writes = [step["value"] for step, _, _ in engine.calls if step["operation"] == "set_value"]
        self.assertEqual(writes, [str(index) for index in range(250)])
        self.assertEqual(len(final["step_receipts"]), 250)
        self.assertEqual(self.runner.progress(first["run_id"])["step_receipts"], final["step_receipts"])

    def test_time_slice_yields_after_a_verified_step(self):
        with mock.patch("workflows.RUN_CHUNK_SECONDS", 0):
            answer = self.run_task(self.task(), ScriptedOperations())
        self.assertEqual(answer["completed_steps"], 1)
        self.assertEqual(answer["continuation"]["reason"], "workflow_chunk_complete")

    def test_policy_action_limit_continues_at_unattempted_next_step(self):
        self.runtime.max_actions = 1
        self.runtime.guard.action_count = 0
        def complete():
            self.runtime.guard.action_count += 1
            return {"task_verified": True, "input_dispatched": True}
        engine = ScriptedOperations([complete])
        first = self.run_task(self.task(), engine)
        self.assertEqual(first["completed_steps"], 1)
        self.assertEqual(first["continuation"], {"reason": "session_budget_exhausted", "session_id": self.runtime.id})
        self.assertIsNone(first["pending_step"])
        self.assertEqual(len(engine.calls), 1)
        # The manager supplies a fresh authorized runtime, then verifies the
        # prior result before dispatching the two remaining writes.
        self.runtime = FakeRuntime(self.temp.name)
        self.runtime.id = "fresh-session"
        final = self.run_task(self.task(), engine, first["run_id"])
        self.assertTrue(final["task_verified"])
        self.assertEqual([s["value"] for s, _, _ in engine.calls if s["operation"] == "set_value"], ["0", "1", "2"])

    def test_policy_deadline_after_verified_step_has_same_continuation(self):
        self.runtime.deadline = time.monotonic() + 60
        def expire():
            self.runtime.deadline = time.monotonic() - 1
            return {"task_verified": True, "input_dispatched": True}
        answer = self.run_task(self.task(), ScriptedOperations([expire]))
        self.assertEqual(answer["continuation"]["reason"], "session_budget_exhausted")
        self.assertEqual(answer["completed_steps"], 1)

    def test_typed_deadline_stop_with_own_stop_flag_can_continue(self):
        self.runtime.deadline = time.monotonic() + 60
        self.runtime.stop_event = threading.Event()
        self.runtime.run_dir = Path(self.temp.name)
        def expire():
            self.runtime.deadline = time.monotonic() - 1
            self.runtime.stop_cause = "time_budget"
            self.runtime.stop_event.set()
            (self.runtime.run_dir / "stop.flag").write_text("stopped", encoding="utf-8")
            return {"task_verified": True, "input_dispatched": True}
        answer = self.run_task(self.task(), ScriptedOperations([expire]))
        self.assertEqual(answer["continuation"]["reason"], "session_budget_exhausted")

    def test_snapshot_too_large_rejects_before_any_input_or_unresumable_record(self):
        engine = ScriptedOperations()
        with mock.patch("workflows.MAX_SNAPSHOT_BYTES", 128), self.assertRaises(WorkflowError):
            self.run_task(self.task(), engine)
        self.assertEqual(engine.calls, [])
        self.assertEqual(list(self.runner.root.glob("*.json")), [])
        self.assertEqual(list((self.runner.root / "snapshots").glob("*.json")), [])

    def test_user_stop_takes_priority_over_exhausted_policy(self):
        self.runtime.max_actions = 1
        self.runtime.guard.action_count = 0
        self.runtime.stop_event = threading.Event()
        def stop():
            self.runtime.guard.action_count = 1
            self.runtime.reason = "사용자가 중지했습니다."
            self.runtime.stop_event.set()
            self.runtime.active = False
            return {"task_verified": True, "input_dispatched": True}
        answer = self.run_task(self.task(), ScriptedOperations([stop]))
        self.assertEqual(answer["diagnostic"]["code"], "interrupted")
        self.assertNotIn("continuation", answer)

    def test_unknown_input_never_becomes_budget_continuation(self):
        self.runtime.max_actions = 1
        self.runtime.guard.action_count = 0
        def uncertain():
            self.runtime.guard.action_count = 1
            raise RuntimeError("Connection ended after dispatch")
        answer = self.run_task(self.task(), ScriptedOperations([uncertain]))
        self.assertEqual(answer["completed_steps"], 0)
        self.assertEqual(answer["pending_step"], 0)
        self.assertEqual(answer["step_delivery"]["state"], "unknown")
        self.assertNotIn("continuation", answer)

    def test_focus_consuming_final_action_can_continue_only_on_proven_non_delivery(self):
        for dispatched in (False, True, None):
            with self.subTest(input_dispatched=dispatched):
                self.runtime.max_actions = 120
                self.runtime.guard.action_count = 119
                def no_budget():
                    self.runtime.guard.action_count = 120
                    return {"task_verified": False, "input_dispatched": dispatched, "focus_dispatched": True}
                engine = ScriptedOperations([no_budget])
                answer = self.run_task(self.task(), engine)
                self.assertEqual(len(engine.calls), 1)
                self.assertEqual(answer["completed_steps"], 0)
                if dispatched is False:
                    self.assertEqual(answer["continuation"]["reason"], "session_budget_exhausted")
                    self.assertIsNone(answer["pending_step"])
                else:
                    self.assertNotIn("continuation", answer)
                    self.assertEqual(answer["pending_step"], 0)

    def test_image_delivery_and_required_checkpoint_are_not_split(self):
        task = self.task(1)
        task["steps"] = [
            {"program_id": "editor", "operation": "image_click", "image_target": image_target()},
            {"program_id": "editor", "operation": "checkpoint", "message": "Verify result"}]
        self.runtime.max_actions = 1
        self.runtime.guard.action_count = 0
        def deliver(*args, **kwargs):
            self.runtime.guard.action_count = 1
            return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}
        with mock.patch("workflows.RUN_CHUNK_STEPS", 1), mock.patch("workflows.execute_image_step", side_effect=deliver), \
                mock.patch("workflows.execute_process_step", return_value={"checkpoint_ready": True}) as capture:
            answer = self.run_task(task, ScriptedOperations())
        self.assertEqual(answer["status"], "needs_review")
        self.assertEqual(answer["pending_step"], 1)
        self.assertEqual(answer["step_receipts"][0]["status"], "awaiting_verification")
        self.assertNotIn("continuation", answer)
        capture.assert_called_once()

    def test_running_record_stays_compact_and_crash_resume_reconstructs_receipts(self):
        class Crash(BaseException): pass
        def crash(): raise Crash()
        task, engine = self.task(), ScriptedOperations([{"task_verified": True}, crash])
        with self.assertRaises(Crash): self.run_task(task, engine)
        paths = list(self.runner.root.glob("*.json"))
        self.assertEqual(len(paths), 1)
        pending = self.runner.progress(paths[0].stem)
        self.assertNotIn("step_receipts", pending)
        self.assertEqual(pending["completed_steps"], 1)
        self.assertEqual(pending["pending_step"], 1)
        self.assertEqual(pending["step_delivery"]["state"], "unknown")
        restarted = ScriptedOperations()
        final = self.run_task(task, restarted, paths[0].stem)
        self.assertTrue(final["task_verified"])
        self.assertEqual([s["operation"] for s, _, _ in restarted.calls], ["assert", "set_value"])
        self.assertEqual(restarted.calls[-1][0]["value"], "2")
        self.assertEqual([r["step_id"] for r in final["step_receipts"]], ["entry-0", "entry-1", "entry-2"])

    def test_running_writes_do_not_rewrite_completed_receipts(self):
        from workflows import atomic_json
        running_sizes = []
        def persist(path, value):
            if value.get("format") == "computer-workflow/v1" and value.get("status") == "running":
                self.assertNotIn("step_receipts", value)
                running_sizes.append(len(json.dumps(value)))
            return atomic_json(path, value)
        with mock.patch("workflows.atomic_json", side_effect=persist):
            answer = self.run_task(self.task(100), ScriptedOperations())
        self.assertTrue(answer["task_verified"])
        self.assertEqual(len(running_sizes), 200)
        # Cursor digits/timestamps may vary; completed history must not make
        # each durable before/after-input record grow with workflow length.
        self.assertLess(max(running_sizes) - min(running_sizes), 100)

    def test_large_progress_and_thousandth_iteration_are_readable(self):
        answer = self.run_task(self.task(1), ScriptedOperations())
        record = self.runner.progress(answer["run_id"])
        record.update(total_steps=10000, completed_steps=10000)
        record["step_receipts"] = [{"step_index": i, "step_id": "entry", "iteration_id": "group:" + str(i // 10 + 1),
            "status": "verified"} for i in range(10000)]
        path = self.runner._path(answer["run_id"])
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertGreater(path.stat().st_size, 256000)
        restored = self.runner.progress(answer["run_id"])
        self.assertEqual(restored["step_receipts"][-1]["iteration_id"], "group:1000")
        record["step_receipts"][-1]["iteration_id"] = "group:1001"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(WorkflowError): self.runner.progress(answer["run_id"])

    def test_full_repetition_expands_without_copying_groups_in_saved_recipe(self):
        task = self.task(10)
        task["variables"] = {"items": {"type": "list", "items": {"type": "text"}, "default": ["item"] * 1000}}
        task["steps"] = [{"operation": "foreach", "step_id": "items", "input": "items", "item": "row", "steps": task["steps"]}]
        original = copy.deepcopy(task)
        rendered, _ = render_steps(task, {})
        self.assertEqual(len(rendered), 10000)
        self.assertEqual(rendered[-1]["iteration_id"], "items:1000")
        self.assertEqual(task, original)

    def test_invalid_continuation_cannot_renew_a_pending_input(self):
        answer = self.run_task(self.task(1), ScriptedOperations())
        record = self.runner.progress(answer["run_id"])
        record.update(status="interrupted", task_verified=False, total_steps=2,
            continuation={"reason": "session_budget_exhausted", "session_id": self.runtime.id}, pending_step=1)
        path = self.runner._path(answer["run_id"])
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(WorkflowError): self.runner.progress(answer["run_id"])


if __name__ == "__main__": unittest.main()
