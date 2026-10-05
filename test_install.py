"""Setup tests never register the real Claude profile or start a real Driver."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import install
REAL_APP_CANDIDATES = install.app_candidates


class ConversationSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="computer-use-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.driver = self.root / "Driver 한글" / "cua-driver.exe"
        self.driver.parent.mkdir()
        self.driver.write_bytes(b"fake driver, never executed")
        self.chrome = self.root / "Chrome 공백" / "chrome.exe"
        self.chrome.parent.mkdir()
        self.chrome.write_bytes(b"fake chrome, never executed")
        self.config = self.root / "설정" / "config.json"
        self.project = self.root / "작업 폴더"
        self.project.mkdir()
        self.entry = {"type": "stdio", "command": "fake-python", "args": ["server.py", "--config", str(self.config)]}
        self.status = {"ok": True, "status": "not_registered", "message": "미등록"}
        self.register_call = Mock(side_effect=self.registered)
        self.diagnostics = Mock(return_value={"ok": True, "checks": [{"label": "연결", "status": "ok"}]})
        patches = [patch.object(install, "driver_candidates", return_value=[]),
                   patch.object(install, "app_candidates", return_value=[str(self.chrome)]),
                   patch.object(install.register, "make_server_entry", return_value=self.entry),
                   patch.object(install.register, "registration_status", side_effect=lambda *a, **kw: dict(self.status)),
                   patch.object(install.register, "register_claude", self.register_call),
                   patch.object(install, "run_diagnostics", self.diagnostics)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def registered(self, *args, **kwargs):
        self.status = {"ok": True, "status": "registered", "message": "등록됨"}
        return dict(self.status)

    def options(self, **updates):
        value = dict(driver=self.driver, apps=["chrome"], scope="local", project=self.project)
        value.update(updates)
        return value

    def plan(self, **updates):
        return install.prepare(self.config, **self.options(**updates))

    def apply(self, plan):
        return install.apply(plan["plan_path"], plan["digest"])

    def test_missing_inputs_are_one_batch_without_writes_or_execution(self):
        result = install.inspect_setup(self.config)
        self.assertEqual({q["field"] for q in result["questions"]}, {"driver", "app", "scope"})
        self.assertFalse(self.config.parent.exists())
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()

    def test_known_values_do_not_repeat_questions_or_enable_other_apps(self):
        result = install.inspect_setup(self.config, **self.options())
        self.assertEqual(result["questions"], [])
        self.assertEqual([p["id"] for p in result["config"]["programs"]], ["chrome"])
        self.assertEqual(result["config"]["approval"], "client")
        self.assertEqual(result["config"]["mode"], "uia")
        self.assertIn("승인 창은 생략", result["summary"]["approval"])
        self.assertIn("승인 설정은 변경하지 않습니다", result["summary"]["approval"])
        self.assertFalse(self.config.exists())

    def test_prepare_creates_only_review_plan(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "confirmation_required")
        self.assertTrue(Path(plan["plan_path"]).is_file())
        self.assertFalse(self.config.exists())
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()

    def test_apply_diagnoses_before_writing_and_registering(self):
        def diagnose(value):
            self.assertFalse(self.config.exists())
            self.register_call.assert_not_called()
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        result = self.apply(self.plan())
        self.assertTrue(result["ok"])
        self.assertEqual(result["scope"], "local")
        self.register_call.assert_called_once_with(str(self.config), scope="local", project_dir=str(self.project))
        self.assertFalse(result["screen_accessed"])
        self.assertTrue((self.config.parent / "install-receipt.json").is_file())

    def test_repeat_apply_and_prepare_do_not_probe_or_register_again(self):
        plan = self.plan()
        self.apply(plan)
        repeated = self.apply(plan)
        prepared = self.plan()
        self.assertFalse(repeated["changed"])
        self.assertFalse(prepared["changed"])
        self.diagnostics.assert_called_once()
        self.register_call.assert_called_once()

    def test_diagnostic_failure_leaves_no_runtime_config_or_registration(self):
        self.diagnostics.return_value = {"ok": False, "checks": [{"status": "error"}]}
        result = self.apply(self.plan())
        self.assertEqual(result["status"], "diagnostics_failed")
        self.assertFalse(self.config.exists())
        self.register_call.assert_not_called()

    def test_wrong_approval_or_changed_plan_never_runs_driver(self):
        plan = self.plan()
        with self.assertRaises(install.SetupError):
            install.apply(plan["plan_path"], "wrong")
        file = Path(plan["plan_path"])
        document = json.loads(file.read_text(encoding="utf-8"))
        document["plan"]["config"]["approval"] = "session"
        file.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(install.SetupError):
            self.apply(plan)
        self.diagnostics.assert_not_called()

    def test_rehashed_nondefault_approval_plan_still_cannot_change_installer_policy(self):
        plan = self.plan()
        file = Path(plan["plan_path"])
        document = json.loads(file.read_text(encoding="utf-8"))
        document["plan"]["config"]["approval"] = "session"
        document["digest"] = install._digest(document["plan"])
        file.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(install.SetupError):
            install.apply(file, document["digest"])
        self.assertFalse(self.config.exists())
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()

    def test_existing_opt_in_approval_settings_are_not_silently_replaced(self):
        base = install.inspect_setup(self.config, **self.options())["config"]
        self.config.parent.mkdir(parents=True)
        for approval in ("session", "each"):
            with self.subTest(approval=approval):
                self.config.write_text(json.dumps(dict(base, approval=approval)), encoding="utf-8")
                original = self.config.read_bytes()
                with self.assertRaises(install.SetupError):
                    self.plan()
                self.assertEqual(self.config.read_bytes(), original)
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()

    def test_changed_driver_is_rejected_without_execution(self):
        plan = self.plan()
        self.driver.write_bytes(b"changed driver")
        with self.assertRaises(install.SetupError):
            self.apply(plan)
        self.diagnostics.assert_not_called()

    def test_driver_changes_during_diagnosis_do_not_register(self):
        def diagnose(value):
            self.driver.write_bytes(b"changed while checking")
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        with self.assertRaises(install.SetupError):
            self.apply(self.plan())
        self.assertFalse(self.config.exists())
        self.register_call.assert_not_called()

    def test_existing_different_settings_preserved(self):
        config = install.inspect_setup(self.config, **self.options())["config"]
        config["max_minutes"] = 20
        install._create(self.config, config)
        before = self.config.read_bytes()
        with self.assertRaises(install.SetupError):
            self.plan()
        self.assertEqual(self.config.read_bytes(), before)

    def test_settings_changed_after_plan_are_preserved(self):
        plan = self.plan()
        config = json.loads(Path(plan["plan_path"]).read_text(encoding="utf-8"))["plan"]["config"]
        config["max_actions"] = 5
        install._create(self.config, config)
        with self.assertRaises(install.SetupError):
            self.apply(plan)
        self.diagnostics.assert_not_called()

    def test_different_existing_registration_not_overwritten(self):
        self.status = {"ok": False, "status": "conflict", "message": "기존 연결 유지"}
        with self.assertRaises(install.SetupError):
            self.plan()
        self.assertFalse(self.config.exists())

    def test_registration_conflict_after_prepare_stops_before_driver(self):
        plan = self.plan()
        self.status = {"ok": False, "status": "conflict", "message": "동시에 연결 변경"}
        with self.assertRaises(install.SetupError):
            self.apply(plan)
        self.diagnostics.assert_not_called()

    def test_registration_failure_preserves_config_and_can_resume(self):
        plan = self.plan()
        self.register_call.side_effect = install.register.RegistrationError("실패")
        with self.assertRaises(install.register.RegistrationError):
            self.apply(plan)
        self.assertTrue(self.config.exists())
        self.assertFalse((self.config.parent / "install-receipt.json").exists())
        self.register_call.side_effect = self.registered
        self.assertTrue(self.apply(plan)["ok"])

    def test_export_creates_only_standalone_connection_file(self):
        plan = self.plan(scope="export", project=None)
        result = self.apply(plan)
        self.assertEqual(result["status"], "exported")
        self.register_call.assert_not_called()
        connection = json.loads(Path(result["connection_file"]).read_text(encoding="utf-8"))
        self.assertEqual(connection["mcpServers"][install.register.SERVER_NAME], self.entry)
        self.assertFalse(self.apply(plan)["changed"])

    def test_local_missing_project_asks_only_for_project(self):
        result = install.inspect_setup(self.config, **self.options(project=None))
        self.assertEqual([q["field"] for q in result["questions"]], ["project"])

    def test_multiple_driver_candidates_require_choice(self):
        with patch.object(install, "driver_candidates", return_value=[str(self.driver), "C:\\other\\cua-driver.exe"]):
            result = install.inspect_setup(self.config, **self.options(driver=None))
        self.assertEqual([q["field"] for q in result["questions"]], ["driver"])

    def test_multiple_app_candidates_do_not_choose_first(self):
        with patch.object(install, "app_candidates", return_value=[str(self.chrome), "C:\\other\\chrome.exe"]):
            result = install.inspect_setup(self.config, **self.options())
        self.assertEqual(result["questions"][0]["field"], "app")
        self.assertEqual(result["programs"], [])

    def test_explicit_app_path_avoids_discovery(self):
        with patch.object(install, "app_candidates") as discovery:
            result = install.inspect_setup(self.config, **self.options(apps=[], app_exes=[self.chrome]))
        discovery.assert_not_called()
        self.assertEqual(result["config"]["programs"][0]["exe"], str(self.chrome))

    def test_chrome_discovery_never_uses_edge(self):
        with patch.object(install, "_app_path", return_value=str(self.chrome)) as registry, patch.object(install, "_candidates", side_effect=lambda values: [str(v) for v in values if v]):
            result = REAL_APP_CANDIDATES("chrome")
        registry.assert_called_once_with("chrome.exe")
        self.assertFalse(any("edge" in v.lower() for v in result))

    def test_shell_program_is_not_allowed(self):
        cmd = self.root / "cmd.exe"
        cmd.write_bytes(b"fake shell")
        with self.assertRaises(ValueError):
            install.inspect_setup(self.config, **self.options(apps=[], app_exes=[cmd]))

    def test_reserved_settings_names_and_relative_paths_rejected(self):
        for name in (".claude.json", "settings.json", ".mcp.json", "install-receipt.json"):
            with self.subTest(name=name), self.assertRaises(install.SetupError):
                install.inspect_setup(self.root / name)
        with self.assertRaises(install.SetupError):
            install.inspect_setup("relative/config.json")

    def test_non_driver_file_rejected(self):
        with self.assertRaises(install.SetupError):
            install.inspect_setup(self.config, **self.options(driver=self.chrome))

    def test_cli_missing_information_returns_korean_questions_without_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = install.main(["inspect", "--config", str(self.config)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "input_required")
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
