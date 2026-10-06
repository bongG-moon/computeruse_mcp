"""Registration and token status tests; no UAC prompt or user profile changes."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import privileges
import register


class AdministratorRegistrationTests(unittest.TestCase):
    def test_bundled_bridge_preserves_config_scope_and_sanitized_python_environment(self):
        with tempfile.TemporaryDirectory(prefix="mcp-admin-entry-") as folder:
            root = Path(folder).resolve() / "한글 배포 폴더"
            (root / "runtime").mkdir(parents=True)
            (root / "runtime/python.exe").write_bytes(b"synthetic Python path")
            (root / "server.py").write_text("# synthetic server", encoding="utf-8")
            (root / "Computer Use MCP 관리자 연결.exe").write_bytes(b"synthetic bridge path")
            config = root / "기존 설정 파일.json"
            config.write_text("{}", encoding="utf-8")
            before = config.read_bytes()
            with patch.object(register, "APP_DIR", root):
                entry = register.make_server_entry(config)
            self.assertEqual(entry["type"], "stdio")
            self.assertEqual(entry["command"], str(root / "Computer Use MCP 관리자 연결.exe"))
            self.assertEqual(entry["args"], ["--config", str(config)])
            self.assertEqual(entry["env"]["PYTHONHOME"], str(root / "runtime"))
            self.assertEqual(entry["env"]["PYTHONPATH"], "")
            self.assertEqual(config.read_bytes(), before)

    def test_bare_developer_source_still_uses_normal_python_entry(self):
        with tempfile.TemporaryDirectory(prefix="mcp-source-entry-") as folder:
            root = Path(folder).resolve()
            (root / "runtime").mkdir()
            (root / "runtime/python.exe").write_bytes(b"synthetic Python path")
            (root / "server.py").write_text("# synthetic server", encoding="utf-8")
            with patch.object(register, "APP_DIR", root):
                entry = register.make_server_entry(root / "config.json")
            self.assertEqual(entry["command"], str(root / "runtime/python.exe"))
            self.assertEqual(entry["args"][:2], ["-B", "-s"])

    def test_redirected_administrator_binary_is_not_registered(self):
        with tempfile.TemporaryDirectory(prefix="mcp-linked-bridge-") as folder:
            root = Path(folder).resolve()
            (root / "runtime").mkdir()
            (root / "runtime/python.exe").write_bytes(b"synthetic Python path")
            (root / "server.py").write_text("# synthetic server", encoding="utf-8")
            (root / "Computer Use MCP 관리자 연결.exe").write_bytes(b"synthetic bridge path")
            with patch.object(register, "APP_DIR", root), patch("consent._plain_chain", return_value=False):
                with self.assertRaises(register.RegistrationError):
                    register.make_server_entry(root / "config.json")

    def test_damaged_administrator_bundle_never_silently_registers_normal_python(self):
        with tempfile.TemporaryDirectory(prefix="mcp-incomplete-bundle-") as folder:
            root = Path(folder).resolve()
            (root / "runtime").mkdir()
            (root / "runtime/python.exe").write_bytes(b"synthetic runtime")
            (root / "server.py").write_text("# synthetic server", encoding="utf-8")
            (root / "BUILD-MANIFEST.json").write_text('{"product":"Computer-Use-MCP","version":"0.7.0"}', encoding="utf-8")
            with patch.object(register, "APP_DIR", root):
                with self.assertRaisesRegex(register.RegistrationError, "일반 권한 연결로 대체하지"):
                    register.make_server_entry(root / "config.json")


class PrivilegeStatusTests(unittest.TestCase):
    def test_unavailable_token_does_not_report_administrator(self):
        with patch.object(privileges.os, "name", "nt"), patch.object(privileges.ctypes, "WinDLL", side_effect=OSError("unavailable")):
            value = privileges.execution_privileges()
        self.assertFalse(value["checked"])
        self.assertIsNone(value["administrator"])

    @unittest.skipUnless(os.name == "nt", "Windows token inspection")
    def test_real_current_token_reports_integrity_without_sid_or_profile(self):
        value = privileges.execution_privileges()
        self.assertTrue(value["checked"])
        self.assertIsInstance(value["administrator"], bool)
        self.assertIn(value["integrity"], ("low", "medium", "high", "system"))
        if value["administrator"]:
            self.assertTrue(value["elevated"])
            self.assertIn(value["integrity"], ("high", "system"))
        self.assertNotIn("sid", value)
        self.assertNotIn("profile", value)


if __name__ == "__main__":
    unittest.main()
