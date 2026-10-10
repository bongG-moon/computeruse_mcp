import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
import io
import subprocess
import sys
from unittest.mock import patch, Mock

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
            from teaching_support import TEACHING_HELPER_FILES, teaching_capabilities
            for name in TEACHING_HELPER_FILES:
                (root/name).write_bytes(b"test placeholder, never executed")
            support = teaching_capabilities(root)
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
                        return {"serverInfo": {"name": "company-computer-use", "version": diagnostics.VERSION}}
                    if method == "tools/list":
                        return {"tools": [{"name": name} for name in ("computer_status", "computer_begin", "computer_stop", "get_window_state", "computer_teach_element", "computer_teach_status", "computer_process_editor", "computer_process_status")]}
                    return {"structuredContent": {"session": None, "driver_schema_error": "", "version": diagnostics.VERSION, "teaching_support": support}}
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

    def test_legacy_connection_is_not_accepted_for_current_teaching(self):
        issues = diagnostics.connection_issues({"serverInfo": {"name": "company-computer-use", "version": "0.2.0"}},
            {"computer_status", "computer_begin", "computer_stop", "get_window_state"}, {"version": "0.2.0"})
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("computer_teach_status" in message for message in issues))

    def test_matching_version_does_not_hide_missing_native_helpers(self):
        from teaching_support import teaching_capabilities
        with tempfile.TemporaryDirectory() as tmp:
            support = teaching_capabilities(Path(tmp))
        names = {"computer_status", "computer_begin", "computer_stop", "get_window_state", "computer_teach_element", "computer_teach_status", "computer_process_editor", "computer_process_status"}
        issues = diagnostics.connection_issues({"serverInfo": {"name": "company-computer-use", "version": diagnostics.VERSION}},
            names, {"version": diagnostics.VERSION, "teaching_support": support})
        self.assertEqual(len(issues), 1)
        self.assertIn("ZIP", issues[0])

    def test_simple_profile_diagnoses_public_tools_without_requiring_legacy_names(self):
        from teaching_support import teaching_capabilities, TEACHING_HELPER_FILES
        from easy_api import schemas
        from server import MANAGEMENT
        with tempfile.TemporaryDirectory() as tmp:
            for filename in TEACHING_HELPER_FILES:
                (Path(tmp) / filename).touch()
            support = teaching_capabilities(Path(tmp))
        names = {item["name"] for item in schemas(MANAGEMENT)}
        initialized = {"serverInfo": {"name": "company-computer-use", "version": diagnostics.VERSION}}
        status = {"version": diagnostics.VERSION, "tool_profile": "simple", "teaching_support": support}
        self.assertEqual(diagnostics.connection_issues(initialized, names, status), [])
        self.assertTrue(any("computer_act" in issue for issue in diagnostics.connection_issues(initialized, names-{"computer_act"}, status)))

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


