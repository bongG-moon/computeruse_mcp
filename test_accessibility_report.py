"""Diagnostic boundary, local delivery and editor integration; no business input."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from accessibility_report import collect
from local_delivery import filter_response
from operations import OperationError
import test_easy_api as api_fixture
import test_authoring_editor as editor_fixture
import test_artifact_lifecycle as lifecycle_fixture
import artifact_lifecycle as lifecycle


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.target = {'pid': 123, 'window_id': 456}
        self.probe = api_fixture.Probe(self.target, 'fixture.exe')
        self.runtime = SimpleNamespace(run_dir=Path(self.tmp.name), check_active=mock.Mock(),
            guard=SimpleNamespace(policy={}, process_resolver=lambda p: 'fixture.exe', window_resolver=lambda w: 123),
            create_transition_probe=lambda target: self.probe)
        self.allow = mock.patch('vendor.guard.validate_arguments'); self.allow.start(); self.addCleanup(self.allow.stop)
        self.native = mock.patch('accessibility_diagnostics.diagnose', return_value={'status': 'partial', 'uia': {'raw': []}}).start()
        self.driver = mock.patch('inspection.inspect_window', return_value={'structuredContent': {'inspection': {
            'diagnostics': {'counts': {'driver_elements': 3, 'mcp_controls_returned': 1}}}}}).start()
        self.addCleanup(mock.patch.stopall)

    def test_report_keeps_sources_and_never_creates_input_handles(self):
        answer = collect(self.runtime, self.target)
        report = json.loads(Path(answer['report_path']).read_text(encoding='utf-8'))
        self.assertFalse(report['authorizes_input']); self.assertFalse(report['task_verified'])
        self.assertFalse(report['comparison']['atomic'])
        self.assertEqual(report['sources']['provider']['status'], 'partial')
        self.assertEqual(self.driver.call_args.kwargs['observation'], 'uia')
        self.assertTrue(self.driver.call_args.kwargs['diagnostics'])
        self.assertNotIn('target_ref', report)
        self.assertEqual(self.driver.call_count, 1); self.assertEqual(self.native.call_count, 1)

    def test_host_strategy_is_in_local_report_without_extra_readers(self):
        from smartfx_profiles import host_profile
        profile = host_profile('SKhynix.SmartFX.PNTLauncher.exe')
        self.driver.return_value['structuredContent']['inspection']['application_strategy'] = profile
        answer = collect(self.runtime, self.target)
        self.assertEqual(answer['diagnostic_report']['application_strategy'], profile)
        self.assertEqual(self.driver.call_count, 1)
        self.assertEqual(self.native.call_count, 1)
        self.assertNotIn('application_strategy', str(filter_response({'structuredContent': answer})))

    def test_timeout_is_not_empty_and_does_not_repeat_reader(self):
        self.native.side_effect = OperationError('timeout', 'accessibility_provider_timeout')
        answer = collect(self.runtime, self.target)
        source = answer['diagnostic_report']['sources']['provider']
        self.assertEqual(source['status'], 'timeout'); self.assertIsNone(source['count'])
        self.assertEqual(self.native.call_count, 1)

    def test_diagnostic_driver_read_cannot_authorize_keyboard_input(self):
        from vendor.guard import Guard
        from test_guard_performance import LegacyTransport
        self.allow.stop()
        executable = str(Path(self.tmp.name) / 'fixture.exe')
        transport = LegacyTransport()
        guard = Guard({'driver': str(Path(self.tmp.name) / 'driver.exe'),
            'allowed_apps': [executable], 'run_dir': self.tmp.name, 'mode': 'uia',
            'approval_mode': 'run', 'max_actions': 10}, transport=transport,
            process_resolver=lambda pid: executable, window_resolver=lambda hwnd: 123)
        self.runtime.guard = guard
        unrelated = (123, 999)
        guard.observed_targets.add(unrelated)
        def read(*args, **kwargs):
            answer = guard.call('get_window_state', self.target)
            self.assertIn((123, 456), guard.observed_targets)
            guard._native_popup_frame = (123, 456, (0, 0, 10, 10, 1), {})
            return answer
        self.driver.side_effect = read
        collect(self.runtime, self.target)
        self.assertEqual(guard.observed_targets, {unrelated})
        self.assertIsNone(guard._native_popup_frame)
        calls = len(transport.calls)
        result = guard.call('press_key', {**self.target, 'key': 'ENTER'})
        self.assertTrue(result['isError'])
        self.assertIn('Reobserve this exact window', str(result))
        self.assertEqual(len(transport.calls), calls)
        self.assertEqual(guard.action_count, 0)

    def test_failed_diagnostic_also_discards_its_observation_lease(self):
        guard = self.runtime.guard
        guard.observed_targets = {(123, 999)}
        unrelated_frame = (123, 999, (0, 0, 10, 10, 1), {})
        guard._native_popup_frame = unrelated_frame
        def fail(*args, **kwargs):
            guard.observed_targets.add((123, 456))
            raise OperationError('cancelled', 'cancelled')
        self.driver.side_effect = fail
        with self.assertRaises(OperationError): collect(self.runtime, self.target)
        self.assertEqual(guard.observed_targets, {(123, 999)})
        self.assertEqual(guard._native_popup_frame, unrelated_frame)
        self.assertEqual(list(Path(self.tmp.name).glob('**/*.json')), [])

    def test_permission_and_lifetime_failure_stop_other_readers(self):
        for code in ('target_denied', 'accessibility_target_changed', 'cancelled'):
            with self.subTest(code=code):
                self.driver.reset_mock(); self.native.side_effect = OperationError('blocked', code)
                with self.assertRaises(OperationError): collect(self.runtime, self.target)
                self.driver.assert_not_called()
        self.assertEqual(list(Path(self.tmp.name).glob('**/*.json')), [])

    def test_changed_window_is_not_saved_as_same_state(self):
        original = self.probe.snapshot
        count = 0
        def changed():
            nonlocal count
            count += 1
            value = original()
            if count >= 4: value['creation_time'] += 1
            return value
        self.probe.snapshot = changed
        with self.assertRaises(OperationError): collect(self.runtime, self.target)
        self.assertEqual(list(Path(self.tmp.name).glob('**/*.json')), [])

    def test_local_receipt_never_returns_business_report_or_path(self):
        answer = collect(self.runtime, self.target)
        answer['diagnostic_report']['secret_business_value'] = 'PRIVATE-CONTROL-VALUE'
        filtered = filter_response({'structuredContent': answer})
        public = json.dumps(filtered)
        self.assertNotIn('PRIVATE-CONTROL-VALUE', public)
        self.assertNotIn('report_path', public); self.assertNotIn('456', public)
        self.assertEqual(filtered['structuredContent']['diagnostic_id'], answer['diagnostic_id'])
        self.assertEqual(filter_response(filtered), filtered)


class DiagnosticApiTests(unittest.TestCase):
    setUp = api_fixture.EasyApiTests.setUp
    open = api_fixture.EasyApiTests.open

    def test_local_policy_allows_only_explicit_diagnostic_receipt(self):
        opened = self.open(); self.manager.config['data_policy'] = 'local'
        report = {'status': 'ok', 'diagnostic_id': 'a'*32, 'diagnostic_report': {'value': 'PRIVATE'},
                  'report_path': 'private/file.json', 'task_verified': False}
        with mock.patch('accessibility_report.collect', return_value=report) as reader:
            response = self.manager.call('computer_observe', {'window_ref': opened['window_ref'], 'diagnostics': True})
            self.assertFalse(response.get('isError'), response)
            reader.assert_called_once()
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertNotIn('report_path', response['structuredContent'])
        blocked = self.manager.call('computer_observe', {'window_ref': opened['window_ref']})
        self.assertTrue(blocked['isError'])

    def test_diagnostic_cannot_request_images_or_become_input_observation(self):
        opened = self.open()
        with mock.patch('accessibility_report.collect') as reader:
            response = self.manager.call('computer_observe', {'window_ref': opened['window_ref'],
                'diagnostics': True, 'observation': 'visual'})
            self.assertTrue(response['isError']); reader.assert_not_called()
        self.assertEqual(self.manager.easy._engine().observations, {})

    def test_explicit_read_diagnostic_can_run_while_editor_is_open(self):
        opened = self.open()
        with mock.patch.object(self.manager.process_editors, 'pending', return_value={'status': 'editing'}), \
                mock.patch('accessibility_report.collect', return_value={'status': 'ok', 'diagnostic_id': 'c'*32}) as reader:
            response = self.manager.call('computer_observe', {'window_ref': opened['window_ref'], 'diagnostics': True})
            self.assertFalse(response.get('isError'), response)
            reader.assert_called_once()
        self.assertEqual(self.manager.easy._engine().observations, {})


class EditorDiagnosticTests(unittest.TestCase):
    setUp = editor_fixture.LocalAuthoringEditorTests.setUp
    spawn = editor_fixture.LocalAuthoringEditorTests.spawn
    start = editor_fixture.LocalAuthoringEditorTests.start
    status = editor_fixture.LocalAuthoringEditorTests.status
    terminal = editor_fixture.LocalAuthoringEditorTests.terminal
    command = editor_fixture.LocalAuthoringEditorTests.command
    context = editor_fixture.LocalAuthoringEditorTests.context
    open_delay = editor_fixture.LocalAuthoringEditorTests.open_delay

    def test_editor_diagnostic_does_not_edit_recording_or_task(self):
        opened, job = self.open_delay()
        before = copy.deepcopy(job['draft'].steps)
        with mock.patch('accessibility_report.collect', return_value={'status': 'ok', 'diagnostic_id': 'b'*32,
                'diagnostic_report': {'read_only': True}, 'report_path': 'local.json'}) as reader:
            result = self.command('diagnose', self.args['targets'][0])
            self.assertEqual(result['status'], 'ok', result); reader.assert_called_once()
        self.assertEqual(job['draft'].steps, before)
        self.status(opened, cancel=True); self.terminal(opened)

    def test_large_report_is_not_sent_through_bounded_editor_ipc(self):
        opened, job = self.open_delay()
        with mock.patch('accessibility_report.collect', return_value={'status': 'ok', 'diagnostic_id': 'b'*32,
                'diagnostic_report': {'text': '가'*300000}, 'report_path': 'complete-local.json'}):
            result = self.command('diagnose', self.args['targets'][0])
            self.assertEqual(result['status'], 'ok', result)
            self.assertNotIn('diagnostic_report', result)
            self.assertLess(len(result['diagnostic_preview']), 161000)
            self.assertEqual(result['report_path'], 'complete-local.json')
        self.status(opened, cancel=True); self.terminal(opened)


class DiagnosticRetentionTests(unittest.TestCase):
    setUp = lifecycle_fixture.ArtifactLifecycleTests.setUp
    tearDown = lifecycle_fixture.ArtifactLifecycleTests.tearDown
    run_folder = lifecycle_fixture.ArtifactLifecycleTests.run_folder
    session = lifecycle_fixture.ArtifactLifecycleTests.session
    file = lifecycle_fixture.ArtifactLifecycleTests.file

    def test_closed_run_diagnostics_retained_seven_days_and_unknown_files_preserved(self):
        run = self.run_folder()
        fresh = self.file(run, 'accessibility-diagnostics/'+'a'*32+'.json')
        old = self.file(run, 'accessibility-diagnostics/'+'b'*32+'.json')
        unknown = self.file(run, 'accessibility-diagnostics/user-note.json')
        timestamp = self.now - lifecycle.RECEIPT_RETENTION_SECONDS - 1
        os.utime(old, (timestamp, timestamp))
        lifecycle.cleanup_artifacts(self.root, now=self.now, time_budget_seconds=5)
        self.assertTrue(fresh.exists()); self.assertFalse(old.exists()); self.assertTrue(unknown.exists())


if __name__ == '__main__': unittest.main()
