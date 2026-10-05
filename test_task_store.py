"""Task persistence under concurrent independent clients; no desktop input."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from server import TaskStore, SessionError


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"programs": [{"id": "editor"}]}
        self.store = TaskStore(self.tmp.name, self.config)
        self.args = {"id": "example", "name": "반복 작업", "instructions": "한글과 English \\\" & <tag>\n" * 500,
                     "expected": "결과를 화면에서 확인", "program_ids": ["editor"]}

    def test_save_edit_reopen_delete_preserves_other_tasks(self):
        saved = self.store.save(self.args)
        other = self.store.save({**self.args, "id": "other"})
        reopened = TaskStore(self.tmp.name, self.config)
        self.assertEqual(reopened.get("example"), saved)
        reopened.save({**self.args, "instructions": "수정한 내용"})
        self.assertEqual(self.store.get("example")["instructions"], "수정한 내용")
        self.assertTrue(reopened.delete("example"))
        self.assertFalse(reopened.delete("example"))
        self.assertEqual(self.store.all(), [other])

    def test_processes_do_not_lose_concurrent_saves(self):
        script = """
import json, sys, time
from server import TaskStore
store = TaskStore(sys.argv[1], {'programs': [{'id':'editor'}]})
original_read = store._read
def delayed_read():
    value = original_read()
    time.sleep(.015)
    return value
store._read = delayed_read
for index in range(8):
    store.save({'id':sys.argv[2]+'_'+str(index), 'name':'작업', 'instructions':'설명', 'expected':'확인', 'program_ids':['editor']})
"""
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, self.tmp.name, str(i)],
                       cwd=str(Path(__file__).parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8") for i in range(4)]
        try:
            for proc in processes:
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, out + err)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
        self.assertEqual({entry["id"] for entry in self.store.all()}, {f"{i}_{j}" for i in range(4) for j in range(8)})

    def test_corrupt_file_is_never_overwritten_by_save_or_delete(self):
        for contents in (b"{broken", b"\xff\xfe", json.dumps({"version": True, "tasks": []}).encode(),
                         json.dumps({"version": 1, "tasks": [{"id": "broken"}]}).encode()):
            self.store.path.write_bytes(contents)
            for operation in (lambda: self.store.save(self.args), lambda: self.store.delete("example"), self.store.all):
                with self.assertRaises(SessionError):
                    operation()
                self.assertEqual(self.store.path.read_bytes(), contents)

    def test_duplicate_ids_and_oversized_loaded_fields_are_rejected(self):
        saved = self.store.save(self.args)
        cases = [[saved, saved], [{**saved, "instructions": "x" * 32001}],
                 [{**saved, "program_ids": ["editor", "editor"]}], [{**saved, "unexpected": "field"}]]
        for tasks in cases:
            contents = json.dumps({"version": 1, "tasks": tasks}, ensure_ascii=False)
            self.store.path.write_text(contents, encoding="utf-8")
            with self.assertRaises(SessionError):
                self.store.all()
            self.assertEqual(self.store.path.read_text(encoding="utf-8"), contents)

    def test_read_and_write_limits_preserve_existing_file(self):
        self.store.save(self.args)
        original = self.store.path.read_bytes()
        self.store.MAX_BYTES = 32
        with self.assertRaisesRegex(SessionError, "제한"):
            self.store.all()
        self.assertEqual(self.store.path.read_bytes(), original)
        self.store.MAX_BYTES = TaskStore.MAX_BYTES
        self.store.MAX_TASKS = 1
        with self.assertRaisesRegex(SessionError, "한도"):
            self.store.save({**self.args, "id": "new"})
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_removed_program_recipe_can_be_read_and_deleted_but_not_resaved(self):
        self.store.save(self.args)
        changed = TaskStore(self.tmp.name, {"programs": []})
        self.assertEqual(changed.get("example")["program_ids"], ["editor"])
        with self.assertRaisesRegex(SessionError, "등록"):
            changed.save(self.args)
        self.assertTrue(changed.delete("example"))

    def test_bad_id_and_executable_payload_cannot_change_storage_paths(self):
        for task_id in ("../tasks", "C:\\tasks", "", 1):
            with self.assertRaises(SessionError):
                self.store.delete(task_id)
        with self.assertRaises(SessionError):
            self.store.save({**self.args, "command": "do not execute"})


if __name__ == "__main__":
    unittest.main()
