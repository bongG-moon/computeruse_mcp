"""Closure evidence, dialog ownership, and retained Win32 handle failure paths."""
import copy
import ctypes
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from closing import ClosureError, ClosureManager, NativeClosureProbe, ProbeError, TRANSIENT_WINDOW_READ_ERRORS
from vendor.guard import check_app


TARGET = {"pid": 42, "window_id": 100}
EXE = r"C:\Tools\Example.exe"


def window(hwnd=100, owner=0, root=None, **kwargs):
    return {"window_id": hwnd, "pid": 42, "owner_window_id": owner,
            "root_owner_window_id": hwnd if root is None else root, "title": "PRIVATE TITLE",
            "class_name": "Window", "visible": True, "enabled": True, "minimized": False, **kwargs}


def state(*rows, exited=False, creation_time=123):
    return {"process_exited": exited, "target_present": any(row["window_id"] == 100 for row in rows),
            "windows": list(rows), "creation_time": creation_time, "executable": EXE}


class Runtime:
    def __init__(self):
        self.id = "session-one"
        self.stop_event = threading.Event()
        self.guard = SimpleNamespace(policy={"allowed_apps": [check_app(EXE)]},
            process_resolver=lambda pid: EXE, window_resolver=lambda hwnd: 42)

    def check_active(self):
        if self.stop_event.is_set():
            raise RuntimeError("stopped")


class Probe:
    def __init__(self, initial, states=()):
        self.initial, self.states = initial, list(states)
        self.closed, self.reads = False, 0

    def capture(self):
        if isinstance(self.initial, Exception):
            raise self.initial
        return copy.deepcopy(self.initial)

    def snapshot(self):
        self.reads += 1
        value = self.states.pop(0) if len(self.states) > 1 else self.states[0] if self.states else self.initial
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def close(self):
        self.closed = True


