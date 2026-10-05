"""Opt-in metadata/registration integration, no model request or screen action.

Uses an OS temporary project OUTSIDE Git and a private CLAUDE_CONFIG_DIR.
Never loads or modifies the operator's Claude profile. Driver must be supplied.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install
import register


@unittest.skipUnless(os.environ.get("COMPANY_COMPUTER_USE_LIVE_SETUP") == "1", "explicit metadata-only live setup test")
class LiveSetupTests(unittest.TestCase):
    def test_real_driver_and_real_cli_in_isolated_profile(self):
        driver = Path(os.environ["COMPANY_COMPUTER_USE_TEST_DRIVER"])
        with tempfile.TemporaryDirectory(prefix="computer-use-live-install-") as temporary:
            root = Path(temporary).resolve()
            profile, project = root / "profile", root / "project"
            profile.mkdir()
            project.mkdir()
            user_file = profile / ".claude.json"
            unrelated = {"type": "stdio", "command": "not-started.exe", "args": []}
            user_file.write_text(json.dumps({"model": "unchanged-test-model", "mcpServers": {"keep-test-connection": unrelated}}), encoding="utf-8")
            config = root / "settings" / "config.json"
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(profile), "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}):
                options = dict(driver=driver, apps=["chrome"], scope="local", project=project)
                prepared = install.prepare(config, **options)
                self.assertEqual(prepared["status"], "confirmation_required", prepared)
                result = install.apply(prepared["plan_path"], prepared["digest"])
                self.assertEqual(result["status"], "registered", result)
                self.assertFalse(result["screen_accessed"])
                actual = json.loads(user_file.read_text(encoding="utf-8-sig"))
                self.assertEqual(actual["model"], "unchanged-test-model")
                self.assertEqual(actual["mcpServers"], {"keep-test-connection": unrelated})
                project_entries = [value for name, value in actual["projects"].items() if Path(name).resolve() == project]
                self.assertEqual(len(project_entries), 1, actual.get("projects"))
                self.assertEqual(project_entries[0]["mcpServers"][register.SERVER_NAME], register.make_server_entry(config))
                self.assertFalse(install.prepare(config, **options)["changed"])
                self.assertFalse(install.apply(prepared["plan_path"], prepared["digest"])["changed"])
                receipt = json.loads((config.parent / "install-receipt.json").read_text(encoding="utf-8"))
                self.assertTrue(any(item["label"] == "MCP 실제 연결" and item["status"] == "ok" for item in receipt["checks"]))
                # Remove only the isolated registration through the product API.
                self.assertEqual(register.unregister_claude(config, scope="local", project_dir=project)["status"], "removed")
                after = json.loads(user_file.read_text(encoding="utf-8-sig"))
                self.assertEqual(after["mcpServers"], {"keep-test-connection": unrelated})
                self.assertEqual(after["model"], "unchanged-test-model")


if __name__ == "__main__":
    unittest.main()
