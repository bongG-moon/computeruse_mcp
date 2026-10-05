"""Cleanup is exercised only against disposable synthetic records."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import maintenance


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {"state_dir": str(self.root)}
        self.log = self.root / "app" / "startup-log.txt"
        self.log.parent.mkdir()
        p = patch.object(maintenance, "_log_paths", return_value=(self.log,))
        p.start()
        self.addCleanup(p.stop)

    def run_record(self, state="stopped"):
        run = self.root / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        self.json(run / "session.json", {"session_id": run.name, "run_dir": str(run), "state": state})
        self.json(run / ".computer-use-active.json", {"version": 1, "run_dir": str(run), "pid": 123,
                  "birth": "test:123", "token": uuid.uuid4().hex + uuid.uuid4().hex})
        (run / "actions.jsonl").write_text('sensitive test input\n', encoding="utf-8")
        return run

    def json(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def schema_record(self, state="stopped"):
        run = self.root / "runs" / ("schema-" + uuid.uuid4().hex)
        run.mkdir(parents=True)
        self.json(run / "schema-session.json", {"format": "computer-use-schema/v1", "session_id": run.name,
                  "run_dir": str(run), "state": state})
        (run / "driver-stderr.log").write_text("test driver diagnostic")
        return run

    def test_schema_cleanup_requires_explicit_successful_driver_termination(self):
        completed = self.schema_record()
        active = self.schema_record("active")
        failed = self.schema_record("stop_failed")
        legacy = self.schema_record()
        (legacy / "schema-session.json").unlink()
        preview = maintenance.preview_cleanup(self.root)
        self.assertEqual(preview["candidate_count"], 1)
        self.assertEqual(preview["preserved_count"], 3)
        result = maintenance.cleanup_completed_runs(self.root, preview)
        self.assertEqual(result["deleted_count"], 1)
        self.assertFalse(completed.exists())
        self.assertTrue(all(path.exists() for path in (active, failed, legacy)))

    def test_schema_user_document_and_tampered_marker_are_preserved(self):
        document = self.schema_record()
        (document / "my-document.txt").write_text("keep")
        invalid = self.schema_record()
        marker = json.loads((invalid / "schema-session.json").read_text())
        marker["run_dir"] = str(self.root)
        self.json(invalid / "schema-session.json", marker)
        self.assertEqual(maintenance.preview_cleanup(self.root)["candidate_count"], 0)

    def test_schema_state_change_after_confirmation_never_deletes(self):
        run = self.schema_record()
        preview = maintenance.preview_cleanup(self.root)
        marker = json.loads((run / "schema-session.json").read_text())
        marker["state"] = "stop_failed"
        self.json(run / "schema-session.json", marker)
        result = maintenance.cleanup_completed_runs(self.root, preview)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue((run / "driver-stderr.log").exists())

    def test_deletes_only_completed_records_preserving_config_tasks_and_active(self):
        completed = self.run_record()
        active = self.run_record("active")
        failed = self.run_record("stop_failed")
        for name in ("config.json", "tasks.json"):
            self.json(self.root / name, {"keep": name})
        preview = maintenance.preview_cleanup(self.config)
        self.assertEqual((preview["candidate_count"], preview["preserved_count"]), (1, 2))
        self.assertTrue(completed.exists())
        result = maintenance.cleanup_completed_runs(self.config, preview)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["deleted_bytes"], preview["candidate_bytes"])
        self.assertFalse(completed.exists())
        self.assertTrue(active.exists() and failed.exists())
        self.assertEqual(json.loads((self.root / "tasks.json").read_text()), {"keep": "tasks.json"})

    def test_user_files_and_unknown_folders_are_preserved(self):
        run = self.run_record()
        (run / "my-document.txt").write_text("keep")
        (self.root / "runs" / "personal").mkdir()
        preview = maintenance.preview_cleanup(self.root)
        self.assertEqual(preview["candidate_count"], 0)
        self.assertEqual(preview["preserved_count"], 2)

    def test_changed_state_or_log_after_preview_is_preserved(self):
        for change in ("state", "payload"):
            with self.subTest(change=change):
                run = self.run_record()
                preview = maintenance.preview_cleanup(self.root)
                if change == "state":
                    data = json.loads((run / "session.json").read_text())
                    data["state"] = "stop_failed"
                    self.json(run / "session.json", data)
                else:
                    (run / "actions.jsonl").write_text("changed value")
                result = maintenance.cleanup_completed_runs(self.root, preview)
                self.assertEqual(result["deleted_count"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertTrue((run / "actions.jsonl").exists())

    def test_new_completed_run_after_preview_is_not_included(self):
        old = self.run_record()
        preview = maintenance.preview_cleanup(self.root)
        new = self.run_record()
        maintenance.cleanup_completed_runs(self.root, preview)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_tampered_path_or_wrong_root_never_deletes(self):
        run = self.run_record()
        preview = maintenance.preview_cleanup(self.root)
        preview["candidates"][0]["path"] = str(self.root)
        result = maintenance.cleanup_completed_runs(self.root, preview)
        self.assertEqual(result["deleted_count"], 0)
        self.assertTrue(run.exists())
        with self.assertRaises(ValueError):
            maintenance.cleanup_completed_runs(self.root / "different", preview)

    def test_known_consent_records_deleted_but_unknown_file_preserved(self):
        run = self.run_record()
        folder = run / "consent"
        folder.mkdir()
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        self.json(folder / (nonce + ".request.json"), {"details": "test private"})
        self.json(folder / (nonce + ".audit.json"), {"approved": False})
        self.assertEqual(maintenance.preview_cleanup(self.root)["candidate_count"], 1)
        (folder / "personal.txt").write_text("keep")
        self.assertEqual(maintenance.preview_cleanup(self.root)["candidate_count"], 0)

    def test_reparse_and_hard_link_payload_preserved(self):
        run = self.run_record()
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        (run / "actions.jsonl").unlink()
        os.link(outside, run / "actions.jsonl")
        self.assertEqual(maintenance.preview_cleanup(self.root)["candidate_count"], 0)
        self.assertEqual(outside.read_text(), "keep")
        with patch.object(maintenance, "_plain_chain", return_value=False):
            with self.assertRaises(ValueError):
                maintenance.preview_cleanup(self.root)

    def test_owned_startup_log_only_and_deletion_failure_reported(self):
        self.log.write_text("unrecognized old log\n")
        self.assertEqual(maintenance.preview_cleanup(self.root)["candidate_count"], 0)
        self.log.write_text(maintenance.LOG_HEADER + "\nsynthetic exception\n")
        preview = maintenance.preview_cleanup(self.root)
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            result = maintenance.cleanup_completed_runs(self.root, preview)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        result = maintenance.cleanup_completed_runs(self.root, preview)
        self.assertEqual(result["deleted_count"], 1)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
