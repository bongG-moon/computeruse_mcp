"""Read-only configuration visibility with isolated files, never user profiles."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configuration_state as state


class ConfigurationStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="computer-config-status-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "config.json"
        self.source = self.root / "settings.py"
        self.source.write_text('VERSION = "0.3.3"\n', encoding="utf-8")
        for context in (patch.object(state, "SETTINGS_PATH", self.source),
                        patch.object(state, "LOADED_VERSION", "0.3.3")):
            context.start()
            self.addCleanup(context.stop)
        self.config = {"version": 1, "driver": "", "programs": [], "mode": "uia",
                       "approval": "client", "log_detail": "metadata", "max_minutes": 10,
                       "max_actions": 120, "approval_timeout_seconds": 300,
                       "state_dir": str(self.root), "private_note": "PRIVATE-TEST-VALUE"}
        self.write(self.config)

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(self):
        before = copy.deepcopy(self.config)
        files = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        answer = state.configuration_status(self.config, self.path)
        self.assertEqual(self.config, before)
        self.assertEqual(files, {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertNotIn("PRIVATE-TEST-VALUE", json.dumps(answer))
        return answer

    def test_valid_config_has_only_fingerprints_and_version_metadata(self):
        value = self.check()
        self.assertEqual(value["source_state"], "current")
        self.assertEqual(value["package_state"], "current")
        self.assertFalse(value["restart_required"])
        self.assertRegex(value["loaded_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(value["loaded_fingerprint"], value["disk_fingerprint"])
        self.assertNotIn("driver", value)
        self.assertNotIn("programs", value)

    def test_formatting_key_order_bom_and_defaulted_fields_do_not_require_restart(self):
        changed = {key: self.config[key] for key in reversed(self.config)}
        changed.pop("log_detail")
        self.path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8-sig")
        value = self.check()
        self.assertEqual(value["source_state"], "current")
        self.assertFalse(value["restart_required"])

    def test_changed_permissions_are_reported_without_applying_them(self):
        changed = {**self.config, "approval": "session", "max_actions": 500}
        self.write(changed)
        value = self.check()
        self.assertEqual(value["source_state"], "changed")
        self.assertTrue(value["restart_required"])
        self.assertNotEqual(value["loaded_fingerprint"], value["disk_fingerprint"])
        self.assertEqual(self.config["approval"], "client")
        self.assertEqual(self.config["max_actions"], 120)

    def test_invalid_json_and_invalid_schema_do_not_expose_contents(self):
        for text in ('{"PRIVATE-TEST-VALUE":', json.dumps({**self.config, "approval": "PRIVATE-TEST-VALUE"})):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                value = self.check()
                self.assertEqual(value["source_state"], "invalid")
                self.assertTrue(value["restart_required"])
                self.assertIsNone(value["disk_fingerprint"])

    def test_deleted_config_requires_repair_without_recreating_it(self):
        self.path.unlink()
        value = self.check()
        self.assertEqual(value["source_state"], "missing")
        self.assertTrue(value["restart_required"])
        self.assertIn("복구", value["message"])
        self.assertFalse(self.path.exists())

    def test_unspecified_path_never_guesses_a_default(self):
        value = state.configuration_status(self.config)
        self.assertEqual(value["source_state"], "untracked")
        self.assertFalse(value["restart_required"])
        self.assertIsNone(value["disk_fingerprint"])

    def test_nonlocal_relative_and_directory_paths_are_unavailable(self):
        for path in (Path("relative.json"), r"\\example.invalid\share\config.json", self.root):
            with self.subTest(path=path):
                value = state.configuration_status(self.config, path)
                self.assertEqual(value["source_state"], "unavailable")
                self.assertTrue(value["restart_required"])

    def test_oversized_configuration_is_rejected(self):
        self.path.write_bytes(b" " * (state.MAX_CONFIG_BYTES + 1))
        self.assertEqual(self.check()["source_state"], "invalid")

    def test_changed_package_version_reports_loaded_and_disk_versions(self):
        self.source.write_text('VERSION: str = "0.4.0"\n', encoding="utf-8")
        value = self.check()
        self.assertEqual(value["source_state"], "current")
        self.assertEqual(value["loaded_version"], "0.3.3")
        self.assertEqual(value["package_version"], "0.4.0")
        self.assertEqual(value["package_state"], "changed")
        self.assertTrue(value["restart_required"])

    def test_package_source_is_parsed_but_never_executed(self):
        marker = self.root / "must-not-exist"
        self.source.write_text('VERSION = "0.3.3"\nfrom pathlib import Path\n'
                               f'Path({str(marker)!r}).write_text("executed")\n', encoding="utf-8")
        value = self.check()
        self.assertEqual(value["package_state"], "current")
        self.assertFalse(marker.exists())

    def test_dynamic_duplicate_or_malformed_versions_are_not_evaluated(self):
        for text in ('VERSION = str("0.3.3")', 'VERSION = "0.3.3"\nVERSION = "0.3.4"',
                     'VERSION = "PRIVATE-TEST-VALUE"', 'VERSION = '):
            with self.subTest(text=text):
                self.source.write_text(text, encoding="utf-8")
                value = self.check()
                self.assertEqual(value["package_state"], "invalid")
                self.assertIsNone(value["package_version"])
                self.assertTrue(value["restart_required"])

    def test_missing_package_source_requires_repair(self):
        self.source.unlink()
        value = self.check()
        self.assertEqual(value["package_state"], "missing")
        self.assertTrue(value["restart_required"])

    def test_unreadable_source_does_not_leak_exception_text(self):
        original = state._read_local_file
        def denied(path, maximum):
            if path == self.path:
                raise PermissionError("PRIVATE-TEST-VALUE")
            return original(path, maximum)
        with patch.object(state, "_read_local_file", side_effect=denied):
            value = self.check()
        self.assertEqual(value["source_state"], "unavailable")
        self.assertTrue(value["restart_required"])


if __name__ == "__main__":
    unittest.main()
