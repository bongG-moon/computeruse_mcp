"""Image actions verify changed UIA conditions and resume without replay."""
import copy
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from image_steps import execute_image_step, validate_image_step
from operations import OperationError
from test_image_steps import image_target, TARGET
from test_workflows import FakeRuntime
from workflows import WorkflowRunner, validate_recipe


class ImageCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runtime.stop_event = threading.Event()
        self.value = "before"; self.calls = 0; self.reads = 0; self.proof = None
        self.runtime.observe_controls = self.observe
        self.runtime.image_action = self.action
        self.runtime.report_progress = mock.Mock()
        self.step = {"operation": "image_click", "image_target": image_target(), "expect": [
            {"selector": {"automation_id": "status"}, "property": "value", "equals": "done", "require_change": True}],
            "verification_timeout_ms": 0}

    def observe(self, target, selectors, timeout_ms):
        self.reads += 1
        return {"structuredContent": {**target, "read_only": True, "scoped_observation": True, "scope_complete": True,
            "elements": [{"automation_id": "status", "name": "status", "role": "Edit", "value": self.value}]}}

    def action(self, step, target):
        self.calls += 1; self.assertNotIn("expect", step)
        self.value = "done"
        return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}

    def save(self, proof):
        self.proof = copy.deepcopy(proof)

    def execute(self, **options):
        return execute_image_step(self.runtime, self.step, TARGET, save_verification=self.save, **options)

    def test_changed_condition_completes_without_checkpoint_and_stores_no_observed_text(self):
        answer = self.execute()
        self.assertTrue(answer["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertEqual(self.reads, 2); self.assertFalse(answer["verification_deferred"])
        self.assertNotIn("before", repr(self.proof)); self.assertNotIn("done", repr(self.proof))
        self.assertTrue(answer["checks"][0]["change_observed"])

    def test_first_image_run_reads_completion_properties_without_fast_mode(self):
        self.runtime.fast_verification_enabled = False
        self.runtime.observe_controls = mock.Mock(side_effect=NotImplementedError("not a fast run"))
        self.runtime.observe_completion_controls = self.observe
        answer = self.execute()
        self.assertTrue(answer["task_verified"])
        self.runtime.observe_controls.assert_not_called()
        self.assertFalse(self.runtime.fast_verification_enabled)

    def test_mutation_metrics_count_guard_inputs_instead_of_one_logical_action(self):
        self.runtime.guard.action_count = 2
        def action(*args):
            self.runtime.guard.action_count += 3
            return self.action(*args)
        self.runtime.image_action = action
        self.assertEqual(self.execute()["metrics"]["mutations"], 3)

    def test_stale_existing_completion_is_not_success(self):
        self.value = "done"
        answer = self.execute()
        self.assertFalse(answer["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertEqual(answer["checks"][0]["reason"], "change_not_observed")

    def test_missing_baseline_property_prevents_input(self):
        original = self.observe
        def missing(*args, **kwargs):
            result = original(*args, **kwargs)
            del result["structuredContent"]["elements"][0]["value"]
            return result
        self.runtime.observe_controls = missing
        self.assertFalse(self.execute()["task_verified"]); self.assertEqual(self.calls, 0)

    def test_unknown_input_resumes_with_old_baseline_and_no_replay(self):
        def interrupted(step, target):
            self.action(step, target)
            raise RuntimeError("lost acknowledgement after dispatch")
        self.runtime.image_action = interrupted
        first = self.execute()
        self.assertFalse(first["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertEqual(first["feedback"]["last_stage"], "completion_check")
        done = self.execute(verification_state=self.proof, verify_only=True)
        self.assertTrue(done["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertTrue(done["result_reobserved"]); self.assertFalse(done["input_replayed"])
        self.assertEqual(done["delivery"]["requested_mode"], "read_only")

    def test_read_failure_is_not_followed_by_retry_or_input(self):
        self.runtime.observe_controls = mock.Mock(side_effect=RuntimeError("provider failed"))
        result = self.execute()
        self.assertFalse(result["task_verified"]); self.assertEqual(self.calls, 0)
        self.assertEqual(self.runtime.observe_controls.call_count, 1)

    def test_resume_rejects_missing_changed_condition_and_changed_session_proof(self):
        self.execute()
        old = copy.deepcopy(self.proof)
        for proof in (None, {}, {**old, "baseline_hashes": [None]}, "corrupt"):
            answer = self.execute(verification_state=proof, verify_only=True)
            self.assertFalse(answer["task_verified"])
        self.runtime.id = "different-session-without-native-identity"
        self.assertFalse(self.execute(verification_state=old, verify_only=True)["task_verified"])
        self.assertEqual(self.calls, 1)

    def test_explicit_no_input_cannot_be_recovered_as_success(self):
        self.runtime.image_action = lambda *_: {"input_dispatched": False}
        self.execute(); self.value = "done"
        result = self.execute(verification_state=self.proof, verify_only=True)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "image_input_not_dispatched")

    def test_partial_action_does_not_immediately_claim_success(self):
        def partial(*args):
            self.action(*args)
            return {"input_dispatched": True, "verification_deferred": False}
        self.runtime.image_action = partial
        self.assertFalse(self.execute()["task_verified"])
        self.assertTrue(self.execute(verification_state=self.proof, verify_only=True)["task_verified"])
        self.assertEqual(self.calls, 1)

    def test_save_failure_prevents_input(self):
        result = execute_image_step(self.runtime, self.step, TARGET,
            save_verification=mock.Mock(side_effect=OSError("disk failed")))
        self.assertFalse(result["task_verified"]); self.assertEqual(self.calls, 0)

    def test_validation_rejects_unchanged_only_condition_and_wait_completion(self):
        step = copy.deepcopy(self.step); step["expect"][0]["require_change"] = False
        with self.assertRaises(OperationError): validate_image_step(step)
        with self.assertRaises(OperationError): validate_image_step({**self.step, "operation": "wait_for_image", "timeout_ms": 0})
        with self.assertRaises(OperationError): validate_image_step({**self.step, "step_timeout_ms": 0})

    def test_all_conditions_must_pass_even_when_one_has_changed(self):
        self.step["expect"].append({"selector": {"automation_id": "status"}, "property": "name", "equals": "wrong"})
        answer = self.execute()
        self.assertFalse(answer["task_verified"]); self.assertTrue(answer["checks"][0]["passed"])

    def test_ambiguous_baseline_prevents_input(self):
        original = self.observe
        def duplicate(*args, **kwargs):
            result = original(*args, **kwargs)
            result["structuredContent"]["elements"] *= 2
            return result
        self.runtime.observe_controls = duplicate
        answer = self.execute()
        self.assertFalse(answer["task_verified"]); self.assertEqual(self.calls, 0)
        self.assertEqual(answer["diagnostic"]["code"], "ambiguous_selector")

    def test_late_observation_cannot_complete_a_step(self):
        clock = [100.0]; original = self.observe
        def late(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.calls: clock[0] += 2
            return result
        self.runtime.observe_controls = late
        self.step["verification_timeout_ms"] = 100
        with mock.patch("image_steps.time.monotonic", side_effect=lambda: clock[0]):
            answer = self.execute()
        self.assertFalse(answer["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertEqual(answer["diagnostic"]["code"], "verification_timeout")

    def test_native_process_identity_allows_new_session_but_rejects_reused_pid(self):
        creation = [123456]
        def factory(target):
            return SimpleNamespace(capture=lambda: {"process_exited": False, "target_present": True,
                "creation_time": creation[0], "executable": "Editor.exe", "windows": [{**target,
                    "class_name": "editor-window", "thread_id": 55}]}, close=lambda: None)
        self.runtime.create_transition_probe = factory
        self.assertTrue(self.execute()["task_verified"])
        proof = copy.deepcopy(self.proof); self.runtime.id = "new-mcp-session"
        self.assertTrue(self.execute(verification_state=proof, verify_only=True)["task_verified"])
        creation[0] += 1
        answer = self.execute(verification_state=proof, verify_only=True)
        self.assertFalse(answer["task_verified"]); self.assertEqual(self.calls, 1)

    def test_wait_image_reports_only_presence(self):
        self.runtime.image_action = lambda *_: {"task_verified": True, "input_dispatched": False}
        result = execute_image_step(self.runtime, {"operation": "wait_for_image", "image_target": image_target(), "timeout_ms": 0}, TARGET)
        self.assertTrue(result["task_verified"]); self.assertFalse(result["business_result_verified"])
        self.assertEqual(result["verification_scope"], "image_presence_only")

    def test_workflow_resume_persists_proof_and_only_reobserves(self):
        runner = WorkflowRunner(self.temp.name)
        task = {"id": "automatic-image", "revision": 1, "program_ids": ["editor"], "variables": {},
            "steps": [{"program_id": "editor", **self.step}]}
        validate_recipe(task["steps"], {}, ["editor"])
        def interrupted(*args):
            self.action(*args); raise RuntimeError("lost response")
        self.runtime.image_action = interrupted
        first = runner.run(self.runtime, task, {}, [{"program_id": "editor", **TARGET}])
        self.assertFalse(first["task_verified"]); self.assertEqual(first["pending_step"], 0)
        persisted = runner.progress(first["run_id"])
        self.assertTrue(persisted["image_verification"]["state"]["input_attempted"])
        done = runner.run(self.runtime, task, {}, [{"program_id": "editor", **TARGET}], resume_run_id=first["run_id"])
        self.assertTrue(done["task_verified"]); self.assertEqual(self.calls, 1)
        self.assertEqual(done["execution"]["mode"], "standard")
        self.assertIn("verifying", [c.args[0] for c in self.runtime.report_progress.call_args_list])

    def test_only_verified_run_qualifies_for_repeat_profile(self):
        runner = WorkflowRunner(self.temp.name)
        task = {"id": "image-repeat", "program_ids": ["editor"], "variables": {},
            "steps": [{"program_id": "editor", **self.step}]}
        targets = [{"program_id": "editor", **TARGET}]
        self.value = "done"
        self.assertFalse(runner.run(self.runtime, task, {}, targets)["task_verified"])
        self.value = "before"
        first = runner.run(self.runtime, task, {}, targets)
        self.assertTrue(first["task_verified"]); self.assertFalse(first["execution"]["profile_matched"])
        self.value = "before"
        second = runner.run(self.runtime, task, {}, targets)
        self.assertTrue(second["task_verified"]); self.assertTrue(second["execution"]["profile_matched"])
        self.assertEqual(self.calls, 3)


if __name__ == "__main__": unittest.main()