class ClosingTests(unittest.TestCase):
    def hosted_runtime(self):
        runtime = Runtime()
        record = {"pid": 42, "window_id": 100, "exe": EXE, "app_pid": 43,
                  "app_window_id": 101, "app_started": 456, "host_started": 123}
        runtime.guard.process_resolver = mock.Mock(return_value=r"C:\Windows\System32\ApplicationFrameHost.exe")
        runtime.guard.target_executable = mock.Mock(return_value=EXE)
        runtime.guard.hosted_target = mock.Mock(side_effect=lambda target: copy.deepcopy(record))
        return runtime, record

    def test_hosted_closure_uses_exact_runtime_probe_and_only_window_scope(self):
        runtime, _ = self.hosted_runtime()
        probe = Probe(state(window()), [state()])
        runtime.create_transition_probe = mock.Mock(return_value=probe)
        manager = ClosureManager(runtime)
        self.addCleanup(manager.close)
        with self.assertRaises(ClosureError) as raised:
            manager.prepare(TARGET, "process")
        self.assertEqual(raised.exception.code, "hosted_window_scope_required")
        runtime.create_transition_probe.assert_not_called()
        ticket = manager.prepare(TARGET)
        runtime.create_transition_probe.assert_called_once_with(TARGET)
        result = manager.verify(ticket["close_id"], 0)
        self.assertTrue(result["task_verified"])
        self.assertTrue(result["window_closed"])
        self.assertFalse(result["process_exited"])
        runtime.guard.process_resolver.assert_not_called()

    def test_hosted_child_replacement_during_prepare_rejects_ticket(self):
        runtime, record = self.hosted_runtime()
        runtime.guard.hosted_target.side_effect = [record, {**record, "app_started": 789}]
        probe = Probe(state(window()))
        manager = ClosureManager(runtime, lambda pid, hwnd: probe)
        self.addCleanup(manager.close)
        with self.assertRaises(ClosureError) as raised:
            manager.prepare(TARGET)
        self.assertEqual(raised.exception.code, "target_changed_during_prepare")
        self.assertTrue(probe.closed)
        self.assertEqual(manager.tickets, {})

    def test_hosted_child_identity_failure_never_reports_closed(self):
        runtime, _ = self.hosted_runtime()
        probe = Probe(state(window()), [ProbeError("hosted_child_changed")])
        manager = ClosureManager(runtime, lambda pid, hwnd: probe)
        self.addCleanup(manager.close)
        ticket = manager.prepare(TARGET)
        result = manager.verify(ticket["close_id"], 0)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "hosted_child_changed")

    def manager(self, initial=None, states=(), scope="window"):
        runtime = Runtime()
        probe = Probe(initial or state(window()), states)
        manager = ClosureManager(runtime, lambda pid, hwnd: probe)
        self.addCleanup(manager.close)
        ticket = manager.prepare(TARGET, scope)
        return manager, ticket["close_id"], probe, runtime

    def test_closed_original_process_is_definitive_for_both_scopes(self):
        for scope in ("window", "process"):
            manager, cid, _, _ = self.manager(states=[state(exited=True)], scope=scope)
            result = manager.verify(cid, 0)
            self.assertTrue(result["task_verified"])
            self.assertTrue(result["process_exited"])
            self.assertTrue(result["window_closed"])

    def test_minimized_and_hidden_windows_are_not_closed(self):
        for row in (window(minimized=True), window(visible=False), window(title="")):
            manager, cid, _, _ = self.manager(states=[state(row)])
            result = manager.verify(cid, 0)
            self.assertEqual(result["status"], "pending")
            self.assertFalse(result["window_closed"])

    def test_save_popup_and_cancel_do_not_count_as_closure(self):
        popup = window(101, owner=100, root=100, class_name="#32770")
        manager, cid, _, _ = self.manager(states=[state(window(enabled=False), popup), state(window())])
        result = manager.verify(cid, 0)
        self.assertEqual(result["status"], "needs_dialog")
        self.assertEqual(result["dialog_candidates"][0]["window_id"], 101)
        self.assertFalse(result["task_verified"])
        cancelled = manager.verify(cid, 0)
        self.assertEqual(cancelled["status"], "pending")
        self.assertFalse(cancelled["window_closed"])

    def test_owned_popup_remaining_after_parent_closes_blocks_completion(self):
        manager, cid, _, _ = self.manager(states=[state(window(101, owner=100, root=100))])
        result = manager.verify(cid, 0)
        self.assertTrue(result["window_closed"])
        self.assertEqual(result["status"], "needs_dialog")
        self.assertFalse(result["task_verified"])

    def test_owner_chain_uses_baseline_when_intermediate_owner_disappears(self):
        initial = state(window(), window(101, owner=100), window(102, owner=101))
        manager, cid, _, _ = self.manager(initial, [state(window(102, owner=101))])
        self.assertEqual(manager.verify(cid, 0)["dialog_candidates"][0]["reason"], "owned_by_target")

    def test_reparented_baseline_popup_still_blocks_window_completion(self):
        for initial in (state(window(), window(200, owner=100, root=100)),
                        state(window(), window(150, owner=100), window(200, owner=150))):
            manager, cid, _, _ = self.manager(initial, [state(window(200, owner=0, root=200))])
            result = manager.verify(cid, 0)
            self.assertEqual(result["status"], "needs_dialog")
            self.assertFalse(result["task_verified"])
            self.assertEqual(result["dialog_candidates"][0]["reason"], "owned_by_target")

    def test_observed_owned_sibling_remains_related_after_later_reparent(self):
        manager, cid, _, _ = self.manager(state(window(), window(200)), [
            state(window(), window(200, owner=100)), state(window(200))])
        self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")
        self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")

    def test_new_same_process_unowned_window_is_suspicious_even_hidden(self):
        manager, cid, _, _ = self.manager(states=[state(window(201, visible=False))])
        result = manager.verify(cid, 0)
        self.assertEqual(result["status"], "needs_dialog")
        self.assertEqual(result["dialog_candidates"][0]["reason"], "new_same_process_window")

    def test_inactive_windows_input_method_helpers_are_reported_not_dialogs(self):
        for class_name in ("IME", "MSCTFIME UI"):
            helper = window(201, owner=100, class_name=class_name, visible=False, enabled=False)
            manager, cid, _, _ = self.manager(states=[state(window(), helper), state(helper)])
            pending = manager.verify(cid, 0)
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(pending["dialog_candidates"], [])
            self.assertEqual(pending["background_helpers"][0]["window_id"], 201)
            result = manager.verify(cid, 0)
            self.assertTrue(result["task_verified"])
            self.assertEqual(result["remaining_windows"], [helper])
            self.assertFalse(result["process_exited"])

    def test_unknown_or_interactive_input_method_windows_still_block(self):
        for change in ({"class_name": "#32770"}, {"visible": True}, {"enabled": True}, {"minimized": True},
                       {"enabled": None}, {"minimized": None}):
            attrs = {"class_name": "IME", "visible": False, "enabled": False, "minimized": False, **change}
            manager, cid, _, _ = self.manager(states=[state(window(201, owner=100, **attrs))])
            result = manager.verify(cid, 0)
            self.assertEqual(result["status"], "needs_dialog")
            self.assertFalse(result["task_verified"])
            self.assertFalse(result["background_helpers"])

    def test_input_method_class_original_target_is_never_ignored(self):
        target = window(class_name="IME", visible=False, enabled=False)
        manager, cid, _, _ = self.manager(state(target), [state(target)])
        result = manager.verify(cid, 0)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["window_closed"])
        self.assertFalse(result["background_helpers"])

    def test_previously_interactive_hwnd_cannot_become_ignored_helper(self):
        for first in (window(201, owner=100), window(201, owner=100, class_name="IME", visible=False, enabled=False, minimized=True)):
            helper = window(201, owner=100, class_name="IME", visible=False, enabled=False)
            manager, cid, _, _ = self.manager(states=[state(window(), first), state(helper)])
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")
            manager, cid, _, _ = self.manager(state(window(), first), [state(helper)])
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")

    def test_previously_hidden_ordinary_popup_cannot_become_ignored_helper(self):
        for class_name in ("#32770", "CustomDialog", None):
            previous = window(201, owner=100, class_name=class_name, visible=False, enabled=False)
            helper = window(201, owner=100, class_name="IME", visible=False, enabled=False)
            manager, cid, _, _ = self.manager(state(window(), previous), [state(helper)])
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")
            manager, cid, _, _ = self.manager(states=[state(window(), previous), state(helper)])
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")
            self.assertEqual(manager.verify(cid, 0)["status"], "needs_dialog")

    def test_existing_unrelated_sibling_does_not_block_window_scope(self):
        manager, cid, _, _ = self.manager(state(window(), window(201)), [state(window(201))])
        result = manager.verify(cid, 0)
        self.assertTrue(result["task_verified"])
        self.assertFalse(result["process_exited"])
        self.assertEqual(len(result["remaining_windows"]), 1)

    def test_process_scope_requires_process_exit_even_without_windows(self):
        manager, cid, _, _ = self.manager(states=[state()], scope="process")
        result = manager.verify(cid, 0)
        self.assertFalse(result["task_verified"])
        self.assertTrue(result["window_closed"])
        self.assertEqual(result["diagnostic"]["code"], "process_still_running")

    def test_read_errors_and_reused_process_identity_never_verify(self):
        for snapshot in (ProbeError("target_handle_reused"), ProbeError("window_enumeration_failed"),
                         OSError("PRIVATE failure details"), state(creation_time=999)):
            manager, cid, _, _ = self.manager(states=[snapshot])
            result = manager.verify(cid, 0)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["task_verified"])
            self.assertIsNone(result["window_closed"])
            self.assertNotIn("PRIVATE failure", str(result))

    def test_incomplete_or_wrong_pid_inventory_is_unknown(self):
        for snapshot in ({**state(), "target_present": True}, state(window(pid=999)),
                         state(window(), window()), {**state(), "windows": None}):
            manager, cid, _, _ = self.manager(states=[snapshot])
            self.assertEqual(manager.verify(cid, 0)["status"], "unknown")

    def test_polling_waits_for_exit_and_is_bounded(self):
        manager, cid, probe, _ = self.manager(states=[state(window()), state(exited=True)])
        manager.POLL_SECONDS = .001
        result = manager.verify(cid, 100)
        self.assertTrue(result["task_verified"])
        self.assertEqual((result["poll_count"], probe.reads), (2, 2))
        manager, cid, probe, _ = self.manager()
        manager.POLL_SECONDS = .001
        started = time.monotonic()
        self.assertEqual(manager.verify(cid, 10)["status"], "pending")
        self.assertLess(time.monotonic() - started, .5)
        self.assertGreater(probe.reads, 1)

    def test_transient_dialog_during_shutdown_settles_to_verified(self):
        manager, cid, probe, _ = self.manager(states=[state(window(), window(201, owner=100)), state(exited=True)])
        manager.POLL_SECONDS = .001
        result = manager.verify(cid, 100)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(probe.reads, 2)

    def test_stable_dialog_remains_needs_dialog_after_bounded_polling(self):
        manager, cid, probe, _ = self.manager(states=[state(window(), window(201, owner=100))])
        manager.POLL_SECONDS = .001
        started = time.monotonic()
        result = manager.verify(cid, 10)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "needs_dialog")
        self.assertFalse(result["task_verified"])
        self.assertGreater(probe.reads, 1)
        self.assertLess(elapsed, .5)

    def test_transient_window_read_errors_recover_to_original_process_exit(self):
        for code in TRANSIENT_WINDOW_READ_ERRORS:
            manager, cid, probe, _ = self.manager(states=[ProbeError(code), state(exited=True)])
            manager.POLL_SECONDS = .001
            result = manager.verify(cid, 100)
            self.assertTrue(result["task_verified"])
            self.assertTrue(result["process_exited"])
            self.assertEqual(result["read_error_count"], 1)
            self.assertEqual(result["last_read_error"], code)
            self.assertEqual(probe.reads, 2)

    def test_persistent_transient_read_error_stays_unknown_at_deadline(self):
        manager, cid, probe, _ = self.manager(states=[ProbeError("window_class_read_failed")])
        manager.POLL_SECONDS = .001
        started = time.monotonic()
        result = manager.verify(cid, 10)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["task_verified"])
        self.assertIsNone(result["window_closed"])
        self.assertGreater(probe.reads, 1)
        self.assertEqual(result["read_error_count"], probe.reads)
        self.assertEqual(result["last_read_error"], "window_class_read_failed")
        self.assertLess(time.monotonic() - started, .5)

    def test_identity_and_process_wait_failures_are_never_retried(self):
        for code in ("target_handle_reused", "process_identity_mismatch", "process_wait_failed",
                     "invalid_native_snapshot", "inconsistent_native_snapshot", "probe_closed"):
            manager, cid, probe, _ = self.manager(states=[ProbeError(code), state(exited=True)])
            result = manager.verify(cid, 100)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["task_verified"])
            self.assertEqual(result["last_read_error"], code)
            self.assertEqual(probe.reads, 1)

    def test_recovered_read_does_not_hide_prior_error_or_guess_closed(self):
        manager, cid, probe, _ = self.manager(states=[ProbeError("window_class_read_failed"), state(window())])
        manager.POLL_SECONDS = .001
        result = manager.verify(cid, 10)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["read_error_count"], 1)
        self.assertEqual(result["last_read_error"], "window_class_read_failed")

    def test_stop_before_and_during_successful_read_never_returns_success(self):
        manager, cid, probe, runtime = self.manager()
        runtime.stop_event.set()
        with self.assertRaises(RuntimeError):
            manager.verify(cid, 0)
        self.assertEqual(probe.reads, 0)
        runtime.stop_event.clear()
        def stop_then_exit():
            runtime.stop_event.set()
            return state(exited=True)
        probe.states = [stop_then_exit]
        with self.assertRaises(RuntimeError):
            manager.verify(cid, 0)

    def test_close_interrupts_polling_and_cleans_without_poll_lock(self):
        manager, cid, probe, runtime = self.manager()
        outcomes = []
        def verify():
            try:
                outcomes.append(manager.verify(cid, 10000))
            except Exception as exc:
                outcomes.append(exc)
        thread = threading.Thread(target=verify)
        thread.start()
        deadline = time.monotonic() + 1
        while not probe.reads and time.monotonic() < deadline:
            time.sleep(.001)
        started = time.monotonic()
        runtime.stop_event.set()
        manager.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(probe.closed)
        self.assertIsInstance(outcomes[0], Exception)

    def test_ticket_session_binding_and_unknown_ticket(self):
        manager, cid, _, runtime = self.manager()
        with self.assertRaises(ClosureError) as err:
            manager.verify("another-ticket", 0)
        self.assertEqual(err.exception.code, "closure_ticket_not_found")
        runtime.id = "new-session"
        with self.assertRaises(ClosureError):
            manager.verify(cid, 0)

    def test_ticket_limit_refuses_before_open_and_close_releases_all(self):
        runtime, probes = Runtime(), []
        def factory(pid, hwnd):
            probe = Probe(state(window()))
            probes.append(probe)
            return probe
        manager = ClosureManager(runtime, factory)
        for _ in range(32):
            manager.prepare(TARGET)
        with self.assertRaises(ClosureError) as err:
            manager.prepare(TARGET)
        self.assertEqual(err.exception.code, "closure_ticket_limit")
        self.assertEqual(len(probes), 32)
        manager.close()
        self.assertTrue(all(probe.closed for probe in probes))
        self.assertFalse(manager.tickets)

    def test_close_attempts_all_handles_and_can_retry_failed_cleanup(self):
        runtime, probes = Runtime(), []
        def factory(pid, hwnd):
            probe = Probe(state(window()))
            probes.append(probe)
            return probe
        manager = ClosureManager(runtime, factory)
        manager.prepare(TARGET)
        manager.prepare(TARGET)
        probes[0].close = mock.Mock(side_effect=[ProbeError("process_handle_close_failed"), None])
        with self.assertRaises(ProbeError):
            manager.close()
        self.assertTrue(probes[1].closed)
        self.assertEqual(len(manager.tickets), 1)
        manager.close()
        self.assertFalse(manager.tickets)
        self.assertEqual(probes[0].close.call_count, 2)

    def test_prepare_checks_allowlist_window_pid_and_retained_executable(self):
        for mutation in (lambda rt: rt.guard.policy.update(allowed_apps=[]),
                         lambda rt: setattr(rt.guard, "window_resolver", lambda hwnd: 99)):
            runtime = Runtime()
            mutation(runtime)
            factory = mock.Mock()
            with self.assertRaises(ClosureError):
                ClosureManager(runtime, factory).prepare(TARGET)
            factory.assert_not_called()
        runtime = Runtime()
        probe = Probe({**state(window()), "executable": r"C:\Tools\Other.exe"})
        with self.assertRaises(ClosureError):
            ClosureManager(runtime, lambda pid, hwnd: probe).prepare(TARGET)
        self.assertTrue(probe.closed)

    def test_prepare_failure_releases_handle_without_creating_ticket(self):
        for initial in (ProbeError("process_open_failed"), state(exited=True), state()):
            runtime, probe = Runtime(), Probe(initial)
            manager = ClosureManager(runtime, lambda pid, hwnd: probe)
            with self.assertRaises(ClosureError):
                manager.prepare(TARGET)
            self.assertTrue(probe.closed)
            self.assertFalse(manager.tickets)

    def test_invalid_arguments_never_open_or_read(self):
        runtime, factory = Runtime(), mock.Mock()
        manager = ClosureManager(runtime, factory)
        for target in ({"pid": True, "window_id": 1}, {"pid": 42}, {**TARGET, "force": True}):
            with self.assertRaises(ClosureError):
                manager.prepare(target)
        with self.assertRaises(ClosureError):
            manager.prepare(TARGET, "all_apps")
        factory.assert_not_called()
        manager, cid, probe, _ = self.manager()
        for timeout in (-1, 10001, True, 1.5):
            with self.assertRaises(ClosureError):
                manager.verify(cid, timeout)
        self.assertEqual(probe.reads, 0)


