import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import diagnostics


def config(root, driver):
    return {"version": 1, "driver": str(driver), "programs": [], "mode": "uia", "approval": "session",
            "max_minutes": 10, "max_actions": 120, "state_dir": str(root), "log_detail": "metadata"}


class SignatureTests(unittest.TestCase):
    def test_offline_trust_is_distinct_from_damaged_or_unsigned(self):
        self.assertEqual(diagnostics.classify_signature(0)["status"], "ok")
        for code in (0x80096010, 0x800B0100, 0x800B010C):
            self.assertEqual(diagnostics.classify_signature(code)["status"], "error")
        for code in (0x800B0109, 0x800B010A, 0x80092013):
            result = diagnostics.classify_signature(code)
            self.assertEqual(result["status"], "warning")
            self.assertIn("손상으로 판정", result["detail"])
        self.assertEqual(diagnostics.classify_signature(0x800B0001)["status"], "unsupported")

    def test_readonly_client_rejects_every_screen_or_model_request(self):
        client = object.__new__(diagnostics.ReadOnlyMCP)
        for method, args in (("tools/call", {"name": "computer_begin", "arguments": {}}),
                             ("tools/call", {"name": "click", "arguments": {}}),
                             ("tools/call", {"name": "computer_status", "arguments": {"extra": True}}),
                             ("sampling/createMessage", {}), ("tools/call", {"name": "computer_save_task", "arguments": {}})):
            with self.subTest(method=method, args=args), self.assertRaises(ValueError):
                client.request(method, args)


class ProbeTests(unittest.TestCase):
    def test_probe_only_initializes_lists_and_reads_status_in_temporary_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = config(root / "real-state", r"C:\Tools\cua-driver.exe")
            original_copy = copy.deepcopy(original)
            requests = []
            saved = []

            def entry(path):
                current = json.loads(Path(path).read_text(encoding="utf-8"))
                saved.append(current)
                return {"command": "test-runtime", "args": [str(path)]}

            class Client:
                closed = False
                def __init__(self, _entry):
                    pass
                def request(self, method, params=None):
                    requests.append((method, params))
                    if method == "initialize":
                        return {"serverInfo": {"name": "company-computer-use", "version": "0.2.0"}}
                    if method == "tools/list":
                        return {"tools": [{"name": name} for name in ("computer_status", "computer_begin", "computer_stop", "get_window_state")]}
                    return {"structuredContent": {"session": None, "driver_schema_error": ""}}
                def initialized(self):
                    requests.append(("notifications/initialized", None))
                def close(self):
                    Client.closed = True

            with patch.object(diagnostics, "make_server_entry", side_effect=entry), patch.object(diagnostics, "ReadOnlyMCP", Client):
                result = diagnostics.probe_connection(original)
            self.assertTrue(result["ok"])
            self.assertEqual([method for method, _ in requests], ["initialize", "notifications/initialized", "tools/list", "tools/call"])
            self.assertEqual(requests[-1][1], {"name": "computer_status", "arguments": {}})
            self.assertEqual(original, original_copy)
            self.assertNotEqual(saved[0]["state_dir"], original["state_dir"])
            self.assertFalse(Path(saved[0]["state_dir"]).exists())
            self.assertTrue(Client.closed)

    def test_missing_driver_does_not_launch_process_and_gives_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(Path(tmp), "")
            with patch.object(diagnostics, "native_claude", return_value=""), patch.object(diagnostics, "probe_connection") as probe, patch.object(diagnostics.subprocess, "run") as run:
                report = diagnostics.run_diagnostics(value)
            self.assertFalse(report["ok"])
            probe.assert_not_called()
            run.assert_not_called()
            self.assertIn("Driver 파일 선택", diagnostics.format_diagnostics(report))
            self.assertIn("Claude Code를 찾지 못했습니다", diagnostics.format_diagnostics(report))

    def test_invalid_signature_does_not_execute_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cua-driver.exe"
            path.write_bytes(b"test file")
            metadata = {"signature": diagnostics.classify_signature(0x80096010), "sha256": "hash", "signer": "", "file_version": ""}
            with patch.object(diagnostics, "file_metadata", return_value=metadata), patch.object(diagnostics, "native_claude", return_value=""), patch.object(diagnostics.subprocess, "run") as run:
                report = diagnostics.run_diagnostics(config(root, path))
            run.assert_not_called()
            self.assertFalse(report["ok"])

    def test_offline_warning_retains_connection_results_without_claiming_ui_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cua-driver.exe"
            path.write_bytes(b"test file")
            metadata = {"signature": diagnostics.classify_signature(0x800B0109), "sha256": "hash", "signer": "Example publisher", "file_version": "0.28.2"}
            connection = {"ok": True, "tool_count": 25, "schema_error": "", "screen_session_started": False}
            version = types.SimpleNamespace(returncode=0, stdout="cua-driver 0.28.2", stderr="")
            with patch.object(diagnostics, "file_metadata", return_value=metadata), patch.object(diagnostics, "native_claude", return_value=""), patch.object(diagnostics.subprocess, "run", return_value=version), patch.object(diagnostics, "probe_connection", return_value=connection):
                report = diagnostics.run_diagnostics(config(root, path))
            self.assertTrue(report["ok"])
            self.assertTrue(any(c["status"] == "warning" for c in report["checks"]))
            self.assertIn("화면 조작·모델 연결", report["scope"])
            self.assertFalse(report["connection"]["screen_session_started"])


if __name__ == "__main__":
    unittest.main()
