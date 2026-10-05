"""Protocol, permission and cancellation tests; never sends real desktop input."""
import copy
import io
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from settings import VERSION
from server import ComputerManager, StdioServer, TaskStore, MANAGEMENT_TOOLS, validate_management, SAFE_TOOL_DESCRIPTIONS
from session_runtime import SessionRuntime, ConsentGuard, SessionError, SAFE_TOOLS
from vendor.guard import GuardError, DriverTransport, diagnostic_code, action_guidance, KEY_ALIASES


def config_at(folder, **changes):
    driver = Path(folder) / "driver.exe"
    editor = Path(folder) / "Editor.exe"
    driver.touch()
    editor.touch()
    config = {"version": 1, "driver": str(driver), "state_dir": str(Path(folder) / "state"),
              "programs": [{"id": "editor", "name": "편집기", "exe": str(editor), "control_exes": [], "hints": "", "enabled": True},
                           {"id": "disabled", "name": "비활성", "exe": str(editor), "control_exes": [], "hints": "", "enabled": False}],
              "mode": "uia", "approval": "session", "max_minutes": 10, "max_actions": 20,
              "approval_timeout_seconds": 1}
    config.update(changes)
    return config


class FakeBroker:
    def __init__(self, approved=True, block=False):
        self.approved = approved
        self.block = block
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.calls = []
        self.details = []

    def confirm(self, kind, title, details, stop_event):
        self.calls.append(kind)
        self.details.append((title, details))
        self.entered.set()
        if self.block:
            while not stop_event.wait(0.005) and not self.closed.is_set():
                pass
            return False
        return self.approved

    def close(self):
        self.closed.set()


class FakeLease:
    def __init__(self):
        self.held = False
        self.releases = 0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            if self.held:
                return False
            self.held = True
            return True

    def release(self):
        with self.lock:
            self.held = False
            self.releases += 1


class FakeEmergency:
    def __init__(self, folder, callback):
        self.callback = callback
        self.available = True
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, policy):
        self.policy = policy
        self.closed = threading.Event()
        self.entered = threading.Event()
        self.calls = []
        self.block_actions = False

    def check_running(self):
        if self.closed.is_set():
            raise GuardError("closed")

    def driver_request(self, method, params):
        self.check_running()
        self.calls.append((method, copy.deepcopy(params)))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        if method == "tools/list":
            return {"tools": [{"name": name, "description": "Legacy: escalate_session, get_desktop_state or terminal/PTY; always both tree and screenshot.", "inputSchema": {"type": "object", "properties":
                {"pid": {"type": "integer"}, "window_id": {"type": "integer"}, "scope": {"type": "string"},
                 "element_token": {"type": "string", "description": "Opaque snapshot-scoped element handle."},
                 "snapshot_id": {"type": "string", "description": "Fresh snapshot id paired with element_index."},
                 "screenshot_out_file": {"type": "string"}, "text": {"type": "string"}}}}
                for name in ("click", "get_window_state", "drag", "set_value", "browser_navigate", "set_config")]}
        if method == "tools/call":
            if params["name"] == "get_window_state":
                return {"structuredContent": {"elements": [{"element_token": "fake", "label": "test"}], "window_id": 200},
                        "content": [{"type": "text", "text": "test snapshot"}]}
            self.entered.set()
            if self.block_actions:
                self.closed.wait(5)
                raise GuardError("driver stopped")
            return {"content": [{"type": "text", "text": "delivered"}], "structuredContent": {"effect": "confirmed"}}
        return {}

    def notify(self, method):
        self.check_running()

    def close(self):
        self.closed.set()