class WinFunction:
    def __init__(self, result=None, callback=None):
        self.result, self.callback = result, callback
    def __call__(self, *args):
        return self.callback(*args) if self.callback else self.result


class NativeProbeTests(unittest.TestCase):
    def make_probe(self):
        def pid(hwnd, ptr):
            ptr._obj.value = 42
            return 7
        def creation(handle, created, *rest):
            created._obj.dwLowDateTime = 123
            return True
        def executable(handle, flags, buffer, size):
            buffer.value = EXE
            return True
        def title(hwnd, buffer, size):
            buffer.value = "Synthetic"
            return 9
        def classname(hwnd, buffer, size):
            buffer.value = "Window"
            return 6
        kernel = SimpleNamespace(OpenProcess=WinFunction(555), CloseHandle=WinFunction(True),
            WaitForSingleObject=WinFunction(258), GetProcessId=WinFunction(42),
            GetProcessTimes=WinFunction(callback=creation), QueryFullProcessImageNameW=WinFunction(callback=executable))
        user = SimpleNamespace(IsWindow=WinFunction(True), GetWindowThreadProcessId=WinFunction(callback=pid),
            EnumWindows=WinFunction(callback=lambda cb, arg: cb(100, arg)), GetWindow=WinFunction(0),
            GetAncestor=WinFunction(100), GetWindowTextW=WinFunction(callback=title),
            GetClassNameW=WinFunction(callback=classname), IsWindowVisible=WinFunction(False),
            IsWindowEnabled=WinFunction(True), IsIconic=WinFunction(True))
        probe = NativeClosureProbe(42, 100, kernel=kernel, user=user)
        self.addCleanup(probe.close)
        return probe, kernel, user

    def capture(self, probe):
        with mock.patch("closing._same_windows_user_session", return_value=True):
            return probe.capture()

    def test_hidden_window_is_enumerated_and_handle_retained_after_capture(self):
        probe, kernel, _ = self.make_probe()
        result = self.capture(probe)
        self.assertEqual(probe.handle, 555)
        self.assertEqual(result["windows"][0]["thread_id"], 7)
        self.assertFalse(result["windows"][0]["visible"])
        self.assertTrue(result["windows"][0]["minimized"])
        kernel.WaitForSingleObject.result = 0
        self.assertTrue(probe.snapshot()["process_exited"])

    def test_open_failure_and_user_session_mismatch_are_rejected(self):
        probe, kernel, _ = self.make_probe()
        kernel.OpenProcess.result = 0
        with self.assertRaises(ProbeError):
            self.capture(probe)
        self.assertIsNone(probe.handle)
        kernel.OpenProcess.result = 555
        with mock.patch("closing._same_windows_user_session", return_value=False):
            with self.assertRaises(ProbeError):
                probe.capture()
        self.assertIsNone(probe.handle)

    def test_wait_failure_is_not_exit(self):
        probe, kernel, _ = self.make_probe()
        self.capture(probe)
        kernel.WaitForSingleObject.result = 0xFFFFFFFF
        with self.assertRaises(ProbeError) as err:
            probe.snapshot()
        self.assertEqual(err.exception.code, "process_wait_failed")

    def test_failed_enumeration_is_not_an_empty_window_list(self):
        probe, _, user = self.make_probe()
        self.capture(probe)
        user.EnumWindows.callback = None
        user.EnumWindows.result = False
        with self.assertRaises(ProbeError) as err:
            probe.snapshot()
        self.assertEqual(err.exception.code, "window_enumeration_failed")

    def test_disappeared_unrelated_desktop_window_does_not_break_inventory(self):
        probe, _, user = self.make_probe()
        self.capture(probe)
        user.EnumWindows.callback = lambda cb, arg: cb(200, arg) and cb(100, arg)
        user.IsWindow.callback = lambda hwnd: hwnd != 200
        result = probe.snapshot()
        self.assertEqual([row["window_id"] for row in result["windows"]], [100])
        self.assertTrue(result["target_present"])

    def test_failed_close_handle_is_retained_for_cleanup_retry(self):
        probe, kernel, _ = self.make_probe()
        self.capture(probe)
        kernel.CloseHandle.result = False
        with self.assertRaises(ProbeError):
            probe.close()
        self.assertEqual(probe.handle, 555)
        kernel.CloseHandle.result = True
        probe.close()
        self.assertIsNone(probe.handle)

    def test_reused_hwnd_for_another_pid_is_unknown(self):
        probe, _, user = self.make_probe()
        self.capture(probe)
        def another_pid(hwnd, ptr):
            ptr._obj.value = 999
            return 7
        user.GetWindowThreadProcessId.callback = another_pid
        with self.assertRaises(ProbeError) as err:
            probe.snapshot()
        self.assertEqual(err.exception.code, "target_handle_reused")

    def test_retained_process_exit_does_not_consult_reused_hwnd_or_pid(self):
        probe, kernel, user = self.make_probe()
        self.capture(probe)
        kernel.WaitForSingleObject.result = 0
        user.IsWindow.callback = lambda hwnd: (_ for _ in ()).throw(AssertionError("must not read reused hwnd"))
        result = probe.snapshot()
        self.assertTrue(result["process_exited"])
        self.assertFalse(result["target_present"])


if __name__ == "__main__":
    unittest.main()
