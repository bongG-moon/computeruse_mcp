"""Registration tests use fake CLI processes and isolated temporary profiles only."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import register


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="computer-use-register-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / '한글 배포 폴더'
        self.app.mkdir()
        self.project = self.root / '선택한 업무 폴더'
        self.project.mkdir()
        (self.app / "runtime").mkdir()
        (self.app / "runtime" / "python.exe").write_bytes(b"fake-python")
        (self.app / "server.py").write_text("# test server only\n", encoding="utf-8")
        self.config = self.app / '설정 공간' / '사용 설정.json'
        self.config.parent.mkdir()
        self.config.write_text("{}", encoding="utf-8")
        self.profile = self.root / '개인 Claude'
        self.profile.mkdir()
        self.user_file = self.profile / ".claude.json"
        self.original = {"model": "existing-model", "oauthAccount": {"accountUuid": "dummy-account"},
                         "projects": {}, "mcpServers": {"other-tool": {"type": "stdio", "command": "existing.exe", "args": []}}}
        self.write_profile(self.original)
        self.calls = []
        self.exe = str(self.app / 'Claude 경로' / 'claude.exe')
        self.patches = [
            patch.object(register, "APP_DIR", self.app),
            patch.object(register.Path, "cwd", return_value=self.app),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile), "PYTHONPATH": "foreign-path",
                                    "GIT_DIR": "", "GIT_WORK_TREE": "", "GIT_COMMON_DIR": ""}),
            patch.object(register, "_native_claude", return_value=self.exe),
            patch.object(register.subprocess, "run", side_effect=self.fake_cli),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def write_profile(self, value):
        self.user_file.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_profile(self):
        return json.loads(self.user_file.read_text(encoding="utf-8"))

    def fake_cli(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], self.exe)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if command[-1] == "--help":
            self.assertIn(kwargs["cwd"], (self.app, self.project))
            return subprocess.CompletedProcess(command, 0, "Usage: <json> --scope (local, user, project)", "")
        self.assertEqual(command[-2], "--scope")
        scope = command[-1]
        self.assertIn(scope, ("user", "local"))
        self.assertEqual(kwargs["cwd"], self.project if scope == "local" else self.app)
        profile = self.read_profile()
        if scope == "local":
            projects = profile.setdefault("projects", {})
            key = next((key for key in projects if Path(key).resolve() == self.project), self.project.as_posix())
            servers = projects.setdefault(key, {}).setdefault("mcpServers", {})
        else:
            servers = profile.setdefault("mcpServers", {})
        if command[2] == "add-json":
            self.assertEqual(command[3], register.SERVER_NAME)
            self.assertNotIn(register.SERVER_NAME, servers)
            servers[register.SERVER_NAME] = json.loads(command[4])
        elif command[2] == "remove":
            self.assertEqual(command[3], register.SERVER_NAME)
            servers.pop(register.SERVER_NAME)
        else:
            self.fail("Unexpected command, particularly an MCP health check: " + repr(command))
        self.write_profile(profile)
        return subprocess.CompletedProcess(command, 0, "Saved", "")

    def test_export_is_standalone_and_preserves_native_unicode_arguments(self):
        before = self.user_file.read_bytes()
        output = register.export_config(self.config)
        entry = json.loads(output.read_text(encoding="utf-8"))["mcpServers"][register.SERVER_NAME]
        self.assertEqual(entry["command"], str(self.app / "runtime" / "python.exe"))
        self.assertEqual(entry["args"], ["-B", "-s", str(self.app / "server.py"), "--config", str(self.config)])
        self.assertEqual(entry["env"]["PYTHONPATH"], "")
        self.assertEqual(entry["env"]["PYTHONNOUSERSITE"], "1")
        self.assertEqual(self.user_file.read_bytes(), before)
        self.assertFalse(self.calls)
        self.assertEqual(register.export_config(self.config), output)

    def test_export_rejects_existing_foreign_file_and_profile_paths(self):
        output = self.app / "existing.json"
        output.write_text('{"keep":true}', encoding="utf-8")
        for path in (output, self.config, self.user_file, self.app / ".mcp.json", self.app / "settings.json"):
            with self.subTest(path=path), self.assertRaises(register.RegistrationError):
                register.export_config(self.config, path)
        self.assertEqual(output.read_text(encoding="utf-8"), '{"keep":true}')
        self.assertFalse(self.calls)

    def test_register_adds_only_user_entry_through_native_cli(self):
        self.assertEqual(register.SERVER_NAME, "local-computer-use")
        result = register.register_claude(self.config)
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["scope"], "user")
        self.assertIsNone(result["project_dir"])
        after = self.read_profile()
        entry = after["mcpServers"].pop(register.SERVER_NAME)
        self.assertEqual(after, self.original)
        self.assertEqual(entry, register.make_server_entry(self.config))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(json.loads(self.calls[-1][0][4]), entry)

    @unittest.skipUnless(os.name == "nt", "Windows argument parser")
    def test_windows_native_command_arguments_round_trip(self):
        import ctypes
        from ctypes import wintypes
        entry = register.make_server_entry(self.config)
        command = [self.exe, "mcp", "add-json", register.SERVER_NAME,
                   json.dumps(entry, ensure_ascii=False), "--scope", "user"]
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        count = ctypes.c_int()
        values = shell.CommandLineToArgvW(subprocess.list2cmdline(command), ctypes.byref(count))
        self.assertTrue(values)
        try:
            self.assertEqual([values[i] for i in range(count.value)], command)
        finally:
            kernel.LocalFree(values)

    def test_identical_duplicate_is_noop_without_cli(self):
        profile = copy.deepcopy(self.original)
        profile["mcpServers"][register.SERVER_NAME] = register.make_server_entry(self.config)
        self.write_profile(profile)
        before = self.user_file.read_bytes()
        result = register.register_claude(self.config)
        self.assertFalse(result["changed"])
        self.assertFalse(self.calls)
        self.assertEqual(self.user_file.read_bytes(), before)

    def test_foreign_duplicate_and_null_duplicate_are_never_overwritten(self):
        for foreign in ({"command": "foreign.exe", "args": []}, None):
            with self.subTest(foreign=foreign):
                profile = copy.deepcopy(self.original)
                profile["mcpServers"][register.SERVER_NAME] = foreign
                self.write_profile(profile)
                before = self.user_file.read_bytes()
                with self.assertRaises(register.RegistrationError):
                    register.register_claude(self.config)
                self.assertEqual(register.registration_status(self.config)["status"], "conflict")
                self.assertEqual(self.user_file.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_local_duplicate_blocks_add_and_is_never_removed(self):
        profile = copy.deepcopy(self.original)
        profile["projects"][str(self.app)] = {"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}
        self.write_profile(profile)
        before = self.user_file.read_bytes()
        with self.assertRaises(register.RegistrationError):
            register.register_claude(self.config)
        result = register.unregister_claude(self.config)
        self.assertFalse(result["changed"])
        self.assertEqual(self.user_file.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_project_duplicate_blocks_add_without_modifying_project(self):
        project = self.app / ".mcp.json"
        project.write_text(json.dumps({"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}), encoding="utf-8")
        before = project.read_bytes()
        with self.assertRaises(register.RegistrationError):
            register.register_claude(self.config)
        self.assertFalse(register.unregister_claude(self.config)["changed"])
        self.assertEqual(project.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_remove_requires_full_match_and_preserves_other_scopes(self):
        profile = copy.deepcopy(self.original)
        expected = register.make_server_entry(self.config)
        profile["mcpServers"][register.SERVER_NAME] = copy.deepcopy(expected)
        profile["mcpServers"][register.SERVER_NAME]["env"]["PYTHONPATH"] = "changed"
        self.write_profile(profile)
        with self.assertRaises(register.RegistrationError):
            register.unregister_claude(self.config)
        self.assertFalse(self.calls)
        profile["mcpServers"][register.SERVER_NAME] = expected
        profile["projects"][str(self.app)] = {"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}
        self.write_profile(profile)
        result = register.unregister_claude(self.config)
        self.assertEqual(result["status"], "removed")
        profile["mcpServers"].pop(register.SERVER_NAME)
        self.assertEqual(self.read_profile(), profile)

    def test_malformed_profile_fails_closed(self):
        self.user_file.write_text("not valid json", encoding="utf-8")
        with self.assertRaises(register.RegistrationError):
            register.register_claude(self.config)
        with self.assertRaises(register.RegistrationError):
            register.unregister_claude(self.config)
        self.assertFalse(self.calls)

    def test_cli_rejection_does_not_claim_registration_or_retry(self):
        def reject(command, **kwargs):
            if command[-1] == "--help":
                return self.fake_cli(command, **kwargs)
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 1, "", "already exists")
        with patch.object(register.subprocess, "run", side_effect=reject):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.read_profile(), self.original)

    def test_changed_profile_during_help_is_not_modified_further(self):
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            profile = self.read_profile()
            profile["model"] = "changed-by-user"
            self.write_profile(profile)
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(register.SERVER_NAME, self.read_profile()["mcpServers"])

    def test_unrelated_changes_after_cli_are_reported_without_rollback(self):
        def unrelated(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            if command[-1] != "--help":
                profile = self.read_profile()
                profile["model"] = "changed-elsewhere"
                self.write_profile(profile)
            return result
        with patch.object(register.subprocess, "run", side_effect=unrelated):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config)
        self.assertEqual(self.read_profile()["model"], "changed-elsewhere")
        self.assertEqual(len(self.calls), 2)

    def test_default_profile_is_current_user_and_custom_profile_is_honored(self):
        self.assertEqual(register.user_config_file(), self.user_file)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}), patch.object(register.Path, "home", return_value=self.root):
            self.assertEqual(register.user_config_file(), self.root / ".claude.json")

    def local_profile(self, entry=None):
        profile = copy.deepcopy(self.original)
        profile["projects"][self.project.as_posix()] = {
            "hasTrustDialogAccepted": True, "allowedTools": ["existing-tool"],
            "mcpServers": {"project-other": {"command": "project-other.exe"}}}
        profile["projects"][self.app.as_posix()] = {
            "hasTrustDialogAccepted": False,
            "mcpServers": {register.SERVER_NAME: {"command": "different-project.exe"}}}
        if entry is not None:
            profile["projects"][self.project.as_posix()]["mcpServers"][register.SERVER_NAME] = entry
        self.write_profile(profile)
        return profile

    def test_local_register_uses_exact_project_cwd_and_preserves_other_projects(self):
        before = self.local_profile()
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["scope"], "local")
        self.assertEqual(result["project_dir"], str(self.project))
        self.assertEqual(result["config_file"], str(self.user_file))
        self.assertEqual(result["other_scopes"], [])
        self.assertFalse(result["can_upgrade"])
        after = self.read_profile()
        self.assertEqual(after["projects"][self.project.as_posix()]["mcpServers"].pop(register.SERVER_NAME),
                         register.make_server_entry(self.config))
        self.assertEqual(after, before)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(kwargs["cwd"] == self.project for _, kwargs in self.calls))
        self.assertEqual(self.calls[-1][0][-2:], ["--scope", "local"])

    def test_local_register_can_create_previously_absent_project(self):
        profile = {"model": "keep-me"}
        self.write_profile(profile)
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["changed"])
        after = self.read_profile()
        self.assertEqual(after["model"], "keep-me")
        self.assertNotIn("mcpServers", after)
        self.assertEqual(after["projects"][self.project.as_posix()]["mcpServers"][register.SERVER_NAME],
                         register.make_server_entry(self.config))

    def test_local_exact_duplicate_is_noop_and_status_never_starts_cli(self):
        self.local_profile(register.make_server_entry(self.config))
        before = self.user_file.read_bytes()
        status = register.registration_status(self.config, scope="local", project_dir=self.project)
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(status["status"], "registered")
        self.assertFalse(result["changed"])
        self.assertEqual(self.user_file.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_local_foreign_and_null_entries_are_never_overwritten_or_removed(self):
        for entry in ({"command": "foreign.exe"}, None):
            with self.subTest(entry=entry):
                profile = self.local_profile()
                profile["projects"][self.project.as_posix()]["mcpServers"][register.SERVER_NAME] = entry
                self.write_profile(profile)
                for method in (register.register_claude, register.unregister_claude):
                    with self.assertRaises(register.RegistrationError):
                        method(self.config, scope="local", project_dir=self.project)
                self.assertEqual(register.registration_status(self.config, scope="local", project_dir=self.project)["status"], "conflict")
                self.assertEqual(self.read_profile(), profile)
        self.assertFalse(self.calls)

    def test_local_user_scope_conflict_is_never_overwritten_even_when_exact(self):
        entry = register.make_server_entry(self.config)
        for same_local in (False, True):
            with self.subTest(same_local=same_local):
                profile = self.local_profile(entry if same_local else None)
                profile["mcpServers"][register.SERVER_NAME] = entry
                self.write_profile(profile)
                status = register.registration_status(self.config, scope="local", project_dir=self.project)
                self.assertFalse(status["ok"])
                self.assertEqual(status["status"], "scope_conflict")
                self.assertEqual(status["other_scopes"], ["user: " + str(self.user_file)])
                with self.assertRaises(register.RegistrationError):
                    register.register_claude(self.config, scope="local", project_dir=self.project)
                self.assertEqual(self.read_profile(), profile)
        self.assertFalse(self.calls)

    def test_local_project_scope_conflicts_in_project_or_ancestor_are_preserved(self):
        for folder in (self.project, self.root):
            with self.subTest(folder=folder):
                file = folder / ".mcp.json"
                file.write_text(json.dumps({"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}), encoding="utf-8")
                before = file.read_bytes()
                try:
                    with self.assertRaises(register.RegistrationError):
                        register.register_claude(self.config, scope="local", project_dir=self.project)
                    status = register.registration_status(self.config, scope="local", project_dir=self.project)
                    self.assertEqual(status["status"], "scope_conflict")
                    self.assertEqual(file.read_bytes(), before)
                    self.assertEqual(self.read_profile(), self.original)
                finally:
                    file.unlink()
        self.assertFalse(self.calls)

    def test_local_does_not_treat_unrelated_distribution_project_as_conflict(self):
        file = self.app / ".mcp.json"
        file.write_text(json.dumps({"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}), encoding="utf-8")
        before = file.read_bytes()
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["changed"])
        self.assertEqual(file.read_bytes(), before)

    def test_local_unregister_preserves_user_entry_and_other_projects(self):
        profile = self.local_profile(register.make_server_entry(self.config))
        profile["mcpServers"][register.SERVER_NAME] = {"command": "user-foreign.exe"}
        self.write_profile(profile)
        result = register.unregister_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(result["status"], "removed")
        self.assertTrue(result["changed"])
        self.assertEqual(result["scope"], "local")
        self.assertEqual(result["project_dir"], str(self.project))
        profile["projects"][self.project.as_posix()]["mcpServers"].pop(register.SERVER_NAME)
        self.assertEqual(self.read_profile(), profile)
        self.assertTrue(all(kwargs["cwd"] == self.project for _, kwargs in self.calls))
        self.calls.clear()
        self.assertFalse(register.unregister_claude(self.config, scope="local", project_dir=self.project)["changed"])
        self.assertFalse(self.calls)

    def test_local_without_entry_never_removes_user_or_other_project_entry(self):
        profile = self.local_profile()
        profile["mcpServers"][register.SERVER_NAME] = register.make_server_entry(self.config)
        self.write_profile(profile)
        result = register.unregister_claude(self.config, scope="local", project_dir=self.project)
        self.assertFalse(result["changed"])
        self.assertEqual(self.read_profile(), profile)
        self.assertFalse(self.calls)

    def test_invalid_scope_or_project_fails_before_cli(self):
        invalid = [("local", None), ("local", Path("relative")), ("local", self.root / "missing"),
                   ("local", self.config), ("project", self.project), ("user", self.project)]
        for scope, project in invalid:
            for method in (register.registration_status, register.register_claude, register.unregister_claude):
                with self.subTest(scope=scope, project=project, method=method.__name__), self.assertRaises(register.RegistrationError):
                    method(self.config, scope=scope, project_dir=project)
        self.assertFalse(self.calls)
        self.assertEqual(self.read_profile(), self.original)

    def test_local_child_of_git_directory_is_rejected_without_scope_expansion(self):
        (self.root / ".git").mkdir()
        before = self.local_profile(register.make_server_entry(self.config))
        for method in (register.registration_status, register.register_claude, register.unregister_claude):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(register.RegistrationError, "상위 Git 프로젝트"):
                method(self.config, scope="local", project_dir=self.project)
        self.assertEqual(self.read_profile(), before)
        self.assertFalse(self.calls)

    def test_local_child_of_git_file_is_rejected_without_scope_expansion(self):
        (self.root / ".git").write_text("gitdir: C:/synthetic-worktree-metadata", encoding="utf-8")
        with self.assertRaisesRegex(register.RegistrationError, "상위 Git 프로젝트"):
            register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(self.read_profile(), self.original)
        self.assertFalse(self.calls)

    def test_local_exact_git_root_is_allowed_even_with_outer_repository(self):
        (self.root / ".git").mkdir()
        (self.project / ".git").mkdir()
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["project_dir"], str(self.project))
        self.assertTrue(register.unregister_claude(self.config, scope="local", project_dir=self.project)["changed"])
        self.assertTrue(all(kwargs["cwd"] == self.project for _, kwargs in self.calls))

    def test_local_exact_git_file_root_is_not_replaced_with_outer_repository(self):
        (self.root / ".git").mkdir()
        (self.project / ".git").write_text("gitdir: C:/synthetic-worktree-metadata", encoding="utf-8")
        result = register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["project_dir"], str(self.project))

    def test_local_git_scope_environment_overrides_are_rejected_and_preserved(self):
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            value = str(self.root / "foreign-git-location")
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                for method in (register.registration_status, register.register_claude, register.unregister_claude):
                    with self.assertRaisesRegex(register.RegistrationError, name):
                        method(self.config, scope="local", project_dir=self.project)
                self.assertEqual(os.environ[name], value)
        self.assertEqual(self.read_profile(), self.original)
        self.assertFalse(self.calls)

    def test_local_git_boundary_changes_during_help_stop_before_registration(self):
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            (self.root / ".git").mkdir()
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaisesRegex(register.RegistrationError, "상위 Git 프로젝트"):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read_profile(), self.original)

    def test_local_git_boundary_changes_during_help_stop_before_removal(self):
        before = self.local_profile(register.make_server_entry(self.config))
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            (self.root / ".git").mkdir()
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaisesRegex(register.RegistrationError, "상위 Git 프로젝트"):
                register.unregister_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read_profile(), before)

    def test_local_git_environment_changes_during_help_stop_before_registration(self):
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            os.environ["GIT_WORK_TREE"] = str(self.root)
            return result
        with patch.dict(os.environ, {}, clear=False), patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaisesRegex(register.RegistrationError, "GIT_WORK_TREE"):
                register.register_claude(self.config, scope="local", project_dir=self.project)
            self.assertEqual(os.environ["GIT_WORK_TREE"], str(self.root))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read_profile(), self.original)

    def test_local_unreadable_git_boundary_fails_closed(self):
        original = register.Path.lstat
        def unreadable(path, *args, **kwargs):
            if path == self.root / ".git":
                raise PermissionError("synthetic denied metadata")
            return original(path, *args, **kwargs)
        with patch.object(register.Path, "lstat", new=unreadable):
            with self.assertRaisesRegex(register.RegistrationError, "Git 프로젝트 경계"):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertFalse(self.calls)

    def test_user_scope_is_unchanged_by_git_ancestor_and_scope_environment(self):
        (self.root / ".git").mkdir()
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / ".git")}):
            self.assertTrue(register.register_claude(self.config)["changed"])
            self.assertTrue(register.unregister_claude(self.config)["changed"])
            self.assertEqual(os.environ["GIT_DIR"], str(self.root / ".git"))

    def test_local_upgrade_requires_verified_previous_selected_entry(self):
        with self.assertRaisesRegex(register.RegistrationError, "선택한 범위 이전 연결"):
            register.upgrade_claude(self.config, scope="local", project_dir=self.project)
        self.assertFalse(self.calls)

    def test_local_malformed_or_ambiguous_project_profile_fails_closed(self):
        base = self.local_profile()
        malformed = []
        bad_projects = copy.deepcopy(base)
        bad_projects["projects"] = []
        malformed.append(bad_projects)
        for details in (None, [], {"mcpServers": None}):
            profile = copy.deepcopy(base)
            profile["projects"][self.project.as_posix()] = details
            malformed.append(profile)
        duplicate = copy.deepcopy(base)
        duplicate["projects"][self.project.as_posix() + "/."] = {}
        malformed.append(duplicate)
        for profile in malformed:
            with self.subTest(profile=profile):
                self.write_profile(profile)
                for method in (register.registration_status, register.register_claude, register.unregister_claude):
                    with self.assertRaises(register.RegistrationError):
                        method(self.config, scope="local", project_dir=self.project)
                self.assertEqual(self.read_profile(), profile)
        self.assertFalse(self.calls)

    def test_local_detects_profile_changes_during_help_before_mutation(self):
        self.local_profile()
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            profile = self.read_profile()
            profile["projects"][self.app.as_posix()]["allowedTools"] = ["user-edit"]
            self.write_profile(profile)
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(register.SERVER_NAME, self.read_profile()["projects"][self.project.as_posix()]["mcpServers"])

    def test_local_detects_new_project_scope_conflict_during_help(self):
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}), encoding="utf-8")
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read_profile(), self.original)

    def test_local_cli_unrelated_changes_are_reported_without_rollback(self):
        for changed in ("global", "other_project", "project_setting", "project_mcp"):
            with self.subTest(changed=changed):
                self.local_profile()
                self.calls.clear()
                def unrelated(command, **kwargs):
                    result = self.fake_cli(command, **kwargs)
                    if command[-1] != "--help":
                        profile = self.read_profile()
                        if changed == "global":
                            profile["mcpServers"]["other-tool"]["command"] = "changed.exe"
                        elif changed == "other_project":
                            profile["projects"][self.app.as_posix()]["hasTrustDialogAccepted"] = True
                        elif changed == "project_setting":
                            profile["projects"][self.project.as_posix()]["allowedTools"] = ["changed"]
                        else:
                            profile["projects"][self.project.as_posix()]["mcpServers"]["project-other"]["command"] = "changed.exe"
                        self.write_profile(profile)
                    return result
                with patch.object(register.subprocess, "run", side_effect=unrelated):
                    with self.assertRaisesRegex(register.RegistrationError, "자동 복원"):
                        register.register_claude(self.config, scope="local", project_dir=self.project)
                self.assertEqual(len(self.calls), 2)
                self.assertIn(register.SERVER_NAME, self.read_profile()["projects"][self.project.as_posix()]["mcpServers"])

    def test_local_cli_must_write_selected_project_not_user_scope(self):
        def wrong_scope(command, **kwargs):
            if command[-1] == "--help":
                return self.fake_cli(command, **kwargs)
            self.calls.append((command, kwargs))
            profile = self.read_profile()
            profile["mcpServers"][register.SERVER_NAME] = json.loads(command[4])
            self.write_profile(profile)
            return subprocess.CompletedProcess(command, 0, "Saved", "")
        with patch.object(register.subprocess, "run", side_effect=wrong_scope):
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(len(self.calls), 2)

    def test_local_detects_unrelated_additions_even_in_previously_empty_profile(self):
        for changed in ("user_mcp", "other_project", "project_mcp"):
            with self.subTest(changed=changed):
                self.write_profile({})
                self.calls.clear()
                def unrelated(command, **kwargs):
                    result = self.fake_cli(command, **kwargs)
                    if command[-1] != "--help":
                        profile = self.read_profile()
                        if changed == "user_mcp":
                            profile["mcpServers"] = {"unexpected": {"command": "foreign.exe"}}
                        elif changed == "other_project":
                            profile["projects"][self.app.as_posix()] = {"allowedTools": ["unexpected"]}
                        else:
                            profile["projects"][self.project.as_posix()]["mcpServers"]["unexpected"] = {"command": "foreign.exe"}
                        self.write_profile(profile)
                    return result
                with patch.object(register.subprocess, "run", side_effect=unrelated):
                    with self.assertRaisesRegex(register.RegistrationError, "자동 복원"):
                        register.register_claude(self.config, scope="local", project_dir=self.project)
                self.assertEqual(len(self.calls), 2)

    def test_local_unregister_rejects_profile_race_and_reports_unrelated_mutation(self):
        for phase in ("help", "remove"):
            with self.subTest(phase=phase):
                self.local_profile(register.make_server_entry(self.config))
                self.calls.clear()
                def race(command, **kwargs):
                    result = self.fake_cli(command, **kwargs)
                    if (command[-1] == "--help") == (phase == "help"):
                        profile = self.read_profile()
                        profile["model"] = "user-edit-during-remove"
                        self.write_profile(profile)
                    return result
                with patch.object(register.subprocess, "run", side_effect=race):
                    with self.assertRaises(register.RegistrationError):
                        register.unregister_claude(self.config, scope="local", project_dir=self.project)
                self.assertEqual(len(self.calls), 1 if phase == "help" else 2)
                self.assertEqual(register.SERVER_NAME in self.read_profile()["projects"][self.project.as_posix()]["mcpServers"], phase == "help")

    def test_local_cli_support_is_checked_before_mutation(self):
        with patch.object(register.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Usage: <json> --scope user", "")) as run:
            with self.assertRaises(register.RegistrationError):
                register.register_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.read_profile(), self.original)

    def test_cli_local_options_pass_exact_scope_and_project(self):
        for action, method in (("status", "registration_status"), ("register", "register_claude"), ("unregister", "unregister_claude"), ("upgrade", "upgrade_claude")):
            arguments = ["register.py", action, "--config", str(self.config), "--scope", "local", "--project", str(self.project)]
            with self.subTest(action=action), patch.object(sys, "argv", arguments), patch.object(register, method, return_value={"ok": True}) as call, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(register.main(), 0)
                call.assert_called_once_with(self.config, scope="local", project_dir=self.project)

    def test_cli_export_rejects_scope_options_and_local_upgrade_fails(self):
        for action in ("export", "upgrade"):
            arguments = ["register.py", action, "--config", str(self.config), "--scope", "local", "--project", str(self.project)]
            with self.subTest(action=action), patch.object(sys, "argv", arguments), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(register.main(), 1)
        self.assertFalse(self.calls)

    def previous_distribution(self):
        old = self.root / "이전 배포본"
        (old / "runtime").mkdir(parents=True)
        (old / "runtime" / "python.exe").write_bytes(b"previous test python")
        (old / "server.py").write_text("# verified previous server", encoding="utf-8")
        lines = [hashlib.sha256((old / name).read_bytes()).hexdigest() + "  " + name
                 for name in ("runtime/python.exe", "server.py")]
        manifest = ("\n".join(lines) + "\n").encode()
        (old / "SHA256SUMS.txt").write_bytes(manifest)
        known = patch.dict(register.PREVIOUS_MANIFESTS, {hashlib.sha256(manifest).hexdigest(): "0.1.0"})
        known.start()
        self.addCleanup(known.stop)
        entry = register._entry_for_paths(old / "runtime" / "python.exe", old / "server.py", self.config)
        profile = copy.deepcopy(self.original)
        profile["mcpServers"][register.SERVER_NAME] = entry
        self.write_profile(profile)
        return old, entry

    def test_upgrade_requires_known_manifest_exact_entry_and_untouched_files(self):
        old, entry = self.previous_distribution()
        self.assertTrue(register.registration_status(self.config)["can_upgrade"])
        changed = copy.deepcopy(entry)
        changed["env"]["PYTHONPATH"] = "foreign"
        self.assertIsNone(register._previous_entry(changed))
        (old / "server.py").write_text("changed source")
        self.assertFalse(register.registration_status(self.config)["can_upgrade"])
        with self.assertRaises(register.RegistrationError):
            register.upgrade_claude(self.config)
        self.assertFalse(self.calls)

    def test_explicit_upgrade_replaces_only_proven_user_entry(self):
        self.previous_distribution()
        with self.assertRaises(register.RegistrationError):
            register.register_claude(self.config)
        result = register.upgrade_claude(self.config)
        self.assertTrue(result["changed"])
        after = self.read_profile()
        self.assertEqual(after["mcpServers"].pop(register.SERVER_NAME), register.make_server_entry(self.config))
        self.assertEqual(after, self.original)
        self.assertEqual([command[2] for command, _ in self.calls], ["remove", "add-json", "remove", "add-json"])

    def test_local_administrator_upgrade_preserves_other_connections_and_config(self):
        _, previous = self.previous_distribution()
        initial = copy.deepcopy(self.original)
        initial["projects"][self.project.as_posix()] = {"mcpServers": {
            register.SERVER_NAME: previous, "local-other": {"command": "other.exe"}}, "keep": "project metadata"}
        self.write_profile(initial)
        (self.app / "Computer Use MCP 관리자 연결.exe").write_bytes(b"synthetic admin bridge")
        config_before = self.config.read_bytes()
        result = register.upgrade_claude(self.config, scope="local", project_dir=self.project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["scope"], "local")
        after = self.read_profile()
        self.assertEqual(after["projects"][self.project.as_posix()]["mcpServers"][register.SERVER_NAME],
                         register.make_server_entry(self.config))
        after["projects"][self.project.as_posix()]["mcpServers"][register.SERVER_NAME] = previous
        self.assertEqual(after, initial)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertTrue(all(kwargs["cwd"] == self.project for command, kwargs in self.calls if command[-1] != "--help"))

    def test_local_failed_upgrade_restores_only_previous_selected_entry(self):
        _, previous = self.previous_distribution()
        initial = copy.deepcopy(self.original)
        initial["projects"][self.project.as_posix()] = {"mcpServers": {register.SERVER_NAME: previous}}
        self.write_profile(initial)
        def fail_new(command, **kwargs):
            if command[2] == "add-json" and command[-1] != "--help" and json.loads(command[4]) != previous:
                self.calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 1, "", "synthetic failure")
            return self.fake_cli(command, **kwargs)
        with patch.object(register.subprocess, "run", side_effect=fail_new):
            with self.assertRaisesRegex(register.RegistrationError, "복원"):
                register.upgrade_claude(self.config, scope="local", project_dir=self.project)
        self.assertEqual(self.read_profile(), initial)

    def test_failed_upgrade_restores_previous_entry_without_touching_other_settings(self):
        _, previous = self.previous_distribution()
        initial = self.read_profile()
        def fail_new(command, **kwargs):
            if command[2] == "add-json" and command[-1] != "--help" and json.loads(command[4]) != previous:
                self.calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 1, "", "synthetic add failure")
            return self.fake_cli(command, **kwargs)
        with patch.object(register.subprocess, "run", side_effect=fail_new):
            with self.assertRaisesRegex(register.RegistrationError, "복원"):
                register.upgrade_claude(self.config)
        self.assertEqual(self.read_profile(), initial)

    def test_upgrade_preserves_concurrent_foreign_registration(self):
        self.previous_distribution()
        foreign = {"command": "new-foreign-registration.exe"}
        def race(command, **kwargs):
            result = self.fake_cli(command, **kwargs)
            if command[2] == "remove" and command[-1] != "--help":
                profile = self.read_profile()
                profile["mcpServers"][register.SERVER_NAME] = foreign
                self.write_profile(profile)
            return result
        with patch.object(register.subprocess, "run", side_effect=race):
            with self.assertRaises(register.RegistrationError):
                register.upgrade_claude(self.config)
        self.assertEqual(self.read_profile()["mcpServers"][register.SERVER_NAME], foreign)
        self.assertEqual(len(self.calls), 3)

    def test_upgrade_rejects_other_scope_even_for_verified_previous_package(self):
        self.previous_distribution()
        before = self.read_profile()
        before["projects"][str(self.app)] = {"mcpServers": {register.SERVER_NAME: {"command": "foreign.exe"}}}
        self.write_profile(before)
        with self.assertRaises(register.RegistrationError):
            register.upgrade_claude(self.config)
        self.assertEqual(self.read_profile(), before)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
