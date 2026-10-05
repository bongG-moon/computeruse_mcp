"""Program registration through real hidden widgets; no app is launched or typed into."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import setup
from server import ComputerManager


@unittest.skipUnless(os.name == "nt", "Program executable paths use Windows semantics")
class HiddenProgramIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="program-widget-test-")
        self.root = Path(self.temp.name)
        self.config_path = self.root / "isolated-config.json"
        self.config = {"version": 1, "driver": "", "mode": "uia", "approval": "session",
                       "log_detail": "metadata", "max_minutes": 10, "max_actions": 120,
                       "approval_timeout_seconds": 300, "state_dir": str(self.root / "records"),
                       "extra_metadata": {"preserve": "사용자가 남긴 설정"},
                       "programs": [
                           {"id": "existing-enabled", "name": "기존 프로그램", "exe": r"C:\Example\original.exe",
                            "enabled": True, "control_exes": [r"C:\Example\original-window.exe"], "hints": "기존 사용법"},
                           {"id": "existing-disabled", "name": "꺼 둔 프로그램", "exe": r"C:\Example\disabled.exe",
                            "enabled": False, "control_exes": [], "hints": "꺼진 상태 유지"},
                       ]}
        setup.save_config(self.config_path, self.config)
        self.stack = ExitStack()
        original_init = setup.tk.Toplevel.__init__

        def hidden_init(widget, *args, **kwargs):
            original_init(widget, *args, **kwargs)
            widget.withdraw()

        self.stack.enter_context(patch.object(setup.Setup, "deiconify"))
        self.stack.enter_context(patch.object(setup.tk.Toplevel, "__init__", hidden_init))
        self.stack.enter_context(patch.object(setup.tk.Toplevel, "grab_set"))
        self.stack.enter_context(patch.object(setup.tk.Misc, "focus_set"))
        self.stack.enter_context(patch.object(setup.tk.Misc, "focus_force"))
        self.errors = self.stack.enter_context(patch.object(setup.messagebox, "showerror"))
        self.information = self.stack.enter_context(patch.object(setup.messagebox, "showinfo"))
        self.process = self.stack.enter_context(patch.object(setup.subprocess, "Popen", side_effect=AssertionError("No process may start")))
        self.apps = []
        try:
            self.app = self.new_app()
        except setup.tk.TclError as exc:
            self.stack.close()
            self.temp.cleanup()
            self.skipTest(f"Native Tk unavailable: {exc}")

    def new_app(self, config_path=None):
        app = setup.Setup(config_path or self.config_path)
        self.apps.append(app)
        self.assertEqual(app.state(), "withdrawn")
        self.assertFalse(app.winfo_ismapped())
        return app

    def tearDown(self):
        for app in self.apps:
            for timer in app.tk.splitlist(app.tk.call("after", "info")):
                app.after_cancel(timer)
            app.destroy()
        self.process.assert_not_called()
        self.errors.assert_not_called()
        self.stack.close()
        self.temp.cleanup()

    @staticmethod
    def button(widget, label):
        pending = [widget]
        while pending:
            child = pending.pop()
            if isinstance(child, (setup.tk.Button, setup.ttk.Button)) and child.cget("text") == label:
                return child
            pending.extend(child.winfo_children())
        raise AssertionError("Button not found: " + label)

    def test_file_choice_fills_only_blank_name_and_cancel_changes_nothing(self):
        dialog = setup.ProgramEditor(self.app)
        first, second = self.root / "DrawingDesk.exe", self.root / "OtherDesk.exe"
        with patch.object(setup.filedialog, "askopenfilename", return_value=str(first)):
            self.button(dialog, "파일 선택").invoke()
        self.assertEqual(dialog.name.get(), "DrawingDesk")
        self.assertEqual(dialog.exe.get(), str(first))
        dialog.name.set("내가 정한 이름")
        with patch.object(setup.filedialog, "askopenfilename", return_value=str(second)):
            self.button(dialog, "파일 선택").invoke()
        self.assertEqual(dialog.name.get(), "내가 정한 이름")
        self.assertEqual(dialog.exe.get(), str(second))
        with patch.object(setup.filedialog, "askopenfilename", return_value=""):
            self.button(dialog, "파일 선택").invoke()
        self.assertEqual(dialog.exe.get(), str(second))
        self.assertEqual(dialog.name.get(), "내가 정한 이름")
        self.button(dialog, "취소").invoke()
        self.assertIsNone(dialog.result)
        self.assertEqual(setup.load_config(self.config_path), self.config)

    def test_add_save_reopen_and_mcp_list_preserve_arbitrary_program_and_old_entries(self):
        primary, child = self.root / "DrawingDesk.exe", self.root / "DrawingCanvas.exe"
        for file in (primary, child):
            file.write_bytes(b"synthetic executable path fixture - never execute")
        created = []

        def fill_and_accept(dialog):
            self.assertEqual(dialog.state(), "withdrawn")
            self.assertFalse(dialog.winfo_ismapped())
            with patch.object(setup.filedialog, "askopenfilename", return_value=str(primary)):
                self.button(dialog, "파일 선택").invoke()
            self.assertEqual(dialog.name.get(), "DrawingDesk")
            dialog.extra.insert("1.0", str(child))
            dialog.hints.insert("1.0", "새 그림 메뉴를 엽니다.\n시험 파일만 저장합니다.")
            self.button(dialog, "저장").invoke()
            self.assertIsNotNone(dialog.result)
            created.append(copy.deepcopy(dialog.result))

        with patch.object(self.app, "wait_window", side_effect=fill_and_accept):
            self.button(self.app, "프로그램 추가").invoke()
        entry = created[0]
        self.assertTrue(entry["id"].startswith("app-"))
        self.assertEqual(self.app.config_value["programs"], [*self.config["programs"], entry])
        self.assertEqual(setup.load_config(self.config_path), self.config, "Dialog save must wait for settings save")
        self.button(self.app, "설정 저장").invoke()
        stored = setup.load_config(self.config_path)
        self.assertEqual(stored, {**self.config, "programs": [*self.config["programs"], entry]})
        self.assertEqual(stored["programs"][-1]["control_exes"], [str(child)])
        self.assertEqual(stored["programs"][-1]["hints"], "새 그림 메뉴를 엽니다.\n시험 파일만 저장합니다.")
        reopened = self.new_app()
        self.assertEqual(reopened.config_value, stored)
        self.assertIn("DrawingDesk", reopened.program_list.get("end"))
        runtime = Mock(side_effect=AssertionError("No screen session may start"))
        driver = Mock(side_effect=AssertionError("No Driver may start"))
        manager = ComputerManager(stored, runtime_factory=runtime, transport_factory=driver)
        result = manager.call("computer_programs", {})
        self.assertEqual(result["structuredContent"]["programs"], stored["programs"])
        self.assertFalse(result["isError"])
        self.assertIsNone(manager.session)
        runtime.assert_not_called()
        driver.assert_not_called()

    def test_open_setup_cannot_overwrite_a_program_added_through_chat_cli(self):
        import programs
        additional = self.root / "ChatAddedDesk.exe"
        additional.write_bytes(b"synthetic path fixture - never execute")
        options = {"exe": additional, "name": "채팅에서 추가한 프로그램", "hints": "채팅에서 지정한 사용법"}
        preview = programs.preview_add(self.config_path, **options)
        added = programs.add_program(self.config_path, expected_config_sha256=preview["expected_config_sha256"], **options)
        disk = self.config_path.read_bytes()
        self.app.config_value["programs"][0]["name"] = "창에서 편집 중인 이름"
        self.assertFalse(self.app.save())
        self.assertEqual(self.config_path.read_bytes(), disk)
        self.assertEqual(setup.load_config(self.config_path)["programs"][-1], added["program"])
        self.assertEqual(self.app.config_value["programs"][0]["name"], "창에서 편집 중인 이름")
        self.errors.assert_called_once()
        self.assertIn("다시 열어주세요", self.errors.call_args.args[1])
        self.errors.reset_mock()

    def test_repeated_own_saves_refresh_the_snapshot(self):
        self.app.actions.set(121)
        self.assertTrue(self.app.save())
        self.assertEqual(self.app._loaded_config_bytes, self.config_path.read_bytes())
        self.app.actions.set(122)
        self.assertTrue(self.app.save())
        self.assertEqual(self.app._loaded_config_bytes, self.config_path.read_bytes())
        self.assertEqual(setup.load_config(self.config_path)["max_actions"], 122)
        self.assertEqual(setup.load_config(self.config_path)["programs"], self.config["programs"])

    def test_newly_appeared_config_is_not_overwritten_by_open_setup(self):
        path = self.root / "new-config.json"
        with patch("settings.discover", return_value={"apps": []}):
            new = self.new_app(path)
        self.assertIsNone(new._loaded_config_bytes)
        foreign = {**self.config, "extra_metadata": {"created": "다른 창에서 작성"}}
        setup.save_config(path, foreign)
        disk = path.read_bytes()
        new.actions.set(400)
        self.assertFalse(new.save())
        self.assertEqual(path.read_bytes(), disk)
        self.assertEqual(new.actions.get(), 400)
        self.errors.assert_called_once()
        self.errors.reset_mock()

    def test_editing_with_advanced_collapsed_preserves_id_extra_hints_and_disabled_state(self):
        original = copy.deepcopy(self.config["programs"][0])
        original["enabled"] = False
        original["hints"] = "첫째 줄 사용법\n둘째 줄 사용법"
        dialog = setup.ProgramEditor(self.app, original)
        self.assertEqual(dialog.state(), "withdrawn")
        self.assertTrue(dialog.advanced_open.get())
        self.assertFalse(dialog.enabled.get())
        dialog.advanced_button.invoke()
        self.assertFalse(dialog.advanced_open.get())
        self.assertEqual(dialog.advanced_frame.winfo_manager(), "")
        self.assertEqual(dialog.extra.get("1.0", "end").strip(), original["control_exes"][0])
        self.assertEqual(dialog.hints.get("1.0", "end").strip(), original["hints"])
        dialog.name.set("새 표시 이름")
        self.button(dialog, "저장").invoke()
        self.assertEqual(dialog.result, {**original, "name": "새 표시 이름"})
        self.assertEqual(self.app.config_value, self.config)
        self.assertEqual(setup.load_config(self.config_path), self.config)

    def test_new_program_keeps_entered_extra_when_advanced_is_closed_before_save(self):
        dialog = setup.ProgramEditor(self.app)
        self.assertFalse(dialog.advanced_open.get())
        dialog.name.set("추가 창이 있는 프로그램")
        dialog.exe.set(str(self.root / "MainDesk.exe"))
        dialog.advanced_button.invoke()
        self.assertTrue(dialog.advanced_open.get())
        extra = str(self.root / "CanvasHost.exe")
        dialog.extra.insert("1.0", extra)
        dialog.hints.insert("1.0", "확인한 창에서만 시험합니다.")
        dialog.advanced_button.invoke()
        self.assertFalse(dialog.advanced_open.get())
        self.button(dialog, "저장").invoke()
        self.assertEqual(dialog.result["control_exes"], [extra])
        self.assertEqual(dialog.result["hints"], "확인한 창에서만 시험합니다.")
        self.assertTrue(dialog.result["enabled"])

    def test_window_picker_empty_refresh_then_primary_selection(self):
        dialog = setup.ProgramEditor(self.app)
        row = {"name": "DrawingDesk", "window_title": "새 그림", "exe": str(self.root / "DrawingDesk.exe")}
        with patch.object(setup, "running_apps", side_effect=[[], [row]]) as discovery:
            popup = dialog.pick_window()
            self.assertEqual(popup.state(), "withdrawn")
            self.assertFalse(popup.winfo_ismapped())
            self.assertEqual(popup.listing.get_children(), ())
            self.assertTrue(popup.empty_text.get().strip())
            self.assertEqual(self.button(popup, "실행파일로 선택").cget("state"), "disabled")
            popup.select_program()
            self.assertTrue(popup.winfo_exists())
            self.assertEqual(dialog.exe.get(), "")
            self.button(popup, "새로고침").invoke()
            self.assertEqual(discovery.call_count, 2)
            ids = popup.listing.get_children()
            self.assertEqual(len(ids), 1)
            self.assertEqual(popup.rows_by_id[ids[0]], row)
            popup.listing.selection_set(ids[0])
            self.assertTrue(popup.listing.bind("<<TreeviewSelect>>"))
            # No Tk event loop or real input runs; execute the selection callback.
            popup.selection_changed()
            self.button(popup, "실행파일로 선택").invoke()
        self.assertEqual(dialog.exe.get(), row["exe"])
        self.assertEqual(dialog.name.get(), row["name"])
        self.assertEqual(dialog.extra.get("1.0", "end").strip(), "")
        self.assertFalse(popup.winfo_exists())
        self.button(dialog, "취소").invoke()

    def test_refresh_drops_old_selection_and_does_not_choose_a_different_program(self):
        dialog = setup.ProgramEditor(self.app)
        dialog.exe.set(str(self.root / "Original.exe"))
        dialog.name.set("사람이 지정한 이름")
        old = {"name": "Old", "window_title": "닫힐 창", "exe": str(self.root / "Old.exe")}
        new = {"name": "New", "window_title": "새 창", "exe": str(self.root / "New.exe")}
        with patch.object(setup, "running_apps", side_effect=[[old], [new]]):
            popup = dialog.pick_window()
            popup.listing.selection_set(popup.listing.get_children()[0])
            self.button(popup, "새로고침").invoke()
            self.assertEqual(popup.listing.selection(), ())
            self.assertEqual(self.button(popup, "실행파일로 선택").cget("state"), "disabled")
            popup.select_program()
            self.assertEqual(dialog.exe.get(), str(self.root / "Original.exe"))
            self.assertTrue(popup.winfo_exists())
            popup.listing.selection_set(popup.listing.get_children()[0])
            popup.selection_changed()
            self.button(popup, "실행파일로 선택").invoke()
        self.assertEqual(dialog.exe.get(), new["exe"])
        self.assertEqual(dialog.name.get(), "사람이 지정한 이름")
        self.button(dialog, "취소").invoke()

    def test_selecting_extra_window_keeps_primary_and_does_not_duplicate_helper(self):
        original = copy.deepcopy(self.config["programs"][1])
        dialog = setup.ProgramEditor(self.app, original)
        row = {"name": "CanvasHost", "window_title": "그림 창", "exe": str(self.root / "CanvasHost.exe")}
        with patch.object(setup, "running_apps", return_value=[row]):
            for _ in range(2):
                popup = dialog.pick_window()
                popup.listing.selection_set(popup.listing.get_children()[0])
                popup.selection_changed()
                self.button(popup, "추가 조작 파일로 선택").invoke()
        self.assertEqual(dialog.exe.get(), original["exe"])
        self.assertEqual(dialog.name.get(), original["name"])
        self.assertEqual(dialog.extra.get("1.0", "end").strip().splitlines(), [row["exe"]])
        self.assertTrue(dialog.advanced_open.get())
        self.button(dialog, "저장").invoke()
        self.assertEqual(dialog.result, {**original, "control_exes": [row["exe"]]})

    def test_escape_cancel_discards_edits_and_footer_stays_outside_scrolling_content(self):
        original = copy.deepcopy(self.config["programs"][0])
        dialog = setup.ProgramEditor(self.app, original)
        dialog.name.set("취소할 변경")
        dialog.extra.insert("end", "\n" + str(self.root / "NotSaved.exe"))
        self.assertTrue(dialog.bind("<Escape>"))
        with patch.object(setup, "running_apps", return_value=[]):
            popup = dialog.pick_window()
        self.assertTrue(popup.bind("<Escape>"))
        popup.cancel()
        self.assertFalse(popup.winfo_exists())
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.name.get(), "취소할 변경")
        # The action footer is a sibling/outside the canvas: scrolling the form
        # cannot hide Save/Cancel. Actual visible sizing is a separate UI check.
        for label in ("저장", "취소"):
            button = self.button(dialog, label)
            ancestor = button.master
            ancestors = []
            while ancestor is not None:
                ancestors.append(ancestor)
                ancestor = getattr(ancestor, "master", None)
            self.assertIn(dialog.footer, ancestors)
            self.assertNotIn(dialog.body_canvas, ancestors)
        dialog.cancel()
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNone(dialog.result)
        self.assertEqual(self.app.config_value, self.config)
        self.assertEqual(setup.load_config(self.config_path), self.config)


if __name__ == "__main__":
    unittest.main()
