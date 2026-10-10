"""Explicit checkpoint images do not broaden the UIA session's input grant."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from session_runtime import SessionError, SessionRuntime
from test_guard_performance import TimedTransport
from vendor.guard import Guard, GuardError, windows_checkpoint_ready


TARGET = {"pid": 100, "window_id": 200}


class CaptureTransport(TimedTransport):
    def __init__(self):
        super().__init__()
        self.closed = threading.Event()
        self.after_request = None
        self.answer = {"structuredContent": {**TARGET, "snapshot_id": "PRIVATE TOKEN",
                         "elements": [{"name": "PRIVATE NAME", "value": "PRIVATE VALUE"}],
                         "screenshot": "PRIVATE PIXELS", "title": "PRIVATE TITLE"},
                       "content": [{"type": "text", "text": "PRIVATE LEGACY TREE"},
                                   {"type": "image", "data": "PRIVATE PIXELS", "mimeType": "image/png"}]}

    def driver_request(self, method, params, timeout=None):
        answer = super().driver_request(method, params, timeout)
        if self.after_request:
            self.after_request()
        return answer

    def close(self):
        self.closed.set()

    def check_running(self):
        if self.closed.is_set():
            raise GuardError("Driver connection closed.")


class CheckpointCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.policy = {"driver": str(self.path / "driver.exe"), "run_dir": self.tmp.name,
                       "allowed_apps": [str(self.path / "Editor.exe"), str(self.path / "Viewer.exe")],
                       "mode": "uia", "approval_mode": "run", "max_actions": 10,
                       "log_detail": "metadata", "observation_timeout_seconds": 7}
        self.transport = CaptureTransport()
        self.owner = 100
        self.executable = self.policy["allowed_apps"][0]
        self.guard = Guard(self.policy, transport=self.transport, process_resolver=lambda pid: self.executable,
                           window_resolver=lambda hwnd: self.owner, checkpoint_ready_resolver=lambda hwnd: True)
        self.guard.observed_targets.update({(100, 200), (100, 201)})
        self.guard.action_count = 4

    def denied_without_image(self, result):
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["task_verified"])
        self.assertFalse(result["structuredContent"]["input_dispatched"])
        self.assertFalse(any(item.get("type") == "image" for item in result.get("content", [])))
        self.assertEqual(self.guard.observed_targets, set())
        self.assertEqual(self.guard.action_count, 4)
        self.assertEqual(self.guard.policy["mode"], "uia")

    def test_one_read_uses_same_transport_timeout_without_changing_policy(self):
        before = copy.deepcopy(self.guard.policy)
        with mock.patch.object(self.guard, "approve", side_effect=AssertionError("read-only capture must not approve input")):
            answer = self.guard.capture_checkpoint(TARGET)
        self.assertFalse(answer.get("isError", False))
        self.assertEqual(len(self.transport.calls), 1)
        method, request = self.transport.calls[0]
        self.assertEqual((method, request["name"]), ("tools/call", "get_window_state"))
        self.assertEqual(request["arguments"], {**TARGET, "include_screenshot": True,
                                              "include_accessibility_tree": False, "max_dimension": 1280})
        self.assertEqual(self.transport.timeouts, [7])
        self.assertEqual(self.guard.policy, before)
        self.assertEqual(self.guard.action_count, 4)
        self.assertEqual(self.guard.observed_targets, set())
        self.assertFalse(answer["structuredContent"]["image_verified"])
        self.assertFalse(answer["structuredContent"]["task_verified"])
        self.assertTrue(answer["structuredContent"]["read_only"])
        self.assertEqual(answer["structuredContent"]["window_id"], 200)
        self.assertEqual(answer["content"], [{"type": "image", "data": "PRIVATE PIXELS", "mimeType": "image/png"}])
        self.assertNotIn("PRIVATE", json.dumps(answer["structuredContent"]))

    def test_checkpoint_never_logs_pixels_or_screen_text_even_in_verbose_session(self):
        self.guard.policy["log_detail"] = "content"
        self.guard.capture_checkpoint(TARGET)
        journal = (self.path / "actions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", journal)
        self.assertIn('"event": "checkpoint_result"', journal)
        self.assertIn('"route": "checkpoint"', journal)
        self.assertEqual(self.guard.policy["log_detail"], "content")

    def test_no_action_is_granted_by_screenshot(self):
        self.guard.capture_checkpoint(TARGET)
        result = self.guard.call("click", {**TARGET, "element_token": "old"})
        self.assertTrue(result["isError"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.guard.action_count, 4)

    def test_invalid_or_foreign_target_never_reaches_driver(self):
        for target in (None, {}, {**TARGET, "scope": "desktop"}, {**TARGET, "x": 1},
                       {**TARGET, "window_id": True}, {**TARGET, "pid": 0}, {"pid": 100}):
            with self.subTest(target=target):
                self.denied_without_image(self.guard.capture_checkpoint(target))
        self.owner = 101
        self.denied_without_image(self.guard.capture_checkpoint(TARGET))
        self.owner = 100
        self.executable = str(self.path / "Other.exe")
        self.denied_without_image(self.guard.capture_checkpoint(TARGET))
        self.assertEqual(self.transport.calls, [])

    def test_reused_window_or_changed_executable_suppresses_image_after_read(self):
        for field, value in (("owner", 101), ("executable", self.policy["allowed_apps"][1])):
            self.owner, self.executable = 100, self.policy["allowed_apps"][0]
            self.transport.after_request = lambda: setattr(self, field, value)
            with self.subTest(field=field):
                self.denied_without_image(self.guard.capture_checkpoint(TARGET))
        self.assertEqual(len(self.transport.calls), 2)

    def test_different_reported_target_suppresses_image(self):
        for key in TARGET:
            self.transport.answer["structuredContent"] = {**TARGET, key: 999}
            with self.subTest(key=key):
                self.denied_without_image(self.guard.capture_checkpoint(TARGET))

    def test_hidden_minimized_or_background_window_is_never_captured(self):
        self.guard.checkpoint_ready_resolver = mock.Mock(return_value=False)
        answer = self.guard.capture_checkpoint(TARGET)
        self.denied_without_image(answer)
        self.assertEqual(answer["structuredContent"]["error_code"], "checkpoint_requires_foreground")
        self.assertEqual(self.transport.calls, [])
        self.guard.checkpoint_ready_resolver.assert_called_once_with(200)

    def test_foreground_loss_during_capture_suppresses_pixels(self):
        self.guard.checkpoint_ready_resolver = mock.Mock(side_effect=[True, False])
        answer = self.guard.capture_checkpoint(TARGET)
        self.denied_without_image(answer)
        self.assertEqual(answer["structuredContent"]["error_code"], "checkpoint_requires_foreground")
        self.assertEqual(len(self.transport.calls), 1)

    def test_stop_during_read_suppresses_image_and_closes_transport(self):
        self.transport.after_request = lambda: (self.path / "stop.flag").write_text("stop", encoding="utf-8")
        self.denied_without_image(self.guard.capture_checkpoint(TARGET))
        self.assertTrue(self.transport.closed.is_set())

    def test_driver_error_and_missing_or_invalid_image_never_report_success(self):
        self.transport.error = GuardError("driver timeout")
        self.denied_without_image(self.guard.capture_checkpoint(TARGET))
        self.transport.error = None
        for content in ([], [{"type": "image", "data": "", "mimeType": "image/png"}],
                        [{"type": "image", "data": "pixels", "mimeType": "text/html"}],
                        [{"type": "image", "data": "pixels", "mimeType": "image/png"}]*3):
            self.transport.answer["content"] = content
            with self.subTest(content=content):
                self.denied_without_image(self.guard.capture_checkpoint(TARGET))

    def runtime(self):
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.guard, runtime.transport = self.guard, self.transport
        runtime.execution_lock = threading.RLock()
        runtime.mode, runtime.state, runtime.reason = "uia", "active", ""
        runtime.stop_event = threading.Event()
        runtime.request_cancel_event = threading.Event()
        runtime.deadline = time.monotonic()+60
        runtime.run_dir = self.path
        def stop(reason):
            runtime.reason, runtime.state = reason, "stopped"
            runtime.stop_event.set()
        runtime.stop = stop
        return runtime

    def test_runtime_capture_stays_uia_and_requires_active_session(self):
        runtime = self.runtime()
        answer = runtime.capture_checkpoint(TARGET)
        self.assertTrue(answer["structuredContent"]["read_only"])
        self.assertEqual((runtime.mode, self.guard.policy["mode"]), ("uia", "uia"))
        runtime.state = "awaiting_approval"
        with self.assertRaises(SessionError):
            runtime.capture_checkpoint(TARGET)
        self.assertEqual(len(self.transport.calls), 1)

    def test_runtime_cancellation_or_dead_driver_during_capture_rejects_result(self):
        for cause in ("cancel", "request_cancel", "driver"):
            runtime = self.runtime()
            self.transport.closed.clear()
            self.transport.after_request = (runtime.stop_event.set if cause == "cancel" else
                runtime.request_cancel_event.set if cause == 'request_cancel' else self.transport.closed.set)
            with self.subTest(cause=cause), self.assertRaises(SessionError):
                runtime.capture_checkpoint(TARGET)
            self.assertEqual(runtime.state, "stopped")
            self.assertEqual(self.guard.observed_targets, set())

    def test_runtime_serializes_capture_with_other_actions(self):
        runtime = self.runtime()
        class ExecutionLock:
            entered = False
            def __enter__(self):
                self.entered = True
            def __exit__(self, *args):
                self.entered = False
        lock = runtime.execution_lock = ExecutionLock()
        self.transport.after_request = lambda: self.assertTrue(lock.entered)
        runtime.capture_checkpoint(TARGET)
        self.assertFalse(lock.entered)


class NativeCaptureReadinessTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(**{name: mock.Mock(return_value=value) for name, value in {
            "IsWindow": True, "IsWindowVisible": True, "IsIconic": False,
            "GetForegroundWindow": 200, "MonitorFromWindow": 10}.items()})
        def rect(hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 0, 0
            pointer._obj.right, pointer._obj.bottom = 600, 400
            return True
        self.user.GetWindowRect = mock.Mock(side_effect=rect)
        self.cloaked = 0
        def cloak(hwnd, attribute, pointer, size):
            self.assertEqual(attribute, 14)
            pointer._obj.value = self.cloaked
            return 0
        self.dwm = SimpleNamespace(DwmGetWindowAttribute=mock.Mock(side_effect=cloak))

    def ready(self):
        with mock.patch("vendor.guard.os.name", "nt"), mock.patch("vendor.guard.ctypes.WinDLL", create=True,
                side_effect=lambda name, **kwargs: self.user if name == "user32" else self.dwm):
            return windows_checkpoint_ready(200)

    def test_visible_nonminimized_exact_foreground_on_screen_is_required(self):
        self.assertTrue(self.ready())
        for name, value in (("IsWindow", False), ("IsWindowVisible", False), ("IsIconic", True),
                            ("GetForegroundWindow", 201), ("MonitorFromWindow", 0)):
            function = getattr(self.user, name)
            previous = function.return_value
            function.return_value = value
            with self.subTest(name=name):
                self.assertFalse(self.ready())
            function.return_value = previous

    def test_cloaked_window_rect_failure_and_late_foreground_switch_fail_closed(self):
        self.cloaked = 1
        self.assertFalse(self.ready())
        self.cloaked = 0
        self.dwm.DwmGetWindowAttribute.side_effect = None
        self.dwm.DwmGetWindowAttribute.return_value = -1
        self.assertFalse(self.ready())
        self.dwm.DwmGetWindowAttribute.return_value = 0
        self.user.GetForegroundWindow.side_effect = [200, 201]
        self.assertFalse(self.ready())
        self.user.GetForegroundWindow.side_effect = None
        self.user.GetWindowRect.side_effect = lambda hwnd, pointer: False
        self.assertFalse(self.ready())


if __name__ == "__main__":
    unittest.main()