class TestGuard(ConsentGuard):
    def __init__(self, policy, transport, session):
        super().__init__(policy, transport, session)
        self.process_resolver = lambda pid: policy["allowed_apps"][0]
        self.window_resolver = lambda hwnd: 100
        self.discovery_provider = lambda *args: []


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_at(self.tmp.name)
        self.broker = FakeBroker()
        self.lease = FakeLease()
        self.sessions = []

    def create(self, config=None, **kwargs):
        config = config or self.config
        runtime = SessionRuntime(config, [config["programs"][0]], "uia", {"instructions": "가짜 문서 입력", "expected": "확인"},
            broker_factory=lambda *args: self.broker, lease_factory=lambda: self.lease,
            emergency_factory=FakeEmergency, transport_factory=FakeTransport, guard_factory=TestGuard,
            registry_register=lambda folder: None, registry_unregister=lambda folder: None, **kwargs)
        self.sessions.append(runtime)
        self.addCleanup(runtime.stop)
        return runtime

    def test_metadata_logs_omit_input_and_screen_text_but_client_keeps_it(self):
        runtime = self.create()
        runtime.start()
        secret = "비공개화면입력-98765"
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        answer = runtime.call("type_text", {"pid": 100, "window_id": 200, "text": secret})
        self.assertFalse(answer.get("isError"))
        runtime.guard.log("result", tool="get_window_state", success=True,
                          summary={"text": secret, "verification": secret, "effect": "confirmed", "image_count": 1})
        runtime.guard.log("error", tool="click", success=False, summary="stale element: " + secret)
        text = (runtime.run_dir / "actions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn('"label"', text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[-2]["summary"]["effect"], "confirmed")
        self.assertEqual(rows[-1]["summary"]["diagnostic_code"], "stale_observation")
        self.assertEqual(runtime.transport.policy["log_detail"], "metadata")
        self.assertEqual(runtime.transport.calls[-1][1]["arguments"]["text"], secret)

    def test_content_log_requires_explicit_setting(self):
        config = {**self.config, "log_detail": "content"}
        runtime = self.create(config)
        runtime.start()
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        runtime.call("type_text", {"pid": 100, "window_id": 200, "text": "선택한 상세기록"})
        self.assertIn("선택한 상세기록", (runtime.run_dir / "actions.jsonl").read_text(encoding="utf-8"))

    def test_modified_press_key_uses_one_hotkey_and_consumes_snapshot(self):
        runtime = self.create()
        runtime.start()
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        args = {"pid": 100, "window_id": 200, "key": "s", "modifiers": ["ctrl"], "delivery_mode": "foreground"}
        answer = runtime.call("press_key", args)
        self.assertFalse(answer.get("isError"))
        call = runtime.transport.calls[-1][1]
        self.assertEqual(call["name"], "hotkey")
        self.assertEqual(call["arguments"]["keys"], ["ctrl", "s"])
        self.assertEqual(call["arguments"]["delivery_mode"], "foreground")
        self.assertNotIn("key", call["arguments"])
        before = len(runtime.transport.calls)
        self.assertTrue(runtime.call("press_key", args)["isError"])
        self.assertEqual(len(runtime.transport.calls), before)
        self.assertEqual(runtime.guard.action_count, 1)

    def test_background_modifier_combinations_never_reach_driver(self):
        runtime = self.create({**self.config, "approval": "each"})
        runtime.start()
        for name, keys in (("hotkey", {"keys": ["ctrl", "w"]}),
                           ("press_key", {"key": "s", "modifiers": ["ctrl"]}),
                           ("hotkey", {"keys": ["alt", "f"]}),
                           ("hotkey", {"keys": ["shift", "a"]})):
            with self.subTest(name=name, keys=keys):
                runtime.call("get_window_state", {"pid": 100, "window_id": 200})
                before = len(runtime.transport.calls)
                answer = runtime.call(name, {"pid": 100, "window_id": 200, **keys})
                self.assertTrue(answer["isError"])
                self.assertEqual(answer["structuredContent"]["error_code"], "background_unavailable")
                self.assertFalse(answer["structuredContent"]["input_sent"])
                self.assertEqual(answer["structuredContent"]["effect"], "not_applied")
                self.assertEqual(len(runtime.transport.calls), before)
                self.assertEqual(runtime.guard.action_count, 0)
                self.assertEqual(self.broker.calls, ["session"])
                self.assertNotIn((100, 200), runtime.guard.observed_targets)

    def test_foreground_hotkey_requires_new_observation_and_dispatches_once(self):
        runtime = self.create()
        runtime.start()
        target = {"pid": 100, "window_id": 200}
        runtime.call("get_window_state", target)
        runtime.call("hotkey", {**target, "keys": ["ctrl", "w"]})
        before = len(runtime.transport.calls)
        foreground = {**target, "keys": ["ctrl", "w"], "delivery_mode": "foreground"}
        self.assertTrue(runtime.call("hotkey", foreground)["isError"])
        self.assertEqual(len(runtime.transport.calls), before)
        runtime.call("get_window_state", target)
        before = len(runtime.transport.calls)
        self.assertFalse(runtime.call("hotkey", foreground).get("isError"))
        self.assertEqual(len(runtime.transport.calls), before + 1)
        self.assertEqual(runtime.transport.calls[-1][1], {"name": "hotkey", "arguments": foreground})

    def test_shortcut_restrictions_precede_delivery_refusal_and_single_keys_remain(self):
        runtime = self.create()
        runtime.start()
        target = {"pid": 100, "window_id": 200}
        for delivery in ("background", "foreground"):
            for keys in (["ctrl", "shift", "i"], ["alt", "tab"], ["win", "r"]):
                runtime.call("get_window_state", target)
                before = len(runtime.transport.calls)
                answer = runtime.call("hotkey", {**target, "keys": keys, "delivery_mode": delivery})
                self.assertTrue(answer["isError"])
                self.assertNotEqual(answer["structuredContent"].get("error_code"), "background_unavailable")
                self.assertEqual(len(runtime.transport.calls), before)
        for name, keys in (("press_key", {"key": "enter"}), ("hotkey", {"keys": ["tab"]})):
            runtime.call("get_window_state", target)
            before = len(runtime.transport.calls)
            self.assertFalse(runtime.call(name, {**target, **keys}).get("isError"))
            self.assertEqual(len(runtime.transport.calls), before + 1)

    def test_modifier_aliases_cannot_bypass_dangerous_shortcut_guards(self):
        runtime = self.create()
        runtime.start()
        before = len(runtime.transport.calls)
        target = {"pid": 100, "window_id": 200}
        combinations = {"ctrl": ["shift", "i"], "shift": ["ctrl", "i"], "alt": ["tab"], "win": ["r"]}
        for alias, canonical in sorted(KEY_ALIASES.items()):
            for delivery in ("background", "foreground"):
                with self.subTest(alias=alias, delivery=delivery):
                    answer = runtime.call("hotkey", {**target, "keys": [alias, *combinations[canonical]], "delivery_mode": delivery})
                    self.assertTrue(answer["isError"])
                    self.assertNotEqual(answer["structuredContent"].get("error_code"), "background_unavailable")
                    self.assertRegex(answer["content"][0]["text"], "System-wide|Developer console")
        for name, args in (("press_key", {"key": "i", "modifiers": ["control_l", "shift_l"]}),
                           ("hotkey", {"keys": ["altgr", "delete"]}),
                           ("hotkey", {"keys": ["Control_L", "Shift_R", "escape"]})):
            answer = runtime.call(name, {**target, **args, "delivery_mode": "foreground"})
            self.assertTrue(answer["isError"])
            self.assertRegex(answer["content"][0]["text"], "System-wide|Developer console|Task Manager")
        self.assertEqual(len(runtime.transport.calls), before)
        self.assertEqual(runtime.guard.action_count, 0)

    def test_stale_element_preserves_diagnostic_and_requires_reobserve(self):
        runtime = self.create()
        runtime.start()
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        with mock.patch.object(runtime.transport, "driver_request", return_value={"isError": True,
                "content": [{"type": "text", "text": "stale element token specific detail"}],
                "structuredContent": {"effect": "unverifiable", "escalation": {"target": "foreground", "reason": "delivery_failed"}}}):
            answer = runtime.call("click", {"pid": 100, "window_id": 200, "element_token": "old"})
        self.assertEqual(answer["structuredContent"]["effect"], "unverifiable")
        self.assertEqual(answer["structuredContent"]["escalation"]["target"], "foreground")
        guidance = answer["structuredContent"]["computer_use_guidance"]
        self.assertIn("specific detail", guidance["diagnostic"])
        self.assertEqual(guidance["diagnostic_code"], "stale_observation")
        self.assertNotIn((100, 200), runtime.guard.observed_targets)

    def test_idle_driver_exit_releases_lease_and_allows_new_session(self):
        runtime = self.create()
        runtime.start()
        runtime.transport.closed.set()
        deadline = time.monotonic() + 2
        while runtime.state != "stopped" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(runtime.state, "stopped")
        self.assertFalse(self.lease.held)
        next_runtime = self.create()
        next_runtime.start()
        self.assertEqual(next_runtime.state, "active")

    def test_driver_failure_during_action_does_not_retry(self):
        runtime = self.create()
        runtime.start()
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        def die(*args):
            runtime.transport.closed.set()
            raise GuardError("Driver process ended.")
        with mock.patch.object(runtime.transport, "driver_request", side_effect=die) as dispatch:
            answer = runtime.call("type_text", {"pid": 100, "window_id": 200, "text": "입력"})
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue(answer["isError"])
        self.assertEqual(answer["structuredContent"]["session_recovery"]["automatic_retry"], False)
        self.assertEqual(runtime.state, "stopped")

    def test_closed_menu_delivery_refusal_is_not_driver_termination(self):
        diagnostic = ("hotkey on a modern XAML / UWP target could not find a UIA AcceleratorKey matching ctrl+a. "
                      "PostMessage WM_KEYDOWN/UP is ignored by this target's input pipeline. "
                      "Common cause: the action is nested behind a closed menu.")
        answer = action_guidance({"isError": True, "content": [{"type": "text", "text": diagnostic}]}, "hotkey")
        guidance = answer["structuredContent"]["computer_use_guidance"]
        self.assertEqual(guidance["diagnostic_code"], "delivery_failed")
        self.assertEqual(guidance["diagnostic"], diagnostic)
        for text in ("The action is behind a closed menu.", "Target application is not running."):
            with self.subTest(text=text):
                self.assertNotEqual(diagnostic_code(text), "driver_ended")
        for text in ("Driver process ended.", "Driver process is not running.", "Driver connection closed."):
            with self.subTest(text=text):
                self.assertEqual(diagnostic_code(text), "driver_ended")

    def test_missing_dialog_window_guides_fresh_parent_observation(self):
        diagnostic = "No window with window_id 12345 exists. Call list_windows to get current windows."
        answer = action_guidance({"isError": True, "content": [{"type": "text", "text": diagnostic}]}, "get_window_state")
        guidance = answer["structuredContent"]["computer_use_guidance"]
        self.assertEqual(guidance["diagnostic_code"], "target_unavailable")
        self.assertEqual(guidance["diagnostic"], diagnostic)
        self.assertIn("list_windows", guidance["next_step"])
        self.assertIn("허용된 부모 창", guidance["next_step"])

    def test_driver_stderr_metadata_default_and_explicit_content(self):
        for detail in (None, "content"):
            transport = DriverTransport.__new__(DriverTransport)
            transport.policy = {} if detail is None else {"log_detail": detail}
            transport.run_dir = Path(self.tmp.name) / (detail or "metadata")
            transport.run_dir.mkdir()
            transport.child = type("Child", (), {"stderr": io.StringIO("secret-screen-text\n")})()
            transport._read_stderr()
            data = (transport.run_dir / "driver-stderr.log").read_text(encoding="utf-8")
            self.assertEqual("secret-screen-text" in data, detail == "content")

    def test_approval_required_before_driver_creation(self):
        self.broker.approved = False
        runtime = self.create()
        with self.assertRaises(SessionError):
            runtime.start()
        self.assertIsNone(runtime.transport)
        self.assertFalse(self.lease.held)
        self.assertTrue((runtime.run_dir / "stop.flag").exists())

    def test_stop_interrupts_blocked_native_consent(self):
        self.broker.block = True
        runtime = self.create()
        errors = []
        worker = threading.Thread(target=lambda: self.capture_error(runtime.start, errors))
        worker.start()
        self.assertTrue(self.broker.entered.wait(1))
        runtime.stop("test cancellation")
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(runtime.transport)
        self.assertFalse(self.lease.held)
        self.assertTrue(errors)

    @staticmethod
    def capture_error(call, errors):
        try:
            call()
        except Exception as exc:
            errors.append(exc)

    def test_each_approval_and_reobserve_gate_are_preserved(self):
        runtime = self.create({**self.config, "approval": "each"})
        runtime.start()
        args = {"pid": 100, "window_id": 200, "key": "tab"}
        self.assertTrue(runtime.call("press_key", args)["isError"])
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        self.assertFalse(runtime.call("press_key", args).get("isError", False))
        self.assertEqual(self.broker.calls, ["session", "action"])
        self.assertIn("키보드 키 누르기", self.broker.details[1][0])
        self.assertIn('"key": "tab"', self.broker.details[1][1])
        self.assertTrue(runtime.call("press_key", args)["isError"])
        self.assertEqual(runtime.guard.action_count, 1)

    def test_client_mode_skips_broker_only_when_configured(self):
        self.broker.approved = False
        runtime = self.create({**self.config, "approval": "client"})
        runtime.start()
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(runtime.status()["approval"], "client")

    def test_stop_closes_inflight_driver_and_releases_lease(self):
        runtime = self.create()
        runtime.start()
        runtime.call("get_window_state", {"pid": 100, "window_id": 200})
        runtime.transport.block_actions = True
        responses = []
        worker = threading.Thread(target=lambda: responses.append(runtime.call("press_key", {"pid": 100, "window_id": 200, "key": "tab"})))
        worker.start()
        self.assertTrue(runtime.transport.entered.wait(1))
        runtime.stop()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(runtime.transport.closed.is_set())
        self.assertFalse(self.lease.held)
        self.assertEqual(self.lease.releases, 1)

    def test_stop_failure_retains_lease_until_actual_driver_exit(self):
        runtime = self.create()
        unregistered = []
        runtime.registry_unregister = lambda folder: unregistered.append(folder)
        runtime.start()
        class Child:
            returncode = None
            def poll(self):
                return self.returncode
        child = Child()
        runtime.transport.child = child
        runtime.stop()
        self.assertEqual(runtime.state, "stop_failed")
        self.assertTrue(self.lease.held)
        self.assertEqual(self.lease.releases, 0)
        self.assertEqual(unregistered, [])
        child.returncode = 0
        runtime.stop()
        self.assertEqual(runtime.state, "stopped")
        self.assertFalse(self.lease.held)
        self.assertEqual(self.lease.releases, 1)
        self.assertEqual(unregistered, [runtime.run_dir])

    def test_active_registry_covers_consent_and_cleans_after_stopped(self):
        runtime = self.create()
        events = []
        runtime.registry_register = lambda folder: events.append(("register", self.lease.held, list(self.broker.calls)))
        runtime.registry_unregister = lambda folder: events.append(("unregister", runtime.state, self.lease.held))
        runtime.start()
        self.assertEqual(events, [("register", True, [])])
        runtime.stop()
        self.assertEqual(events[-1], ("unregister", "stopped", False))
        runtime.stop()
        self.assertEqual(self.lease.releases, 1)

    def test_deadline_stops_waiting_session(self):
        self.broker.block = True
        runtime = self.create()
        runtime.deadline = time.monotonic() + 0.08
        errors = []
        worker = threading.Thread(target=lambda: self.capture_error(runtime.start, errors))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(runtime.state, "stopped")
        self.assertFalse(self.lease.held)

    def test_second_session_cannot_acquire_same_desktop(self):
        first = self.create()
        first.start()
        second = self.create()
        with self.assertRaises(SessionError):
            second.start()
        self.assertTrue(self.lease.held)
        first.stop()
        self.assertFalse(self.lease.held)

    def test_setup_stop_flag_interrupts_session(self):
        runtime = self.create()
        runtime.start()
        (runtime.run_dir / "stop.flag").touch()
        self.assertTrue(runtime.stop_event.wait(1))
        deadline = time.monotonic() + 1
        while self.lease.held and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.lease.held)

    def test_launch_only_exact_selected_program_without_arguments(self):
        calls = []
        class Process:
            pid = 321
        runtime = self.create(process_factory=lambda argv, **kw: (calls.append((argv, kw)) or Process()))
        runtime.start()
        with self.assertRaises(SessionError):
            runtime.launch("disabled")
        output = runtime.launch("editor")
        self.assertEqual(output["requested_pid"], 321)
        self.assertEqual(len(calls[0][0]), 1)
        self.assertIs(calls[0][1]["shell"], False)
        self.assertEqual(calls[0][1]["stdout"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertEqual(runtime.guard.action_count, 1)

    def test_manifest_is_bounded_and_contains_only_selected_apps(self):
        runtime = self.create()
        runtime.start()
        manifest = json.loads((runtime.run_dir / "capabilities.json").read_text())
        self.assertFalse(manifest["resources"]["desktop"]["display"])
        self.assertEqual(len(manifest["resources"]["apps"]), 1)
        self.assertFalse(manifest["resources"]["apps"][0]["launch"])
        self.assertEqual(runtime.transport.policy["driver_env"]["CUA_DRIVER_PERMISSION_MODE"], "bounded")

    def test_session_consent_displays_extra_control_executables(self):
        configuration = copy.deepcopy(self.config)
        extra = str(Path(self.tmp.name) / "Secondary.exe")
        configuration["programs"][0]["control_exes"] = [extra]
        runtime = self.create(configuration)
        runtime.start()
        self.assertIn(extra, self.broker.details[0][1])
        self.assertIn("추가 조작 실행파일", self.broker.details[0][1])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_at(self.tmp.name)
        self.transports = []
        def transport(policy):
            value = FakeTransport(policy)
            self.transports.append(value)
            return value
        self.manager = ComputerManager(self.config, transport_factory=transport)
        self.addCleanup(self.manager.stop)

    def test_driver_missing_keeps_management_tools_and_clear_diagnostic(self):
        self.manager.config["driver"] = str(Path(self.tmp.name) / "missing.exe")
        tools = self.manager.tools_list()
        self.assertEqual({t["name"] for t in tools["tools"]}, {t["name"] for t in MANAGEMENT_TOOLS})
        self.assertTrue(tools["_meta"]["driver_schema_error"])
        self.assertFalse(self.manager.status()["driver_exists"])

    def test_schemas_are_cached_safe_union_from_real_surface(self):
        first = self.manager.tools_list()
        second = self.manager.tools_list()
        names = {t["name"] for t in first["tools"]}
        self.assertTrue({"drag", "set_value", "get_window_state"} <= names)
        self.assertNotIn("browser_navigate", names)
        self.assertNotIn("set_config", names)
        self.assertEqual(first, second)
        self.assertEqual(len(self.transports), 1)
        self.assertTrue(self.transports[0].closed.is_set())
        observation = next(t for t in first["tools"] if t["name"] == "get_window_state")
        self.assertNotIn("screenshot_out_file", observation["inputSchema"]["properties"])
        self.assertEqual(set(observation["inputSchema"]["required"]), {"pid", "window_id"})
        self.assertEqual([method for method, _ in self.transports[0].calls], ["initialize", "tools/list"])
        marker = json.loads((Path(self.transports[0].policy["run_dir"]) / "schema-session.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["format"], "computer-use-schema/v1")
        self.assertEqual(marker["state"], "stopped")

    def test_schema_probe_with_surviving_driver_is_not_marked_safe_to_clean(self):
        def factory(policy):
            transport = FakeTransport(policy)
            transport.child = type("LiveChild", (), {"poll": lambda self: None})()
            return transport
        self.manager.transport_factory = factory
        self.assertEqual(len(self.manager.tools_list()["tools"]), len(MANAGEMENT_TOOLS))
        markers = list((Path(self.config["state_dir"]) / "runs").glob("schema-*/schema-session.json"))
        self.assertEqual(len(markers), 1)
        self.assertEqual(json.loads(markers[0].read_text(encoding="utf-8"))["state"], "stop_failed")
        self.assertFalse(self.manager.stop()["stopped"])
        self.assertIsNotNone(self.manager.probe_transport)
        self.manager.probe_transport.child.poll = lambda: 0
        self.assertTrue(self.manager.stop()["stopped"])
        self.assertEqual(json.loads(markers[0].read_text(encoding="utf-8"))["state"], "stopped")

    def test_returned_driver_clears_previous_missing_error_without_stale_tools(self):
        original = self.manager.tools_list()
        path = Path(self.config["driver"])
        parked = path.with_suffix(".parked")
        path.rename(parked)
        missing = self.manager.tools_list()
        self.assertEqual(len(missing["tools"]), len(MANAGEMENT_TOOLS))
        self.assertIn("설정", missing["_meta"]["driver_schema_error"])
        parked.rename(path)
        restored = self.manager.tools_list()
        self.assertEqual(restored, original)
        self.assertEqual(self.manager.status()["driver_schema_error"], "")

    def test_lowlevel_calls_and_launch_denied_before_begin(self):
        for name, args in (("click", {}), ("computer_launch", {"program_id": "editor"}), ("shell", {})):
            with self.assertRaises(SessionError):
                self.manager.call(name, args)

    def test_descriptions_replace_legacy_paths_but_keep_parameter_routing(self):
        self.assertEqual(set(SAFE_TOOL_DESCRIPTIONS), SAFE_TOOLS)
        tools = self.manager.tools_list()["tools"]
        for item in tools:
            if item["name"] not in SAFE_TOOLS:
                continue
            for phrase in ("escalate_session", "get_desktop_state", "terminal/PTY", "always both tree and screenshot"):
                self.assertNotIn(phrase, item["description"])
        click = next(item for item in tools if item["name"] == "click")
        self.assertEqual(click["inputSchema"]["properties"]["element_token"]["description"], "Opaque snapshot-scoped element handle.")
        self.assertIn("paired with element_index", click["inputSchema"]["properties"]["snapshot_id"]["description"])
        self.assertIn("replace the entire field", SAFE_TOOL_DESCRIPTIONS["type_text"])

    def test_model_cannot_widen_programs_paths_approval_or_budget(self):
        base = {"program_ids": ["editor"], "task_description": "write fake note"}
        for override in ({"program_ids": ["disabled"]}, {"program_ids": ["other"]},
                         {"allowed_apps": ["C:\\evil.exe"]}, {"approval": "client"},
                         {"max_actions": 21}, {"max_minutes": 11}):
            with self.assertRaises(SessionError):
                self.manager.begin({**base, **override})
        with self.assertRaises(SessionError):
            validate_management("computer_launch", {"program_id": "editor", "args": ["/evil"]})

    def test_saved_recipe_is_inert_and_never_changes_config(self):
        original = copy.deepcopy(self.manager.config)
        task = self.manager.tasks.save({"name": "My flow", "instructions": "Literal: $(cmd) and Python text remain inert.",
                                      "expected": "screen result", "program_ids": ["editor"]})
        self.assertEqual(self.manager.tasks.get(task["id"])["instructions"], task["instructions"])
        self.assertEqual(self.manager.config, original)
        self.assertIsNone(self.manager.session)
        self.assertEqual(self.transports, [])
        raw = json.loads((Path(self.config["state_dir"]) / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        with self.assertRaises(SessionError):
            self.manager.tasks.save({**task, "id": "../../outside"})

    def test_cancel_before_runtime_registration_does_not_start_session(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(SessionError):
            self.manager.begin({"program_ids": ["editor"], "task_description": "fake"}, event)
        self.assertIsNone(self.manager.session)


class QueueInput:
    def __init__(self):
        self.items = queue.Queue()

    def send(self, message):
        self.items.put(json.dumps(message) + "\n")

    def eof(self):
        self.items.put(None)

    def __iter__(self):
        while True:
            item = self.items.get()
            if item is None:
                return
            yield item


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_at(self.tmp.name)
        self.broker = FakeBroker(block=True)
        self.lease = FakeLease()
        def runtime(config, programs, mode, task, **kwargs):
            return SessionRuntime(config, programs, mode, task, broker_factory=lambda *a: self.broker,
                lease_factory=lambda: self.lease, emergency_factory=FakeEmergency,
                transport_factory=FakeTransport, guard_factory=TestGuard,
                registry_register=lambda folder: None, registry_unregister=lambda folder: None, **kwargs)
        self.manager = ComputerManager(self.config, runtime_factory=runtime, transport_factory=FakeTransport)
        self.input = QueueInput()
        self.output = io.StringIO()
        self.server = StdioServer(self.manager, self.input, self.output)
        self.worker = threading.Thread(target=self.server.run)
        self.worker.start()
        def cleanup():
            self.input.eof()
            self.worker.join(3)
        self.addCleanup(cleanup)
        self.input.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.wait_for(1)

    def wait_for(self, request_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            for line in self.output.getvalue().splitlines():
                row = json.loads(line)
                if row.get("id") == request_id:
                    return row
            time.sleep(0.005)
        self.fail("No protocol response for " + str(request_id))

    def begin(self, request_id=2):
        self.input.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
            "name": "computer_begin", "arguments": {"program_ids": ["editor"], "task_description": "가짜 문서"}}})
        self.assertTrue(self.broker.entered.wait(1))

    def test_initialize_and_unknown_method_response_are_pure_json(self):
        self.assertEqual(self.wait_for(1)["result"]["serverInfo"], {"name": "company-computer-use", "version": VERSION})
        self.input.send({"jsonrpc": "2.0", "id": 3, "method": "unknown"})
        self.assertEqual(self.wait_for(3)["error"]["code"], -32601)

    def test_non_jsonrpc_message_is_rejected(self):
        self.input.send({"id": 5, "method": "tools/call", "params": {"name": "computer_stop"}})
        self.assertEqual(self.wait_for(5)["error"]["code"], -32600)

    def test_stop_bypasses_blocked_begin_and_cancels_queued_actions(self):
        self.begin()
        self.input.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "click", "arguments": {}}})
        self.input.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "computer_stop", "arguments": {}}})
        self.assertTrue(self.wait_for(4)["result"]["structuredContent"]["stopped"])
        self.assertTrue(self.wait_for(2)["result"]["isError"])
        self.assertEqual(self.wait_for(3)["error"]["code"], -32800)
        self.assertFalse(self.lease.held)

    def test_cancel_notification_interrupts_current_consent(self):
        self.begin()
        self.input.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
        self.assertTrue(self.wait_for(2)["result"]["isError"])
        self.assertFalse(self.lease.held)

    def test_unknown_cancel_does_not_poison_later_request_id(self):
        self.input.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
        self.input.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "computer_status"}})
        self.assertFalse(self.wait_for(3)["result"]["isError"])
        self.assertEqual(self.server.cancelled, set())

    def test_eof_stops_blocking_consent_and_releases_lease(self):
        self.begin()
        self.input.eof()
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertTrue(self.broker.closed.is_set())
        self.assertFalse(self.lease.held)

    def test_eof_waits_for_daemon_emergency_stop_to_persist_final_state(self):
        consent_return = threading.Event()
        self.addCleanup(consent_return.set)
        def controlled_consent(*args):
            self.broker.entered.set()
            consent_return.wait(5)
            return False
        self.broker.confirm = controlled_consent
        self.begin()
        runtime = self.manager.session
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        recording_threads = []
        original = runtime._record_state
        def delayed_record():
            recording_threads.append(threading.get_ident())
            entered.set()
            release.wait(5)
            original()
        runtime._record_state = delayed_record
        emergency = threading.Thread(target=lambda: runtime.stop("모의 긴급 중지"), daemon=True)
        emergency.start()
        self.assertTrue(entered.wait(1))
        try:
            # stop_event intentionally precedes cleanup ownership. Hold the
            # broker until this daemon owns cleanup rather than letting the
            # awakened begin worker nondeterministically win that race.
            self.assertEqual(recording_threads, [emergency.ident])
            consent_return.set()
            self.assertTrue(self.wait_for(2)["result"]["isError"])
            self.assertTrue(runtime.stop_event.is_set())
            before = time.monotonic()
            runtime.stop("동시 중지 요청")
            self.assertLess(time.monotonic() - before, .2)
            self.assertFalse(runtime.wait_stopped(.01))
            self.input.eof()
            self.worker.join(.05)
            self.assertTrue(self.worker.is_alive(), "EOF returned before the final stop record was written")
        finally:
            release.set()
            emergency.join(2)
            self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertTrue(runtime.wait_stopped(.01))
        record = json.loads((runtime.run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "stopped")
        self.assertFalse(self.lease.held)

    def test_eof_also_waits_when_request_worker_owns_stop_cleanup(self):
        consent_return = threading.Event()
        self.addCleanup(consent_return.set)
        def controlled_consent(*args):
            self.broker.entered.set()
            consent_return.wait(5)
            return False
        self.broker.confirm = controlled_consent
        self.begin()
        runtime = self.manager.session
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        recording_threads = []
        original = runtime._record_state
        def delayed_record():
            recording_threads.append(threading.get_ident())
            entered.set()
            release.wait(5)
            original()
        runtime._record_state = delayed_record
        consent_return.set()  # Rejection makes the request worker own cleanup.
        self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(recording_threads, [self.server.worker.ident])
            self.input.eof()
            self.worker.join(.05)
            self.assertTrue(self.worker.is_alive())
        finally:
            release.set()
            self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertTrue(runtime.wait_stopped(.01))
        self.assertEqual(json.loads((runtime.run_dir / "session.json").read_text(encoding="utf-8"))["state"], "stopped")
        self.assertFalse(self.lease.held)


if __name__ == "__main__":
    unittest.main()
