import copy
import unittest

from hosted_windows import APP_CLASS, HOST_CLASS, discover, validate, focus_window


APP = r"C:\Program Files\WindowsApps\Example.Calculator_1_x64__publisher\CalculatorApp.exe"
OTHER = r"C:\Program Files\WindowsApps\Example.Store_1_x64__publisher\StoreApp.exe"
HOST = r"C:\Windows\System32\ApplicationFrameHost.exe"


class FakeNative:
    def __init__(self):
        self.frames = [10]
        self.classes = {10: HOST_CLASS, 11: APP_CLASS, 12: "ApplicationFrameTitleBarWindow"}
        self.owners = {10: (100, 1001), 11: (200, 2001), 12: (100, 1001)}
        self.roots = {10: 10, 11: 10, 12: 10}
        self.shown = {10, 11, 12}
        self.descendants = {10: [11, 12]}
        self.identities = {100: {"started": 111, "package": ""}, 200: {"started": 222, "package": "Example.Calculator_1_x64__publisher"}}
        self.paths = {100: HOST, 200: APP}
        self.focused = 11
        self.protected = False
    def windows(self): return list(self.frames)
    def children(self, hwnd): return list(self.descendants.get(hwnd, []))
    def class_name(self, hwnd): return self.classes[hwnd]
    def owner(self, hwnd): return self.owners[hwnd]
    def root(self, hwnd): return self.roots[hwnd]
    def visible(self, hwnd): return hwnd in self.shown
    def identity(self, pid): return copy.deepcopy(self.identities[pid])
    def trusted_host_exe(self): return HOST
    def process(self, pid): return self.paths[pid]
    def focus(self, thread): return self.focused if thread == 2001 else 0
    def password(self, hwnd): return self.protected


class HostedWindowsTests(unittest.TestCase):
    def setUp(self): self.native = FakeNative()
    def discover(self, allowed=(APP,)):
        return discover(allowed, native=self.native, process_resolver=self.native.process)
    def validate(self, record, allowed=(APP,)):
        return validate(record, allowed, native=self.native, process_resolver=self.native.process)

    def test_exact_host_frame_binds_registered_package_child_and_logical_executable(self):
        record, = self.discover()
        self.assertEqual((record["pid"], record["window_id"]), (100, 10))
        self.assertEqual((record["app_pid"], record["app_window_id"]), (200, 11))
        self.assertEqual(record["exe"], APP.lower())
        self.assertEqual(record["host_exe"], HOST.lower())
        self.assertTrue(self.validate(record))
        self.assertFalse(self.validate(record, (OTHER,)))

    def test_unregistered_child_and_host_executable_alone_grant_nothing(self):
        self.assertEqual(self.discover((OTHER,)), [])
        self.assertEqual(self.discover((HOST,)), [])

    def test_host_basename_spoof_nonpackaged_child_or_same_host_pid_rejected(self):
        for change in (
            lambda: self.native.paths.update({100: r"C:\Users\example\ApplicationFrameHost.exe"}),
            lambda: self.native.identities[200].update(package=""),
            lambda: self.native.owners.update({11: (100, 1001)}),
            lambda: self.native.classes.update({10: "UntrustedFrame"}),
            lambda: self.native.classes.update({11: "UntrustedChild"}),
        ):
            self.native = FakeNative(); change()
            self.assertEqual(self.discover(), [])

    def test_two_visible_core_windows_refuse_even_when_both_apps_are_registered(self):
        self.native.descendants[10].append(13)
        self.native.classes[13] = APP_CLASS
        self.native.shown.add(13)
        self.native.owners[13] = (300, 3001)
        self.native.roots[13] = 10
        self.native.paths[300] = OTHER
        self.native.identities[300] = {"started": 333, "package": "Example.Store_1_x64__publisher"}
        self.assertEqual(self.discover((APP, OTHER)), [])

    def test_pid_reuse_hwnd_reparent_replacement_threads_and_package_change_invalidate(self):
        for change in (
            lambda: self.native.identities[100].update(started=999),
            lambda: self.native.identities[200].update(started=999),
            lambda: self.native.identities[200].update(package="Another.Package"),
            lambda: self.native.roots.update({11: 99}),
            lambda: self.native.owners.update({10: (100, 999)}),
            lambda: self.native.owners.update({11: (200, 999)}),
            lambda: self.native.paths.update({200: OTHER}),
            lambda: self.native.shown.remove(11),
            lambda: self.native.classes.update({11: "ChangedClass"}),
        ):
            self.native = FakeNative(); record, = self.discover(); change()
            self.assertFalse(self.validate(record))

    def test_identity_or_child_set_changing_during_observation_is_rejected(self):
        original = self.native.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            value = original(pid)
            if calls > 2: value["started"] += 1
            return value
        self.native.identity = identity
        self.assertEqual(self.discover(), [])

    def test_wrong_session_access_denied_fails_closed_without_metadata_leak(self):
        def deny(pid): raise RuntimeError("different Windows user session")
        self.assertEqual(discover([APP], native=self.native, process_resolver=deny), [])
        record, = self.discover()
        self.assertFalse(validate(record, [APP], native=self.native, process_resolver=deny))

    def test_record_schema_and_boolean_ids_are_not_trusted(self):
        record, = self.discover()
        for changed in ({**record, "pid": True}, {**record, "window_id": 0},
                        {**record, "untrusted": "field"}, {"pid": 100, "window_id": 10}, None):
            self.assertFalse(self.validate(changed))

    def test_keyboard_focus_is_the_registered_child_not_shared_host_or_other_app(self):
        record, = self.discover()
        def read(): return focus_window(record, [APP], native=self.native, process_resolver=self.native.process)
        self.assertEqual(read(), 11)
        for focus in (0, 10, 12):
            self.native.focused = focus
            with self.assertRaises(RuntimeError): read()
        self.native.focused = 11; self.native.protected = True
        with self.assertRaisesRegex(RuntimeError, "비밀번호"): read()

    def test_focus_change_during_inspection_never_authorizes_keyboard_input(self):
        record, = self.discover()
        calls = 0
        def focus(thread):
            nonlocal calls
            calls += 1
            return 11 if calls == 1 else 12
        self.native.focus = focus
        with self.assertRaises(RuntimeError):
            focus_window(record, [APP], native=self.native, process_resolver=self.native.process)


if __name__ == "__main__": unittest.main()
