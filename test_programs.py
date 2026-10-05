"""Arbitrary app registration with fake executables; no UI/Driver/model call."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import programs
from settings import load_config
from server import ComputerManager


class ProgramRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="computer-use-programs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "별도 설치 프로그램" / "BusinessEditor.exe"
        self.app.parent.mkdir()
        self.app.write_bytes(b"Fake arbitrary third-party exe; never executed")
        self.old_app = self.root / "ExistingEditor.exe"
        self.old_app.write_bytes(b"Fake existing exe; never executed")
        self.config = self.root / "selected-config.json"
        # Leave optional fields absent to prove the new command preserves them.
        self.original = {"version": 1, "driver": "", "mode": "uia", "approval": "each", "max_minutes": 7,
                         "max_actions": 34, "state_dir": str(self.root / "records"),
                         "programs": [{"id": "keep-id", "name": "기존 편집기", "exe": str(self.old_app), "enabled": False}],
                         "custom_metadata": {"keep": ["value", 12]}, "notes": "유지할 기타 정보"}
        self.write(self.original)
        self.tasks = self.root / "records" / "tasks.json"
        self.tasks.parent.mkdir()
        self.tasks.write_text("[]", encoding="utf-8")

    def write(self, value):
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def options(self, **updates):
        options = {"exe": self.app, "name": "업무 편집기", "hints": "새 시험 문서에서 작업합니다."}
        options.update(updates)
        return options

    def preview(self, **updates):
        return programs.preview_add(self.config, **self.options(**updates))

    def add(self, preview=None, **updates):
        preview = preview or self.preview(**updates)
        return programs.add_program(self.config, expected_config_sha256=preview["expected_config_sha256"], **self.options(**updates))

    def test_inspect_and_preview_are_read_only_and_do_not_require_known_presets(self):
        before = self.config.read_bytes()
        files = sorted(str(p) for p in self.root.rglob("*"))
        result = programs.inspect_programs(self.config)
        self.assertEqual(result["approval"], "each")
        self.assertFalse(result["programs"][0]["enabled"])
        preview = self.preview()
        self.assertEqual(preview["status"], "ready_to_add")
        self.assertEqual(preview["program"]["exe"], str(self.app))
        self.assertRegex(preview["program"]["id"], r"^app-[a-f0-9]{12}$")
        self.assertEqual(preview["expected_config_sha256"], result["config_sha256"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), files)

    def test_add_preserves_every_existing_field_and_reconnect_exposes_entry_in_mcp(self):
        not_started = Mock(side_effect=AssertionError("No Driver or session may be started"))
        old_manager = ComputerManager(load_config(self.config), runtime_factory=not_started, transport_factory=not_started)
        before_tasks = self.tasks.read_bytes()
        added = self.add()
        self.assertEqual(added["status"], "added")
        self.assertTrue(added["reconnect_required"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["programs"][:-1], self.original["programs"])
        self.assertEqual({key: value for key, value in saved.items() if key != "programs"},
                         {key: value for key, value in self.original.items() if key != "programs"})
        self.assertEqual(self.tasks.read_bytes(), before_tasks)
        old_list = old_manager.call("computer_programs", {})["structuredContent"]["programs"]
        self.assertEqual(len(old_list), 1)
        new_manager = ComputerManager(load_config(self.config), runtime_factory=not_started, transport_factory=not_started)
        reloaded = new_manager.call("computer_programs", {})["structuredContent"]["programs"]
        self.assertEqual(reloaded[-1], added["program"])
        self.assertEqual(new_manager.status()["approval"], "each")
        not_started.assert_not_called()

    def test_exact_repeat_is_idempotent_even_using_original_preview_hash(self):
        preview = self.preview()
        first = self.add(preview)
        original = self.config.read_bytes()
        second = self.add(preview)
        self.assertEqual(second["status"], "already_present")
        self.assertFalse(second["changed"])
        self.assertEqual(first["program"], second["program"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.preview()["status"], "already_present")

    def test_arbitrary_id_and_explicit_helper_exe_are_persisted(self):
        helper = self.root / "WindowHost.exe"
        helper.touch()
        added = self.add(program_id="team-editor", control_exes=[helper])
        self.assertEqual(added["program"]["id"], "team-editor")
        self.assertEqual(added["program"]["control_exes"], [str(helper)])
        self.assertEqual(load_config(self.config)["programs"][-1], added["program"])

    def test_real_stdio_mcp_reload_returns_the_added_arbitrary_program(self):
        def request_programs():
            server = Path(__file__).resolve().with_name("server.py")
            child = subprocess.Popen([sys.executable, "-B", str(server), "--config", str(self.config)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     encoding="utf-8", text=True)
            responses = queue.Queue()
            def read():
                for line in child.stdout:
                    responses.put(json.loads(line))
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                         "clientInfo": {"name": "metadata-only-test", "version": "1"}}}) + "\n")
                child.stdin.flush()
                self.assertEqual(responses.get(timeout=5)["id"], 1)
                child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "computer_programs", "arguments": {}}}) + "\n")
                child.stdin.flush()
                answer = responses.get(timeout=5)
                self.assertEqual(answer["id"], 2)
                self.assertFalse(answer["result"].get("isError", False))
                return answer["result"]["structuredContent"]["programs"]
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                    self.fail("The isolated metadata-only MCP did not stop on EOF")
                reader.join(timeout=2)
                errors = child.stderr.read()
                child.stdout.close()
                child.stderr.close()
                self.assertEqual(child.returncode, 0, errors)
        self.assertEqual(len(request_programs()), 1)
        added = self.add()
        self.assertEqual(request_programs()[-1], added["program"])
        self.assertFalse((self.tasks.parent / "runs").exists())

    def test_another_cli_writer_lock_prevents_changes(self):
        preview = self.preview()
        before = self.config.read_bytes()
        with programs._write_lock(self.config):
            with self.assertRaises(programs.ProgramError):
                self.add(preview)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.add(preview)["status"], "added")

    def test_conflicting_existing_exe_does_not_rename_replace_or_reenable(self):
        before = self.config.read_bytes()
        with self.assertRaises(programs.ProgramError):
            self.preview(exe=self.old_app, name="기존 편집기", hints="")
        self.assertEqual(self.config.read_bytes(), before)
        self.add()
        before = self.config.read_bytes()
        for updates in ({"name": "다른 이름"}, {"program_id": "new-id"}, {"hints": "다른 사용법"}):
            with self.subTest(updates=updates), self.assertRaises(programs.ProgramError):
                self.preview(**updates)
            self.assertEqual(self.config.read_bytes(), before)

    def test_id_collision_does_not_overwrite_existing_entry(self):
        before = self.config.read_bytes()
        with self.assertRaises(programs.ProgramError):
            self.preview(program_id="keep-id")
        self.assertEqual(self.config.read_bytes(), before)

    def test_stale_source_hash_never_overwrites_another_change(self):
        preview = self.preview()
        foreign = copy.deepcopy(self.original)
        foreign["approval"] = "session"
        foreign["notes"] = "다른 창에서 저장한 변경"
        self.write(foreign)
        before = self.config.read_bytes()
        with self.assertRaises(programs.ProgramError):
            self.add(preview)
        self.assertEqual(self.config.read_bytes(), before)

    def test_changes_while_writing_temp_file_are_rechecked_before_replace(self):
        preview = self.preview()
        foreign = {**self.original, "notes": "임시 파일을 쓰는 동안 변경"}
        real_fsync = os.fsync
        def change_during_write(fd):
            real_fsync(fd)
            self.write(foreign)
        with patch.object(programs.os, "fsync", side_effect=change_during_write), self.assertRaises(programs.ProgramError):
            self.add(preview)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), foreign)
        self.assertEqual(list(self.root.glob("*.programs-tmp-*")), [])

    def test_invalid_and_shell_paths_never_change_configuration(self):
        cmd = self.root / "cmd.exe"
        cmd.touch()
        not_exe = self.root / "other.txt"
        not_exe.touch()
        before = self.config.read_bytes()
        for exe in (self.root / "missing.exe", Path("relative.exe"), cmd, not_exe, self.app.parent, Path(r"\\server\share\file.exe")):
            with self.subTest(exe=exe), self.assertRaises((programs.ProgramError, ValueError)):
                self.preview(exe=exe)
            self.assertEqual(self.config.read_bytes(), before)
        with self.assertRaises(programs.ProgramError):
            self.preview(control_exes=[cmd])
        self.assertEqual(self.config.read_bytes(), before)

    def test_linked_exe_config_and_helper_paths_are_rejected_without_mutation(self):
        before = self.config.read_bytes()
        real_chain = programs._plain_chain
        for linked in (self.app, self.config):
            def chain(path, **kwargs):
                return False if Path(path) == linked else real_chain(path, **kwargs)
            with self.subTest(linked=linked), patch.object(programs, "_plain_chain", side_effect=chain), self.assertRaises(programs.ProgramError):
                self.preview()
            self.assertEqual(self.config.read_bytes(), before)
        helper = self.root / "LinkedHelper.exe"
        helper.touch()
        with patch.object(programs, "_plain_chain", side_effect=lambda path, **kw: False if Path(path) == helper else real_chain(path, **kw)), self.assertRaises(programs.ProgramError):
            self.preview(control_exes=[helper])
        self.assertEqual(self.config.read_bytes(), before)

    def test_link_created_during_temp_write_blocks_replace(self):
        preview = self.preview()
        before = self.config.read_bytes()
        real_chain, real_fsync = programs._plain_chain, os.fsync
        changed = False
        def during_write(fd):
            nonlocal changed
            real_fsync(fd)
            changed = True
        def chain(path, **kwargs):
            return False if changed and Path(path) == self.app else real_chain(path, **kwargs)
        with patch.object(programs.os, "fsync", side_effect=during_write), patch.object(programs, "_plain_chain", side_effect=chain), self.assertRaises(programs.ProgramError):
            self.add(preview)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.programs-tmp-*")), [])

    def test_linked_lock_is_not_followed(self):
        preview = self.preview()
        before = self.config.read_bytes()
        real_chain = programs._plain_chain
        lock_path = self.config.with_name(self.config.name + ".programs.lock")
        with patch.object(programs, "_plain_chain", side_effect=lambda path, **kw: False if Path(path) == lock_path else real_chain(path, **kw)), self.assertRaises(programs.ProgramError):
            self.add(preview)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(lock_path.exists())

    def test_invalid_names_ids_limits_and_duplicate_control_exes_rejected(self):
        before = self.config.read_bytes()
        for updates in ({"name": " "}, {"name": "x" * 101}, {"program_id": "../bad"},
                        {"hints": "x" * 10001}, {"control_exes": [self.app]}):
            with self.subTest(updates=updates), self.assertRaises((programs.ProgramError, ValueError)):
                self.preview(**updates)
            self.assertEqual(self.config.read_bytes(), before)
        helper = self.root / "Helper.exe"
        helper.touch()
        with self.assertRaises(programs.ProgramError):
            self.preview(control_exes=[helper, helper])

    def test_nonexistent_relative_and_malformed_config_preserved(self):
        with self.assertRaises(programs.ProgramError):
            programs.inspect_programs(self.root / "missing-config.json")
        with self.assertRaises(programs.ProgramError):
            programs.inspect_programs("relative.json")
        self.config.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.config.read_text(encoding="utf-8"), "not JSON")

    def test_cli_preview_apply_and_error_return_json(self):
        common = ["--config", str(self.config), "--exe", str(self.app), "--name", "임의 앱"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = programs.main(["preview-add", *common])
        self.assertEqual(code, 0)
        preview = json.loads(output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = programs.main(["add", *common, "--expected-config-sha256", preview["expected_config_sha256"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "added")
        cmd = self.root / "cmd.exe"
        cmd.touch()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = programs.main(["preview-add", "--config", str(self.config), "--exe", str(cmd), "--name", "등록 불가"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
