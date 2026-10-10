"""Saved-image recovery is an immutable, one-use, pre-dispatch handoff."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operations import OperationError
from test_image_steps import image_target, png
from test_workflows import FakeRuntime, ScriptedOperations
from workflows import WorkflowRunner, WorkflowError


def missing(code="image_not_found", sent=False):
    return {"task_verified": False, "input_dispatched": sent, "diagnostic": {"code": code}}


class WorkflowTargetReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runtime.image_action = mock.Mock(return_value=missing())
        self.runner = WorkflowRunner(self.temp.name); self.addCleanup(self.runner.close)
        self.recovery = self.runner.visual_targets = mock.Mock()
        self.recovery.open.side_effect = lambda *a, **k: {"state": "needs_target_review",
            "target_review": {"recovery_id": "target-a"}, "input_dispatched": False,
            "image_content": [{"type": "image", "mimeType": "image/png", "data": png()}] * 2}
        self.recovery.verify.return_value = {"recovery_id": "target-a", "input_dispatched": False}
        self.recovery.execute.return_value = {"task_verified": False, "input_dispatched": True,
            "verification_deferred": True}
        self.reviews = self.runner.visual_reviews = mock.Mock()
        self.reviews.open.side_effect = lambda *a, **k: {"state": "needs_observation_review",
            "visual_review": {"review_id": "review-"+"a"*32}, "image_content": []}
        self.reviews.verify.return_value = {"task_verified": True}
        self.image = {"step_id": "image", "program_id": "editor", "operation": "image_click",
            "image_target": image_target()}
        self.checkpoint = {"step_id": "after", "program_id": "editor", "operation": "checkpoint",
            "message": "The requested action completed", "review_mode": "visual"}
        self.task = {"id": "target-review-task", "revision": 1, "program_ids": ["editor"],
            "steps": [copy.deepcopy(self.image), copy.deepcopy(self.checkpoint)]}
        self.targets = [{"program_id": "editor", "pid": 100, "window_id": 200}]
        self.engine = ScriptedOperations()
        self.submission = {"recovery_id": "target-a", "evidence": "SENSITIVE MODEL EVIDENCE"}

    def run_task(self, **kwargs):
        kwargs.setdefault("image_delivery_enabled", True)
        with mock.patch("operations.Operations", return_value=self.engine):
            return self.runner.run(self.runtime, self.task, {}, self.targets, **kwargs)

    def with_prior_value(self):
        self.task["steps"].insert(0, {"step_id": "value", "program_id": "editor", "operation": "set_value",
            "selector": {"name": "Condition", "role": "Edit"}, "value": "W"})

    def with_prior_visual(self):
        self.task["steps"] = [{**copy.deepcopy(self.image), "step_id": "first"},
            {**copy.deepcopy(self.checkpoint), "step_id": "first-result"},
            copy.deepcopy(self.image), copy.deepcopy(self.checkpoint)]
        self.runtime.image_action.side_effect = [
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True}, missing()]
        first = self.run_task()
        return self.run_task(resume_run_id=first["run_id"], observation_review={"review_id": "review-"+"a"*32})

    def test_only_proven_unsent_image_failures_open_handoff(self):
        for code, sent, eligible in [("image_not_found", False, True), ("image_ambiguous", False, True),
                ("image_not_found", True, False), ("image_not_found", None, False), ("different_error", False, False)]:
            with self.subTest(code=code, sent=sent):
                self.recovery.open.reset_mock()
                self.runtime.image_action.return_value = missing(code, sent)
                result = self.run_task()
                self.assertEqual(self.recovery.open.call_count, int(eligible))
                self.assertEqual("target_recovery" in result, eligible)
                if eligible:
                    self.assertEqual(result["state"], "needs_target_review")
                    self.assertEqual(len(result["observation_content"]), 2)
                    self.assertEqual(result["step_delivery"]["state"], "not_sent")

    def test_text_only_preserves_pause_without_disclosing_images(self):
        result = self.run_task(image_delivery_enabled=False)
        self.assertEqual(result["state"], "needs_input")
        self.assertFalse(result["target_review_available"])
        self.assertNotIn("observation_content", result)
        self.recovery.open.assert_not_called()
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=result["run_id"], target_review=self.submission, image_delivery_enabled=False)

    def test_recovery_once_preserves_saved_task_snapshot_and_checkpoints(self):
        original = copy.deepcopy(self.task)
        first = self.run_task()
        snapshot = self.runner._snapshot_path(first["run_id"])
        original_bytes = snapshot.read_bytes()
        after = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(after["state"], "needs_observation_review")
        self.assertEqual(after["completed_steps"], 1)
        self.assertNotIn("target_recovery", after)
        self.assertEqual(self.task, original)
        self.assertEqual(snapshot.read_bytes(), original_bytes)
        call = self.recovery.execute.call_args
        self.assertEqual(call.args[2], original["steps"][0])
        self.assertTrue(call.kwargs["image_delivery_enabled"])
        self.assertTrue(callable(call.kwargs["save_verification"]))
        self.assertEqual(call.args[1]["step_delivery"]["state"], "unknown")
        persisted = self.runner._path(first["run_id"]).read_text(encoding="utf-8")
        self.assertNotIn("SENSITIVE MODEL EVIDENCE", persisted)
        self.assertNotIn("template_png", persisted)
        self.assertNotIn("target-a", persisted)
        final = self.run_task(resume_run_id=first["run_id"], observation_review={"review_id": "review-"+"a"*32})
        self.assertTrue(final["task_verified"])
        self.assertEqual(final["completed_steps"], 2)
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.assertEqual(self.recovery.execute.call_count, 1)
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], target_review=self.submission)

    def test_submission_requires_resume_and_cannot_replace_completion_review(self):
        with self.assertRaises(WorkflowError): self.run_task(target_review=self.submission)
        first = self.run_task()
        for extra in ({"observation_review": {}}, {"acknowledge_checkpoint": "b"*32}):
            with self.subTest(extra=extra), self.assertRaises(WorkflowError):
                self.run_task(resume_run_id=first["run_id"], target_review=self.submission, **extra)
        self.recovery.verify.assert_not_called()

    def test_bad_context_neither_opens_fresh_challenge_nor_dispatches(self):
        first = self.run_task()
        self.recovery.verify.side_effect = OperationError("wrong frame", "target_recovery_context_mismatch")
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(result["diagnostic"]["code"], "target_recovery_context_mismatch")
        self.assertEqual(self.recovery.open.call_count, 1)
        self.recovery.execute.assert_not_called()

    def test_forged_step_marker_is_rejected_before_any_input(self):
        first = self.run_task()
        path = self.runner._path(first["run_id"])
        record = json.loads(path.read_text(encoding="utf-8")); record["target_recovery"]["step_hash"] = "0"*64
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.recovery.verify.assert_not_called()

    def test_expired_or_restarted_challenge_rechecks_prior_state(self):
        self.with_prior_value()
        first = self.run_task()
        self.recovery.verify.side_effect = OperationError("expired", "target_recovery_expired")
        self.engine.responses = [{"task_verified": False}]
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(self.engine.calls[-1][0]["operation"], "assert")
        self.assertEqual(self.recovery.open.call_count, 1)
        self.recovery.execute.assert_not_called()
        self.assertEqual(result["pending_step"], 1)
        self.engine.responses = [{"task_verified": True}]
        fresh = self.run_task(resume_run_id=first["run_id"])
        self.assertEqual(fresh["state"], "needs_target_review")
        self.assertEqual(self.recovery.open.call_count, 2)
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_valid_same_frame_review_does_not_loop_previous_visual_checkpoint(self):
        first = self.with_prior_visual()
        self.assertEqual(first["state"], "needs_target_review")
        self.assertNotIn("checkpoint", first)
        self.assertEqual(first["completed_steps"], 2)
        after = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(after["pending_step"], 3)
        self.assertEqual(after["checkpoint"]["step_index"], 3)
        self.assertEqual(self.recovery.execute.call_count, 1)
        self.assertEqual(self.runtime.image_action.call_count, 2)

    def test_restart_requires_prior_visual_checkpoint_before_new_target(self):
        first = self.with_prior_visual()
        self.recovery.verify.side_effect = OperationError("restarted", "target_recovery_expired")
        again = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(again["state"], "needs_observation_review")
        self.assertEqual(again["checkpoint"]["source_step_index"], 1)
        self.assertEqual(self.recovery.open.call_count, 1)
        fresh = self.run_task(resume_run_id=first["run_id"], observation_review={"review_id": "review-"+"a"*32})
        self.assertEqual(fresh["state"], "needs_target_review")
        self.assertEqual(self.recovery.open.call_count, 2)
        self.recovery.execute.assert_not_called()

    def test_frame_changed_after_verify_does_not_open_fresh_challenge_before_prior_check(self):
        self.with_prior_value()
        first = self.run_task()
        self.recovery.execute.return_value = missing("target_recovery_observation_changed")
        blocked = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(blocked["state"], "needs_input")
        self.assertEqual(blocked["step_delivery"]["state"], "not_sent")
        self.assertEqual(self.recovery.open.call_count, 1)
        self.engine.responses = [{"task_verified": False}]
        after = self.run_task(resume_run_id=first["run_id"])
        self.assertEqual(self.engine.calls[-1][0]["operation"], "assert")
        self.assertFalse(after["last_result"]["task_verified"])
        self.assertEqual(self.recovery.open.call_count, 1)
        self.assertEqual(self.recovery.execute.call_count, 1)

    def test_execute_exception_keeps_unknown_and_cannot_recover_again(self):
        first = self.run_task()
        self.recovery.execute.side_effect = RuntimeError("possible dispatch")
        interrupted = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["step_delivery"]["state"], "unknown")
        self.assertNotIn("target_recovery", interrupted)
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        result = self.run_task(resume_run_id=first["run_id"])
        self.assertFalse(result["last_result"]["task_verified"])
        self.assertEqual(self.recovery.execute.call_count, 1)
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_recovery_unknown_receipt_never_reopens_challenge(self):
        first = self.run_task()
        self.recovery.execute.return_value = missing("target_recovery_input_unknown", None)
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(result["step_delivery"]["state"], "unknown")
        self.assertNotIn("target_recovery", result)
        self.assertEqual(self.recovery.open.call_count, 1)

    def test_wrapped_completion_failure_preserves_refresh_prior_boundary(self):
        self.with_prior_value()
        first = self.run_task()
        self.recovery.execute.return_value = {**missing("image_input_unconfirmed"),
            "input_result": {"diagnostic": {"code": "target_recovery_observation_changed"}}}
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["target_recovery"]["step_index"], 1)
        self.assertEqual(self.recovery.open.call_count, 1)

    def test_verified_target_does_not_replace_prior_value_assertion(self):
        self.with_prior_value()
        first = self.run_task()
        self.engine.responses = [{"task_verified": False}]
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(self.engine.calls[-1][0]["operation"], "assert")
        self.assertEqual(result["pending_step"], 1)
        self.recovery.execute.assert_not_called()

    def test_visual_prior_in_other_window_cannot_use_current_window_pixels(self):
        self.task["steps"] = [{**copy.deepcopy(self.image), "step_id": "first", "window_ref": "other"},
            {**copy.deepcopy(self.checkpoint), "step_id": "first-result", "window_ref": "other"},
            copy.deepcopy(self.image), copy.deepcopy(self.checkpoint)]
        self.targets.append({"program_id": "editor", "window_ref": "other", "pid": 100, "window_id": 201})
        self.runtime.image_action.side_effect = [
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True}, missing()]
        first = self.run_task()
        opened = self.run_task(resume_run_id=first["run_id"], observation_review={"review_id": "review-"+"a"*32})
        result = self.run_task(resume_run_id=first["run_id"], target_review=self.submission)
        self.assertEqual(opened["state"], "needs_target_review")
        self.assertEqual(result["diagnostic"]["code"], "target_recovery_prior_window_unverified")
        self.assertEqual(result["pending_step"], 2)
        self.assertEqual(self.recovery.open.call_count, 1)
        self.recovery.execute.assert_not_called()


class GuardedWorkflowTargetTests(unittest.TestCase):
    """Exercise real recovery + workflow + Guard together, fake Driver only."""
    def setUp(self):
        import test_interaction
        test_interaction.FacadeTests.setUp(self)
        self.runtime.id = "target-review-session"
        self.runtime.programs = [{"id": "editor", "exe": self.policy["allowed_apps"][0], "control_exes": []}]
        self.runtime.image_action = mock.Mock(return_value=missing())
        self.runner = WorkflowRunner(self.tmp.name); self.addCleanup(self.runner.close)
        self.task = {"id": "guarded-target-task", "revision": 2, "program_ids": ["editor"], "steps": [
            {"step_id": "image", "program_id": "editor", "operation": "image_click", "image_target": image_target(),
             "expect": [{"selector": {"automation_id": "status"}, "property": "value", "equals": "done", "require_change": True}],
             "verification_timeout_ms": 0}]}
        self.value = "before"
        self.runtime.observe_controls = self.observe
        self.targets = [{"program_id": "editor", "pid": 100, "window_id": 200}]
        self.transport.after = self.dispatch

    def observe(self, target, selectors, timeout_ms):
        return {"structuredContent": {**target, "read_only": True, "scoped_observation": True, "scope_complete": True,
            "elements": [{"automation_id": "status", "name": "status", "role": "Edit", "value": self.value}]}}

    def dispatch(self, request, result):
        if request["name"] == "click": self.value = "done"

    def run_task(self, **kwargs):
        return self.runner.run(self.runtime, self.task, {}, self.targets, image_delivery_enabled=True, **kwargs)

    def test_real_recovery_keeps_automatic_postconditions_and_baseline(self):
        original = copy.deepcopy(self.task)
        first = self.run_task()
        self.assertEqual(first["state"], "needs_target_review")
        challenge = first["target_review"]
        submission = {**{key: challenge[key] for key in ("recovery_id", "run_id", "step_index", "step_id",
            "iteration_id", "observation_id", "frame_id")}, "target_region": self.region, "scope_region": self.scope,
            "evidence": "Same target identified in the first named row."}
        final = self.run_task(resume_run_id=first["run_id"], target_review=submission)
        self.assertTrue(final["task_verified"], final)
        self.assertEqual(final["last_result"]["verification_scope"], "changed_uia_postconditions")
        self.assertTrue(final["last_result"]["checks"][0]["change_observed"])
        self.assertTrue(final["image_verification"]["state"]["input_dispatched"])
        mutations = [row for row in self.transport.calls if row["name"] != "get_window_state"]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(self.task, original)
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.assertEqual(mutations[0]["arguments"]["x"], 28)


if __name__ == "__main__": unittest.main()
