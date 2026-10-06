import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from settings import load_config, save_config, validate_config, stop_runs, default_config, VERSION


class SettingsTests(unittest.TestCase):
    def config(self, root):
        return {"version": 1, "driver": "C:\\Tools\\cua-driver.exe", "programs": [
            {"id": "notepad", "name": "메모장", "exe": "C:\\Windows\\notepad.exe", "enabled": True}],
            "mode": "uia", "approval": "session", "max_minutes": 10, "max_actions": 120, "state_dir": str(root)}

    def test_roundtrip_and_broken_original_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(path, self.config(Path(tmp)))
            self.assertEqual(load_config(path)["programs"][0]["name"], "메모장")
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
            self.assertEqual(path.read_text(), "broken")

    def test_duplicates_commands_network_targets_and_bad_limits_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.config(Path(tmp))
            cases = []
            duplicate = copy.deepcopy(base)
            duplicate["programs"] *= 2
            cases.append(duplicate)
            for field, value in (("exe", "C:\\Windows\\System32\\cmd.exe"), ("exe", "\\\\server\\app.exe"), ("args", ["run"])):
                config = copy.deepcopy(base)
                config["programs"][0][field] = value
                cases.append(config)
            for key, value in (("max_minutes", 31), ("max_actions", True), ("approval", "allow_all")):
                config = copy.deepcopy(base)
                config[key] = value
                cases.append(config)
            for config in cases:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    validate_config(config)

    def test_stop_only_this_config_run_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs/one").mkdir(parents=True)
            (root / "elsewhere").mkdir()
            self.assertEqual(stop_runs(self.config(root)), 1)
            self.assertTrue((root / "runs/one/stop.flag").exists())
            self.assertFalse((root / "elsewhere/stop.flag").exists())

    def test_metadata_default_migrates_existing_config_without_changing_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = self.config(Path(tmp))
            original["custom_note"] = {"preserve": True}
            checked = validate_config(original)
            self.assertEqual(checked["log_detail"], "metadata")
            self.assertNotIn("log_detail", original)
            self.assertEqual(checked["custom_note"], original["custom_note"])
            path = Path(tmp) / "another-config.json"
            save_config(path, original)
            self.assertEqual(load_config(path)["state_dir"], str(Path(tmp)))
            self.assertEqual(load_config(path)["log_detail"], "metadata")

    def test_explicit_content_setting_roundtrips_and_bad_value_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = self.config(Path(tmp))
            value["log_detail"] = "content"
            path = Path(tmp) / "config.json"
            save_config(path, value)
            self.assertEqual(load_config(path)["log_detail"], "content")
            for invalid in ("all", True, [], None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    validate_config(dict(value, log_detail=invalid))

    def test_new_default_is_simple_and_notepad_hints_use_save_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("settings.discover", return_value={"apps": [{"id": "notepad", "name": "메모장", "exe": "C:\\Windows\\notepad.exe", "available": True}]}):
                value = default_config(Path(tmp) / "config.json")
            self.assertEqual((value["mode"], value["approval"], value["log_detail"]), ("uia", "client", "metadata"))
            self.assertIn("파일 → 저장", value["programs"][0]["hints"])
        self.assertEqual(VERSION, "0.8.0")

    def test_loading_saved_approval_choices_does_not_migrate_or_rewrite_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            for approval in ("session", "each", "client"):
                with self.subTest(approval=approval):
                    path = Path(tmp) / (approval + ".json")
                    saved = dict(self.config(Path(tmp)), approval=approval)
                    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
                    original = path.read_bytes()
                    self.assertEqual(load_config(path)["approval"], approval)
                    self.assertEqual(path.read_bytes(), original)

    def test_new_default_runs_without_native_consent_and_keeps_action_guards(self):
        # Inject every desktop dependency; this never opens a window or Driver.
        from session_runtime import SessionRuntime
        import test_server as fixtures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            editor, driver = root / "Editor.exe", root / "cua-driver.exe"
            editor.touch()
            driver.touch()
            with patch("settings.discover", return_value={"apps": [
                    {"id": "editor", "name": "시험 편집기", "exe": str(editor), "available": True}]}):
                config = default_config(root / "config.json")
            config["driver"] = str(driver)
            broker = fixtures.FakeBroker(approved=False)
            broker.confirm = Mock(side_effect=AssertionError("Native consent must not be requested"))
            lease = fixtures.FakeLease()
            runtime = SessionRuntime(config, config["programs"], "uia", {"instructions": "가짜 내용", "expected": "확인"},
                broker_factory=lambda *args: broker, lease_factory=lambda: lease,
                emergency_factory=fixtures.FakeEmergency, transport_factory=fixtures.FakeTransport,
                guard_factory=fixtures.TestGuard, registry_register=lambda folder: None,
                registry_unregister=lambda folder: None)
            try:
                self.assertEqual(runtime.start()["approval"], "client")
                self.assertEqual(runtime.minutes, 10)
                self.assertEqual(runtime.max_actions, 120)
                self.assertEqual(len(runtime.transport.policy["allowed_apps"]), 1)
                self.assertEqual(runtime.transport.policy["allowed_apps"][0].casefold(), str(editor).casefold())
                action = {"pid": 100, "window_id": 200, "key": "tab"}
                self.assertTrue(runtime.call("press_key", action)["isError"])
                runtime.call("get_window_state", {"pid": 100, "window_id": 200})
                self.assertFalse(runtime.call("press_key", action).get("isError", False))
                self.assertEqual(runtime.guard.action_count, 1)
                broker.confirm.assert_not_called()
            finally:
                runtime.stop()
            self.assertFalse(lease.held)


if __name__ == "__main__":
    unittest.main()
