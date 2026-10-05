"""Boundary regressions: fake executables, temporary state, no live registration."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import install
import test_install as fixtures


class ConversationSetupBoundaryTests(unittest.TestCase):
    # Reuse the isolated fixtures without inheriting/discovering the base tests.
    setUp = fixtures.ConversationSetupTests.setUp
    registered = fixtures.ConversationSetupTests.registered
    options = fixtures.ConversationSetupTests.options
    plan = fixtures.ConversationSetupTests.plan
    apply = fixtures.ConversationSetupTests.apply

    def assert_apply_rejected(self, plan):
        try:
            result = self.apply(plan)
        except (install.SetupError, install.register.RegistrationError):
            pass
        else:
            self.assertFalse(result.get("ok"), result)
        self.assertFalse((self.config.parent / "install-receipt.json").exists())

    def test_reparse_driver_created_during_diagnostics_blocks_save_and_registration(self):
        self.check_post_diagnostics_reparse(self.driver)

    def test_reparse_app_created_during_diagnostics_blocks_save_and_registration(self):
        self.check_post_diagnostics_reparse(self.chrome)

    def test_reparse_project_created_during_diagnostics_blocks_save_and_registration(self):
        self.check_post_diagnostics_reparse(self.project)

    def test_reparse_config_parent_created_during_diagnostics_blocks_save_and_registration(self):
        self.check_post_diagnostics_reparse(self.config.parent)

    def check_post_diagnostics_reparse(self, target):
        original = install._plain_chain
        changed = False
        def chain(path, **kwargs):
            path = Path(path)
            if changed and (path == target or target in path.parents):
                return False
            return original(path, **kwargs)
        def diagnose(value):
            nonlocal changed
            changed = True
            return {"ok": True, "checks": []}
        plan = self.plan()
        self.diagnostics.side_effect = diagnose
        with patch.object(install, "_plain_chain", side_effect=chain):
            self.assert_apply_rejected(plan)
        self.diagnostics.assert_called_once()
        self.register_call.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_app_removed_during_diagnostics_is_not_registered(self):
        plan = self.plan()
        def diagnose(value):
            self.chrome.unlink()
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        self.assert_apply_rejected(plan)
        self.register_call.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_project_removed_during_diagnostics_is_not_registered(self):
        plan = self.plan()
        def diagnose(value):
            self.project.rmdir()
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        self.assert_apply_rejected(plan)
        self.register_call.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_config_created_by_someone_else_during_diagnostics_is_preserved(self):
        plan = self.plan()
        foreign = {"foreign": "do not overwrite"}
        def diagnose(value):
            self.config.write_text(json.dumps(foreign), encoding="utf-8")
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        self.assert_apply_rejected(plan)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), foreign)
        self.register_call.assert_not_called()

    def test_failed_registration_never_writes_success_receipt(self):
        plan = self.plan()
        self.register_call.side_effect = None
        self.register_call.return_value = {"ok": False, "status": "registered", "changed": False}
        with patch.object(install, "_connection_present", return_value=True):
            self.assert_apply_rejected(plan)
        self.register_call.assert_called_once()

    def test_scope_conflict_registration_never_writes_success_receipt(self):
        plan = self.plan()
        self.register_call.side_effect = None
        self.register_call.return_value = {"ok": False, "status": "scope_conflict", "changed": True}
        with patch.object(install, "_connection_present", return_value=True):
            self.assert_apply_rejected(plan)
        self.register_call.assert_called_once()

    def test_wrong_registration_status_never_writes_success_receipt(self):
        plan = self.plan()
        self.register_call.side_effect = None
        self.register_call.return_value = {"ok": True, "status": "exported", "changed": True}
        with patch.object(install, "_connection_present", return_value=True):
            self.assert_apply_rejected(plan)

    def test_success_response_without_persisted_connection_has_no_success_receipt(self):
        plan = self.plan()
        self.register_call.side_effect = None
        self.register_call.return_value = {"ok": True, "status": "registered", "changed": True}
        # The independent status probe deliberately remains not_registered.
        self.assert_apply_rejected(plan)
        self.register_call.assert_called_once()

    def test_receipt_version_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("version", "unrelated-version")

    def test_receipt_entry_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("entry", {"command": "unrelated.exe", "args": []})

    def test_receipt_not_ok_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("ok", False)

    def test_receipt_scope_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("scope", "user")

    def test_receipt_project_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("project", str(self.root))

    def test_receipt_bundle_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("bundle", str(self.root))

    def test_receipt_driver_hash_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("driver_sha256", "0" * 64)

    def test_receipt_code_hash_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("code_sha256", "0" * 64)

    def test_receipt_config_path_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("config_path", str(self.root / "different-config.json"))

    def test_receipt_status_change_rejects_prepare_and_apply_without_rerunning(self):
        self.check_changed_receipt("status", "scope_conflict")

    def check_changed_receipt(self, key, value):
        plan = self.plan()
        self.assertTrue(self.apply(plan)["ok"])
        receipt_path = self.config.parent / "install-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt[key] = value
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        before = receipt_path.read_bytes()
        self.diagnostics.reset_mock()
        self.register_call.reset_mock()
        for operation in (self.plan, lambda: self.apply(plan)):
            with self.subTest(operation=operation):
                try:
                    result = operation()
                except (install.SetupError, install.register.RegistrationError):
                    pass
                else:
                    self.assertFalse(result.get("ok"), result)
                self.assertEqual(receipt_path.read_bytes(), before)
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()

    def copied_bundle(self):
        bundle = self.root / "임시 배포본"
        files = ("install.py", "register.py", "settings.py", "diagnostics.py", "server.py",
                 "consent.py", "vendor/guard.py", "vendor/windows.py")
        for name in files:
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(install.APP_DIR / name, target)
        return bundle

    def test_changed_bundle_code_after_prepare_blocks_diagnostics(self):
        bundle = self.copied_bundle()
        with patch.object(install, "APP_DIR", bundle):
            plan = self.plan()
            target = bundle / "register.py"
            target.write_bytes(target.read_bytes() + b"\n# changed temporary fixture\n")
            self.assert_apply_rejected(plan)
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_changed_bundle_code_during_diagnostics_blocks_registration(self):
        bundle = self.copied_bundle()
        def diagnose(value):
            target = bundle / "vendor" / "guard.py"
            target.write_bytes(target.read_bytes() + b"\n# changed temporary fixture\n")
            return {"ok": True, "checks": []}
        self.diagnostics.side_effect = diagnose
        with patch.object(install, "APP_DIR", bundle):
            self.assert_apply_rejected(self.plan())
        self.register_call.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_bundle_code_change_prevents_reusing_completed_receipt(self):
        bundle = self.copied_bundle()
        with patch.object(install, "APP_DIR", bundle):
            plan = self.plan()
            self.assertTrue(self.apply(plan)["ok"])
            receipt_path = self.config.parent / "install-receipt.json"
            receipt_before = receipt_path.read_bytes()
            target = bundle / "settings.py"
            target.write_bytes(target.read_bytes() + b"\n# changed temporary fixture\n")
            self.diagnostics.reset_mock()
            self.register_call.reset_mock()
            with self.assertRaises(install.SetupError):
                self.plan()
            with self.assertRaises(install.SetupError):
                self.apply(plan)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
        self.diagnostics.assert_not_called()
        self.register_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
