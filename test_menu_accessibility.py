import copy
import json
import subprocess
import time
import unittest
from unittest import mock

import menu_accessibility as menu
from operations import OperationError

TARGET = {'pid': 42, 'window_id': 19}


def source(name='uia_raw', complete=True):
    return {**TARGET, 'source': name, 'status': 'ok' if complete else 'partial',
            'complete': complete, 'truncated': not complete, 'elements': [],
            'provider_errors': [], 'read_only': True, 'authorizes_input': False}


def row():
    return {'element_index': 0, 'index': 0, 'parent_index': -1, 'name': 'Export', 'role': 'MenuItem',
            'enabled': True, 'visible': True, 'verification_only': True, 'authorizes_input': False,
            'identity_lifetime': 'current_menu_observation_only', 'locator': {}, 'expected': {},
            'supported_actions': ['expand']}


def action():
    return ({'source': 'direct_msaa', 'path': [0], 'root_object_id': -4},
            {'target': TARGET, 'path': [{}, {}], 'observed_at_ms': time.time() * 1000})


class MenuAccessibilityTests(unittest.TestCase):
    def test_normalized_parent_semantics_preserve_exact_native_evidence(self):
        native = {'has_children': False, 'has_popup': True, 'expansion_state': 'Collapsed'}
        value = {**native, 'expected': {'path': [copy.deepcopy(native)]}}
        menu._normalize_source({'elements': [value]})
        self.assertTrue(value['has_children'])
        self.assertFalse(value['provider_has_children'])
        self.assertEqual(value['expansion_state'], 'collapsed')
        self.assertEqual(value['expected']['path'][0], native)
        unknown = {'has_children': False, 'has_popup': None}
        menu._normalize_source({'elements': [unknown]})
        self.assertIsNone(unknown['has_children'])

    def test_independent_sources_preserve_partial_observation(self):
        first, second = source(), source('direct_msaa', False)
        with mock.patch.object(menu, '_native', side_effect=[first, second]) as native:
            data = menu.observe(TARGET)
        self.assertEqual([call.args[0]['source'] for call in native.call_args_list], list(menu.SOURCES))
        self.assertFalse(data['complete'])
        self.assertTrue(data['sources']['uia_raw']['complete'])
        self.assertTrue(data['sources']['direct_msaa']['truncated'])
        self.assertFalse(data['authorizes_input'])
        self.assertFalse(data['input_dispatched'])

    def test_failure_does_not_become_complete_empty_source(self):
        with mock.patch.object(menu, '_native', side_effect=[OperationError('timeout', 'menu_accessibility_timeout'), source('direct_msaa')]):
            data = menu.observe(TARGET)
        self.assertEqual(data['sources']['uia_raw']['status'], 'timeout')
        self.assertFalse(data['sources']['uia_raw']['complete'])
        self.assertFalse(data['complete'])

    def test_guard_failure_stops_before_second_source(self):
        with mock.patch.object(menu, '_native', side_effect=OperationError('stopped', 'session_stopped')) as native:
            with self.assertRaises(OperationError) as caught:
                menu.observe(TARGET)
        self.assertEqual(caught.exception.code, 'session_stopped')
        self.assertEqual(native.call_count, 1)

    def test_no_driver_tokens_or_invalid_parent_graph_is_accepted(self):
        for change in ({'element_token': 'driver-id'}, {'snapshot_id': 'old'}, {'parent_index': 0},
                       {'supported_actions': ['arbitrary_input']}, {'identity_lifetime': 'durable'}, {'enabled': 1}, {'visible': 0}):
            value = source()
            value['elements'] = [{**row(), **change}]
            with self.assertRaises(OperationError):
                menu._validate_source(value, 10)

    def test_complete_cannot_cover_provider_errors_or_truncation(self):
        for change in ({'provider_errors': [{'code': 'bad'}]}, {'truncated': True}, {'status': 'partial'}):
            with self.assertRaises(OperationError):
                menu._validate_source({**source(), **change}, 10)

    def test_invalid_inputs_do_not_spawn(self):
        with mock.patch.object(menu, '_native') as native:
            for target in ({'pid': True, 'window_id': 1}, {'pid': 1, 'window_id': 0}, {**TARGET, 'anything': 1}):
                with self.assertRaises(OperationError): menu.observe(target)
            for timeout in (True, float('nan'), 0, 31):
                with self.assertRaises(OperationError): menu.observe(TARGET, timeout=timeout)
        native.assert_not_called()

    def test_stale_or_durable_locator_rejected_before_input(self):
        locator, expected = action()
        with mock.patch.object(menu, '_native') as native:
            for age in (11000, -1000):
                with self.assertRaises(OperationError):
                    menu.act(TARGET, locator, {**expected, 'observed_at_ms': time.time() * 1000 - age}, 'invoke')
            with self.assertRaises(OperationError):
                menu.act(TARGET, {**locator, 'driver_handle': '99'}, expected, 'invoke')
        native.assert_not_called()

    def test_one_shot_action_forwards_exact_expected_and_never_retries(self):
        locator, expected = action()
        data = {**TARGET, 'source': 'direct_msaa', 'status': 'ok', 'operation': 'invoke',
                'delivery': 'sent', 'input_dispatched': True, 'authorizes_input': False, 'business_result_verified': False}
        with mock.patch.object(menu, '_native', return_value=data) as native:
            result = menu.act(TARGET, locator, expected, 'invoke')
        self.assertIs(result, data)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(native.call_args.args[0]['expected'], expected)

    def test_uncertain_native_failure_carries_unknown_delivery_without_retry(self):
        locator, expected = action()
        with mock.patch.object(menu, '_native', side_effect=OperationError('timeout', 'menu_accessibility_timeout')) as native:
            with self.assertRaises(OperationError) as caught: menu.act(TARGET, locator, expected, 'invoke')
        self.assertEqual(native.call_count, 1)
        self.assertIsNone(caught.exception.input_dispatched)
        self.assertEqual(caught.exception.delivery, 'unknown')

    def test_partial_or_stale_native_action_is_explicit_not_sent(self):
        locator, expected = action()
        data = {**TARGET, 'source': 'direct_msaa', 'status': 'error', 'operation': 'expand',
                'delivery': 'not_sent', 'input_dispatched': False, 'business_result_verified': False,
                'diagnostic': {'code': 'menu_accessibility_evidence_changed'}}
        with mock.patch.object(menu, '_native', return_value=data):
            self.assertEqual(menu.act(TARGET, locator, expected, 'expand')['delivery'], 'not_sent')

    def test_bad_subprocess_payload_is_never_an_observation(self):
        for output in ('not-json', '[]', '{}', json.dumps({**source(), 'window_id': 99})):
            child = mock.Mock(returncode=0)
            child.communicate.return_value = (output, '')
            child.poll.return_value = 0
            with mock.patch.object(menu.Path, 'is_file', return_value=True), mock.patch.object(menu.subprocess, 'Popen', return_value=child):
                with self.assertRaises(OperationError):
                    menu._native({**TARGET, 'source': 'uia_raw', 'operation': 'observe'}, 1, lambda: None)

    def test_cancellation_kills_the_helper_without_second_launch(self):
        child = mock.Mock(returncode=None)
        child.poll.return_value = None
        child.communicate.side_effect = [subprocess.TimeoutExpired('helper', .01), ('', '')]
        checks = mock.Mock(side_effect=[None, None, OperationError('stop', 'session_stopped')])
        with mock.patch.object(menu.Path, 'is_file', return_value=True), mock.patch.object(menu.subprocess, 'Popen', return_value=child) as launch:
            with self.assertRaises(OperationError): menu._native({**TARGET, 'source': 'uia_raw'}, 1, checks)
        launch.assert_called_once()
        child.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
