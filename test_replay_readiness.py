"""Regression cases from blocked recordings, safe resume and legacy image apps."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from image_steps import execute_image_step
from image_targets import match_image, png_dimensions, validate_match
from operations import OperationError
from replay_preflight import image_replay_preflight, input_coordinate_contract, prepare_image_foreground
from test_image_steps import TARGET, image_target, png
import test_image_steps
from test_workflows import FakeRuntime, ScriptedOperations
from workflows import WorkflowError, WorkflowRunner, validate_recipe


class LegacyCoordinateTests(unittest.TestCase):
    # Reuse setup only; run existing safety cases in their original test module.
    setUp = test_image_steps.ImageGuardTests.setUp
    run_step = test_image_steps.ImageGuardTests.run_step
    mutations = test_image_steps.ImageGuardTests.mutations
    def legacy(self):
        self.geometry = (0, 0, 100, 100, 0)
        self.proof = {"physical_bounds": [0, 0, 100, 100], "logical_bounds": [0, 0, 100, 100],
            "monitor_scale_percent": 100, "single_monitor": True}
        self.guard.image_coordinate_resolver = lambda hwnd: copy.deepcopy(self.proof)

    def test_legacy_100_percent_measured_png_contract_allows_single_click(self):
        self.legacy()
        result = self.run_step()
        self.assertTrue(result["input_dispatched"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["diagnostic"]["coordinate_contract"]["mode"], "legacy_verified_1to1")
        self.assertTrue(result["diagnostic"]["coordinate_contract"]["capture_size_verified"])
        self.assertEqual(len(self.mutations()), 1)

    def test_legacy_mismatch_or_changed_scale_never_sends(self):
        for patch in ({"monitor_scale_percent": 125}, {"logical_bounds": [0, 0, 80, 80]}, {"single_monitor": False}):
            self.legacy(); self.proof.update(patch)
            self.assertEqual(self.run_step()["diagnostic"]["code"], "image_dpi_unsupported")
        self.legacy(); self.geometry = (0, 0, 101, 100, 0)
        self.proof.update(physical_bounds=[0, 0, 101, 100], logical_bounds=[0, 0, 101, 100])
        self.assertEqual(self.run_step()["diagnostic"]["code"], "image_coordinate_mismatch")
        self.legacy()
        self.matcher.side_effect = lambda *_: (self.proof.update(monitor_scale_percent=125) or self.match)
        self.assertEqual(self.run_step()["diagnostic"]["code"], "image_target_changed")
        self.assertEqual(self.mutations(), [])

    def test_image_presence_reads_legacy_scaled_app_without_input_contract(self):
        self.legacy(); self.proof["monitor_scale_percent"] = 150
        result = self.run_step({"operation": "wait_for_image", "image_target": image_target(), "timeout_ms": 0})
        self.assertTrue(result["task_verified"])
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.mutations(), [])

    def test_preflight_distinguishes_window_front_and_unsupported_without_input(self):
        runtime = SimpleNamespace(guard=self.guard, check_active=lambda: None)
        self.ready = False
        result = image_replay_preflight(runtime, TARGET)
        self.assertEqual(result["status"], "needs_foreground")
        self.assertTrue(image_replay_preflight(runtime, TARGET, capture=False)["ready_for_input"])
        self.assertEqual(self.transport.calls, [])
        self.legacy(); self.ready = True
        result = image_replay_preflight(runtime, TARGET)
        self.assertTrue(result["capture_verified"])
        self.proof["monitor_scale_percent"] = 150
        blocked = image_replay_preflight(runtime, TARGET, capture=False)
        self.assertFalse(blocked["ready_for_input"])
        self.assertEqual(blocked["diagnostic"]["code"], "image_dpi_unsupported")
        self.assertEqual(self.mutations(), [])

    def test_match_diagnostics_survive_blocking_without_pixels_or_coordinates(self):
        self.match.update(status="not_found", score=.81, threshold=.94, candidate_count=0,
            capture_window={"width": 100, "height": 100})
        result = self.run_step()
        details = result["diagnostic"]["image_match"]
        self.assertEqual(details["score"], .81)
        self.assertNotIn("x", details)
        self.assertNotIn("template_png", details)
        self.assertFalse(result["input_dispatched"])

    def test_preflight_preserves_driver_refusal_instead_of_claiming_capture_failure(self):
        def refuse(request, result):
            result.clear()
            result.update(isError=True, structuredContent={"status": "refused"},
                content=[{"type": "text", "text": "Permission session expired; authorize a new bounded session."}])
        self.transport.after = refuse
        runtime = SimpleNamespace(guard=self.guard, check_active=lambda: None)
        result = image_replay_preflight(runtime, TARGET)
        self.assertFalse(result["ready_for_input"])
        self.assertFalse(result["capture_verified"])
        self.assertTrue(result["diagnostic"]["driver_refused"])
        self.assertEqual(result["diagnostic"]["code"], "image_capture_refused")
        self.assertIn("Permission session expired", result["diagnostic"]["message"])
        self.assertEqual(self.mutations(), [])

    def test_explicit_driver_non_delivery_does_not_become_uncertain_attempt(self):
        def refuse(request, result):
            if request["name"] == "click":
                result.update(isError=True, structuredContent={"input_sent": False, "effect": "refused"})
        self.transport.after = refuse
        result = self.run_step()
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.guard.action_count, 1)  # Still consumes request budget.

    def test_later_non_delivery_never_erases_prior_focus_click(self):
        def refuse(request, result):
            if request["name"] == "type_text":
                result.update(isError=True, structuredContent={"input_sent": False})
        self.transport.after = refuse
        result = self.run_step({"operation": "image_type_text", "image_target": image_target(), "value": "data"})
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["verification_deferred"])
        self.assertEqual(self.guard.action_count, 2)


class ReadinessContractTests(unittest.TestCase):
    def test_invalid_geometry_and_proof_are_not_silently_accepted(self):
        for geometry in ((0, 0, 100, 100, -1), (False, 0, 100, 100, 2), (0, 0, 0, 100, 2)):
            with self.assertRaises(OperationError): input_coordinate_contract(geometry)

    def test_low_detail_scores_duplicates_and_dimensions_are_preserved(self):
        result = validate_match({"status": "not_found", "code": "template_low_detail", "score": .6,
            "second_score": .59, "candidate_count": 2, "private_text": "never forwarded"}, image_target(), png(100, 100))
        self.assertTrue(result["low_detail"])
        self.assertEqual(result["code"], "image_template_low_detail")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["second_score"], .59)
        self.assertNotIn("private_text", result)

    def test_foreground_preparation_is_once_and_no_business_input(self):
        state = {"ready": False}
        calls = []
        def call(name, args):
            calls.append(name)
            if name == "bring_to_front": state["ready"] = True
            return {}
        runtime = SimpleNamespace(guard=SimpleNamespace(checkpoint_ready_resolver=lambda hwnd: state["ready"]),
            check_active=lambda: None, stop_event=threading.Event(), call=call)
        self.assertIsNone(prepare_image_foreground(runtime, TARGET))
        self.assertEqual(calls, ["get_window_state", "bring_to_front"])
        self.assertIsNone(prepare_image_foreground(runtime, TARGET))
        self.assertEqual(len(calls), 2)

    def test_foreground_failure_proves_no_image_input_dispatched(self):
        runtime = SimpleNamespace(mode="uia", guard=SimpleNamespace(checkpoint_ready_resolver=lambda hwnd: False),
            check_active=lambda: None, stop_event=threading.Event(), call=lambda *_: {"isError": True}, image_action=mock.Mock())
        result = execute_image_step(runtime, {"operation": "image_click", "image_target": image_target()}, TARGET,
                                    prepare_foreground=True)
        self.assertFalse(result["input_dispatched"])
        runtime.image_action.assert_not_called()


class PendingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = FakeRuntime(self.tmp.name); self.runtime.stop_event = threading.Event()
        self.runtime.image_action = mock.Mock()
        self.runtime.capture_checkpoint = lambda t: {"structuredContent": t,
            "content": [{"type": "image", "data": png(), "mimeType": "image/png"}]}
        self.task = {"id": "recorded", "program_ids": ["editor"], "variables": {}, "steps": [
            {"program_id": "editor", "operation": "image_click", "image_target": image_target()},
            {"program_id": "editor", "operation": "checkpoint", "message": "Check actual result"}]}
        self.targets = [{"program_id": "editor", **TARGET}]
        self.runner = WorkflowRunner(self.tmp.name)

    def run_task(self, **kwargs):
        return self.runner.run(self.runtime, self.task, {}, self.targets, **kwargs)

    def test_not_sent_pending_reexecutes_once_after_fresh_matching(self):
        self.runtime.image_action.side_effect = [
            {"input_dispatched": False, "task_verified": False, "diagnostic": {"code": "image_requires_foreground"}},
            {"input_dispatched": True, "task_verified": False, "verification_deferred": True}]
        first = self.run_task()
        self.assertTrue(first["can_resume_pending_input"])
        self.assertEqual(self.runner.progress(first["run_id"])["step_delivery"]["state"], "not_sent")
        resumed = self.run_task(resume_run_id=first["run_id"])
        self.assertEqual(resumed["completed_steps"], 1)
        self.assertTrue(resumed["checkpoint"]["capture_available"])
        self.assertEqual(self.runtime.image_action.call_count, 2)
        done = self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=resumed["checkpoint"]["id"])
        self.assertTrue(done["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 2)

    def test_unknown_legacy_record_and_sent_input_never_reexecute(self):
        for response in ({"task_verified": False}, {"task_verified": False, "input_dispatched": True}):
            self.runtime.image_action.reset_mock()
            self.runtime.image_action.return_value = response
            first = self.run_task()
            result = self.run_task(resume_run_id=first["run_id"])
            self.assertEqual(result["last_result"]["diagnostic"]["code"], "image_action_uncertain")
            self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_v014_record_without_delivery_evidence_never_replays_image(self):
        self.runtime.image_action.return_value = {"task_verified": False, "input_dispatched": False}
        first = self.run_task()
        path = self.runner._path(first["run_id"])
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("step_delivery")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertNotIn("step_delivery", self.runner.progress(first["run_id"]))
        self.runtime.id = "new-session-after-upgrade"
        result = self.run_task(resume_run_id=first["run_id"])
        self.assertEqual(result["last_result"]["diagnostic"]["code"], "image_action_uncertain")
        self.assertFalse(result["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_v014_pending_uia_record_reobserves_without_repeating_input(self):
        self.task["steps"] = [{"program_id": "editor", "operation": "set_value",
            "selector": {"name": "Family", "role": "Edit"}, "value": "W"}]
        engine = ScriptedOperations([RuntimeError("old client lost result"), {"task_verified": True}])
        with mock.patch("operations.Operations", return_value=engine):
            first = self.run_task()
            path = self.runner._path(first["run_id"])
            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy.pop("step_delivery")
            path.write_text(json.dumps(legacy), encoding="utf-8")
            result = self.run_task(resume_run_id=first["run_id"])
        self.assertTrue(result["task_verified"])
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["set_value", "assert"])

    def test_v014_checkpoint_record_remains_acknowledgeable_without_input_replay(self):
        self.runtime.image_action.return_value = {"task_verified": False, "input_dispatched": True, "verification_deferred": True}
        first = self.run_task()
        path = self.runner._path(first["run_id"])
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("step_delivery")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        result = self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
        self.assertTrue(result["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_safe_resume_never_replays_preceding_completed_image(self):
        self.task["steps"] += copy.deepcopy(self.task["steps"])
        self.runtime.image_action.side_effect = [
            {"input_dispatched": True, "task_verified": False, "verification_deferred": True},
            {"input_dispatched": False, "task_verified": False},
            {"input_dispatched": True, "task_verified": False, "verification_deferred": True}]
        first = self.run_task()
        second = self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
        self.assertEqual(second["pending_step"], 2)
        third = self.run_task(resume_run_id=first["run_id"])
        self.assertEqual(third["pending_step"], 2)
        self.assertEqual(third["checkpoint"]["source_step_index"], 1)
        self.assertEqual(self.runtime.image_action.call_count, 2)
        fourth = self.run_task(resume_run_id=first["run_id"], acknowledge_checkpoint=third["checkpoint"]["id"])
        self.assertEqual(fourth["pending_step"], 3)
        self.assertEqual(self.runtime.image_action.call_count, 3)

    def test_not_sent_input_rechecks_prior_semantic_condition_before_retry(self):
        self.task["steps"].insert(0, {"program_id": "editor", "operation": "set_value",
            "selector": {"name": "Family", "role": "Edit"}, "value": "W"})
        self.runtime.image_action.return_value = {"task_verified": False, "input_dispatched": False}
        engine = ScriptedOperations([{"task_verified": True}, {"task_verified": False}])
        with mock.patch("operations.Operations", return_value=engine):
            first = self.run_task()
            self.assertEqual(first["pending_step"], 1)
            resumed = self.run_task(resume_run_id=first["run_id"])
        self.assertFalse(resumed["task_verified"])
        self.assertEqual([step["operation"] for step, _, _ in engine.calls], ["set_value", "assert"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_wrong_starting_screen_explains_restore_not_rerecord(self):
        self.runtime.image_action.return_value = {"task_verified": False, "input_dispatched": False,
            "diagnostic": {"code": "image_not_found", "image_match": {"score": .52}}}
        result = self.run_task()
        diagnostic = result["last_result"]["diagnostic"]
        self.assertEqual(diagnostic["code"], "start_screen_not_ready")
        self.assertEqual(diagnostic["cause"], "image_not_found")
        self.assertEqual(diagnostic["image_match"]["score"], .52)
        self.assertTrue(result["can_resume_pending_input"])

    def test_human_uia_click_requires_checkpoint_and_never_asserts_fake_success(self):
        self.task["steps"][0] = {"program_id": "editor", "operation": "click",
            "selector": {"name": "Query", "role": "Button"}, "completion_mode": "human"}
        validate_recipe(self.task["steps"], {}, ["editor"])
        with self.assertRaises(WorkflowError): validate_recipe(self.task["steps"][:1], {}, ["editor"])
        engine = ScriptedOperations([{"task_verified": False, "input_dispatched": True, "verification_deferred": True}])
        with mock.patch("operations.Operations", return_value=engine):
            result = self.run_task()
            self.assertEqual(result["completed_steps"], 1)
            self.assertFalse(result["task_verified"])
            done = self.run_task(resume_run_id=result["run_id"], acknowledge_checkpoint=result["checkpoint"]["id"])
        self.assertTrue(done["task_verified"])
        self.assertEqual(len(engine.calls), 1)

    def test_corrupt_delivery_evidence_cannot_enable_replay(self):
        self.runtime.image_action.return_value = {"task_verified": False, "input_dispatched": False}
        first = self.run_task()
        path = self.runner._path(first["run_id"])
        persisted = json.loads(path.read_text(encoding="utf-8"))
        persisted["step_delivery"]["state"] = "probably_not_sent"
        path.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaises(WorkflowError): self.run_task(resume_run_id=first["run_id"])


class ExactImageCacheTests(unittest.TestCase):
    def test_unchanged_fresh_image_reuses_global_search_changed_image_rechecks_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = SimpleNamespace(run_dir=folder, check_active=lambda: None, stop_event=threading.Event())
            count = [0]
            def matcher_process(args, **kwargs):
                request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
                count[0] += 1
                width, height = png_dimensions(request["screenshot_png"])
                response = {"nonce": request["nonce"], "status": "matched" if count[0] == 1 else "ambiguous",
                    "score": .99, "candidate_count": count[0], "screenshot": {"width": width, "height": height},
                    "rect": {"x": 10, "y": 10, "width": 16, "height": 16}}
                Path(args[3]).write_text(json.dumps(response), encoding="utf-8")
                return SimpleNamespace(poll=lambda: 0, returncode=0)
            with mock.patch("image_targets.subprocess.Popen", side_effect=matcher_process):
                first = match_image(runtime, image_target(), png(100, 100))
                same = match_image(runtime, image_target(), png(100, 100))
                changed = match_image(runtime, image_target(), png(101, 100))
            self.assertFalse(first["search_reused"])
            self.assertTrue(same["search_reused"])
            self.assertEqual(count[0], 2)
            self.assertEqual(changed["status"], "ambiguous")
            self.assertNotIn("x", changed)


if __name__ == "__main__": unittest.main()
