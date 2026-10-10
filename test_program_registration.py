"""Chat registration contracts; fake executables, no desktop input."""
import copy
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import program_registration as registration
import programs
from server import ComputerManager, StdioServer
from settings import load_config
from test_server import QueueInput
from test_learning import Runtime, TARGET


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.old = self.root / "Existing.exe"
        self.app = self.root / "BusinessEditor.exe"
        self.old.touch()
        self.app.touch()
        self.config_path = self.root / "config.json"
        self.original = {"version": 1, "tool_profile": "legacy", "driver": str(self.root / "driver.exe"), "mode": "uia", "approval": "client",
                         "max_minutes": 7, "max_actions": 34, "state_dir": str(self.root / "state"),
                         "programs": [{"id": "old", "name": "기존", "exe": str(self.old), "enabled": True}],
                         "notes": "preserve", "custom_metadata": {"keep": 1}}
        Path(self.original["driver"]).touch()
        self.write(self.original)
        self.manager = ComputerManager(load_config(self.config_path), config_path=self.config_path)

    def write(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def call(self, **options):
        return self.manager.call("computer_register_program", options or {"exe": str(self.app)})["structuredContent"]

    def row(self, **overrides):
        return {"pid": 10, "window_id": 20, "creation_time": 30, "exe": str(self.app),
                "class_name": "Form", "window_title": "시험 창", "name": "BusinessEditor", **overrides}

    def test_add_preserves_all_settings_and_updates_stores_without_starting_driver(self):
        with patch.object(self.manager, "runtime_factory") as runtime, patch.object(self.manager, "transport_factory") as driver:
            answer = self.call(name="친절한 이름", exe=str(self.app))
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["reconnect_required"])
        self.assertEqual(answer["status"], "registered")
        self.assertEqual(answer["program"]["name"], "친절한 이름")
        runtime.assert_not_called()
        driver.assert_not_called()
        disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["programs"][:-1], self.original["programs"])
        self.assertEqual({k: v for k, v in disk.items() if k != "programs"}, {k: v for k, v in self.original.items() if k != "programs"})
        for store in (self.manager, self.manager.tasks, self.manager.elements):
            self.assertEqual(store.config["programs"][-1], answer["program"])
        self.assertEqual(self.manager.status()["configuration"]["source_state"], "current")

    def test_idempotent_short_request_preserves_existing_name_hints_and_launch(self):
        first = self.call(exe=str(self.app), name="사용자 이름", hints="사용법", arguments=["--from-shortcut"])
        before = self.config_path.read_bytes()
        again = self.call(exe=str(self.app))
        self.assertEqual(again["status"], "already_present")
        self.assertEqual(first["program"], again["program"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_chat_arguments_preserve_empty_and_repeated_values(self):
        arguments = ["--label", "", "--label", "same value", "same value"]
        answer = self.call(exe=str(self.app), arguments=arguments)
        self.assertEqual(answer["status"], "registered")
        self.assertEqual(answer["program"]["launch"]["arguments"], arguments)

    def test_chat_accepts_explicit_empty_arguments_and_control_list(self):
        answer = self.call(exe=str(self.app), arguments=[], control_exes=[])
        self.assertEqual(answer["status"], "registered")

    def test_explicit_empty_arguments_does_not_reuse_a_previous_shortcut(self):
        previous = self.call(exe=str(self.app), arguments=["--from-shortcut"])
        answer = self.call(exe=str(self.app), arguments=[])
        self.assertEqual(answer["status"], "registered")
        self.assertEqual(answer["program"]["launch"]["arguments"], [])
        self.assertNotEqual(answer["program"]["id"], previous["program"]["id"])
        old = next(p for p in self.manager.config["programs"] if p["id"] == previous["program"]["id"])
        self.assertEqual(old["launch"]["arguments"], ["--from-shortcut"])

    def test_explicit_empty_arguments_reuses_single_plain_registration(self):
        previous = self.call(exe=str(self.app))
        answer = self.call(exe=str(self.app), arguments=[])
        self.assertEqual(answer["status"], "already_present")
        self.assertEqual(answer["program"], previous["program"])

    def test_active_session_scope_is_unchanged_and_next_begin_receives_new_program(self):
        active = SimpleNamespace(state="active", config=copy.deepcopy(self.manager.config),
                                 programs=copy.deepcopy(self.manager.config["programs"]))
        self.manager.session = active
        before = copy.deepcopy(active.__dict__)
        answer = self.call()
        self.assertEqual(active.__dict__, before)
        self.assertFalse(answer["active_session_scope_changed"])
        self.assertEqual(answer["next_tool"], "computer_end")
        active.state = "stopped"
        factory = Mock(return_value=SimpleNamespace(start=lambda: {"started": True}))
        self.manager.runtime_factory = factory
        self.manager.begin({"program_ids": [answer["program"]["id"]], "task_description": "시험"})
        self.assertEqual(factory.call_args.args[1], [answer["program"]])

    def test_new_program_can_save_tasks_and_learn_without_reconnect(self):
        answer = self.call()
        app = answer["program"]
        task = self.manager.tasks.save({"name": "새 앱 작업", "instructions": "시험", "expected": "확인", "program_ids": [app["id"]]})
        self.assertEqual(task["program_ids"], [app["id"]])
        runtime = Runtime({"programs": [app]})
        learned = self.manager.elements.teach(runtime, TARGET, app["id"], 2, "상태", expected_selector={"automation_id": "status"})
        self.assertEqual(learned["program_id"], app["id"])
        self.assertIs(self.manager.teachings.library, self.manager.elements)

    def test_candidate_is_metadata_only_and_registration_revalidates_identity(self):
        self.manager.registration.candidate_source = Mock(return_value=[self.row()])
        response = self.manager.call("computer_program_candidates", {})["structuredContent"]
        self.assertFalse(response["screen_accessed"])
        choice = response["candidates"][0]
        self.assertEqual(set(choice), {"candidate_id", "name", "exe", "window_title"})
        answer = self.call(candidate_id=choice["candidate_id"])
        self.assertEqual(answer["program"]["exe"], str(self.app))
        self.assertEqual(self.manager.registration.candidate_source.call_count, 2)

    def test_uri_only_offers_actual_windows_without_registering_handler(self):
        self.manager.registration.candidate_source = Mock(return_value=[self.row()])
        before = self.config_path.read_bytes()
        with patch('program_registration.protocol_handler', return_value={'registered_handler': 'UnrelatedLauncher.exe', 'handler_is_control_target': False}):
            answer = self.call(launch_uri='company-test://server/menu')
        self.assertEqual(answer['status'], 'choose_program_window')
        self.assertFalse(answer['saved'])
        self.assertFalse(answer['input_dispatched'])
        self.assertEqual(self.config_path.read_bytes(), before)
        selected = self.call(candidate_id=answer['candidates'][0]['candidate_id'], launch_uri=answer['launch_uri'])
        self.assertEqual(selected['status'], 'registered')
        self.assertEqual(selected['program']['exe'], str(self.app))
        self.assertEqual(selected['program']['launch'], {'kind': 'uri', 'target': 'company-test://server/menu'})
        self.assertFalse(selected['reconnect_required'])

    def test_previously_confirmed_uri_does_not_require_reselecting_window(self):
        first = self.call(exe=str(self.app), launch_uri='company-test://server/menu')
        self.manager.registration.candidate_source = Mock(side_effect=AssertionError('no rediscovery'))
        again = self.call(launch_uri='company-test://server/menu')
        self.assertEqual(again['status'], 'already_present')
        self.assertEqual(again['program'], first['program'])

    def test_missing_program_returns_next_tool_not_configuration_hunt(self):
        answer = self.manager.call('computer_register_program', {})['structuredContent']
        self.assertEqual(answer['next_tool'], 'computer_program_candidates')
        self.assertFalse(answer['input_dispatched'])
        self.assertEqual(answer['diagnostic']['stage'], 'program_registration')

    def test_stale_ambiguous_and_pid_reused_candidates_never_write(self):
        source = Mock(return_value=[self.row()])
        self.manager.registration.candidate_source = source
        original = self.config_path.read_bytes()
        for changed in ([], [self.row(creation_time=31)], [self.row(window_id=21)], [self.row(exe=str(self.old))], [self.row(), self.row()]):
            source.return_value = [self.row()]
            token = self.manager.registration.list_candidates()["candidates"][0]["candidate_id"]
            source.return_value = changed
            answer = self.call(candidate_id=token)
            self.assertEqual(answer["diagnostic"]["code"], "candidate_stale")
            self.assertEqual(self.config_path.read_bytes(), original)

    def test_expired_candidate_is_rejected_before_inventory(self):
        source = Mock(return_value=[self.row()])
        self.manager.registration.candidate_source = source
        self.manager.registration.clock = lambda: 100
        token = self.manager.registration.list_candidates()["candidates"][0]["candidate_id"]
        self.manager.registration.clock = lambda: 401
        self.assertEqual(self.call(candidate_id=token)["diagnostic"]["code"], "candidate_stale")
        self.assertEqual(source.call_count, 1)

    def test_external_config_change_and_concurrent_preview_are_not_overwritten(self):
        changed = copy.deepcopy(self.original)
        changed["max_actions"] = 19
        self.write(changed)
        self.assertEqual(self.call()["diagnostic"]["code"], "configuration_changed")
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), changed)
        self.write(self.original)
        original_preview = programs.preview_add
        def concurrent(*args, **kwargs):
            self.write(changed)
            return original_preview(*args, **kwargs)
        with patch.object(programs, "preview_add", side_effect=concurrent):
            self.assertEqual(self.call()["diagnostic"]["code"], "configuration_changed")
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), changed)

    def test_permissions_error_is_structured_and_keeps_memory_and_disk(self):
        before = self.config_path.read_bytes(), copy.deepcopy(self.manager.config)
        with patch.object(programs, "add_program", side_effect=PermissionError("쓰기 권한 없음")):
            answer = self.call()
        self.assertFalse(answer["saved"])
        self.assertEqual(answer["diagnostic"]["code"], "registration_denied")
        self.assertEqual(before, (self.config_path.read_bytes(), self.manager.config))

    def test_failed_partial_memory_apply_rolls_back_all_stores_and_reports_saved(self):
        originals = self.manager.config, self.manager.tasks.config, self.manager.elements.config
        def fail(updated):
            self.manager.config = updated
            raise RuntimeError("simulated store failure")
        with patch.object(self.manager.registration, "_apply", side_effect=fail):
            answer = self.call()
        self.assertEqual(answer["status"], "saved_restart_required")
        self.assertTrue(answer["saved"])
        self.assertFalse(answer["applied_to_next_session"])
        self.assertTrue(answer["reconnect_required"])
        self.assertEqual(len(json.loads(self.config_path.read_text(encoding="utf-8"))["programs"]), 2)
        for before, after in zip(originals, (self.manager.config, self.manager.tasks.config, self.manager.elements.config)):
            self.assertIs(before, after)

    def test_registration_while_learning_or_editing_is_not_applied(self):
        self.manager.session = SimpleNamespace(state="active")
        before = self.config_path.read_bytes()
        with patch.object(self.manager.teachings, "pending", return_value={"id": "teach"}):
            self.assertEqual(self.call()["status"], "teaching_pending")
        with patch.object(self.manager.process_editors, "pending", return_value={"id": "edit"}):
            self.assertEqual(self.call()["status"], "editing")
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_arguments_start_folder_uri_and_conflicts(self):
        answer = self.call(exe=str(self.app), arguments=["--mode", "한 글"], working_directory=str(self.root))
        self.assertEqual(answer["program"]["launch"], {"kind": "exe", "arguments": ["--mode", "한 글"], "cwd": str(self.root)})
        other = self.root / "Launcher.exe"
        other.touch()
        answer = self.call(exe=str(other), launch_uri="business://menu/test")
        self.assertEqual(answer["program"]["launch"], {"kind": "uri", "target": "business://menu/test"})
        self.assertEqual(self.call(exe=str(other), launch_uri="business://menu/test", arguments=["x"])["diagnostic"]["code"], "invalid_launch")

    def test_untracked_and_missing_choice_reject_without_file_search(self):
        self.assertEqual(self.call(name="입력 없음")["diagnostic"]["code"], "choose_program")
        self.manager.config_path = None
        self.assertEqual(self.call()["diagnostic"]["code"], "configuration_untracked")

    def test_tools_list_and_call_roundtrip(self):
        input_stream, output = QueueInput(), io.StringIO()
        self.manager.lowlevel_schemas = lambda *args: []
        server = StdioServer(self.manager, input_stream, output)
        thread = threading.Thread(target=server.run)
        thread.start()
        self.addCleanup(lambda: (input_stream.eof(), thread.join(3)))
        def request(identity, method, params=None):
            input_stream.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params or {}})
            until = time.monotonic() + 3
            while time.monotonic() < until:
                for line in output.getvalue().splitlines():
                    row = json.loads(line)
                    if row.get("id") == identity:
                        return row["result"]
                time.sleep(0.005)
            self.fail("MCP response timed out")
        request(1, "initialize")
        tools = request(2, "tools/list")["tools"]
        self.assertTrue({"computer_register_program", "computer_program_candidates"} <= {item["name"] for item in tools})
        response = request(3, "tools/call", {"name": "computer_register_program", "arguments": {"exe": str(self.app)}})
        self.assertFalse(response["isError"])
        listed = request(4, "tools/call", {"name": "computer_programs"})
        self.assertEqual(listed["structuredContent"]["programs"][-1], response["structuredContent"]["program"])

    @unittest.skipUnless(os.name == "nt", "Windows ctypes metadata adapter")
    def test_native_candidates_exclude_other_user_before_title_read(self):
        def pid(hwnd, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = hwnd // 10
            return 1
        def times(handle, creation, *_):
            ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents.dwLowDateTime = 123
            return True
        def executable(handle, flags, buffer, size):
            buffer.value = str(self.app)
            return True
        def title(hwnd, buffer, size):
            buffer.value = "창 제목"
            return 4
        kernel = SimpleNamespace(OpenProcess=Mock(side_effect=lambda flags, inherit, pid: pid), CloseHandle=Mock(),
                                 GetProcessTimes=Mock(side_effect=times), QueryFullProcessImageNameW=Mock(side_effect=executable))
        user = SimpleNamespace(EnumWindows=Mock(side_effect=lambda cb, _: bool(cb(100, 0) and cb(200, 0))),
                               IsWindowVisible=Mock(return_value=True), GetWindowThreadProcessId=Mock(side_effect=pid),
                               GetWindowTextW=Mock(side_effect=title), GetClassNameW=Mock(side_effect=title))
        with patch.object(registration.ctypes, "WinDLL", side_effect=lambda name, **kwargs: kernel if name == "kernel32" else user), \
             patch.object(registration, "_same_windows_user_session", side_effect=lambda pid, handle: pid == 10):
            rows = registration.live_candidates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 10)
        self.assertEqual(user.GetWindowTextW.call_count, 1)
        self.assertEqual(kernel.CloseHandle.call_count, 2)


if __name__ == "__main__":
    unittest.main()