class ProcessDiagnosisTests(unittest.TestCase):
    @staticmethod
    def no_job(process):
        def forbidden_tree_cleanup():
            raise AssertionError("No process-tree cleanup is allowed in this diagnostic fallback")
        return types.SimpleNamespace(job=None, job_error={"stage": "assign_process", "winerror": 5}, close=forbidden_tree_cleanup)

    def test_no_job_still_performs_real_metadata_handshake_and_confirms_exit(self):
        source = '''import json,sys
for line in sys.stdin:
 value=json.loads(line)
 if 'id' in value:
  result={'serverInfo':{'version':'synthetic'}} if value['method']=='initialize' else {'structuredContent':{'session':None}}
  print(json.dumps({'jsonrpc':'2.0','id':value['id'],'result':result}),flush=True)
'''
        entry={"command":sys.executable,"args":["-B","-c",source]}
        client=diagnostics.ReadOnlyMCP(entry,owner_factory=self.no_job)
        try:
            response=client.request('initialize',{},timeout=5)
            self.assertEqual(response['serverInfo']['version'],'synthetic')
            client.initialized()
            self.assertIsNone(client.request('tools/call',{'name':'computer_status','arguments':{}},timeout=5)['structuredContent']['session'])
            self.assertEqual(client.process_management['mode'],'stdio_lifecycle')
            self.assertEqual(client.process_management['job_error']['winerror'],5)
        finally:client.close()
        self.assertEqual(client.cleanup,{'verified':True,'graceful':True,'exit_code':0})

    def test_actual_startup_message_and_exit_code_survive_stdout_eof(self):
        source="import sys; print('computer-use-admin: 관리자 연결 실패: 같은 로그인 계정 권한 확인',file=sys.stderr,flush=True);sys.exit(5)"
        client=diagnostics.ReadOnlyMCP({'command':sys.executable,'args':['-B','-c',source], 'env':{'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}},owner_factory=self.no_job)
        try:
            with self.assertRaisesRegex(RuntimeError,'같은 로그인 계정'):
                client.request('initialize',{},timeout=5)
        finally:
            with self.assertRaisesRegex(RuntimeError,'종료 코드: 5'):
                client.close()
        self.assertTrue(client.cleanup['verified'])
        self.assertFalse(client.cleanup['graceful'])

    def test_uac_cancellation_is_not_reported_as_company_policy(self):
        source="import sys;print('computer-use-admin: 관리자 권한 요청이 취소되었습니다.',file=sys.stderr,flush=True);sys.exit(1223)"
        client=diagnostics.ReadOnlyMCP({'command':sys.executable,'args':['-B','-c',source], 'env':{'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}},owner_factory=self.no_job)
        try:
            with self.assertRaisesRegex(RuntimeError,'UAC.*취소'):
                client.request('initialize',{},timeout=5)
        finally:
            with self.assertRaisesRegex(RuntimeError,'UAC.*취소'):
                client.close()

    def test_unrecognized_stderr_is_not_exposed_as_diagnostic_content(self):
        source="import sys;print('private log contents should stay hidden',file=sys.stderr,flush=True);sys.exit(3)"
        client=diagnostics.ReadOnlyMCP({'command':sys.executable,'args':['-B','-c',source], 'env':{'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}},owner_factory=self.no_job)
        try:
            with self.assertRaises(RuntimeError) as caught:
                client.request('initialize',{},timeout=5)
            self.assertNotIn('private log contents',str(caught.exception))
            self.assertIn('단정할 수 없습니다',str(caught.exception))
        finally:
            with self.assertRaises(RuntimeError):client.close()

    def test_failed_normal_shutdown_is_not_connection_success(self):
        client=object.__new__(diagnostics.ReadOnlyMCP)
        process=types.SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(),stderr=io.StringIO(),returncode=-1,
                                      wait=unittest.mock.Mock(side_effect=[subprocess.TimeoutExpired('owned',20),-1]),
                                      kill=unittest.mock.Mock())
        client.process=process;client.owner=self.no_job(process);client.cleanup={'verified':False,'graceful':False}
        client.reader=types.SimpleNamespace(join=lambda timeout:None)
        client.stderr_reader=types.SimpleNamespace(join=lambda timeout:None)
        with self.assertRaisesRegex(RuntimeError,'연결 성공으로 처리하지'):
            client.close()
        process.kill.assert_called_once()
        self.assertTrue(client.cleanup['verified'])
        self.assertFalse(client.cleanup['graceful'])


class PipeCloseDiagnosisTests(unittest.TestCase):
    def client(self, returncode):
        client = object.__new__(diagnostics.ReadOnlyMCP)
        client.process = Mock(returncode=returncode)
        client.process.stdin.close.side_effect = OSError(22, "Invalid argument")
        client.owner = Mock(job=None)
        client.reader, client.stderr_reader = Mock(), Mock()
        client.cleanup = {}
        client._exit_error = Mock(return_value=RuntimeError("known startup failure"))
        return client

    def test_broken_stdin_close_preserves_startup_error_and_verifies_exit(self):
        client = self.client(42)
        with self.assertRaisesRegex(RuntimeError, "known startup failure"):
            client.close()
        client.process.wait.assert_called_once_with(timeout=20)
        self.assertEqual(client.cleanup, {"verified": True, "graceful": False, "exit_code": 42})
        client.process.stdout.close.assert_called_once()

    def test_broken_stdin_close_does_not_report_clean_connection(self):
        client = self.client(0)
        with self.assertRaisesRegex(RuntimeError, "연결 성공으로 처리하지"):
            client.close()
        self.assertTrue(client.cleanup["verified"])
        self.assertFalse(client.cleanup["graceful"])
        client._exit_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
