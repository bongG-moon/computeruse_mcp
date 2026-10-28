"""Exercise the actual local status boundary, not an unfiltered status mock."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import diagnostics
from local_delivery import receipt
from server import ComputerManager
from settings import VERSION, load_config, validate_config
from teaching_support import TEACHING_HELPER_FILES, teaching_capabilities


PRIVATE = 'PRIVATE-business-path-and-error'
INITIALIZED = {'serverInfo': {'name': 'company-computer-use', 'version': VERSION}}


class LocalInstallContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for filename in TEACHING_HELPER_FILES:
            (self.root / filename).touch()
        self.support = teaching_capabilities(self.root)
        support = patch('teaching_support.teaching_capabilities', return_value=self.support)
        support.start()
        self.addCleanup(support.stop)
        self.config = {'version': 1, 'driver': str(self.root / 'cua-driver.exe'),
                       'programs': [], 'mode': 'uia', 'approval': 'client',
                       'max_minutes': 10, 'max_actions': 120,
                       'state_dir': str(self.root / 'state'), 'log_detail': 'metadata'}

    def manager(self, **updates):
        value = {**self.config, **updates}
        manager = ComputerManager(validate_config(value),
            runtime_factory=Mock(side_effect=AssertionError('No screen session')),
            transport_factory=Mock(side_effect=AssertionError('No Driver input')))
        self.addCleanup(manager.close)
        return manager

    def status(self, manager):
        return manager.call('computer_status', {})['structuredContent']

    def issues(self, manager, status=None):
        names = {item['name'] for item in manager.tools_list()['tools']}
        return diagnostics.connection_issues(INITIALIZED, names,
            self.status(manager) if status is None else status)

    def probe(self, *, transform=None, schema_error='', active=False, policy=None):
        value = copy.deepcopy(self.config)
        if policy is not None:
            value['data_policy'] = policy
        before = copy.deepcopy(value)
        requests, instances = [], []
        class Client:
            def __init__(self, path):
                self.manager = ComputerManager(load_config(path),
                    runtime_factory=Mock(side_effect=AssertionError('No UI')),
                    transport_factory=Mock(side_effect=AssertionError('No Driver')))
                self.manager.schema_error = schema_error
                self.closed = False
                instances.append(self)

            def request(self, method, params=None):
                requests.append((method, params))
                if method == 'initialize':
                    return INITIALIZED
                if method == 'tools/list':
                    return self.manager.tools_list()
                if method != 'tools/call' or params != {'name': 'computer_status', 'arguments': {}}:
                    raise AssertionError('Read-only protocol only')
                raw_status = self.manager.status()
                raw_status['session'] = {'private': PRIVATE} if active else None
                with patch.object(self.manager, 'status', return_value=raw_status):
                    result = self.manager.call('computer_status', {})
                if transform:
                    result['structuredContent'] = transform(result['structuredContent'])
                return result

            def initialized(self):
                requests.append(('notifications/initialized', None))

            def close(self):
                self.manager.close()
                self.closed = True

        try:
            with patch.object(diagnostics, 'make_server_entry', side_effect=lambda path: path), \
                 patch.object(diagnostics, 'ReadOnlyMCP', Client):
                return diagnostics.probe_connection(value)
        finally:
            self.assertEqual(value, before)
            self.assertTrue(all(item.closed for item in instances))
            self.assertEqual([item[0] for item in requests],
                ['initialize', 'notifications/initialized', 'tools/list', 'tools/call'])

    def test_default_local_real_manager_passes_simple_tool_diagnostics(self):
        manager = self.manager()
        self.assertEqual(len(manager.tools_list()['tools']), 12)
        self.assertEqual(self.issues(manager), [])
        status = self.status(manager)
        self.assertEqual(status['tool_profile'], 'simple')
        self.assertIs(status['screen_session_present'], False)
        self.assertIs(status['driver_schema_error_present'], False)
        self.assertIsNone(manager.session)

    def test_local_legacy_configuration_diagnoses_effective_simple_profile(self):
        self.assertEqual(self.issues(self.manager(data_policy='local', tool_profile='legacy')), [])

    def test_only_fixed_install_metadata_survives_repeated_filtering(self):
        support = copy.deepcopy(self.support)
        support['identity'] = {'path': PRIVATE}
        support['message'] = PRIVATE
        support['direct_picker']['helper']['path'] = PRIVATE
        support['image_process']['helpers'][0]['path'] = PRIVATE
        status = receipt({'version': VERSION, 'tool_profile': 'simple',
            'teaching_support': support, 'session': {'path': PRIVATE},
            'driver_schema_error': PRIVATE})
        self.assertNotIn(PRIVATE, json.dumps(status))
        self.assertEqual(receipt(status), status)
        self.assertTrue(status['screen_session_present'])
        self.assertTrue(status['driver_schema_error_present'])
        self.assertEqual(set(status['teaching_support']),
            {'format', 'version', 'direct_picker', 'image_process'})
        self.assertEqual(set(status['teaching_support']['direct_picker']['helper']),
            {'filename', 'file_present'})

    def test_missing_helpers_still_fail(self):
        self.support['direct_picker']['helper']['file_present'] = False
        self.assertTrue(any('ZIP' in issue for issue in self.issues(self.manager())))

    def test_unknown_filename_and_string_presence_cannot_pass_or_leak(self):
        for key, value in (('filename', PRIVATE), ('file_present', PRIVATE)):
            with self.subTest(key=key):
                support = copy.deepcopy(self.support)
                support['direct_picker']['helper'][key] = value
                status = self.status(self.manager())
                status['teaching_support'] = receipt({'teaching_support': support})['teaching_support']
                self.assertNotIn(PRIVATE, json.dumps(status))
                self.assertTrue(any('ZIP' in issue for issue in self.issues(self.manager(), status)))

    def test_missing_diagnostic_presence_is_not_healthy(self):
        for key in ('tool_profile', 'screen_session_present', 'driver_schema_error_present'):
            with self.subTest(key=key):
                result = self.probe(transform=lambda status: {k: v for k, v in status.items() if k != key})
                self.assertFalse(result['ok'])

    def test_non_boolean_presence_is_rejected(self):
        for key in ('screen_session_present', 'driver_schema_error_present'):
            for invalid in (0, 'false', None):
                with self.subTest(key=key, invalid=invalid):
                    result = self.probe(transform=lambda status: {**status, key: invalid})
                    self.assertFalse(result['ok'])

    def test_readonly_probe_passes_filtered_local_status(self):
        result = self.probe()
        self.assertTrue(result['ok'])
        self.assertEqual(result['tool_count'], 12)
        self.assertFalse(result['screen_session_started'])

    def test_active_session_is_not_hidden_by_local_filter(self):
        with self.assertRaisesRegex(RuntimeError, '화면 세션'):
            self.probe(active=True)

    def test_schema_error_fails_without_releasing_raw_message(self):
        result = self.probe(schema_error=PRIVATE)
        self.assertFalse(result['ok'])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertTrue(result['schema_error'])

    def test_connected_simple_retains_existing_status_and_diagnostics(self):
        result = self.probe(policy='connected')
        self.assertTrue(result['ok'])
        self.assertIn('session', result['status'])
        self.assertIn('identity', result['status']['teaching_support'])


if __name__ == '__main__':
    unittest.main()
