"""Exact shared-host scope through Guard/runtime; no desktop input."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from hosted_windows import discover
from session_runtime import HostedWindowProbe, SessionRuntime, manifest_for
from test_hosted_windows import FakeNative, APP, HOST
from test_guard_performance import LegacyTransport
from test_server import config_at, FakeBroker, FakeLease, FakeEmergency, FakeTransport
from vendor.guard import Guard, GuardError, discover_allowed_windows, validate_arguments


class HostedGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.native = FakeNative()
        self.native_patch = mock.patch('hosted_windows.NativeWindows', return_value=self.native)
        self.native_patch.start(); self.addCleanup(self.native_patch.stop)
        self.record, = discover([APP], process_resolver=self.native.process)
        self.policy = {'driver': str(Path(self.temp.name)/'driver.exe'), 'run_dir': self.temp.name,
            'allowed_apps': [APP], 'mode': 'uia', 'approval_mode': 'run', 'max_actions': 20,
            'hosted_windows': [self.record]}
        self.transport = LegacyTransport()
        self.guard = Guard(self.policy, transport=self.transport, process_resolver=self.native.process,
            window_resolver=lambda hwnd: self.native.owner(hwnd)[0])
        self.target = {'pid': 100, 'window_id': 10}

    def test_exact_frame_maps_logical_app_without_allowing_host_process_or_other_frames(self):
        self.assertEqual(self.guard.target_executable(self.target), APP.lower())
        validate_arguments('get_window_state', self.target, self.policy, self.native.process, lambda hwnd: 100)
        with self.assertRaises(GuardError):
            validate_arguments('get_window_state', {'pid': 100, 'window_id': 99}, self.policy, self.native.process, lambda hwnd: 100)
        with self.assertRaises(GuardError):
            validate_arguments('get_window_state', {'pid': 999, 'window_id': 10}, self.policy, self.native.process, lambda hwnd: 100)
        self.assertNotIn(HOST, self.guard.policy['allowed_apps'])

    def test_hosted_discovery_never_reads_an_ungranted_sibling_title(self):
        seen = []
        def metadata(hwnd):
            seen.append(hwnd)
            return {'title': 'Approved calculator', 'is_on_screen': True}
        rows = discover_allowed_windows(self.policy, {}, self.native.process, lambda hwnd: 100,
            handles_provider=lambda: [10, 99], metadata_provider=metadata)
        self.assertEqual(seen, [10])
        self.assertEqual([(row['window_id'], row['exe']) for row in rows], [(10, APP.lower())])

    def test_stale_frame_does_not_hide_other_approved_frames_in_shared_host(self):
        other = {**self.record, 'window_id': 20, 'app_window_id': 21}
        policy = {**self.policy, 'hosted_windows': [self.record, other]}
        with mock.patch('hosted_windows.validate', side_effect=lambda record, *args, **kwargs: record['window_id'] == 20):
            answer = validate_arguments('list_windows', {'pid': 100}, policy, self.native.process, lambda hwnd: 100)
            self.assertEqual(answer, {'pid': 100})
            seen = []
            rows = discover_allowed_windows(policy, answer, self.native.process, lambda hwnd: 100,
                handles_provider=lambda: [10, 20, 99], metadata_provider=lambda hwnd: seen.append(hwnd) or {'is_on_screen': True})
        self.assertEqual(seen, [20])
        self.assertEqual([row['window_id'] for row in rows], [20])

    def test_changed_package_after_observation_stops_input_before_driver(self):
        self.assertFalse(self.guard.call('get_window_state', self.target).get('isError'))
        self.native.identities[200]['started'] += 1
        answer = self.guard.call('click', {**self.target, 'element_token': 'fresh'})
        self.assertTrue(answer['isError'])
        self.assertEqual(len(self.transport.calls), 1)

    def test_package_change_during_read_suppresses_tree_and_pixels(self):
        def request(method, params):
            self.native.identities[200]['started'] += 1
            return {'structuredContent': {'elements': [{'name': 'Other app secret'}]},
                    'content': [{'type': 'image', 'data': 'Other app pixels', 'mimeType': 'image/png'}]}
        self.transport.driver_request = request
        answer = self.guard.call('get_window_state', self.target)
        self.assertTrue(answer['isError'])
        self.assertNotIn('Other app', json.dumps(answer))
        self.assertEqual(self.guard.observed_targets, set())

    def test_manifest_grants_only_exact_frame_pair_and_keeps_desktop_disabled(self):
        manifest = manifest_for([APP], 4, [self.record])
        self.assertEqual(manifest['resources']['desktop'], {'display': False, 'windows': [self.target]})
        self.assertEqual(manifest['resources']['apps'], [{'executable': APP, 'launch': False, 'windows': 'all'}])
        self.assertNotIn(HOST.lower(), json.dumps(manifest).lower())

    def test_hosted_probe_pins_child_identity_and_only_returns_its_single_frame(self):
        probe = HostedWindowProbe(self.guard, self.target)
        with mock.patch('vendor.guard._win32_window_metadata', return_value={'title': 'Calculator',
                'bounds': {'x': 0, 'y': 0, 'width': 400, 'height': 600}, 'minimized': False, 'is_on_screen': True}):
            snapshot = probe.capture()
        self.assertEqual(snapshot['executable'], APP.lower())
        self.assertEqual([row['window_id'] for row in snapshot['windows']], [10])
        self.native.identities[200]['started'] += 1
        with self.assertRaises(RuntimeError): probe.snapshot()
        probe.close()

    def test_runtime_freezes_proved_frame_pairs_in_new_manifest_only(self):
        config = config_at(self.temp.name, approval='client')
        program = {**config['programs'][0], 'exe': APP}
        runtime = SessionRuntime(config, [program], 'uia', {'instructions': 'Read calculator'},
            broker_factory=lambda *args: FakeBroker(), lease_factory=FakeLease, emergency_factory=FakeEmergency,
            transport_factory=FakeTransport, registry_register=lambda path: None, registry_unregister=lambda path: None)
        self.addCleanup(runtime.stop)
        with mock.patch('vendor.guard.windows_process_exe', side_effect=self.native.process): runtime.start()
        manifest = json.loads((runtime.run_dir/'capabilities.json').read_text('utf-8'))
        self.assertEqual(manifest['resources']['desktop']['windows'], [self.target])
        self.assertEqual(runtime.guard.policy['hosted_windows'], [self.record])
        self.assertEqual(runtime.guard.policy['allowed_apps'], [APP.lower()])

    def test_image_keyboard_focus_uses_validated_package_child(self):
        self.assertEqual(self.guard.image_focus_resolver(self.target), 11)
        self.native.focused = 12
        with self.assertRaises(GuardError): self.guard.image_focus_resolver(self.target)

    def test_owner_read_failure_is_not_proof_that_hosted_window_closed(self):
        probe = HostedWindowProbe(self.guard, self.target)
        self.guard.window_resolver = mock.Mock(side_effect=OSError('owner read unavailable'))
        with mock.patch('session_runtime._hosted_window_exists', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '종료'): probe.snapshot()
        with mock.patch('session_runtime._hosted_window_exists', return_value=False):
            snapshot = probe.snapshot()
        self.assertFalse(snapshot['target_present'])
        self.assertEqual(snapshot['windows'], [])

    def test_internal_runtime_stop_does_not_cancel_caller_request_and_external_cancel_still_stops(self):
        config = config_at(self.temp.name, approval='client')
        for externally_cancelled in (False, True):
            request = threading.Event()
            runtime = SessionRuntime(config, [config['programs'][0]], 'uia', {'instructions': 'Read'},
                broker_factory=lambda *args: FakeBroker(), lease_factory=FakeLease, emergency_factory=FakeEmergency,
                transport_factory=FakeTransport, registry_register=lambda path: None,
                registry_unregister=lambda path: None, cancel_event=request)
            if externally_cancelled:
                request.set()
                with self.assertRaises(RuntimeError): runtime.check_active()
                self.assertTrue(runtime.stop_event.is_set())
            else:
                runtime.stop('Internal exact-window scope renewal')
                self.assertFalse(request.is_set())


if __name__ == '__main__': unittest.main()
