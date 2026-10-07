"""Partial recording provenance survives storage and ordinary editing."""
import copy
import json
import tempfile
import unittest

from server import ComputerManager, SessionError, TaskStore


class RecordingProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"state_dir": self.temp.name, "programs": [{"id": "editor"}]}
        self.store = TaskStore(self.temp.name, self.config)
        self.review = {"partial": True, "acknowledged": True,
                       "warning_codes": ["outside_target_not_recorded", "recording_timeout_partial"]}
        self.task = {"id": "partial", "name": "부분 녹화", "instructions": "확인한 단계만 실행",
            "expected": "값 확인", "program_ids": ["editor"], "variables": {}, "recording_review": self.review,
            "steps": [{"program_id": "editor", "operation": "set_value", "selector": {"name": "입력칸"}, "value": "값"}]}

    def test_saved_metadata_survives_reopen_and_text_only_update(self):
        first = self.store.save(self.task)
        reopened = TaskStore(self.temp.name, self.config)
        self.assertEqual(reopened.get("partial")["recording_review"], self.review)
        update = {k: copy.deepcopy(self.task[k]) for k in ("id", "name", "instructions", "expected", "program_ids")}
        update["name"] = "새 이름"
        second = reopened.save(update)
        self.assertEqual(second["recording_review"], self.review)
        self.assertEqual(second["steps"], first["steps"])
        self.assertEqual(second["revision"], first["revision"] + 1)

    def test_replacing_steps_does_not_silently_erase_recording_warning(self):
        self.store.save(self.task)
        updated = {k: copy.deepcopy(v) for k, v in self.task.items() if k != "recording_review"}
        updated["steps"][0]["value"] = "다른 값"
        self.assertEqual(self.store.save(updated)["recording_review"], self.review)

    def test_unreviewed_unknown_duplicate_or_incomplete_metadata_is_rejected_atomically(self):
        self.store.save(self.task)
        before = self.store.path.read_bytes()
        invalid = [None, {}, {**self.review, "acknowledged": False}, {**self.review, "partial": False},
            {**self.review, "warning_codes": []}, {**self.review, "warning_codes": ["unknown_warning"]},
            {**self.review, "warning_codes": ["recording_timeout_partial"] * 2},
            {**self.review, "acknowledged": 1}, {**self.review, "extra": "untrusted"}]
        for review in invalid:
            with self.subTest(review=review), self.assertRaises(SessionError):
                self.store.save({**self.task, "recording_review": review})
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_partial_flag_is_visible_in_task_summary_and_details(self):
        self.store.save(self.task)
        manager = ComputerManager(self.config)
        summary = manager.call("computer_tasks", {})["structuredContent"]["tasks"][0]
        self.assertTrue(summary["partial_recording"])
        self.assertNotIn("instructions", summary)
        details = manager.call("computer_get_task", {"id": "partial"})["structuredContent"]
        self.assertIn("recording_timeout_partial", json.dumps(details))

    def test_corrupt_saved_provenance_is_not_silently_migrated_or_dropped(self):
        self.store.save(self.task)
        document = json.loads(self.store.path.read_text(encoding="utf-8"))
        document["tasks"][0]["recording_review"]["acknowledged"] = False
        self.store.path.write_text(json.dumps(document), encoding="utf-8")
        before = self.store.path.read_bytes()
        with self.assertRaises(SessionError):
            TaskStore(self.temp.name, self.config).get("partial")
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
