"""Task edits are atomic, image-preserving and revision-bound; no desktop I/O."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from server import TaskStore, SessionError
from task_revision import TaskRevisions, TaskRevisionError, stable_task
from workflows import WorkflowRunner, WorkflowError, render_steps, validate_recipe
from test_image_steps import image_target
from test_workflows import FakeRuntime, ScriptedOperations


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name, {"programs": [{"id": "editor", "name": "Editor"}]})
        self.api = TaskRevisions(self.store)
        self.args = {"id": "sample", "name": "Task", "instructions": "Use saved steps", "expected": "Two values match",
            "program_ids": ["editor"], "variables": {"condition": {"type": "enum", "values": ["W", "N"], "default": "W"}},
            "steps": [{"step_id": "input", "program_id": "editor", "operation": "set_value", "selector": {"name": "Family"},
                "value": {"input_ref": "condition"}, "expect": [{"selector": {"name": "Family"}, "property": "value", "equals": {"input_ref": "condition"}}]},
                {"step_id": "image", "program_id": "editor", "operation": "image_click", "image_target": image_target()},
                {"step_id": "review", "program_id": "editor", "operation": "checkpoint", "message": "Check result"}]}
        self.saved = self.store.save(self.args)

    def test_partial_input_patch_preserves_every_other_step_and_raw_image(self):
        answer = self.api.update("sample", 1, [{"op": "set_default", "name": "condition", "value": "N"}])
        after = answer["task"]
        self.assertEqual(after["steps"], self.saved["steps"])
        self.assertEqual(after["revision"], 2)
        self.assertEqual(self.api.get("sample", 1)["variables"]["condition"]["default"], "W")
        self.assertEqual(render_steps(after, {})[0][0]["value"], "N")
        self.assertEqual(render_steps(after, {})[0][0]["expect"][0]["equals"], "N")
        self.assertEqual(self.api.history("sample")["current_revision"], 2)

    def test_unversioned_legacy_resaves_preserve_original_revision(self):
        legacy = copy.deepcopy(self.saved)
        legacy.pop("revision")
        self.store._write([legacy])
        revised = self.store.save({**self.args, "name": "Second"})
        self.assertEqual(revised["revision"], 2)
        third = self.store.save({**self.args, "name": "Third"})
        self.assertEqual(third["revision"], 3)
        self.assertEqual(self.api.get("sample", 1)["name"], "Task")
        self.assertEqual(self.api.get("sample", 2)["name"], "Second")
        self.assertEqual([r["revision"] for r in self.api.history("sample")["revisions"]], [1, 2, 3])

    def test_legacy_revision_api_exposes_first_revision_without_disk_migration(self):
        legacy = copy.deepcopy(self.saved)
        legacy.pop("revision")
        self.store._write([legacy])
        before = self.store.path.read_bytes()
        current = self.api.get("sample")
        self.assertEqual(current["revision"], 1)
        self.assertEqual(self.api.get("sample", 1), current)
        self.assertNotIn("revision", self.store.get("sample"))
        self.assertEqual(self.store.path.read_bytes(), before)
        revised = self.api.update("sample", current["revision"],
            [{"op": "set_default", "name": "condition", "value": "N"}])["task"]
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(self.api.get("sample", 1)["variables"]["condition"]["default"], "W")

    def test_partial_step_change_does_not_need_round_trip_image_data(self):
        changed = self.api.update("sample", 1, [{"op": "set_step", "step_id": "input", "fields": {"selector": {"name": "Other"}}}])["task"]
        self.assertEqual(changed["steps"][1:], self.saved["steps"][1:])
        self.assertEqual(changed["steps"][0]["selector"], {"name": "Other"})
        self.assertEqual(changed["steps"][0]["expect"], self.saved["steps"][0]["expect"])

    def test_conflict_and_failed_validation_never_change_task_bytes(self):
        self.api.update("sample", 1, [{"op": "set_default", "name": "condition", "value": "N"}])
        original = self.store.path.read_bytes()
        with self.assertRaises(TaskRevisionError) as failure:
            self.api.update("sample", 1, [{"op": "set_text", "fields": {"name": "old edit"}}])
        self.assertEqual(failure.exception.code, "revision_conflict")
        with self.assertRaises(ValueError):
            self.api.update("sample", 2, [{"op": "set_default", "name": "condition", "value": "unsupported"}])
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_atomic_write_failure_keeps_current_and_visible_history(self):
        original = self.store.path.read_bytes()
        with mock.patch.object(self.store, "_write", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(SessionError):
                self.api.update("sample", 1, [{"op": "set_text", "fields": {"name": "not saved"}}])
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(len(self.api.history("sample")["revisions"]), 1)
        self.assertEqual(self.api.get("sample")["revision"], 1)

    def test_restore_creates_new_revision_and_preserves_old_image_assets(self):
        revised = self.api.update("sample", 1, [{"op": "set_default", "name": "condition", "value": "N"}])["task"]
        restored = self.api.restore("sample", 2, 1)["task"]
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["variables"]["condition"]["default"], "W")
        self.assertEqual(restored["steps"], self.saved["steps"])
        self.assertEqual(self.api.get("sample", 2)["variables"], revised["variables"])
        self.assertEqual([r["revision"] for r in self.api.history("sample")["revisions"]], [1, 2, 3])

    def test_editor_save_preserves_ids_and_rejects_stale_draft(self):
        args = copy.deepcopy(self.args)
        args["steps"][0]["selector"] = {"name": "Replacement"}
        edited = self.api.save(args, expected_revision=1)
        self.assertEqual([s["step_id"] for s in edited["steps"]], [s["step_id"] for s in self.saved["steps"]])
        self.assertEqual(edited["steps"][1]["image_target"], self.saved["steps"][1]["image_target"])
        with self.assertRaises(TaskRevisionError): self.api.save(args, expected_revision=1)

    def test_concurrent_partial_edits_have_single_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def edit(name):
            api = TaskRevisions(TaskStore(self.tmp.name, self.store.config))
            barrier.wait()
            try: outcomes.append(api.update("sample", 1, [{"op": "set_text", "fields": {"name": name}}]))
            except TaskRevisionError as error: outcomes.append(error.code)
        threads = [threading.Thread(target=edit, args=(name,)) for name in ("one", "two")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertIn("revision_conflict", outcomes)
        self.assertEqual(self.api.get("sample")["revision"], 2)

    def test_skill_is_revision_pinned_without_pixels_coordinates_or_installation(self):
        answer = self.api.export_skill("sample", 1)
        self.assertEqual(answer["manifest"]["pinned_revision"], 1)
        self.assertFalse(answer["installed"])
        self.assertNotIn("template_png", answer["skill_markdown"])
        self.assertNotIn("selector", answer["skill_markdown"])
        self.assertIn("computer_run_task", answer["skill_markdown"])

    def test_revision_tampering_is_rejected(self):
        self.api.update("sample", 1, [{"op": "set_text", "fields": {"name": "new"}}])
        ref = self.api.history("sample")["revisions"][0]
        path = self.api._path(ref["sha256"])
        value = json.loads(path.read_text(encoding="utf-8"))
        value["steps"][0]["value"] = "changed"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(TaskRevisionError): self.api.get("sample", 1)

    def test_chat_cannot_patch_program_identity_image_bytes_or_remove_checks(self):
        for fields in ({"program_id": "other"}, {"image_target": image_target()}, {"operation": "hotkey"}, {"expect": []}):
            with self.subTest(fields=list(fields)), self.assertRaises((ValueError, SessionError)):
                self.api.update("sample", 1, [{"op": "set_step", "step_id": "input", "fields": fields}])
        self.assertEqual(self.api.get("sample")["revision"], 1)

    def test_insert_move_delete_validate_whole_recipe_and_preserve_assets(self):
        result = self.api.update("sample", 1, [{"op": "insert_step", "after_step_id": None,
            "step": {"step_id": "pause", "program_id": "editor", "operation": "delay", "duration_ms": 10}}])["task"]
        self.assertEqual(result["steps"][0]["step_id"], "pause")
        result = self.api.update("sample", 2, [{"op": "move_step", "step_id": "pause", "after_step_id": "review"}])["task"]
        self.assertEqual(result["steps"][-1]["step_id"], "pause")
        result = self.api.update("sample", 3, [{"op": "delete_step", "step_id": "pause"}])["task"]
        self.assertEqual(result["steps"], self.saved["steps"])
        before = self.store.path.read_bytes()
        with self.assertRaises((ValueError, SessionError)):
            self.api.update("sample", 4, [{"op": "delete_step", "step_id": "review"}])
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.api.update("sample", 4, [{"op": "insert_step", "after_step_id": "review", "step": self.saved["steps"][1]}])


class TypedInputTests(unittest.TestCase):
    def task(self, variables, value, *, checked=False):
        return {"id": "test", "revision": 1, "program_ids": ["editor"], "variables": variables,
            "steps": [{"step_id": "set", "program_id": "editor", "operation": "set_checked" if checked else "set_value",
                "selector": {"name": "Field"}, "checked" if checked else "value": value}]}

    def test_typed_numbers_dates_booleans_and_enums(self):
        for spec, value, expected in (({"type": "number"}, 12.5, "12.5"), ({"type": "integer"}, 3, "3"),
                ({"type": "date"}, "2026-10-10", "2026-10-10"), ({"type": "enum", "values": ["W", "N"]}, "N", "N")):
            task = self.task({"field": spec}, {"input_ref": "field"})
            self.assertEqual(render_steps(task, {"field": value})[0][0]["value"], expected)
        boolean = self.task({"field": {"type": "boolean", "default": True}}, {"input_ref": "field"}, checked=True)
        self.assertIs(render_steps(boolean, {})[0][0]["checked"], True)

    def test_wrong_types_and_dates_rejected_without_coercion(self):
        for spec, value in (({"type": "number"}, True), ({"type": "integer"}, 1.5), ({"type": "date"}, "2026-02-30"),
                ({"type": "enum", "values": ["W", "N"]}, "w"), ({"type": "text"}, 5)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_steps(self.task({"field": spec}, {"input_ref": "field"}), {"field": value})

    def test_legacy_ids_are_stable_without_mutation(self):
        task = self.task({}, "old")
        del task["steps"][0]["step_id"]
        first = stable_task(task)
        self.assertEqual(first, stable_task(copy.deepcopy(task)))
        self.assertNotIn("step_id", task["steps"][0])

    def test_inputs_cannot_replace_program_operation_or_key_names(self):
        for field in ("program_id", "operation", "key"):
            task = self.task({"value": {"default": "editor"}}, "fixed")
            task["steps"][0][field] = "${value}"
            with self.subTest(field=field), self.assertRaises(ValueError): render_steps(task, {})

    def test_foreach_freezes_typed_targets_and_emits_iteration_ids(self):
        task = self.task({"devices": {"type": "list", "items": {"type": "text"}, "default": ["one", "two"]}}, "unused")
        task["steps"] = [{"step_id": "devices", "operation": "foreach", "input": "devices", "item": "device", "steps": [
            {"step_id": "device-name", "program_id": "editor", "operation": "set_value", "selector": {"name": {"input_ref": "device"}}, "value": "ready"}]}]
        steps, values = render_steps(task, {})
        self.assertEqual([s["selector"]["name"] for s in steps], ["one", "two"])
        self.assertEqual([s["iteration_id"] for s in steps], ["devices:1", "devices:2"])
        with tempfile.TemporaryDirectory() as folder:
            runtime, runner, engine = FakeRuntime(folder), WorkflowRunner(folder), ScriptedOperations()
            with mock.patch("operations.Operations", return_value=engine):
                answer = runner.run(runtime, task, {}, [{"program_id": "editor", "pid": 100, "window_id": 200}])
            self.assertTrue(answer["task_verified"])
            self.assertEqual([r["iteration_id"] for r in answer["step_receipts"]], ["devices:1", "devices:2"])
            self.assertEqual([r["step_id"] for r in answer["step_receipts"]], ["device-name", "device-name"])
        self.assertEqual(task["variables"]["devices"]["default"], values["devices"])

    def test_foreach_limits_and_nested_groups_rejected(self):
        task = self.task({"devices": {"type": "list", "items": {"type": "text"}}}, "unused")
        child = task["steps"][0]
        task["steps"] = [{"operation": "foreach", "input": "devices", "item": "device", "steps": [child]}]
        for value in ([], ["item"]*101, [123]):
            with self.subTest(length=len(value)), self.assertRaises(ValueError): render_steps(task, {"devices": value})
        task["steps"][0]["steps"] = [copy.deepcopy(task["steps"][0])]
        with self.assertRaises(ValueError): render_steps(task, {"devices": ["item"]})

    def test_snapshot_tampering_and_changed_list_cannot_dispatch(self):
        task = self.task({"field": {"default": "original"}}, {"input_ref": "field"})
        with tempfile.TemporaryDirectory() as folder:
            runtime, runner = FakeRuntime(folder), WorkflowRunner(folder)
            target = [{"program_id": "editor", "pid": 100, "window_id": 200}]
            with mock.patch("operations.Operations", return_value=ScriptedOperations([{"task_verified": False}])):
                first = runner.run(runtime, task, {}, target)
            engine = ScriptedOperations()
            with mock.patch("operations.Operations", return_value=engine), self.assertRaises(WorkflowError):
                runner.run(runtime, task, {"field": "changed"}, target, resume_run_id=first["run_id"])
            path = runner._snapshot_path(first["run_id"])
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["inputs"]["field"] = "tampered"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with mock.patch("operations.Operations", return_value=engine), self.assertRaises(WorkflowError):
                runner.run(runtime, task, {}, target, resume_run_id=first["run_id"])
            self.assertFalse(engine.calls)


if __name__ == "__main__": unittest.main()
