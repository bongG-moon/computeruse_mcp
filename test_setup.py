"""Setup logic and hidden Tk widget tests; no visible windows or desktop input."""
import copy
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import setup


class Value:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class SetupTests(unittest.TestCase):
    def fake(self, folder):
        original = {"version": 1, "driver": "C:\\Tools\\old.exe", "programs": [], "mode": "uia", "approval": "session",
                    "max_minutes": 10, "max_actions": 120, "state_dir": str(Path(folder) / "different-state"),
                    "log_detail": "metadata", "custom_metadata": {"preserve": True}}
        fake = types.SimpleNamespace(config_value=copy.deepcopy(original), config_path=Path(folder) / "chosen.json",
                                     driver=Value("C:\\Tools\\cua-driver.exe"), mode=Value("uia"), approval=Value("session"),
                                     minutes=Value(10), actions=Value(120), log_detail=Value("metadata"), status=Value(""))
        fake._loaded_config_bytes = None
        fake.current = lambda: setup.Setup.current(fake)
        return fake, original

    def test_current_keeps_explicit_paths_and_existing_extra_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            fake, original = self.fake(folder)
            value = setup.Setup.current(fake)
            self.assertEqual(value["state_dir"], original["state_dir"])
            self.assertEqual(value["custom_metadata"], original["custom_metadata"])
            self.assertEqual(fake.config_value, original)

    def test_content_logging_requires_human_confirmation_before_save(self):
        with tempfile.TemporaryDirectory() as folder:
            fake, _ = self.fake(folder)
            fake.log_detail.set("content")
            with patch.object(setup.messagebox, "askyesno", return_value=False), patch.object(setup, "save_config") as save:
                self.assertFalse(setup.Setup.save(fake))
                save.assert_not_called()
            with patch.object(setup.messagebox, "askyesno", return_value=True), patch.object(setup, "save_config") as save:
                self.assertTrue(setup.Setup.save(fake))
                self.assertEqual(save.call_args.args[0], fake.config_path)
                self.assertEqual(save.call_args.args[1]["log_detail"], "content")

    def test_check_copies_current_settings_without_saving_and_ignores_duplicate_click(self):
        with tempfile.TemporaryDirectory() as folder:
            fake, original = self.fake(folder)
            snapshots = []
            fake.busy = False
            fake.save = Mock()
            fake.background = lambda callback: callback()
            fake.check_files = lambda value: snapshots.append(value)
            setup.Setup.check(fake)
            fake.save.assert_not_called()
            self.assertEqual(snapshots[0]["driver"], "C:\\Tools\\cua-driver.exe")
            self.assertEqual(fake.config_value, original)
            fake.busy = True
            setup.Setup.check(fake)
            self.assertEqual(len(snapshots), 1)

    def test_external_change_while_content_confirmation_is_open_blocks_save(self):
        with tempfile.TemporaryDirectory() as folder:
            fake, _ = self.fake(folder)
            fake.log_detail.set("content")
            foreign = b'{"external": "new file while confirmation was open"}'
            def confirm(*args, **kwargs):
                fake.config_path.write_bytes(foreign)
                return True
            with patch.object(setup.messagebox, "askyesno", side_effect=confirm), \
                 patch.object(setup.messagebox, "showerror") as error, patch.object(setup, "save_config") as save:
                self.assertFalse(setup.Setup.save(fake))
                save.assert_not_called()
                error.assert_called_once()
            self.assertEqual(fake.config_path.read_bytes(), foreign)

    def test_cleanup_confirmation_passes_exact_preview_snapshot(self):
        import maintenance
        preview = {"root": "C:\\Records\\runs", "candidate_count": 2, "candidate_bytes": 50, "preserved_count": 1, "candidates": []}
        captured = []
        fake = types.SimpleNamespace(status=Value(""), background=lambda action: captured.append(action()), cleanup_errors=lambda value: "", cleanup_target_summary=setup.Setup.cleanup_target_summary)
        with patch.object(setup.messagebox, "askyesno", return_value=True), patch.object(maintenance, "cleanup_completed_runs", return_value={"deleted_count": 2}) as cleanup:
            setup.Setup.handle_result(fake, {"kind": "cleanup_preview", "config": {"state_dir": "C:\\Records"}, "preview": preview})
        self.assertIs(cleanup.call_args.args[1], preview)
        self.assertEqual(captured[0]["kind"], "cleanup_done")

    def test_cleanup_prompt_includes_candidate_locations_outside_run_root(self):
        preview = {"root": "C:\\Records\\runs", "candidates": [{"path": "C:\\Records\\runs\\abc"}, {"path": "C:\\Package\\startup-log.txt"}]}
        summary = setup.Setup.cleanup_target_summary(preview)
        self.assertIn("C:\\Package\\startup-log.txt", summary)
        self.assertIn("C:\\Records\\runs\\abc", summary)


