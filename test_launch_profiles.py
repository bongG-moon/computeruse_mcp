"""Saved launch profiles; all URI dispatch is injected and never leaves this PC."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import install
import programs
from program_launch import (create_program, validate_launch_profile, UriLaunchRequest, LaunchObservation)
from settings import load_config, validate_config
from test_program_launch import Clock


class LaunchProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launch-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.exe = self.root / "BusinessEditor.exe"
        self.child = self.root / "BusinessWindow.exe"
        self.driver = self.root / "cua-driver.exe"
        for path in (self.exe, self.child, self.driver):
            path.write_bytes(b"fake executable, never run")
        self.config_path = self.root / "settings" / "config.json"
        self.config_path.parent.mkdir()
        self.config = {"version": 1, "driver": str(self.driver), "mode": "uia", "approval": "client",
                       "max_minutes": 10, "max_actions": 120, "state_dir": str(self.config_path.parent),
                       "programs": []}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def app(self, launch=None):
        value = {"id": "editor", "name": "업무 편집기", "exe": str(self.exe), "enabled": True,
                 "control_exes": [str(self.child)], "hints": "시험 문서"}
        if launch is not None:
            value["launch"] = launch
        return value

    def test_legacy_config_does_not_gain_launch_fields(self):
        original = {**self.config, "programs": [self.app()]}
        checked = validate_config(original)
        self.assertNotIn("launch", checked["programs"][0])
        self.assertEqual(checked["programs"], original["programs"])

    def test_exe_arguments_keep_boundaries_without_a_command_shell(self):
        args = ["--from-shortcut", "value with spaces", "name&other", "$(not-a-command)"]
        profile = {"kind": "exe", "arguments": args, "cwd": str(self.root)}
        factory = Mock()
        create_program(self.app(profile), factory)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], [str(self.exe), *args])
        self.assertFalse(factory.call_args.kwargs["shell"])
        self.assertEqual(factory.call_args.kwargs["cwd"], str(self.root))

    def test_missing_explicit_working_directory_does_not_fallback(self):
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "시작 폴더"):
            create_program(self.app({"kind": "exe", "cwd": str(self.root / "missing")}), factory)
        factory.assert_not_called()

    def test_http_and_registered_protocol_dispatch_exactly_once(self):
        for target in ("https://example.test/Updater.application?system=A&menu=B",
                       "http://example.test/Updater.application", "example-desk://workspace/menu?id=12"):
            with self.subTest(target=target):
                dispatch, factory = Mock(), Mock()
                result = create_program(self.app({"kind": "uri", "target": target}), factory, uri_launcher=dispatch)
                dispatch.assert_called_once_with(target, "open")
                factory.assert_not_called()
                self.assertIsNone(result.pid)
                self.assertIsNone(result.poll())

    def test_uri_dispatch_error_is_not_retried_or_transformed(self):
        dispatch = Mock(side_effect=OSError("protocol not installed"))
        with self.assertRaises(OSError):
            create_program(self.app({"kind": "uri", "target": "example-desk://work/menu"}), uri_launcher=dispatch)
        dispatch.assert_called_once()

    def test_malformed_dangerous_or_credential_uri_never_dispatches(self):
        targets = ["file://local/app.exe", "shell://AppsFolder", "ms-settings://privacy",
                   "ms-excel://ofv/u/https://example.test/a.xlsx", "cmd://anything", "javascript://code",
                   "data://anything", "https://user:secret@example.test/", "https://user@example.test/",
                   "https://example.test:bad/", "https://[bad/", "https://example.test/a\nrun",
                   "https://example.test/a%0d%0arun", "https://example.test/%00", "https://example.test/%22x",
                   'https://example.test/"x', "https://example.test/\\x", "https://example.test/%5cx",
                   "https://example.test/a b", " example-app://work/menu", "relative/path", "example-app:menu",
                   "//example.test/menu", "example-app:///menu", "C://Windows/app.exe"]
        for target in targets:
            with self.subTest(target=target):
                dispatch = Mock()
                with self.assertRaises(ValueError):
                    create_program(self.app({"kind": "uri", "target": target}), uri_launcher=dispatch)
                dispatch.assert_not_called()

    def test_invalid_profile_shapes_are_rejected(self):
        values = [None, {}, {"kind": "shell"}, {"kind": "uri", "target": "https://example.test", "arguments": []},
                  {"kind": "exe", "command": "a b"}, {"kind": "exe", "arguments": "--flag"},
                  {"kind": "exe", "arguments": [True]}, {"kind": "exe", "arguments": ["bad\0arg"]},
                  {"kind": "exe", "arguments": ["a"] * 33}, {"kind": "exe", "cwd": "relative"},
                  {"kind": "exe", "cwd": r"\\server\share"}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_launch_profile(value)

    def test_uri_existing_window_is_not_fresh_launch_evidence(self):
        clock = Clock()
        row = {"pid": 13, "window_id": 15, "exe": str(self.exe)}
        probe = LaunchObservation([str(self.exe)], lambda: {"structuredContent": {"windows": [row]}},
                                  clock=clock, wait=clock.wait)
        probe.capture()
        result = probe.finish(UriLaunchRequest(), timeout_seconds=.3)
        self.assertEqual(result["launch_status"], "dispatch_unverified")
        self.assertIsNone(result["process_started"])
        self.assertIsNone(result["process_running"])
        self.assertFalse(result["window_verified"])
        self.assertFalse(result["application_ready_verified"])
        self.assertFalse(result["automatic_retry"])

    def test_uri_new_child_window_is_candidate_not_verified_handoff(self):
        clock = Clock()
        after = {"pid": 13, "window_id": 15, "exe": str(self.child)}
        observe = Mock(side_effect=[{"structuredContent": {"windows": []}}] +
                                  [{"structuredContent": {"windows": [after]}}] * 10)
        probe = LaunchObservation([str(self.exe), str(self.child)], observe, clock=clock, wait=clock.wait)
        probe.capture()
        result = probe.finish(UriLaunchRequest())
        self.assertEqual(result["launch_status"], "handoff_unverified")
        self.assertTrue(result["window_verified"])
        self.assertFalse(result["handoff_verified"])
        self.assertFalse(result["task_verified"])

    def test_program_registration_preserves_profile_and_separates_menu_targets(self):
        for menu in ("one", "two"):
            options = {"exe": str(self.exe), "name": "업무 " + menu,
                       "launch": {"kind": "uri", "target": "example-desk://workspace/" + menu},
                       "control_exes": [str(self.child)]}
            preview = programs.preview_add(self.config_path, **options)
            result = programs.add_program(self.config_path, expected_config_sha256=preview["expected_config_sha256"], **options)
            self.assertEqual(result["program"]["launch"], options["launch"])
            self.assertEqual(programs.preview_add(self.config_path, **options)["status"], "already_present")
        saved = load_config(self.config_path)
        self.assertEqual(len(saved["programs"]), 2)
        self.assertNotEqual(saved["programs"][0]["id"], saved["programs"][1]["id"])

    def test_registration_cli_receives_separate_arguments(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = programs.main(["preview-add", "--config", str(self.config_path), "--exe", str(self.exe),
                                  "--name", "시험 편집기", "--argument=--from-shortcut", "--argument=value with spaces",
                                  "--working-directory", str(self.root)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["program"]["launch"],
                         {"kind": "exe", "arguments": ["--from-shortcut", "value with spaces"], "cwd": str(self.root)})

    def test_prepare_uri_and_handoff_executable_apply_without_launch(self):
        self.config_path.unlink()
        launch = Mock(side_effect=AssertionError("Installation must not launch business app"))
        with patch("program_launch.os.startfile", launch), patch.object(install, "run_diagnostics", return_value={"ok": True, "checks": []}):
            result = install.prepare(self.config_path, driver=self.driver, app_exes=[self.exe], scope="export",
                                     app_launch_uri="example-desk://workspace/menu", app_control_exes=[self.child])
            applied = install.apply(result["plan_path"], result["digest"])
        self.assertTrue(applied["ok"])
        saved = load_config(self.config_path)["programs"][0]
        self.assertEqual(saved["launch"], {"kind": "uri", "target": "example-desk://workspace/menu"})
        self.assertEqual(saved["control_exes"], [str(self.child)])
        launch.assert_not_called()

    def test_prepare_profile_is_bound_to_exactly_one_explicit_app(self):
        for exe_list in ([], [self.exe, self.child]):
            with self.subTest(exe_list=exe_list), self.assertRaises(install.SetupError):
                install.inspect_setup(self.config_path, driver=self.driver, app_exes=exe_list, scope="export",
                                      app_launch_uri="example-desk://workspace/menu")

    def test_changing_saved_uri_invalidates_prepared_plan_digest(self):
        self.config_path.unlink()
        result = install.prepare(self.config_path, driver=self.driver, app_exes=[self.exe], scope="export",
                                 app_launch_uri="example-desk://workspace/one")
        plan_path = Path(result["plan_path"])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["plan"]["config"]["programs"][0]["launch"]["target"] = "example-desk://workspace/two"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(install.SetupError):
            install.apply(plan_path, result["digest"])
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
