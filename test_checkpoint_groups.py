import copy
import threading
import unittest
from unittest import mock

from checkpoint_groups import group_reviews, batch_links
from operations import OperationError
import test_workflow_visual_review as visual_fixture
import test_task_revision as revision_fixture
import test_recording_windows as window_fixture
from task_revision import TaskRevisionError
from workflows import validate_recipe, WorkflowError


class BatchExecutionTests(visual_fixture.WorkflowVisualTests):
    def setUp(self):
        super().setUp()
        self.task["steps"][1]["review_origin"] = "recording_generated"
        second = copy.deepcopy(self.task["steps"])
        second[0]["step_id"], second[1]["step_id"] = "second-input", "final-result"
        self.task["steps"].extend(second)
        self.task["steps"] = group_reviews(self.task["steps"], ["result", "final-result"], "Both settings match")

    def test_batch_has_one_review_and_no_early_success(self):
        first = self.run_task(image_delivery_enabled=True)
        self.assertEqual(first["pending_step"], 3)
        self.assertEqual(self.runtime.image_action.call_count, 2)
        self.assertEqual(self.reviews.open.call_count, 1)
        self.assertTrue(all(r["status"] == "awaiting_verification" for r in first["step_receipts"]))
        self.assertEqual(first["performance"]["batched_reviews"], 1)
        self.assertEqual(first["performance"]["visual_review_requests"], 1)
        final = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertTrue(final["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 2)
        self.assertTrue(all(r["status"] == "verified" for r in final["step_receipts"]))
        self.assertEqual(final["performance"]["calls"], 2)
        self.assertGreaterEqual(final["performance"]["active_ms"], 0)
        self.assertIn("review_capture", final["performance"]["phases_ms"])

    def test_batch_fail_keeps_prior_inputs_pending_without_reclick(self):
        first = self.run_task(image_delivery_enabled=True)
        self.reviews.verify.return_value = {"task_verified": False, "state": "uncertain"}
        again = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertEqual(again["pending_step"], 3)
        self.assertFalse(again["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 2)
        self.assertEqual(again["step_receipts"][0]["status"], "awaiting_verification")

    def test_final_review_uses_current_deferred_condition(self):
        self.task["steps"][1]["message"] = "MES Setup must now equal P"
        self.run_task(image_delivery_enabled=True)
        self.assertIn("MES Setup must now equal P", self.reviews.open.call_args.args[2]["message"])

    def test_deferred_recovery_rejects_human_ack(self):
        self.runtime.image_action.side_effect = [
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True},
            {"task_verified": False, "input_dispatched": False, "diagnostic": {"code": "focus_unavailable"}}]
        first = self.run_task(image_delivery_enabled=True)
        recovery = self.run_task(resume_run_id=first["run_id"], image_delivery_enabled=True)
        self.assertEqual(recovery["checkpoint"]["source_step_index"], 1)
        with self.assertRaises(WorkflowError):
            self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=recovery["checkpoint"]["id"], image_delivery_enabled=True)
        self.assertEqual(self.runtime.image_action.call_count, 2)

    def test_invalid_review_reuses_pending_template_without_recapture(self):
        first = self.run_task(image_delivery_enabled=True)
        self.reviews.verify.side_effect = OperationError("wrong ids", "invalid_visual_review")
        self.reviews.current_request.return_value = {"observation_review_template": {"review_id": "correct"}, "image_content": []}
        again = self.run_task(resume_run_id=first["run_id"], observation_review={"review_id": "wrong"}, image_delivery_enabled=True)
        self.assertEqual(again["observation_review_template"]["review_id"], "correct")
        self.assertEqual(self.reviews.open.call_count, 1)
        self.assertEqual(self.runtime.image_action.call_count, 2)

    def test_unsent_second_input_rechecks_deferred_conditions_before_retry(self):
        self.runtime.image_action.side_effect = [
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True},
            {"task_verified": False, "input_dispatched": False, "diagnostic": {"code": "focus_unavailable"}},
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True}]
        first = self.run_task(image_delivery_enabled=True)
        self.assertEqual(first["pending_step"], 2)
        recovery = self.run_task(resume_run_id=first["run_id"], image_delivery_enabled=True)
        self.assertEqual(recovery["checkpoint"]["source_step_index"], 1)
        self.assertEqual(self.reviews.open.call_args.args[2]["review_mode"], "visual")
        self.assertEqual(self.runtime.image_action.call_count, 2)
        final_check = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission, image_delivery_enabled=True)
        self.assertEqual(final_check["pending_step"], 3)
        self.assertEqual(self.runtime.image_action.call_count, 3)
        self.assertFalse(final_check["task_verified"])
        self.assertEqual(final_check["step_receipts"][0]["status"], "awaiting_verification")

    def test_uncertain_second_input_is_never_repeated(self):
        self.runtime.image_action.side_effect = [
            {"task_verified": False, "input_dispatched": True, "verification_deferred": True},
            {"task_verified": False, "input_dispatched": None, "diagnostic": {"code": "input_unknown"}}]
        first = self.run_task(image_delivery_enabled=True)
        again = self.run_task(resume_run_id=first["run_id"], image_delivery_enabled=True)
        self.assertEqual(again["pending_step"], 2)
        self.assertFalse(again["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 2)

    def test_popup_image_pass_keeps_earlier_group_receipts_pending(self):
        image = copy.deepcopy(self.task["steps"][0])
        first_review = {"step_id": "result", "program_id": "editor", "operation": "checkpoint",
                        "message": "MES Setup is N", "review_mode": "visual",
                        "review_origin": "recording_generated"}
        opening = {**copy.deepcopy(image), "step_id": "open-popup"}
        selection = {**copy.deepcopy(image), "step_id": "select-popup", "window_ref": "popup_1"}
        popup_review = {"step_id": "popup-ready", "program_id": "editor", "operation": "checkpoint",
                        "window_ref": "popup_1", "opened_from": "main", "message": "The option is visible",
                        "review_mode": "popup_image", "review_origin": "recording_generated",
                        "image_target": copy.deepcopy(image["image_target"])}
        final_review = {**first_review, "step_id": "final-result", "message": "Family is Hybrid",
                        "return_from": "popup_1"}
        self.task["steps"] = group_reviews(
            [image, first_review, opening, window_fixture.step(step_id="wait-popup"),
             popup_review, selection, final_review], ["result", "final-result"], "Both fields match")
        self.targets = [{"program_id": "editor", "pid": 42, "window_id": 10}]
        self.runtime.stop_event = threading.Event()
        self.runtime.guard.window_resolver = lambda hwnd: 42 if hwnd in (10, 20) else 0
        self.runtime.create_transition_probe = lambda target: window_fixture.Probe([window_fixture.row(20)])

        def action(step, target):
            if step["operation"] == "wait_for_image":
                return {"task_verified": True, "input_dispatched": False}
            return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}

        self.runtime.image_action.side_effect = action
        first = self.run_task(image_delivery_enabled=True)
        self.assertEqual(first["pending_step"], 6)
        self.assertFalse(first["task_verified"])
        receipts = first["step_receipts"]
        self.assertEqual(receipts[0]["status"], "awaiting_verification")
        self.assertEqual(receipts[1]["status"], "awaiting_verification")
        self.assertEqual(receipts[4]["status"], "verified")
        self.assertEqual(self.runner.progress(first["run_id"])["step_receipts"], receipts)
        self.assertEqual(self.reviews.open.call_count, 1)
        self.assertEqual([call.args[0]["operation"] for call in self.runtime.image_action.call_args_list],
                         ["image_click", "image_click", "wait_for_image", "image_click"])
        final = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission,
                              image_delivery_enabled=True)
        self.assertTrue(final["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 4)
        self.assertTrue(all(receipt["status"] == "verified" for receipt in final["step_receipts"]))

    def test_stop_between_grouped_inputs_never_verifies_or_replays_pending_input(self):
        self.runtime.stop_event = threading.Event()
        original_check = self.runtime.check_active

        def check_active():
            original_check()
            if self.runtime.stop_event.is_set():
                raise OperationError("Stop requested", "session_stopped")

        self.runtime.check_active = check_active
        self.runtime.report_progress = lambda phase, **fields: (
            self.runtime.stop_event.set() if phase == "running_step" and fields["step_index"] == 3 else None)
        stopped = self.run_task(image_delivery_enabled=True)
        self.assertEqual(stopped["status"], "interrupted")
        self.assertFalse(stopped["task_verified"])
        self.assertEqual(stopped["completed_steps"], 2)
        self.assertTrue(all(receipt["status"] == "awaiting_verification" for receipt in stopped["step_receipts"]))
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.reviews.verify.assert_not_called()
        with self.assertRaises(OperationError):
            self.run_task(resume_run_id=stopped["run_id"], image_delivery_enabled=True)
        self.runtime.stop_event.clear()
        self.runtime.report_progress = lambda *args, **kwargs: None
        resumed = self.run_task(resume_run_id=stopped["run_id"], image_delivery_enabled=True)
        self.assertFalse(resumed["task_verified"])
        self.assertEqual(resumed["run_id"], stopped["run_id"])
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.assertEqual(resumed["step_receipts"][0]["status"], "awaiting_verification")

    def test_cancel_during_final_group_review_does_not_accept_review_or_replay(self):
        first = self.run_task(image_delivery_enabled=True)
        self.runtime.active = False
        with self.assertRaises(revision_fixture.SessionError):
            self.run_task(resume_run_id=first["run_id"], observation_review=self.submission,
                          image_delivery_enabled=True)
        self.reviews.verify.assert_not_called()
        self.assertEqual(self.runtime.image_action.call_count, 2)
        progress = self.runner.progress(first["run_id"])
        self.assertFalse(progress["task_verified"])
        self.assertTrue(all(receipt["status"] == "awaiting_verification" for receipt in progress["step_receipts"]))
        self.runtime.active = True
        self.reviews.verify.return_value = {"task_verified": False, "state": "uncertain"}
        resumed = self.run_task(resume_run_id=first["run_id"], observation_review=self.submission,
                                image_delivery_enabled=True)
        self.assertFalse(resumed["task_verified"])
        self.assertEqual(resumed["pending_step"], 3)
        self.assertEqual(self.runtime.image_action.call_count, 2)
        self.assertTrue(all(receipt["status"] == "awaiting_verification" for receipt in resumed["step_receipts"]))

    # Inherited two-step tests intentionally use a different task shape.


# Reuse fixture setup and runner utility without collecting the parent tests.
for _name in dir(visual_fixture.WorkflowVisualTests):
    if _name.startswith("test_") and _name not in BatchExecutionTests.__dict__:
        setattr(BatchExecutionTests, _name, None)


class BatchRevisionTests(revision_fixture.RevisionTests):
    def test_revisioned_deferred_condition_edit_reaches_saved_task_final_review(self):
        steps = copy.deepcopy(self.saved["steps"][1:])
        steps[1].update(message="Old MES Setup is W", review_mode="visual", review_origin="recording_generated")
        second = copy.deepcopy(steps)
        second[0]["step_id"], second[1]["step_id"] = "input2", "review2"
        second[1]["message"] = "Family is Hybrid"
        self.api.save({"id": "sample", "steps": steps + second}, expected_revision=1)
        self.api.update("sample", 2, [{"op": "group_reviews", "checkpoint_ids": ["review", "review2"],
                                       "condition": "Both fields match"}])
        grouped = self.api.get("sample", 3)
        self.api.update("sample", 3, [{"op": "set_step", "step_id": "review",
                                       "fields": {"message": "MES Setup must now equal P"}}])
        saved = self.api.get("sample")
        self.assertEqual(saved["revision"], 4)
        self.assertEqual(self.api.get("sample", 3), {key: value for key, value in grouped.items() if key != "revision_history"})
        self.assertEqual([saved["steps"][index] for index in (0, 2)], [grouped["steps"][index] for index in (0, 2)])
        runtime = visual_fixture.FakeRuntime(self.tmp.name)
        runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True,
                                                       "verification_deferred": True})
        runner = visual_fixture.WorkflowRunner(self.tmp.name)
        self.addCleanup(runner.close)
        reviews = runner.visual_reviews = mock.Mock()
        reviews.open.return_value = {"state": "needs_observation_review", "task_verified": False,
                                     "visual_review": {"review_id": "review-" + "a" * 32}, "image_content": []}
        with mock.patch("operations.Operations", return_value=visual_fixture.ScriptedOperations()):
            result = runner.run(runtime, saved, {}, [{"program_id": "editor", "pid": 100, "window_id": 200}],
                                image_delivery_enabled=True)
        self.assertEqual(result["pending_step"], 3)
        self.assertFalse(result["task_verified"])
        self.assertEqual(runtime.image_action.call_count, 2)
        message = reviews.open.call_args.args[2]["message"]
        self.assertIn("MES Setup must now equal P", message)
        self.assertIn("Family is Hybrid", message)
        self.assertNotIn("Old MES Setup is W", message)

    def test_explicit_legacy_group_edit_preserves_history_and_labels_user_provenance(self):
        steps = copy.deepcopy(self.saved["steps"][1:])
        steps[1]["review_mode"] = "visual"
        steps[1].pop("review_origin", None)
        second = copy.deepcopy(steps)
        second[0]["step_id"], second[1]["step_id"] = "input2", "review2"
        self.api.save({"id": "sample", "steps": steps + second}, expected_revision=1)
        before = self.api.get("sample", 2)
        changed = self.api.update("sample", 2, [{"op": "group_reviews", "checkpoint_ids": ["review", "review2"],
                                                "condition": "N and Hybrid"}])
        after = changed["task"]
        self.assertEqual(after["revision"], 3)
        self.assertEqual(self.api.get("sample", 2), {key: value for key, value in before.items() if key != "revision_history"})
        self.assertNotIn("review_origin", before["steps"][1])
        self.assertNotIn("review_origin", before["steps"][3])
        for index in (1, 3):
            self.assertEqual(after["steps"][index]["review_origin"], "user_grouped")
            self.assertEqual(after["steps"][index]["step_id"], before["steps"][index]["step_id"])
        self.assertEqual([after["steps"][index] for index in (0, 2)], [before["steps"][index] for index in (0, 2)])
        self.assertEqual(after["steps"][1]["review_mode"], "deferred_visual")
        self.assertEqual(after["steps"][3]["review_mode"], "visual")
        self.assertEqual(changed["changes"][0]["legacy_origin_unspecified"], ["review", "review2"])

    def test_group_edit_preserves_images_ids_and_prior_revision(self):
        steps = copy.deepcopy(self.saved["steps"][1:])
        steps[1].update(review_mode="visual", review_origin="recording_generated")
        second = copy.deepcopy(steps)
        second[0]["step_id"], second[1]["step_id"] = "input2", "review2"
        self.api.save({"id": "sample", "steps": steps+second}, expected_revision=1)
        before = self.api.get("sample")
        change = {"op": "group_reviews", "checkpoint_ids": ["review", "review2"], "condition": "N and Hybrid"}
        changed = self.api.update("sample", 2, [change])["task"]
        self.assertEqual(changed["revision"], 3)
        self.assertEqual(changed["steps"][0], before["steps"][0])
        self.assertEqual(changed["steps"][2], before["steps"][2])
        self.assertEqual(self.api.get("sample", 2)["steps"], before["steps"])
        self.assertIn("Check result", changed["steps"][3]["message"])
        with self.assertRaises(TaskRevisionError):
            self.api.update("sample", 2, [change])

    def test_human_and_explicit_checkpoint_are_not_grouped(self):
        for mode, origin in [("human", "recording_generated"), ("visual", "explicit")]:
            steps = [{"operation": "checkpoint", "step_id": k, "message": k,
                      "program_id": "editor", "review_mode": mode, "review_origin": origin} for k in ["a", "b"]]
            with self.assertRaises(ValueError): group_reviews(steps, ["a", "b"], "Both")

    def test_missing_final_cross_window_or_intervening_human_rejected(self):
        base = [{"operation": "checkpoint", "step_id": k, "message": k,
                 "program_id": "editor", "review_mode": "visual", "review_origin": "recording_generated"} for k in ["a", "b", "c"]]
        with self.assertRaises(ValueError): group_reviews(base, ["a", "c"], "Both")
        for changed in [dict(window_ref="different"), dict(program_id="different")]:
            steps = copy.deepcopy(base[:2]); steps[1].update(changed)
            with self.assertRaises(ValueError): group_reviews(steps, ["a", "b"], "Both")
        broken = group_reviews(base[:2], ["a", "b"], "Both")[:-1]
        with self.assertRaises(ValueError): batch_links(broken)


for _name in dir(revision_fixture.RevisionTests):
    if _name.startswith("test_") and _name not in BatchRevisionTests.__dict__:
        setattr(BatchRevisionTests, _name, None)

if __name__ == "__main__": unittest.main()
