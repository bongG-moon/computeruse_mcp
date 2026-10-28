"""Trial binding regressions; native HWND lifetime, synthetic discovery/input.

The Windows test creates only hidden fixture windows. It uses the live Win32
owner resolver, while popup discovery and all workflow input remain test doubles.
"""
import copy
import ctypes
from ctypes import wintypes
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from operations import OperationError
from process_editor import ProcessEditors
from test_workflows import FakeRuntime, ScriptedOperations
from vendor.guard import GuardError, windows_window_pid
from workflows import WorkflowRunner


def target(ref="main", hwnd=10):
    return {"program_id": "editor", "window_ref": ref, "pid": 42,
            "window_id": hwnd, "label": "Fixture " + ref}


def waiting(ref="popup", owner="main"):
    return {"program_id": "editor", "operation": "wait_for_window",
            "window_ref": ref, "owner_ref": owner, "title": "Fixture popup",
            "class_name": "Static", "timeout_ms": 0}


def entry(ref="main"):
    return {"program_id": "editor", "operation": "set_value", "window_ref": ref,
            "selector": {"name": "Fixture field"}, "value": "synthetic"}


class EditorTrialTargetTests(unittest.TestCase):
    def setUp(self):
        self.editors = ProcessEditors(None, None)
        self.validate = mock.Mock()
        self.editors._program = self.validate

    def select(self, programs, steps):
        return self.editors._trial_targets(
            {"draft": SimpleNamespace(programs=programs)}, {"steps": steps})

    def test_recorded_popup_is_not_an_initial_binding_and_draft_is_unchanged(self):
        programs = [target(), target("popup", 20)]
        before = copy.deepcopy(programs)
        selected = self.select(programs, [waiting(), entry("popup")])
        self.assertEqual(selected, [{k: v for k, v in programs[0].items() if k != "label"}])
        self.assertEqual(programs, before)
        self.assertEqual(self.validate.call_args.args[2], {"pid": 42, "window_id": 10})
        self.assertEqual(self.validate.call_count, 1)

    def test_nested_popup_owners_require_only_the_original_root(self):
        programs = [target(), target("menu", 20), target("submenu", 30)]
        selected = self.select(programs,
            [waiting("menu"), waiting("submenu", "menu"), entry("submenu")])
        self.assertEqual([(row["window_ref"], row["window_id"]) for row in selected], [("main", 10)])
        self.assertEqual(self.validate.call_count, 1)

    def test_foreach_leaf_static_target_is_retained(self):
        programs = [target(), target("details", 40)]
        selected = self.select(programs, [{"operation": "foreach", "input": "items",
            "item": "item", "steps": [entry("details")]}])
        self.assertEqual([row["window_ref"] for row in selected], ["details"])

    def test_foreach_leaf_popup_uses_its_owner_without_retaining_recorded_hwnd(self):
        programs = [target(), target("popup", 20)]
        selected = self.select(programs, [waiting(), {"operation": "foreach", "input": "items",
            "item": "item", "steps": [entry("popup")]}])
        self.assertEqual([row["window_id"] for row in selected], [10])

    def test_unused_root_is_not_validated_and_required_named_roots_are_kept(self):
        unused = {**target("unused", 99), "program_id": "unused_program"}
        programs = [unused, target("first", 11), target("second", 12)]
        selected = self.select(programs, [entry("first"), entry("second")])
        self.assertEqual([row["window_id"] for row in selected], [11, 12])
        self.assertEqual([call.args[2]["window_id"] for call in self.validate.call_args_list], [11, 12])

    def test_missing_or_unbound_required_root_fails_before_any_validation(self):
        for programs in ([], [target(hwnd=0)]):
            with self.subTest(programs=programs), self.assertRaises(OperationError) as caught:
                self.select(programs, [waiting(), entry("popup")])
            self.assertEqual(caught.exception.code, "editor_target_unavailable")
        self.validate.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Requires native Windows HWND lifetime")
class NativeTrialWindowLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        self.user.CreateWindowExW.restype = wintypes.HWND
        self.user.DestroyWindow.argtypes = [wintypes.HWND]
        self.user.DestroyWindow.restype = wintypes.BOOL
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL

    def hidden_window(self, owner=0):
        hwnd = self.user.CreateWindowExW(0, "Static", "Fixture popup", 0x80000000,
            0, 0, 1, 1, owner or None, None, None, None)
        self.assertTrue(hwnd, ctypes.get_last_error())
        self.assertFalse(self.user.IsWindowVisible(hwnd))
        self.addCleanup(lambda: self.user.DestroyWindow(hwnd) if self.user.IsWindow(hwnd) else None)
        return int(hwnd)

    def test_closed_recording_hwnd_is_rejected_but_trial_rediscovers_new_popup(self):
        main = self.hidden_window()
        old_popup = self.hidden_window(main)
        current_popup = self.hidden_window(main)
        pid = os.getpid()
        self.assertEqual(windows_window_pid(old_popup), pid)
        self.assertTrue(self.user.DestroyWindow(old_popup))
        with self.assertRaisesRegex(GuardError, "Window no longer exists"):
            windows_window_pid(old_popup)

        runtime = FakeRuntime(self.temp.name)
        runtime.stop_event = threading.Event()
        runtime.guard.window_resolver = windows_window_pid
        checked = []
        def executable(exact):
            checked.append(exact["window_id"])
            if windows_window_pid(exact["window_id"]) != exact["pid"]:
                raise GuardError("Fixture target process changed")
            return runtime.programs[0]["exe"]
        runtime.guard.target_executable = executable
        library = SimpleNamespace(_program=lambda rt, program, exact: executable(exact))
        editors = ProcessEditors(library, None)
        programs = [{**target(hwnd=main), "pid": pid},
                    {**target("popup", old_popup), "pid": pid}]
        task = {"id": "native-lifetime-fixture", "program_ids": ["editor"],
                "variables": {}, "steps": [waiting(), entry("popup")]}
        job = {"runtime": runtime, "draft": SimpleNamespace(programs=programs)}
        runner = WorkflowRunner(self.temp.name)
        engine = ScriptedOperations()

        # Reproduce the old caller's stale explicit target without OS input.
        old_targets = [{key: row[key] for key in ("program_id", "window_ref", "pid", "window_id")}
                       for row in programs]
        with mock.patch("operations.Operations", return_value=engine):
            with self.assertRaisesRegex(GuardError, "Window no longer exists"):
                runner.run(runtime, task, {}, old_targets)
        self.assertEqual(engine.calls, [])

        selected = editors._trial_targets(job, task)
        self.assertEqual([row["window_id"] for row in selected], [main])
        checked.clear()

        # Discovery is deliberately synthetic. Only native handle liveness is
        # real; the fake engine records an action without touching a control.
        rows = [{"pid": pid, "window_id": hwnd, "owner_window_id": owner,
                 "root_owner_window_id": main, "title": title, "class_name": "Static",
                 "thread_id": 1, "visible": True}
                for hwnd, owner, title in ((main, 0, "Fixture root"),
                                          (current_popup, main, "Fixture popup"))]
        state = {"process_exited": False, "target_present": True, "creation_time": 1,
                 "executable": runtime.programs[0]["exe"], "windows": rows}
        probes = []
        def probe(exact):
            self.assertEqual(exact, {"pid": pid, "window_id": main})
            result = SimpleNamespace(capture=lambda: copy.deepcopy(state),
                snapshot=lambda: copy.deepcopy(state), close=mock.Mock())
            probes.append(result)
            return result
        runtime.create_transition_probe = probe
        with mock.patch("operations.Operations", return_value=engine), \
                mock.patch("transient_popup.register_popup"):
            result = runner.run(runtime, task, {}, selected)
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(result["completed_steps"], 2)
        self.assertEqual([call[1] for call in engine.calls], [{"pid": pid, "window_id": current_popup}])
        self.assertNotIn(old_popup, checked)
        self.assertIn(current_popup, checked)
        self.assertTrue(probes)
        for item in probes:
            item.close.assert_called_once()
        self.assertEqual(programs[1]["window_id"], old_popup)


if __name__ == "__main__":
    unittest.main()
