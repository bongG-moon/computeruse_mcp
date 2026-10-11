"""Task persistence under concurrent independent clients; no desktop input."""
import json
import copy
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

    def pending_args(self):
        return {**self.args, "variables": {"query": {"default": "0012"}}, "steps": [
            {"operation": "set_value", "program_id": "editor", "window_ref": "main", "step_id": "known",
             "selector": {"role": "Edit", "name": "검색"}, "value": "${query}"},
            {"operation": "manual_entry", "program_id": "editor", "window_ref": "main", "step_id": "pending",
             "manual_reason": "template_low_detail", "repair_action": "click"},
            {"operation": "checkpoint", "program_id": "editor", "window_ref": "main", "step_id": "result", "message": "결과 확인"}],
            "recording_review": {"partial": True, "acknowledged": False, "warning_codes": ["semantic_target_unverified"]},
            "draft": {"format": "computer-process-draft/v1", "labels": ["검색", "첫 번째 재생 버튼", "현재 창"],
                      "target_warning_steps": ["pending"], "persistent_semantic_warning": False}}

    def test_draft_roundtrip_repairs_only_one_step_and_keeps_revision(self):
        from process_editor import ProcessDraft
        from task_revision import TaskRevisions
        from workflows import render_steps
        saved = self.store.save(self.pending_args())
        reopened = TaskStore(self.tmp.name, self.config).get(saved["id"])
        self.assertEqual(reopened, saved)
        draft = ProcessDraft([{"program_id": "editor", "pid": 10, "window_id": 20, "label": "test"}], reopened)
        self.assertEqual(draft.labels, saved["draft"]["labels"])
        draft.selections["chosen"] = {"target": ("editor", "main"), "recognition": "uia", "selector": {"role": "Button", "name": "실행"}, "actions": ["click"], "label": "실행"}
        draft.repair(1, {"selection_id": "chosen"}, action="click")
        self.assertEqual(draft.recording_warnings, [])
        self.assertTrue(draft.readiness()["runnable"])
        args = {key: copy.deepcopy(saved[key]) for key in ("id", "name", "instructions", "expected", "program_ids", "variables")}
        ready = TaskRevisions(self.store).save({**args, "steps": draft.steps}, expected_revision=1)
        self.assertNotIn("draft", ready)
        self.assertNotIn("recording_review", ready)
        self.assertEqual(ready["steps"][0], saved["steps"][0])
        self.assertEqual(ready["steps"][2:], saved["steps"][2:])
        self.assertEqual(ready["steps"][1]["step_id"], "pending")
        self.assertEqual(render_steps(ready, {})[0][0]["value"], "0012")
        self.assertIn("draft", TaskRevisions(self.store).get(saved["id"], 1))

    def test_draft_never_renders_or_exports_as_executable(self):
        from task_revision import TaskRevisions
        from workflows import WorkflowError, render_steps
        saved = self.store.save(self.pending_args())
        for run in (lambda: render_steps(saved, {}), lambda: TaskRevisions(self.store).export_skill(saved["id"])):
            with self.assertRaisesRegex(WorkflowError, "초안"):
                run()

    def test_draft_strict_shape_and_unacknowledged_review_cannot_bypass_store(self):
        self.store.save(self.args)
        before = self.store.path.read_bytes()
        for bad in ("unknown_key", "secret_value", "missing_marker", "forged_provenance"):
            args = self.pending_args()
            if bad == "unknown_key": args["steps"][1]["window_id"] = 123
            elif bad == "secret_value": args["steps"][1]["value"] = "DO_NOT_STORE_UNKNOWN_INPUT"
            elif bad == "missing_marker": args.pop("draft")
            else: args["draft"]["target_warning_steps"] = ["known"]
            with self.subTest(bad=bad), self.assertRaises(SessionError): self.store.save(args)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_recipe_and_draft_limits_accept_1000_reject_1001_atomically(self):
        from server import MANAGEMENT
        self.assertEqual(MANAGEMENT["computer_save_task"]["inputSchema"]["properties"]["steps"]["maxItems"], 1000)
        for pending in (False, True):
            step = {"operation": "manual_entry", "program_id": "editor", "manual_reason": "image_capture_required"} if pending else {"operation": "delay", "program_id": "editor", "duration_ms": 0}
            args = {**self.args, "steps": [copy.deepcopy(step) for _ in range(1000)]}
            if pending: args["draft"] = {"format": "computer-process-draft/v1", "labels": ["target"]*1000, "target_warning_steps": [], "persistent_semantic_warning": False}
            self.assertEqual(len(self.store.save(args)["steps"]), 1000)
            before = self.store.path.read_bytes()
            args["steps"].append(copy.deepcopy(step))
            if pending: args["draft"]["labels"].append("target")
            with self.subTest(pending=pending), self.assertRaises(SessionError): self.store.save(args)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_draft_step_ids_unique_across_pending_and_executable_steps(self):
        args = self.pending_args()
        args["steps"][1]["step_id"] = "known"
        args["draft"]["target_warning_steps"] = ["known"]
        with self.assertRaises(SessionError): self.store.save(args)

    def test_more_than_thirty_pending_target_warnings_are_durable(self):
        args = self.pending_args()
        args["steps"] = [{"step_id": "pending-"+str(i), "operation": "manual_entry", "program_id": "editor",
                          "manual_reason": "template_low_detail", "repair_action": "click"} for i in range(33)]
        args["draft"].update(labels=["target"]*33, target_warning_steps=[step["step_id"] for step in args["steps"]])
        saved = self.store.save(args)
        self.assertEqual(len(TaskStore(self.tmp.name, self.config).get(saved["id"])["draft"]["target_warning_steps"]), 33)

    def test_chat_draft_structural_edits_preserve_label_and_warning_identity(self):
        from task_revision import TaskRevisions
        self.store.save(self.pending_args())
        revisions = TaskRevisions(self.store)
        added = revisions.update("example", 1, [{"op": "insert_step", "after_step_id": None,
            "step": {"step_id": "extra", "operation": "delay", "program_id": "editor", "duration_ms": 0}}])["task"]
        self.assertEqual(added["draft"]["labels"][1:], ["검색", "첫 번째 재생 버튼", "현재 창"])
        moved = revisions.update("example", 2, [{"op": "move_step", "step_id": "extra", "after_step_id": "result"}])["task"]
        self.assertEqual(moved["draft"]["labels"][:3], ["검색", "첫 번째 재생 버튼", "현재 창"])
        removed = revisions.update("example", 3, [{"op": "delete_step", "step_id": "pending"}])["task"]
        self.assertEqual(removed["draft"]["target_warning_steps"], [])
        self.assertTrue(removed["draft"]["persistent_semantic_warning"])
        self.assertFalse(removed["recording_review"]["acknowledged"])

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
