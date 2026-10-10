"""Pinned Driver lifecycle recovery, with no process or desktop interaction."""
import copy
from contextlib import contextmanager
import threading
import time
import unittest

from session_runtime import SAFE_TOOLS, manifest_for, SessionError
from vendor.guard import DriverTransport


def refused(code='session_ended'):
    return {'isError': True, 'structuredContent': {'status': 'refused', 'refusal': {'code': code}},
            'content': [{'type': 'text', 'text': 'Driver lifecycle refusal'}]}


class Transport(DriverTransport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.lifecycle_lock = threading.RLock()
        self.last_activity = time.monotonic()-301
        self.policy = {'request_timeout_seconds': 12}
        self.calls, self.verifications = [], 0
        self.identity_current = True
        self.callback = None
        self.readonly_recovery_scope = self.scope
        self.scope_closed = False

    @contextmanager
    def scope(self, target):
        self.target = copy.deepcopy(target)
        def verify():
            self.verifications += 1
            if not self.identity_current: raise SessionError('Target changed or parent authorization ended')
        try: yield verify
        finally: self.scope_closed = True

    def _driver_request_once(self, method, params=None, timeout=None):
        self.calls.append((method, copy.deepcopy(params), timeout))
        if self.callback: self.callback(params)
        return copy.deepcopy(self.responses.pop(0))


class DriverLifecycleTests(unittest.TestCase):
    request = {'name': 'get_window_state', 'arguments': {'pid': 100, 'window_id': 200, 'max_depth': 3}}

    def test_idle_refusal_recovers_same_transport_once_and_only_rereads(self):
        current = {'structuredContent': {'elements': [{'label': 'Current'}]}}
        transport = Transport([refused(), {'structuredContent': {'active': True}}, current])
        answer = transport.driver_request('tools/call', self.request, timeout=5)
        self.assertEqual([call[1]['name'] for call in transport.calls], ['get_window_state', 'start_session', 'get_window_state'])
        self.assertEqual(transport.calls[0][1], transport.calls[-1][1])
        self.assertEqual(transport.calls[1][1]['arguments'], {})
        self.assertEqual(transport.target, {'pid': 100, 'window_id': 200})
        self.assertEqual(transport.verifications, 4)
        self.assertTrue(transport.scope_closed)
        self.assertFalse(answer['structuredContent']['read_only_recovery']['input_replayed'])
        self.assertTrue(all(0 < call[2] <= 5 for call in transport.calls))

    def test_revocation_manifest_expiry_and_arbitrary_refusal_are_not_regranted(self):
        for code in ('authorization_revoked', 'authorization_suspended', 'permission_denied', 'capability_manifest_expired', 'bounded_resource_outside_manifest'):
            transport = Transport([refused(code)])
            self.assertEqual(transport.driver_request('tools/call', self.request), refused(code))
            self.assertEqual(len(transport.calls), 1)

    def test_lifecycle_start_denial_is_returned_without_new_process_or_observation_retry(self):
        denial = refused('authorization_revoked')
        transport = Transport([refused(), denial])
        self.assertEqual(transport.driver_request('tools/call', self.request), denial)
        self.assertEqual([call[1]['name'] for call in transport.calls], ['get_window_state', 'start_session'])

    def test_mutation_unknown_or_expired_is_never_replayed(self):
        for name in ('click', 'type_text', 'hotkey', 'bring_to_front'):
            transport = Transport([refused()])
            answer = transport.driver_request('tools/call', {'name': name, 'arguments': {'pid': 100, 'window_id': 200}})
            self.assertTrue(answer['isError'])
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.verifications, 0)

    def test_missing_lifecycle_acknowledgement_keeps_read_failed(self):
        for acknowledgement in ({}, {'structuredContent': None}, {'structuredContent': {'active': False}}):
            transport = Transport([refused(), acknowledgement])
            answer = transport.driver_request('tools/call', self.request)
            self.assertTrue(answer['isError'])
            self.assertEqual(answer['structuredContent']['refusal']['code'], 'session_ended')
            self.assertFalse(answer['structuredContent']['read_only_recovery']['observation_repeated'])
            self.assertEqual(len(transport.calls), 2)

    def test_malformed_refusal_does_not_trigger_recovery(self):
        for body in ({'status': 'refused', 'refusal': None}, None, []):
            response = {'isError': True, 'structuredContent': body}
            transport = Transport([response])
            self.assertEqual(transport.driver_request('tools/call', self.request), response)
            self.assertEqual(len(transport.calls), 1)

    def test_short_idle_missing_scope_or_missing_target_cannot_recover(self):
        for mode in ('short_idle', 'no_parent_scope', 'desktop_target'):
            transport = Transport([refused()])
            request = copy.deepcopy(self.request)
            if mode == 'short_idle': transport.last_activity = time.monotonic()-30
            elif mode == 'no_parent_scope': transport.readonly_recovery_scope = None
            else: request['arguments'] = {}
            transport.driver_request('tools/call', request)
            self.assertEqual(len(transport.calls), 1)

    def test_window_change_during_recovery_prevents_recapture(self):
        transport = Transport([refused(), {'structuredContent': {'active': True}}])
        def change(request):
            if request['name'] == 'start_session': transport.identity_current = False
        transport.callback = change
        with self.assertRaises(SessionError): transport.driver_request('tools/call', self.request)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.scope_closed)

    def test_second_refusal_does_not_loop(self):
        transport = Transport([refused(), {'structuredContent': {'active': True}}, refused()])
        self.assertTrue(transport.driver_request('tools/call', self.request)['isError'])
        self.assertEqual(len(transport.calls), 3)

    def test_private_lifecycle_tool_does_not_expand_public_input_or_app_scope(self):
        manifest = manifest_for(['C:\\Apps\\Editor.exe'], 9)
        self.assertIn('start_session', manifest['allow']['tools'])
        self.assertNotIn('start_session', SAFE_TOOLS)
        self.assertEqual(manifest['expires_after'], '9m')
        self.assertEqual(manifest['idle_timeout'], '9m')
        self.assertEqual(manifest['resources']['apps'], [{'executable': 'C:\\Apps\\Editor.exe', 'launch': False, 'windows': 'all'}])
        self.assertFalse(manifest['resources']['desktop']['display'])


if __name__ == '__main__': unittest.main()
