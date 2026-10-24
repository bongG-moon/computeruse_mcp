"""Explicit popup review conversion preserves recipes and fails closed; no UI I/O."""
import copy
import tempfile
import unittest
from unittest import mock

from checkpoint_groups import use_popup_image_conditions
from server import TaskStore
from task_revision import TaskRevisions, TaskRevisionError
from test_image_steps import image_target, png
from test_recording_windows import step as window_wait
import test_workflow_windows as window_fixture
from test_workflows import ScriptedOperations
from workflows import validate_recipe


def popup_steps(prefix="one", *, origin=None, final_mode="human"):
    popup = "popup_" + prefix
    review = {"step_id": prefix+"-opened", "program_id": "editor", "window_ref": popup,
        "operation": "checkpoint", "review_mode": "visual", "opened_from": "main",
        "message": "Original popup review " + prefix}
    if origin is not None:
        review["review_origin"] = origin
    return [
        {"step_id": prefix+"-open", "program_id": "editor", "operation": "image_click", "image_target": image_target()},
        window_wait(step_id=prefix+"-wait", window_ref=popup),
        review,
        {"step_id": prefix+"-choose", "program_id": "editor", "window_ref": popup,
         "operation": "image_click", "image_target": image_target()},
        {"step_id": prefix+"-result", "program_id": "editor", "operation": "checkpoint",
         "review_mode": final_mode, "return_from": popup, "message": "Value is " + prefix}]


class PopupReviewRevisionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = TaskStore(folder.name, {"programs": [{"id": "editor", "name": "Editor"}]})
        self.api = TaskRevisions(self.store)
        self.args = {"id": "popup-review", "name": "Existing recording", "instructions": "Select and verify",
            "expected": "Selected value checked", "program_ids": ["editor"], "steps": popup_steps()}
        self.saved = self.store.save(self.args)

    def convert(self, ids=None):
        return self.api.update(self.saved["id"], 1,
            [{"op": "use_popup_image_conditions", "checkpoint_ids": ids or ["one-opened"]}])

    def test_explicit_revision_preserves_original_reviews_ids_and_input_images(self):
        answer = self.convert()
        self.assertEqual(answer["revision"], 2)
        self.assertEqual(self.api.get(self.saved["id"], 1)["steps"], self.saved["steps"])
        current = answer["task"]["steps"]
        self.assertEqual([s["step_id"] for s in current], [s["step_id"] for s in self.saved["steps"]])
        for index in (0, 1, 3, 4):
            self.assertEqual(current[index], self.saved["steps"][index])
        self.assertEqual(current[2]["review_mode"], "popup_image")
        self.assertEqual(current[2]["review_origin"], "user_grouped")
        self.assertEqual(current[2]["image_target"], current[3]["image_target"])
        summary = answer["changes"][0]
        self.assertFalse(summary["input_steps_changed"])
        self.assertFalse(summary["business_result_verified"])
        self.assertEqual(summary["original_reviews"][0]["message"], self.saved["steps"][2]["message"])
        self.assertIsNone(summary["original_reviews"][0]["review_origin"])
        with self.assertRaises(TaskRevisionError): self.convert()

    def test_allowed_origins_and_right_click_preserve_the_input(self):
        for origin in (None, "recording_generated", "user_grouped"):
            steps = popup_steps(origin=origin)
            steps[3]["operation"] = "image_right_click"
            original = copy.deepcopy(steps)
            changed = use_popup_image_conditions(steps, ["one-opened"])
            self.assertEqual(steps, original)
            self.assertEqual(changed[3], original[3])
            self.assertEqual(changed[2]["review_origin"], origin or "user_grouped")
            validate_recipe(changed, {}, ["editor"])

    def test_human_explicit_deferred_and_non_popup_reviews_are_rejected(self):
        for fields in ({"review_mode": "human"}, {"review_origin": "explicit"},
                       {"review_mode": "deferred_visual"}, {"opened_from": None},
                       {"return_from": "other"}):
            with self.subTest(fields=fields):
                steps = popup_steps()
                steps[2].update(fields)
                with self.assertRaises(ValueError): use_popup_image_conditions(steps, ["one-opened"])

    def test_only_immediately_following_same_popup_click_is_allowed(self):
        for fields in ({"program_id": "other"}, {"window_ref": "main"},
                       {"operation": "image_type_text"}, {"operation": "image_double_click"},
                       {"image_target": None}):
            with self.subTest(fields=fields):
                steps = popup_steps()
                steps[3].update(fields)
                with self.assertRaises(ValueError): use_popup_image_conditions(steps, ["one-opened"])
        with self.assertRaises(ValueError): use_popup_image_conditions(popup_steps()[:3], ["one-opened"])

    def test_unknown_duplicate_or_invalid_ids_and_mixed_selection_are_atomic(self):
        before = self.store.path.read_bytes()
        for ids in ([], ["absent"], ["one-opened", "one-opened"], [1], ["bad id"],
                    ["one-opened", "one-result"]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    self.api.update(self.saved["id"], 1,
                        [{"op": "use_popup_image_conditions", "checkpoint_ids": ids}])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_existing_nine_visual_checks_reduce_to_four_without_rerecording(self):
        steps = []
        for prefix in ("a", "b", "c", "d"):
            steps.extend(popup_steps(prefix, final_mode="visual"))
        steps.append({"step_id": "final", "program_id": "editor", "operation": "checkpoint",
            "review_mode": "visual", "message": "Final business result"})
        saved = self.store.save({**self.args, "id": "nine-checks", "steps": steps})
        changed = self.api.update("nine-checks", saved["revision"], [
            {"op": "use_popup_image_conditions", "checkpoint_ids": [p+"-opened" for p in ("a", "b", "c", "d")]},
            {"op": "group_reviews", "checkpoint_ids": ["a-result", "b-result"], "condition": "Both selected values remain set"}
        ])["task"]
        self.assertEqual(sum(s.get("review_mode") == "visual" for s in changed["steps"]), 4)
        self.assertEqual(sum(s.get("review_mode") == "popup_image" for s in changed["steps"]), 4)
        self.assertEqual([s for s in changed["steps"] if s["operation"] != "checkpoint"],
                         [s for s in saved["steps"] if s["operation"] != "checkpoint"])
        self.assertEqual(self.api.get("nine-checks", 1)["steps"], saved["steps"])


class PopupReviewExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = window_fixture.WorkflowWindowTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.task["steps"] = use_popup_image_conditions(popup_steps(), ["one-opened"])

    def test_ambiguous_or_missing_target_never_clicks_next_or_repeats_open(self):
        fixture = self.fixture
        for code in ("image_ambiguous", "image_not_found"):
            with self.subTest(code=code):
                calls = []
                def action(spec, target):
                    calls.append(spec["operation"])
                    if spec["operation"] == "wait_for_image":
                        return {"task_verified": False, "input_dispatched": False, "diagnostic": {"code": code}}
                    return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}
                fixture.runtime.image_action = action
                first = fixture.run_task(ScriptedOperations())
                self.assertEqual(first["pending_step"], 2)
                self.assertFalse(first["task_verified"])
                resumed = fixture.run_task(ScriptedOperations(), resume_run_id=first["run_id"])
                self.assertFalse(resumed["task_verified"])
                self.assertEqual(calls.count("image_click"), 1)

    def test_unique_popup_condition_does_not_approve_final_human_result(self):
        fixture = self.fixture
        calls = []
        def action(spec, target):
            calls.append(spec["operation"])
            return ({"task_verified": True, "input_dispatched": False} if spec["operation"] == "wait_for_image" else
                    {"task_verified": False, "input_dispatched": True, "verification_deferred": True})
        fixture.runtime.image_action = action
        fixture.runtime.capture_checkpoint = mock.Mock(return_value={"content": [
            {"type": "image", "mimeType": "image/png", "data": png()}]})
        result = fixture.run_task(ScriptedOperations())
        self.assertEqual(calls, ["image_click", "wait_for_image", "image_click"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["pending_step"], 4)
        self.assertTrue(result["checkpoint"]["human_review_required"])
        self.assertEqual(result["step_receipts"][3]["status"], "awaiting_verification")


if __name__ == "__main__":
    unittest.main()
