"""Retention tests use disposable directories only; no real run data is touched."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import artifact_lifecycle as lifecycle


class ArtifactLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "runs").mkdir()
        self.now = time.time() + 10

    def tearDown(self):
        # Close cached directory iterators before Windows removes the fixture.
        with lifecycle._SCAN_LOCK:
            for key in list(lifecycle._SCANS):
                if key.startswith(str(lifecycle._local_path(self.root))):
                    lifecycle._SCANS.pop(key)[0].close()
        self.temporary.cleanup()

    def run_folder(self, *, owned=True, closed=True, name=None):
        run = self.root / "runs" / (name or uuid.uuid4().hex)
        run.mkdir()
        self.session(run, "active")
        if owned:
            lifecycle.initialize_run_artifacts(run, {"id": "work-1", "name": "조회 업무"}, run.name)
        if closed:
            self.session(run, "stopped")
            if owned:
                lifecycle.mark_run_closed(run)
        return run

    def session(self, run, state):
        (run / "session.json").write_text(json.dumps({"state": state, "session_id": run.name, "run_dir": str(run)}))

    def file(self, run, relative, data=b"temporary screenshot payload"):
        path = run / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        return path

    def receipt(self, run, *, saved=False, age=0):
        receipt_id = uuid.uuid4().hex
        path = self.file(run, "recording-receipts/" + receipt_id + ".json", json.dumps({
            "format": "computer-recording-receipt/v1", "receipt_id": receipt_id,
            "recording": {"events": [{"kind": "click", "template_png": "pretend-image"}]}}).encode())
        if saved:
            with patch.object(lifecycle.time, "time", return_value=self.now - age):
                lifecycle.mark_recording_saved(run, receipt_id, "task-saved-1")
        return receipt_id, path

    def clean(self, **kwargs):
        return lifecycle.cleanup_artifacts(self.root, now=self.now, time_budget_seconds=2, **kwargs)

    def test_closed_run_cleans_known_helper_files_and_keeps_session(self):
        run = self.run_folder()
        prefix = "a" * 16
        paths = [self.file(run, item) for item in [
            f"im/{prefix}.in", f"im/{prefix}.out", f"im/{prefix}.out.tmp", f"im/{prefix}.air.in", f"im/{prefix}.air.out",
            f"visual/{prefix}.req.json", f"visual/{prefix}.res.json.progress.json.tmp",
            f"learning/{prefix}.request.json", f"learning/{prefix}.response.json.ready.json",
            f"process-editor/{prefix}.response.json.trial.ready.json",
            "checkpoint-review-" + "b" * 32 + "/screen.png",
            "checkpoint-review-" + "b" * 32 + "/request.json",
            "checkpoint-review-" + "b" * 32 + "/cancel.flag",
            "image-crops/crop-a8b3c0_4/request.json", "image-crops/crop-a8b3c0_4/response.json"]]
        result = self.clean()
        self.assertEqual(result["error_count"], 0, result)
        self.assertEqual(result["deleted_files"], len(paths), result)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue((run / "session.json").exists())
        self.assertTrue((run / lifecycle.MARKER).exists())

    def test_active_stopped_without_closed_and_legacy_runs_are_preserved(self):
        active = self.run_folder(closed=False)
        stopped_unclosed = self.run_folder(closed=False)
        self.session(stopped_unclosed, "stopped")
        legacy = self.run_folder(owned=False)
        files = [self.file(run, "visual/" + "c" * 16 + ".req.json") for run in [active, stopped_unclosed, legacy]]
        self.clean()
        self.assertTrue(all(path.exists() for path in files))
        self.assertFalse((legacy / ".artifacts.lock").exists())

    def test_mark_closed_requires_stopped_matching_session(self):
        run = self.run_folder(closed=False)
        with self.assertRaises(ValueError):
            lifecycle.mark_run_closed(run)
        self.session(run, "stopped")
        value = json.loads((run / "session.json").read_text())
        value["session_id"] = "wrong"
        (run / "session.json").write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            lifecycle.mark_run_closed(run)

    def test_metadata_owner_and_policy_tamper_preserved(self):
        for field, value in [("owner", "different"), ("run_dir", str(self.root)), ("policy", {})]:
            with self.subTest(field=field):
                run = self.run_folder()
                path = self.file(run, "im/" + "d" * 16 + ".in")
                marker = json.loads((run / lifecycle.MARKER).read_text(encoding="utf-8"))
                marker[field] = value
                (run / lifecycle.MARKER).write_text(json.dumps(marker), encoding="utf-8")
                self.clean()
                self.assertTrue(path.exists())

    def test_closed_session_rewrite_preserved_even_if_stopped(self):
        run = self.run_folder()
        path = self.file(run, "im/" + "d" * 16 + ".in")
        value = json.loads((run / "session.json").read_text())
        value["unverified_edit"] = True
        (run / "session.json").write_text(json.dumps(value))
        self.clean()
        self.assertTrue(path.exists())

    def test_marker_changed_during_cleanup_prevents_unlink(self):
        run = self.run_folder()
        path = self.file(run, "im/" + "d" * 16 + ".in")
        original = lifecycle._unlink

        def mutate(*args, **kwargs):
            marker = json.loads((run / lifecycle.MARKER).read_text(encoding="utf-8"))
            marker["owner"] = "changed"
            (run / lifecycle.MARKER).write_text(json.dumps(marker), encoding="utf-8")
            return original(*args, **kwargs)

        with patch.object(lifecycle, "_unlink", side_effect=mutate):
            result = self.clean()
        self.assertTrue(path.exists())
        self.assertGreater(result["error_count"], 0)

    def test_unknown_and_permanent_task_files_are_preserved(self):
        run = self.run_folder()
        retained = [self.file(run, path) for path in ["visual/user.png", "visual/note.txt", "visual/other/request.json",
                    "recording-receipts/user.json", "screen.png", "tasks.json", "elements/user-image.png",
                    "task-revisions/a.json", "workflows/task.json"]]
        retained += [self.file(self.root, path) for path in ["tasks.json", "elements/template.png", "task-revisions/1.json", "workflows/user.json"]]
        expected = {path: path.read_bytes() for path in retained}
        self.clean()
        self.assertEqual(expected, {path: path.read_bytes() for path in retained})

    def test_only_saved_unchanged_receipts_older_than_seven_days_are_removed(self):
        run = self.run_folder()
        _, unsaved = self.receipt(run)
        old_id, old = self.receipt(run, saved=True, age=lifecycle.RECEIPT_RETENTION_SECONDS + 1)
        _, recent = self.receipt(run, saved=True, age=lifecycle.RECEIPT_RETENTION_SECONDS - 1)
        _, changed = self.receipt(run, saved=True, age=lifecycle.RECEIPT_RETENTION_SECONDS + 1)
        changed.write_bytes(changed.read_bytes() + b" ")
        result = self.clean()
        self.assertFalse(old.exists(), result)
        self.assertTrue(unsaved.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(changed.exists())
        marker = json.loads((run / lifecycle.MARKER).read_text(encoding="utf-8"))
        self.assertNotIn(old_id, marker["saved_recordings"])

    def test_mark_recording_saved_rejects_wrong_receipt_and_id(self):
        run = self.run_folder()
        receipt_id, path = self.receipt(run)
        value = json.loads(path.read_text())
        value["receipt_id"] = "e" * 32
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            lifecycle.mark_recording_saved(run, receipt_id, "task-1")
        with self.assertRaises(ValueError):
            lifecycle.mark_recording_saved(run, "../../other", "task-1")

    def test_receipt_format_revalidated_before_removal(self):
        run = self.run_folder()
        receipt_id, path = self.receipt(run, saved=True, age=lifecycle.RECEIPT_RETENTION_SECONDS + 1)
        data = json.loads(path.read_text())
        data["format"] = "user-document/v1"
        path.write_text(json.dumps(data))
        result = self.clean()
        self.assertTrue(path.exists())
        self.assertGreater(result["error_count"], 0)

    def test_hardlink_and_marker_hardlink_are_preserved(self):
        run = self.run_folder()
        outside = self.file(self.root, "user-image.png")
        linked = run / "im" / ("f" * 16 + ".in")
        linked.parent.mkdir()
        os.link(outside, linked)
        self.clean()
        self.assertTrue(linked.exists())
        self.assertTrue(outside.exists())
        other = self.run_folder()
        regular = self.file(other, "im/" + "b" * 16 + ".in")
        os.link(other / lifecycle.MARKER, self.root / "linked-marker.json")
        self.clean()
        self.assertTrue(regular.exists())

    def test_symlink_folder_and_file_are_preserved(self):
        run = self.run_folder()
        outside = self.root / "outside"
        outside.mkdir()
        source = self.file(outside, "a" * 16 + ".in")
        try:
            (run / "im").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest("OS does not permit disposable symlink creation: " + str(exc))
        self.clean()
        self.assertTrue(source.exists())
        other = self.run_folder()
        (other / "im").mkdir()
        (other / "im" / ("b" * 16 + ".in")).symlink_to(source)
        self.clean()
        self.assertTrue(source.exists())
        self.assertTrue((other / "im" / ("b" * 16 + ".in")).is_symlink())

    def test_path_escape_and_non_run_paths_are_rejected(self):
        run = self.run_folder()
        with self.assertRaises(ValueError):
            lifecycle.initialize_run_artifacts(run / ".." / run.name, "task", run.name)
        with self.assertRaises(ValueError):
            lifecycle.initialize_run_artifacts(self.root, "task", self.root.name)
        with self.assertRaises(ValueError):
            lifecycle.initialize_run_artifacts(run, "task", "wrong")

    def test_reparse_attribute_folder_and_file_are_preserved(self):
        run = self.run_folder()
        target = self.file(run, "im/" + "d" * 16 + ".in")
        original = Path.lstat

        class ReparseInfo:
            def __init__(self, info):
                self.info = info
                self.st_file_attributes = getattr(info, "st_file_attributes", 0) | 0x400

            def __getattr__(self, name):
                return getattr(self.info, name)

        for selected in [target, target.parent]:
            with self.subTest(selected=selected):
                def reparse(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    return ReparseInfo(info) if path == selected else info
                with patch.object(Path, "lstat", reparse):
                    result = self.clean()
                self.assertTrue(target.exists())
                self.assertGreater(result["error_count"], 0)

    def test_file_changed_between_identity_check_and_unlink_is_preserved(self):
        run = self.run_folder()
        path = self.file(run, "im/" + "f" * 16 + ".in")
        original = lifecycle._unlink

        def change(target, *args, **kwargs):
            target.write_bytes(b"changed payload")
            return original(target, *args, **kwargs)

        with patch.object(lifecycle, "_unlink", side_effect=change):
            self.clean()
        self.assertEqual(path.read_bytes(), b"changed payload")

    def test_locked_file_reports_and_retries_next_cleanup(self):
        run = self.run_folder()
        path = self.file(run, "im/" + "a" * 16 + ".in")
        original = Path.unlink

        def locked(target, *args, **kwargs):
            if target.name == path.name:
                raise PermissionError("fixture file locked")
            return original(target, *args, **kwargs)

        with patch.object(Path, "unlink", locked):
            first = self.clean()
        self.assertTrue(path.exists())
        self.assertEqual(first["error_count"], 1, first)
        self.clean()
        self.assertFalse(path.exists())

    def test_concurrent_saved_updates_do_not_lose_receipts(self):
        run = self.run_folder()
        receipts = [self.receipt(run)[0] for _ in range(8)]
        errors = []

        def save(receipt_id):
            try:
                lifecycle.mark_recording_saved(run, receipt_id, "task-saved-1")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(identity,)) for identity in receipts]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(3)
        self.assertFalse(errors, errors)
        value = json.loads((run / lifecycle.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(set(value["saved_recordings"]), set(receipts))

    def test_held_run_lock_preserves_artifacts(self):
        run = self.run_folder()
        path = self.file(run, "im/" + "a" * 16 + ".in")
        with lifecycle._lock(lifecycle._run(run), ".artifacts.lock"):
            result = self.clean()
        self.assertTrue(path.exists())
        self.assertGreater(result["error_count"], 0)
        self.clean()
        self.assertFalse(path.exists())

    def test_run_batch_cursor_reaches_beyond_legacy_runs(self):
        for _ in range(7):
            self.run_folder(owned=False)
        run = self.run_folder()
        target = self.file(run, "im/" + "a" * 16 + ".in")
        for _ in range(8):
            result = self.clean(max_runs=2)
            self.assertLessEqual(result["scanned_runs"], 2)
            if not target.exists():
                break
        self.assertFalse(target.exists())

    def test_file_cursor_passes_unknown_files_and_bounds_batch(self):
        run = self.run_folder()
        folder = run / "visual"
        folder.mkdir()
        for index in range(300):
            (folder / f"unknown-{index:04}.txt").write_text("user file")
        target = self.file(run, "visual/" + "f" * 16 + ".req.json")
        for _ in range(8):
            result = self.clean()
            self.assertLessEqual(result["inspected_files"], lifecycle.MAX_FILES_PER_CLEANUP)
            if not target.exists():
                break
        self.assertFalse(target.exists())
        self.assertEqual(len(list(folder.glob("unknown-*.txt"))), 300)

    def test_zero_time_budget_does_not_scan_or_delete(self):
        run = self.run_folder()
        target = self.file(run, "im/" + "a" * 16 + ".in")
        start = time.monotonic()
        result = lifecycle.cleanup_artifacts(self.root, now=self.now, time_budget_seconds=0)
        self.assertLess(time.monotonic() - start, .2)
        self.assertEqual(result["scanned_runs"], 0)
        self.assertEqual(result["status"], "deferred")
        self.assertTrue(target.exists())

    def test_file_budget_resumes_next_run_even_after_directory_enumeration_ends(self):
        first = self.run_folder(name="0" * 32)
        last = self.run_folder(name="f" * 32)
        for kind in ["im", "visual", "learning"]:
            for index in range(150):
                self.file(first, kind + f"/unknown-{index:04}.txt")
        target = self.file(last, "im/" + "a" * 16 + ".in")
        first_result = self.clean()
        self.assertEqual(first_result["cursor"], 1)
        self.assertTrue(target.exists())
        self.clean()
        self.assertFalse(target.exists())

    def test_new_install_missing_state_folder_is_successful_noop(self):
        path = self.root / "not-created"
        result = lifecycle.cleanup_artifacts(path)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(result["error_count"], 0)
        self.assertFalse(path.exists())

    def test_status_is_small_and_identifies_task_and_retention(self):
        run = self.run_folder()
        self.clean()
        summary = lifecycle.read_last_cleanup(self.root)
        self.assertEqual(summary["format"], lifecycle.CLEANUP_FORMAT)
        status = lifecycle.storage_status(run)
        self.assertEqual(status["task"]["name"], "조회 업무")
        self.assertTrue(status["closed"])
        self.assertEqual(status["saved_recording_retention_days"], 7)
        self.assertEqual(status["unsaved_recordings"], "preserved")
        self.assertLess(len(json.dumps(status)), 10000)

    def test_status_missing_or_invalid_owner_does_not_break_session_status(self):
        run = self.run_folder(owned=False)
        status = lifecycle.storage_status(run)
        self.assertEqual(status["status"], "unmanaged")
        self.assertTrue(status["records_preserved"])
        (run / lifecycle.MARKER).write_text("broken json")
        self.assertEqual(lifecycle.storage_status(run)["status"], "unmanaged")


if __name__ == "__main__":
    unittest.main()
