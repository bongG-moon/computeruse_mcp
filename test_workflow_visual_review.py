"""Saved visual review is an explicit observation boundary, never a replay."""
import copy
import tempfile
import unittest
from unittest import mock

from operations import OperationError
from process_steps import validate_process_step
from workflows import WorkflowRunner, WorkflowError
from test_workflows import FakeRuntime, ScriptedOperations
from test_image_steps import image_target, png


class WorkflowVisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True,
            "verification_deferred": True})
        self.runner = WorkflowRunner(self.temp.name)
        self.addCleanup(self.runner.close)
        self.reviews = self.runner.visual_reviews = mock.Mock()
        self.reviews.open.return_value = {"state": "needs_observation_review", "task_verified": False,
            "visual_review": {"review_id": "review-"+"a"*32}, "image_content": [{"type": "image", "mimeType": "image/png", "data": png()}]}
        self.reviews.verify.return_value = {"task_verified": True, "state": "completed", "evidence_level": "visual_assessed"}
        self.task = {"id": "review-task", "revision": 1, "program_ids": ["editor"], "steps": [
            {"step_id": "image", "program_id": "editor", "operation": "image_click", "image_target": image_target()},
            {"step_id": "result", "program_id": "editor", "operation": "checkpoint", "message": "The application is running", "review_mode": "visual"}]}
        self.targets = [{"program_id": "editor", "pid": 100, "window_id": 200}]
        self.submission = {"review_id": "review-"+"a"*32}

    def run_task(self, **kwargs):
        with mock.patch("operations.Operations", return_value=ScriptedOperations()):
            return self.runner.run(self.runtime, self.task, {}, self.targets, **kwargs)

    def test_visual_checkpoint_requires_image_enabled_and_never_human_ack(self):
        first = self.run_task()
        self.assertEqual(first["diagnostic"]["code"], "vision_client_required")
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.reviews.open.assert_not_called()
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])

    def test_passed_visual_review_completes_without_replaying_image_input(self):
        first = self.run_task(image_delivery_enabled=True)
        self.assertEqual(first["state"], "needs_observation_review")
        self.assertNotIn("checkpoint_content", first)
        self.assertEqual(len(first["observation_content"]), 1)
        self.assertEqual(first["step_receipts"][0]["status"], "awaiting_verification")
        final = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertTrue(final["task_verified"])
        self.assertEqual(final["completed_steps"], 2)
        self.assertEqual(final["evidence_level"], "visual_assessed")
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.assertEqual(self.runner.progress(final["run_id"])["visual_assessments"][0]["step_index"], 1)

    def test_failed_or_uncertain_review_never_advances_or_repeats_input(self):
        first = self.run_task(image_delivery_enabled=True)
        self.reviews.verify.return_value = {"task_verified": False, "state": "uncertain"}
        final = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertFalse(final["task_verified"])
        self.assertEqual(final["pending_step"], 1)
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_expired_review_recaptures_same_checkpoint_only(self):
        first = self.run_task(image_delivery_enabled=True)
        self.reviews.verify.side_effect = OperationError("expired", "visual_review_expired")
        again = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertEqual(again["pending_step"], 1)
        self.assertEqual(again["state"], "needs_observation_review")
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.assertEqual(self.reviews.open.call_count, 2)

    def test_human_checkpoint_cannot_accept_visual_submission(self):
        self.task["steps"][1].pop("review_mode")
        self.runtime.capture_checkpoint = mock.Mock(return_value={"content": [{"type": "image", "mimeType": "image/png", "data": png()}]})
        first = self.run_task(image_delivery_enabled=True)
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.reviews.verify.assert_not_called()
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_unknown_review_mode_rejected(self):
        with self.assertRaises(OperationError):
            validate_process_step({"operation": "checkpoint", "message": "Verify", "review_mode": "automatic_pass"})


if __name__ == "__main__": unittest.main()