class HiddenTkTaskIntegrationTests(unittest.TestCase):
    """Real widget callbacks and storage, with display/focus/grab suppressed.

    This verifies data flow, not keyboard focus, screen layout, or real typing.
    No event loop runs, and every Toplevel is withdrawn before it can map.
    """

    @staticmethod
    def button(widget, label):
        pending = [widget]
        while pending:
            item = pending.pop()
            if isinstance(item, setup.ttk.Button) and item.cget("text") == label:
                return item
            pending.extend(item.winfo_children())
        raise AssertionError(f"Button was not found: {label}")

    def test_approval_options_are_visible_and_new_default_keeps_advanced_collapsed(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(setup.Setup, "deiconify"), \
             patch("settings.discover", return_value={"apps": []}):
            app = setup.Setup(Path(folder) / "config.json")
            try:
                self.assertFalse(app.winfo_ismapped())
                self.assertEqual(app.approval.get(), "client")
                self.assertFalse(app.advanced_open.get())
                pending, approval_buttons = [app], []
                while pending:
                    item = pending.pop(0)
                    if isinstance(item, setup.ttk.Radiobutton) and str(item.cget("variable")) == str(app.approval):
                        approval_buttons.append(item)
                    pending.extend(item.winfo_children())
                self.assertEqual([str(item.cget("value")) for item in approval_buttons], ["client", "session", "each"])
                self.assertTrue(all(item.master is approval_buttons[0].master for item in approval_buttons))
                self.assertIsNot(approval_buttons[0].master, app.advanced_frame)
                self.assertFalse((Path(folder) / "config.json").exists())
            finally:
                for timer in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(timer)
                app.destroy()

    def test_hidden_task_widgets_create_read_edit_cancel_and_delete(self):
        original_toplevel_init = setup.tk.Toplevel.__init__

        def hidden_toplevel_init(widget, *args, **kwargs):
            original_toplevel_init(widget, *args, **kwargs)
            widget.withdraw()

        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "isolated-config.json"
            config = {"version": 1, "driver": "", "mode": "uia", "approval": "session",
                      "log_detail": "metadata", "max_minutes": 10, "max_actions": 120,
                      "approval_timeout_seconds": 300, "state_dir": str(Path(folder) / "isolated-state"),
                      "programs": [{"id": item, "name": "시험 프로그램 " + item, "exe": "",
                                    "enabled": False, "control_exes": [], "hints": "사람이 정한 범위"}
                                   for item in ("editor", "browser")]}
            setup.save_config(config_path, config)
            with patch.object(setup.Setup, "deiconify"), \
                 patch.object(setup.tk.Toplevel, "__init__", hidden_toplevel_init), \
                 patch.object(setup.TaskEditor, "grab_set"), \
                 patch.object(setup.tk.Misc, "focus_set"), \
                 patch.object(setup.tk.Misc, "focus_force"), \
                 patch.object(setup.messagebox, "showerror") as errors, \
                 patch.object(setup.messagebox, "showinfo") as information:
                app = setup.Setup(config_path)
                try:
                    self.assertEqual(app.state(), "withdrawn")
                    self.assertFalse(app.winfo_ismapped())
                    preserved = app.task_store().save({"name": "그대로 둘 작업", "instructions": "시험 설명",
                                                       "expected": "변경 없음", "program_ids": ["browser"]})
                    app.refresh_tasks()
                    observed = []

                    def create_dialog(dialog):
                        self.assertEqual(dialog.state(), "withdrawn")
                        self.assertFalse(dialog.winfo_ismapped())
                        # Insert through the actual Entry/Text/Listbox widgets.
                        body = dialog.winfo_children()[0]
                        entry = next(item for item in body.winfo_children() if isinstance(item, setup.ttk.Entry))
                        entry.insert(0, "한글 반복 작업")
                        dialog.instructions.insert("1.0", "첫 줄: 가짜 내용 입력\n둘째 줄: 저장 후 재열기")
                        dialog.expected.insert("1.0", "저장한 내용이 그대로 보임")
                        dialog.program_list.selection_set(0, 1)
                        self.button(dialog, "저장").invoke()
                        observed.append(dialog.result)

                    with patch.object(app, "wait_window", side_effect=create_dialog):
                        self.button(app, "새 작업").invoke()
                    created = next(item for item in app.task_store().all() if item["id"] != preserved["id"])
                    self.assertEqual(created["instructions"], observed[0]["instructions"])
                    self.assertEqual(created["program_ids"], ["editor", "browser"])
                    self.assertIn(created["id"], app.task_list.get_children())
                    self.assertEqual(app.task_list.item(created["id"], "values")[0], "한글 반복 작업")
                    app.task_list.selection_set(created["id"])

                    def read_and_cancel(dialog):
                        self.assertEqual(dialog.name.get(), created["name"])
                        self.assertEqual(dialog.instructions.get("1.0", "end").strip(), created["instructions"])
                        self.assertEqual(dialog.expected.get("1.0", "end").strip(), created["expected"])
                        self.assertEqual(dialog.program_list.curselection(), (0, 1))
                        dialog.instructions.delete("1.0", "end")
                        dialog.instructions.insert("1.0", "취소할 변경")
                        self.button(dialog, "취소").invoke()

                    with patch.object(app, "wait_window", side_effect=read_and_cancel):
                        self.button(app, "읽기·수정").invoke()
                    self.assertEqual(app.task_store().get(created["id"]), created)

                    def edit_dialog(dialog):
                        dialog.name.set("수정한 한글 작업")
                        dialog.instructions.delete("1.0", "end")
                        dialog.instructions.insert("1.0", "수정한 지시\n재열기 확인")
                        dialog.expected.delete("1.0", "end")
                        dialog.expected.insert("1.0", "수정 결과 확인")
                        dialog.program_list.selection_clear(0, "end")
                        dialog.program_list.selection_set(0)
                        self.button(dialog, "저장").invoke()

                    with patch.object(app, "wait_window", side_effect=edit_dialog):
                        self.button(app, "읽기·수정").invoke()
                    edited = app.task_store().get(created["id"])
                    self.assertEqual(edited["name"], "수정한 한글 작업")
                    self.assertEqual(edited["instructions"], "수정한 지시\n재열기 확인")
                    self.assertEqual(edited["expected"], "수정 결과 확인")
                    self.assertEqual(edited["program_ids"], ["editor"])
                    app.task_list.selection_set(edited["id"])
                    with patch.object(setup.messagebox, "askyesno", return_value=False):
                        self.button(app, "선택 작업 삭제").invoke()
                    self.assertEqual(app.task_store().get(edited["id"]), edited)
                    with patch.object(setup.messagebox, "askyesno", return_value=True):
                        self.button(app, "선택 작업 삭제").invoke()
                    self.assertEqual(app.task_store().all(), [preserved])
                    self.assertEqual(app.task_list.get_children(), (preserved["id"],))
                    self.assertEqual(setup.load_config(config_path), config)
                    self.assertFalse(app.winfo_ismapped())
                    errors.assert_not_called()
                    information.assert_not_called()
                finally:
                    for timer in app.tk.splitlist(app.tk.call("after", "info")):
                        app.after_cancel(timer)
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
