"""Local authoring integration; real store/IPC, inert mock app input."""
import copy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from operations import OperationError
from task_revision import TaskRevisions
import test_process_editor as fixture


class LocalAuthoringEditorTests(unittest.TestCase):
    # Reuse only the fixture methods, not its unrelated regression test cases.
    setUp = fixture.EditorIPCTests.setUp
    spawn = fixture.EditorIPCTests.spawn
    start = fixture.EditorIPCTests.start
    status = fixture.EditorIPCTests.status
    terminal = fixture.EditorIPCTests.terminal
    command = fixture.EditorIPCTests.command
    context = fixture.EditorIPCTests.context

    def open_delay(self):
        self.context()
        opened = self.start()
        result = self.command("add_step", {**self.args["targets"][0], "action": "delay", "seconds": .1})
        self.assertEqual(result["status"], "ok", result)
        return opened, self.editors.jobs[opened["editor_id"]]

    def save(self, name="local test"):
        saved = self.command("save", {"name": name, "description": "local only"})
        self.assertEqual(saved["status"], "ok", saved)
        return saved["saved_task"]

    def test_saved_editor_stays_open_and_later_save_is_new_revision(self):
        opened, job = self.open_delay()
        first = self.save()
        self.assertTrue(first["runnable"])
        self.assertEqual(first["step_count"], 1)
        self.assertFalse(job["done"].is_set())
        self.assertFalse(job["dirty"])
        self.command("edit_step", {"index": 0, "seconds": .2})
        second = self.save()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["revision"], first["revision"] + 1)
        revisions = TaskRevisions(self.manager.tasks)
        self.assertEqual(revisions.get(first["id"], first["revision"])["steps"][0]["duration_ms"], 100)
        self.assertEqual(revisions.get(first["id"], second["revision"])["steps"][0]["duration_ms"], 200)
        self.status(opened, cancel=True)
        self.terminal(opened)

    def test_undo_redo_changes_only_draft_and_keeps_latest_saved_identity(self):
        _, job = self.open_delay()
        self.save()
        self.command("edit_step", {"index": 0, "seconds": .2})
        latest = self.save()
        undo = self.command("undo", {})
        self.assertEqual(undo["status"], "ok", undo)
        self.assertEqual(job["draft"].steps[0]["duration_ms"], 100)
        self.assertEqual((job["draft"].task_id, job["draft"].revision), (latest["id"], latest["revision"]))
        self.assertTrue(undo["dirty"])
        self.assertTrue(undo["can_redo"])
        self.assertEqual(self.manager.tasks.get(latest["id"])["steps"][0]["duration_ms"], 200)
        redo = self.command("redo", {})
        self.assertEqual(redo["status"], "ok", redo)
        self.assertEqual(job["draft"].steps[0]["duration_ms"], 200)
        self.assertFalse(redo["can_redo"])

    def test_invalid_input_edit_rolls_back_draft_and_does_not_create_history(self):
        _, job = self.open_delay()
        before, history = copy.deepcopy(job["draft"].__dict__), len(job.get("undo", []))
        result = self.command("input_edit", {"action": "bind", "index": 0, "name": "condition",
            "spec": {"type": "text", "default": "N"}})
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(job["draft"].__dict__, before)
        self.assertEqual(len(job.get("undo", [])), history)

    def test_input_edit_integration_and_typed_preview_never_touch_app(self):
        _, job = self.open_delay()
        job["draft"].steps = [{"operation": "set_value", "program_id": "editor",
            "selector": fixture.FIELD_SELECTOR, "value": "10", "step_id": "saved-step",
            "expect": [{"selector": fixture.FIELD_SELECTOR, "property": "value", "equals": "10"}]}]
        job["draft"].labels = ["입력값"]
        result = self.command("input_edit", {"edit": {"action": "bind", "index": 0, "name": "count",
            "spec": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["inputs"][0]["use_count"], 2)
        preview = self.command("preview_step", {"index": 0})["preview"]
        self.assertEqual(preview["editable"]["variable"], "count")
        self.assertEqual(preview["editable"]["variable_spec"]["type"], "integer")
        self.assertEqual(self.runtime.calls, [])
        self.command("undo", {})
        self.assertEqual(job["draft"].steps[0]["value"], "10")
        self.assertEqual(job["draft"].variables, {})

    def test_trial_inputs_and_task_are_frozen_before_runner_and_resume(self):
        opened, job = self.open_delay()
        self.command("input_edit", {"action": "upsert", "name": "count", "spec": {"type": "integer"}})
        supplied = {"count": 12}
        def trial(runtime, task, targets, *, inputs):
            self.assertEqual(inputs, {"count": 12})
            inputs["count"] = 999
            task["steps"][0]["duration_ms"] = 999
            targets[0]["window_id"] = 999
            return {"status": "needs_review", "task_verified": False, "run_id": "frozen-local-trial"}
        self.editors.test_runner = trial
        answer = self.command("test_run", {"name": "trial", "description": "", "inputs": supplied})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(job["test_inputs"], {"count": 12})
        self.assertEqual(job["test_task"]["steps"][0]["duration_ms"], 100)
        self.assertEqual(job["test_targets"], self.args["targets"])
        self.command("input_edit", {"action": "upsert", "name": "count", "spec": {"type": "integer", "default": 15}})
        self.command("edit_step", {"index": 0, "seconds": .2})
        self.editors.test_runner = mock.Mock(return_value={"status": "verified", "task_verified": True, "run_id": "frozen-local-trial"})
        with mock.patch.object(self.editors, "_trial_signal"):
            self.editors.resume_test(opened["editor_id"], self.runtime, observation_review={"token": "observed"})
        call = self.editors.test_runner.call_args
        self.assertEqual(call.kwargs["inputs"], {"count": 12})
        self.assertEqual(call.kwargs["resume_run_id"], "frozen-local-trial")
        self.assertEqual(call.args[1]["steps"][0]["duration_ms"], 100)
        self.assertNotIn("default", call.args[1]["variables"]["count"])

    def test_invalid_trial_input_does_not_reach_runner(self):
        self.open_delay()
        self.command("input_edit", {"action": "upsert", "name": "count", "spec": {"type": "integer", "minimum": 1}})
        self.editors.test_runner = mock.Mock()
        for inputs in ({}, {"count": "2"}, {"count": 0}, {"count": 2, "extra": "bad"}):
            answer = self.command("test_run", {"name": "trial", "description": "", "inputs": inputs})
            self.assertEqual(answer["status"], "error", answer)
        self.editors.test_runner.assert_not_called()

    def test_saved_recording_trial_does_not_bind_closed_popup_hwnds(self):
        from vendor.guard import GuardError
        opened, job = self.open_delay()
        main = self.args['targets'][0]
        popup = main['window_id'] + 100
        job['draft'].recorded([
            {**fixture.image_choice(), 'operation': 'click', 'program_id': 'editor'},
            {**fixture.image_choice(window_id=popup), 'operation': 'click',
             'program_id': 'editor', 'window_ref': 'recorded_popup'}], windows=[
            {'program_id': 'editor', 'window_ref': 'recorded_popup', 'owner_ref': 'main',
             'pid': main['pid'], 'window_id': popup, 'owner_window_id': main['window_id'],
             'owner_verified': True, 'closed': True, 'title': '', 'class_name': 'ComboLBox'}])
        saved = self.save('recorded dropdown')
        before = copy.deepcopy(self.manager.tasks.get(saved['id']))
        self.assertTrue(any(row['window_id'] == popup for row in job['draft'].programs))
        def trial(runtime, task, targets, **kwargs):
            if any(target['window_id'] == popup for target in targets):
                raise GuardError('Window no longer exists.')
            self.assertEqual(targets, [main])
            self.assertTrue(any(step['operation'] == 'wait_for_window' for step in task['steps']))
            return {'status': 'needs_review', 'task_verified': False, 'run_id': 'dropdown-trial'}
        self.editors.test_runner = mock.Mock(side_effect=trial)
        answer = self.command('test_run', {'name': 'recorded dropdown', 'description': ''})
        self.assertEqual(answer['status'], 'ok', answer)
        self.assertEqual(job['test_targets'], [main])
        with mock.patch.object(self.editors, '_trial_signal'):
            self.editors.resume_test(opened['editor_id'], self.runtime,
                observation_review={'token': 'fresh-result'})
        self.assertEqual(self.editors.test_runner.call_count, 2)
        self.assertEqual(self.manager.tasks.get(saved['id']), before)

    def test_closed_main_trial_keeps_saved_task_and_reports_reconnect_without_input(self):
        from vendor.guard import GuardError
        _, job = self.open_delay()
        saved = self.save('main closed')
        before = copy.deepcopy(self.manager.tasks.get(saved['id']))
        self.runtime.guard.window_resolver = mock.Mock(side_effect=GuardError('Window no longer exists.'))
        self.editors.test_runner = mock.Mock()
        answer = self.command('test_run', {'name': 'main closed', 'description': ''})
        self.assertEqual(answer['status'], 'error', answer)
        self.assertEqual(answer['code'], 'editor_target_unavailable')
        self.assertNotIn('Window no longer exists', answer['message'])
        self.editors.test_runner.assert_not_called()
        self.assertEqual(self.manager.tasks.get(saved['id']), before)
        self.assertFalse(job['done'].is_set())

    def test_skill_preview_requires_saved_draft_and_exports_exact_bytes(self):
        _, job = self.open_delay()
        denied = self.command("skill_preview", {})
        self.assertEqual(denied["code"], "skill_unsaved_changes", denied)
        saved = self.save()
        answer = self.command("skill_preview", {})
        self.assertEqual(answer["status"], "ok", answer)
        preview = answer["skill_preview"]
        self.assertEqual(preview["revision"], saved["revision"])
        self.assertNotIn("local test", preview["skill_markdown"])
        result = self.command("skill_export", {"preview_id": preview["preview_id"]})
        self.assertEqual(result["status"], "ok", result)
        output = Path(result["skill_export"]["path"]).read_bytes()
        self.assertEqual(output, preview["skill_markdown"].encode("utf-8"))
        self.assertEqual(hashlib.sha256(output).hexdigest(), preview["sha256"])
        self.assertEqual(self.runtime.calls, [])

    def test_edit_invalidates_preview_and_undo_does_not_revive_it(self):
        self.open_delay()
        self.save()
        preview = self.command("skill_preview", {})["skill_preview"]
        self.command("edit_step", {"index": 0, "seconds": .2})
        self.command("undo", {})
        denied = self.command("skill_export", {"preview_id": preview["preview_id"]})
        self.assertEqual(denied["code"], "skill_unsaved_changes")
        self.save()
        denied = self.command("skill_export", {"preview_id": preview["preview_id"]})
        self.assertEqual(denied["code"], "skill_preview_stale")

    def test_recording_boundary_drops_stale_undo_history(self):
        _, job = self.open_delay()
        self.command("edit_step", {"index": 0, "seconds": .2})
        self.assertTrue(job["undo"])
        # The actual recorder is covered separately. Exercise the wrapper with
        # a successful import boundary; no OS input is generated by this test.
        with mock.patch.object(self.editors, "_command_impl", return_value={"status": "ok"}):
            self.editors._command(job, {"action": "record", "payload": {}})
        self.assertFalse(job.get("undo"))
        self.assertFalse(job.get("redo"))
        self.assertTrue(job["dirty"])

    def test_undo_does_not_erase_explicit_ack_for_same_recording_warnings(self):
        _, job = self.open_delay()
        draft = job["draft"]
        draft.recording_warnings = ["uia_text_unavailable"]
        draft.recording_acknowledged = False
        self.command("edit_step", {"index": 0, "seconds": .2})
        answer = self.command("ack_recording_warnings", {"acknowledged": True})
        self.assertEqual(answer["status"], "ok", answer)
        self.assertTrue(draft.recording_acknowledged)
        self.command("undo", {})
        self.assertTrue(draft.recording_acknowledged)
        self.assertEqual(draft.recording_warnings, ["uia_text_unavailable"])

    def test_undo_keeps_live_selections_made_after_edit(self):
        _, job = self.open_delay()
        self.command("edit_step", {"index": 0, "seconds": .2})
        # The live selection cache is not recipe data and must not be rewound.
        selection = {"selection_id": "fresh-selection", "selector": copy.deepcopy(fixture.FIELD_SELECTOR)}
        job["draft"].selections["fresh-selection"] = copy.deepcopy(selection)
        self.command("undo", {})
        self.assertEqual(job["draft"].selections["fresh-selection"], selection)

    def test_undo_cannot_reuse_acknowledgement_for_a_different_warning_set(self):
        _, job = self.open_delay()
        draft = job["draft"]
        draft.recording_warnings = ["uia_text_unavailable"]
        draft.recording_acknowledged = True
        self.command("edit_step", {"index": 0, "seconds": .2})
        draft.recording_warnings = ["outside_target_not_recorded"]
        draft.recording_acknowledged = True
        result = self.command("undo", {})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(draft.recording_warnings, ["uia_text_unavailable"])
        self.assertFalse(draft.recording_acknowledged)
        self.assertTrue(result["readiness"]["review_required"])


if __name__ == "__main__":
    unittest.main()
