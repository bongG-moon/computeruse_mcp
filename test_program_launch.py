"""Launch compatibility, honest startup reporting, and a real relative-file app."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from program_launch import create_application, launch_environment, LaunchObservation


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def wait(self, seconds):
        self.now += seconds


class Process:
    pid = 20
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
    def poll(self):
        return self.exit_code


class LaunchTests(unittest.TestCase):
    exe = str(Path("C:/Program Files/Business/App.exe"))
    child = str(Path("C:/Program Files/Business/Child.exe"))

    def row(self, pid=20, hwnd=11, exe=None):
        return {"pid": pid, "window_id": hwnd, "exe": exe or self.exe}

    def probe(self, initial, after, process=None):
        clock = Clock()
        reads = [initial]
        def observe():
            value = reads.pop(0) if reads else after(clock.now) if callable(after) else after
            return {"isError": True} if value is None else {"structuredContent": {"windows": value}}
        probe = LaunchObservation([self.exe, self.child], observe, clock=clock, wait=clock.wait)
        probe.capture()
        return probe.finish(process or Process())

    def test_environment_removes_runtime_overrides_case_insensitively_only(self):
        source = {"PythonHome": "mcp/runtime", "PYTHONPATH": "mcp", "TK_LIBRARY": "mcp/tcl",
                  "TCL_LIBRARY": "mcp/tcl", "Path": "company/bin", "APP_SETTING": "yes",
                  "PYTHON_CUSTOM_APP_SETTING": "keep"}
        cleaned = launch_environment(source)
        self.assertEqual(cleaned, {"Path": "company/bin", "APP_SETTING": "yes", "PYTHON_CUSTOM_APP_SETTING": "keep"})
        self.assertIn("PythonHome", source)

    def test_executable_directory_without_shell_arguments_or_hidden_target(self):
        calls = []
        create_application(self.exe, lambda argv, **kw: calls.append((argv, kw)))
        argv, options = calls[0]
        self.assertEqual(argv, [self.exe])
        self.assertEqual(options["cwd"], str(Path(self.exe).parent))
        self.assertIs(options["shell"], False)
        self.assertEqual(options["creationflags"], 0)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_started_then_exited_is_not_success_even_with_old_window(self):
        old = [self.row(pid=50)]
        result = self.probe(old, old, Process(23))
        self.assertEqual(result["launch_status"], "startup_failed")
        self.assertFalse(result["launched"])
        self.assertEqual(result["exit_code"], 23)
        self.assertFalse(result["window_verified"])
        self.assertEqual(result["existing_window_count"], 1)

    def test_process_that_exits_during_splash_is_reported_with_actual_exit_code(self):
        class ExitingProcess(Process):
            calls = 0
            def poll(self):
                self.calls += 1
                return None if self.calls < 3 else 29
        result = self.probe([], lambda now: [self.row()] if now < .2 else [], ExitingProcess())
        self.assertEqual(result["launch_status"], "startup_failed")
        self.assertEqual(result["exit_code"], 29)
        self.assertFalse(result["window_verified"])

    def test_launcher_child_window_is_candidate_not_assumed_verified_handoff(self):
        result = self.probe([], [self.row(pid=21, exe=self.child)], Process(0))
        self.assertEqual(result["launch_status"], "handoff_unverified")
        self.assertTrue(result["window_verified"])
        self.assertFalse(result["handoff_verified"])
        self.assertFalse(result["application_ready_verified"])
        self.assertEqual(result["windows"][0]["pid"], 21)
        self.assertFalse(result["automatic_retry"])

    def test_zero_exit_without_window_remains_unverified(self):
        result = self.probe([], [], Process(0))
        self.assertEqual(result["launch_status"], "launcher_exited_unverified")
        self.assertFalse(result["window_verified"])

    def test_live_process_without_window_remains_unverified(self):
        result = self.probe([], [])
        self.assertEqual(result["launch_status"], "process_running_unverified")
        self.assertTrue(result["launched"])
        self.assertFalse(result["application_ready_verified"])

    def test_stable_direct_window_does_not_imply_business_ready(self):
        result = self.probe([], [self.row()])
        self.assertEqual(result["launch_status"], "window_observed")
        self.assertTrue(result["window_verified"])
        self.assertFalse(result["task_verified"])
        self.assertGreaterEqual(result["observed_ms"], 250)

    def test_transient_splash_does_not_count_as_open_window(self):
        result = self.probe([], lambda now: [self.row()] if now < .2 else [])
        self.assertEqual(result["launch_status"], "process_running_unverified")

    def test_other_selected_program_windows_are_not_launch_evidence(self):
        result = self.probe([], [self.row(exe="C:/Other/Other.exe")])
        self.assertEqual(result["windows"], [])
        self.assertFalse(result["window_verified"])

    def test_unknown_initial_state_does_not_make_existing_child_window_new(self):
        result = self.probe(None, [self.row(pid=21, exe=self.child)], Process(0))
        self.assertEqual(result["launch_status"], "launcher_exited_unverified")
        self.assertEqual(result["observation_error"], "initial_window_discovery_failed")

    def test_read_failure_does_not_report_window_success(self):
        result = self.probe([], None)
        self.assertFalse(result["window_verified"])
        self.assertEqual(result["observation_error"], "window_discovery_failed")

    @unittest.skipUnless(os.name == "nt" and Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(),
                         "Windows .NET Framework compiler required for the local fixture")
    def test_real_relative_configuration_app_and_embedded_runtime_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            application = root / "application"
            application.mkdir()
            logs = root / "mcp-run"
            logs.mkdir()
            (application / "startup-marker.txt").write_text("fixture")
            source = root / "Fixture.cs"
            source.write_text('''using System; using System.IO;
class Fixture { static int Main() {
 if (!File.Exists("startup-marker.txt")) return 17;
 if (Environment.GetEnvironmentVariable("PYTHONHOME") != null) return 18;
 File.WriteAllText("startup-proof.txt", Directory.GetCurrentDirectory());
 return 0;
} }''', encoding="utf-8")
            executable = application / "Fixture.exe"
            subprocess.run([r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "/nologo", "/target:winexe",
                            "/out:" + str(executable), str(source)], check=True, capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
            old = subprocess.Popen([str(executable)], cwd=logs, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(old.wait(timeout=5), 17)
            with patch.dict(os.environ, {"PYTHONHOME": "MCP runtime must not leak"}):
                fixed = create_application(str(executable))
                self.assertEqual(fixed.wait(timeout=5), 0)
            self.assertEqual((application / "startup-proof.txt").read_text(), str(application))


if __name__ == "__main__":
    unittest.main()
