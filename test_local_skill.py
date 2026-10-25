"""Local previews pin reviewed bytes without leaking business task contents."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_skill import LocalSkillError, LocalSkillPreviews
from local_task_ids import LocalTaskIds
from server import TaskStore
from task_revision import TaskRevisions


class LocalSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name, {"programs": [{"id": "app", "name": "SECRET-PROGRAM"}]})
        self.args = {"id": "74a42409017846e6a34ec9d01eea1b2a", "name": "SECRET-WORKFLOW",
                     "instructions": "SECRET-INSTRUCTIONS", "expected": "SECRET-RESULT",
                     "program_ids": ["app"],
                     "variables": {"secret_column": {"type": "text", "description": "SECRET-DESCRIPTION",
                                                       "default": "https://secret.internal/SECRET-DEFAULT"}},
                     "steps": [{"step_id": "input", "program_id": "app", "operation": "set_value",
                                "selector": {"name": "SECRET-CONTROL"}, "value": {"input_ref": "secret_column"},
                                "expect": [{"selector": {"name": "SECRET-CONTROL"}, "property": "value",
                                            "equals": {"input_ref": "secret_column"}}]}]}
        self.saved = self.store.save(self.args)
        self.revisions = TaskRevisions(self.store)
        self.previews = LocalSkillPreviews(self.store, self.tmp.name)

    def make_preview(self, revision=1):
        return self.previews.preview(self.saved["id"], revision)

    def export(self, preview):
        return self.previews.export(preview["preview_id"], preview["task_id"], preview["revision"])

    def assert_code(self, code, callable_):
        with self.assertRaises(LocalSkillError) as failure:
            callable_()
        self.assertEqual(failure.exception.code, code)

    def test_preview_is_inert_and_omits_every_business_content_field(self):
        before = self.store.path.read_bytes()
        with mock.patch("socket.socket", side_effect=AssertionError("network not allowed")):
            preview = self.make_preview()
        body = preview["skill_markdown"]
        self.assertNotIn("SECRET", body)
        self.assertNotIn("secret_column", body)
        self.assertNotIn("secret.internal", body)
        self.assertNotIn(self.tmp.name, body)
        self.assertNotIn('"action": "get"', body)
        self.assertNotIn('"inputs"', body)
        self.assertIn('"action": "run_local"', body)
        self.assertIn('"revision": 1', body)
        self.assertIn(self.saved["id"], body)
        self.assertFalse(preview["installed"])
        self.assertFalse((Path(self.tmp.name) / "exports").exists())
        self.assertEqual(before, self.store.path.read_bytes())

    def test_export_uses_exact_preview_bytes_without_rendering_or_installation(self):
        preview = self.make_preview()
        expected = preview["skill_markdown"].encode("utf-8")
        with mock.patch("local_skill._render", side_effect=AssertionError("must not rerender")), \
             mock.patch("socket.socket", side_effect=AssertionError("network not allowed")):
            result = self.export(preview)
        content = Path(result["path"]).read_bytes()
        self.assertEqual(content, expected)
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["sha256"], preview["sha256"])
        self.assertEqual(result["bytes"], len(content))
        self.assertFalse(result["installed"])
        self.assertTrue(Path(result["path"]).is_relative_to(Path(self.tmp.name) / "exports" / "local"))
        self.assertEqual(list(Path(self.tmp.name).rglob("*.tmp")), [])

    def test_legacy_task_id_is_opaque_in_preview_and_exact_export(self):
        args = copy.deepcopy(self.args)
        args["id"] = "PRIVATE-smartmmdm-customer-query"
        saved = self.store.save(args)
        before = self.store.path.read_bytes()
        # A regular alias() call would acquire the already-held nonreentrant
        # store lock. Assert the explicit held-lock path instead of hanging.
        with mock.patch.object(LocalTaskIds, "alias", side_effect=AssertionError("nested lock")), \
             mock.patch.object(self.store, "_locked", wraps=self.store._locked) as locked:
            preview = self.previews.preview(saved["id"], saved["revision"])
            self.assertEqual(locked.call_count, 1)
        body = preview["skill_markdown"]
        request = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertRegex(request["task_id"], r"^task-[a-f0-9]{32}$")
        self.assertNotIn("PRIVATE", body)
        self.assertNotIn(saved["id"], body)
        self.assertEqual(request["revision"], saved["revision"])
        registry = LocalTaskIds(self.store, self.tmp.name)
        self.assertEqual(registry.resolve(request["task_id"]), saved["id"])
        self.assertEqual(preview["task_id"], saved["id"])
        with mock.patch.object(LocalTaskIds, "alias", side_effect=AssertionError("nested lock")):
            result = self.export(preview)
        self.assertEqual(Path(result["path"]).read_bytes(), body.encode("utf-8"))
        self.assertEqual(result["sha256"], preview["sha256"])
        self.assertEqual(self.store.path.read_bytes(), before)
        restarted = LocalSkillPreviews(TaskStore(self.tmp.name, self.store.config), self.tmp.name)
        self.assertEqual(restarted.preview(saved["id"], saved["revision"])["skill_markdown"], body)

    def test_replaced_legacy_alias_binding_invalidates_export(self):
        args = copy.deepcopy(self.args)
        args["id"] = "private-old-task"
        saved = self.store.save(args)
        preview = self.previews.preview(saved["id"], 1)
        mapping_path = self.previews.task_ids.path
        mapping = json.loads(mapping_path.read_text())
        previous_alias = next(key for key, real in mapping["map"].items() if real == saved["id"])
        del mapping["map"][previous_alias]
        mapping["map"]["task-" + "c" * 32] = saved["id"]
        mapping_path.write_text(json.dumps(mapping))
        self.assert_code("local_skill_preview_stale", lambda: self.export(preview))
        self.assertFalse((Path(self.tmp.name) / "exports").exists())

    def test_returned_preview_cannot_mutate_exported_bytes(self):
        preview = self.make_preview()
        expected = preview["skill_markdown"].encode("utf-8")
        preview["skill_markdown"] = "untrusted replacement"
        preview["sha256"] = "0" * 64
        result = self.export(preview)
        self.assertEqual(Path(result["path"]).read_bytes(), expected)

    def test_new_task_revision_invalidates_old_preview(self):
        preview = self.make_preview()
        self.revisions.update(self.saved["id"], 1, [{"op": "set_text", "fields": {"name": "NEW-NAME"}}])
        self.assert_code("local_skill_preview_stale", lambda: self.export(preview))
        self.assertFalse((Path(self.tmp.name) / "exports").exists())
        self.assert_code("local_skill_preview_missing", lambda: self.export(preview))

    def test_same_revision_recipe_or_schema_change_is_stale(self):
        for change_schema in (False, True):
            with self.subTest(change_schema=change_schema):
                preview = self.make_preview()
                with self.store._locked():
                    tasks = self.store._read()
                    if change_schema:
                        tasks[0]["variables"]["secret_column"]["default"] = "NEW-DEFAULT"
                    else:
                        tasks[0]["steps"][0]["selector"] = {"name": "OTHER-TARGET"}
                    self.store._write(tasks)
                self.assert_code("local_skill_preview_stale", lambda: self.export(preview))

    def test_historical_preview_is_explicit_and_invalidated_by_later_save(self):
        self.revisions.update(self.saved["id"], 1, [{"op": "set_text", "fields": {"name": "SECOND"}}])
        preview = self.make_preview(1)
        self.assertEqual(preview["revision"], 1)
        result = self.export(preview)
        self.assertIn('"revision": 1', Path(result["path"]).read_text(encoding="utf-8"))
        another = self.make_preview(1)
        self.revisions.update(self.saved["id"], 2, [{"op": "set_text", "fields": {"name": "THIRD"}}])
        self.assert_code("local_skill_preview_stale", lambda: self.export(another))

    def test_unknown_cancelled_expired_and_consumed_previews_cannot_export(self):
        self.assert_code("local_skill_preview_missing", lambda: self.previews.export("missing", self.saved["id"], 1))
        preview = self.make_preview()
        self.assertTrue(self.previews.cancel(preview["preview_id"])["cancelled"])
        self.assertFalse(self.previews.cancel(preview["preview_id"])["cancelled"])
        self.assert_code("local_skill_preview_missing", lambda: self.export(preview))
        preview = self.make_preview()
        item = self.previews._previews[preview["preview_id"]]
        with mock.patch("local_skill.time.monotonic", return_value=item.expires + .01):
            self.assert_code("local_skill_preview_missing", lambda: self.export(preview))
        preview = self.make_preview()
        self.export(preview)
        self.assert_code("local_skill_preview_missing", lambda: self.export(preview))

    def test_wrong_identity_or_boolean_revision_cannot_export(self):
        preview = self.make_preview()
        for task_id, revision in (("another", 1), (self.saved["id"], 2), (self.saved["id"], True)):
            with self.subTest(task_id=task_id, revision=revision):
                self.assert_code("local_skill_identity_mismatch", lambda: self.previews.export(preview["preview_id"], task_id, revision))
        self.assertEqual(self.export(preview)["status"], "exported")

    def test_internal_byte_integrity_failure_invalidates_preview(self):
        preview = self.make_preview()
        item = self.previews._previews[preview["preview_id"]]
        self.previews._previews[preview["preview_id"]] = replace(item, body=b"corrupt")
        self.assert_code("local_skill_preview_changed", lambda: self.export(preview))
        self.assert_code("local_skill_preview_missing", lambda: self.export(preview))

    def test_memory_bound_evicts_oldest_without_touching_saved_tasks(self):
        first = self.make_preview()
        for _ in range(self.previews.MAX_PREVIEWS):
            last = self.make_preview()
        self.assertEqual(len(self.previews._previews), self.previews.MAX_PREVIEWS)
        self.assert_code("local_skill_preview_missing", lambda: self.export(first))
        self.assertEqual(self.export(last)["status"], "exported")
        self.assertEqual(self.revisions.get(self.saved["id"]), self.saved)

    def test_failed_atomic_replace_preserves_prior_file_and_allows_explicit_retry(self):
        original = self.export(self.make_preview())
        path = Path(original["path"])
        path.write_bytes(b"previous export")
        preview = self.make_preview()
        with mock.patch("local_skill.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(Exception):
                self.export(preview)
        self.assertEqual(path.read_bytes(), b"previous export")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        self.assertEqual(Path(self.export(preview)["path"]).read_bytes(), preview["skill_markdown"].encode("utf-8"))

    def test_linked_destination_is_rejected_without_overwriting(self):
        preview = self.make_preview()
        def reject(path):
            if path.name == "SKILL.md":
                raise ValueError("linked destination")
        with mock.patch.object(self.store, "_reject_link", side_effect=reject):
            with self.assertRaises(ValueError):
                self.export(preview)
        self.assertEqual(list(Path(self.tmp.name).rglob("SKILL.md")), [])
        self.assertEqual(self.export(preview)["status"], "exported")

    def test_descriptive_task_without_executable_steps_cannot_be_exported(self):
        args = {k: v for k, v in self.args.items() if k not in {"steps", "variables"}}
        args["id"] = "empty"
        self.store.save(args)
        self.assert_code("local_skill_empty", lambda: self.previews.preview("empty", 1))


if __name__ == "__main__":
    unittest.main()
