import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import interact
from local_core import EngineError

REF = '@stest123:e1'


def args(**kw):
    result = dict(session='', ref=REF, action='set-value', value='N', headed=False,
                  direction=None, amount=None)
    result.update(kw)
    return SimpleNamespace(**result)


def raw(value='Y', **kw):
    node = {'role': 'combobox', 'name': 'Private Combo', 'value': value,
            'native_id': {'kind': 'automation_id', 'value': 'Combo'}, 'ref_id': REF}
    node.update(kw)
    return {'ok': True, 'data': {'app': 'App.exe', 'snapshot_id': 'stest123',
        'tree': {'role': 'window', 'name': 'Private Title', 'children': [node]}}}


def action_response(delivery='delivered_verified', ok=True):
    report = {'disposition': {'delivery': delivery, 'retry': 'unsafe' if delivery != 'not_delivered' else 'safe'}}
    return {'ok': ok, 'data' if ok else 'error': report}


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / ('a' * 32)
        self.run.mkdir()
        self.identity = [11, 22, 'App.exe', 1, 2]
        self.session = {'version': interact.VERSION, 'exe_name': 'App.exe', 'image': 'App.exe',
            'pid': 11, 'hwnd': 22, 'identity': self.identity, 'created_at': time.time(),
            'lifetime_seconds': interact.SESSION_SECONDS, 'snapshot_id': 'stest123'}
        self.map = {'inner': {'@e1': {'pid': 11, 'source_window_id': 'w-22', 'source_app': 'App.exe',
            'role': 'combobox', 'name': 'Private Combo'}}}
        self.patches = [patch.object(interact, 'validate_local_directory', side_effect=lambda p: Path(p)),
            patch.object(interact, '_identity', return_value=tuple(self.identity)),
            patch.object(interact.observe, 'verify_binary', return_value=Path('engine.exe')),
            patch.object(interact, '_environment', return_value={})]
        for p in self.patches:
            p.start()
        self.save_session()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def save_session(self, snapshot=None):
        before = json.dumps(snapshot or raw()).encode()
        mapping = json.dumps(self.map).encode()
        (self.run / 'before.json').write_bytes(before)
        folder = self.run / 'agent-state' / 'snapshots' / 'stest123'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'refmap.json').write_bytes(mapping)
        self.session['snapshot_sha256'] = hashlib.sha256(before).hexdigest()
        self.session['refmap_sha256'] = hashlib.sha256(mapping).hexdigest()
        (self.run / 'session.json').write_text(json.dumps(self.session))

    def invoke(self, *, response=None, after=None, get=None, **kw):
        response = response if response is not None else action_response()
        preflight = get if get is not None else {'ok': True, 'data': {'ref': REF, 'value': []}}
        effects = [preflight, response]
        with patch.object(interact, '_call', side_effect=effects) as call, \
             patch.object(interact, 'snapshot', return_value=after or raw('N')):
            result = interact.run_act(args(session=str(self.run), **kw))
        return result, call

    def test_only_explicit_actions_and_qualified_refs(self):
        for change in ({'action': 'mouse-click'}, {'action': 'clipboard-get'}, {'ref': '@e1'},
                       {'ref': REF + ' --headed'}, {'action': 'set-value', 'value': 'x\0x'},
                       {'action': 'click', 'value': 'N'}, {'value': 'x' * 10001}):
            with self.subTest(change=change), self.assertRaises(EngineError):
                interact.command(args(**change))

    def test_option_looking_value_is_only_positional_data(self):
        command = interact.command(args(value='--headed --app Another.exe'))
        self.assertEqual(command, ['set-value', REF, '--timeout-ms', '5000', '--', '--headed --app Another.exe'])
        self.assertNotIn('--headed', command)

    def test_headed_is_explicit_and_has_no_automatic_fallback(self):
        for action in ('type', 'focus', 'right-click', 'double-click'):
            value = 'text' if action == 'type' else None
            with self.subTest(action=action), self.assertRaisesRegex(EngineError, 'headed_required'):
                interact.command(args(action=action, value=value))
            self.assertEqual(interact.command(args(action=action, value=value, headed=True))[0], '--headed')

    def test_scroll_is_bounded_and_no_unrelated_options(self):
        for amount in (0, 11, True):
            with self.assertRaises(EngineError):
                interact.command(args(action='scroll', value=None, direction='down', amount=amount))
        with self.assertRaises(EngineError):
            interact.command(args(direction='down'))

    def test_value_changed_and_fresh_readback_completes_without_business_claim(self):
        result, call = self.invoke()
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(result['business_commit'], 'not_verified')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0].args[0], ['get', REF, '--property', 'states'])
        self.assertEqual(json.loads((self.run / 'verification.json').read_text())['node']['value'], 'N')
        self.assertNotIn('Private', json.dumps(result))

    def test_session_replay_is_blocked_before_any_engine_command(self):
        self.invoke()
        with patch.object(interact, '_call') as call:
            result = interact.run_act(args(session=str(self.run)))
        self.assertEqual(result['diagnostic'], 'session_consumed')
        self.assertFalse(result['input_dispatched'])
        call.assert_not_called()

    def test_expired_session_blocks_without_consuming_or_launch(self):
        self.session['created_at'] -= interact.SESSION_SECONDS + 1
        self.save_session()
        result, call = self.invoke()
        self.assertEqual(result['diagnostic'], 'expired_session')
        self.assertFalse((self.run / 'consumed.json').exists())
        call.assert_not_called()

    def test_process_lifetime_change_blocks(self):
        with patch.object(interact, '_identity', return_value=(11, 22, 'App.exe', 3, 4)):
            result, call = self.invoke()
        self.assertEqual(result['diagnostic'], 'target_identity_changed')
        call.assert_not_called()

    def test_changed_snapshot_is_not_accepted(self):
        (self.run / 'before.json').write_text(json.dumps(raw('altered')))
        result, call = self.invoke()
        self.assertEqual(result['diagnostic'], 'snapshot_changed')
        call.assert_not_called()

    def test_ref_from_another_snapshot_is_not_accepted(self):
        result, call = self.invoke(ref='@sanother:e1')
        self.assertEqual(result['diagnostic'], 'ref_not_in_snapshot')
        call.assert_not_called()

    def test_refmap_other_window_or_process_is_rejected_even_with_valid_hash(self):
        for key, value in (('source_window_id', 'w-23'), ('pid', 99), ('source_app', 'Other.exe')):
            self.map['inner']['@e1'][key] = value
            self.save_session()
            result, call = self.invoke()
            self.assertEqual(result['diagnostic'], 'ref_scope_mismatch')
            call.assert_not_called()
            self.map['inner']['@e1'][key] = {'source_window_id': 'w-22', 'pid': 11, 'source_app': 'App.exe'}[key]

    def test_secure_snapshot_target_is_blocked(self):
        self.save_session(raw(states=['protected']))
        result, call = self.invoke()
        self.assertEqual(result['diagnostic'], 'secure_target')
        call.assert_not_called()

    def test_secure_live_preflight_prevents_dispatch(self):
        result, call = self.invoke(get={'ok': True, 'data': {'ref': REF, 'value': ['secure']}})
        self.assertEqual(result['diagnostic'], 'secure_target')
        self.assertEqual(call.call_count, 1)
        self.assertFalse(result['input_dispatched'])

    def test_stale_engine_ref_is_blocked_without_action(self):
        result, call = self.invoke(get={'ok': False, 'error': {'code': 'STALE_REF'}})
        self.assertEqual(result['diagnostic'], 'ref_preflight_failed')
        self.assertEqual(call.call_count, 1)

    def test_timeout_after_launch_never_claims_no_input(self):
        result, call = self.invoke(response=EngineError('observation_timeout'))
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(result['delivery'], 'possibly_dispatched')
        self.assertEqual(result['retry'], 'unsafe')
        self.assertEqual(call.call_count, 2)
        self.assertTrue((self.run / 'dispatch-started.json').exists())

    def test_keyboard_interrupt_after_launch_never_claims_no_input(self):
        result, _ = self.invoke(response=KeyboardInterrupt())
        self.assertEqual(result['status'], 'interrupted')
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(result['retry'], 'unsafe')

    def test_failure_after_dispatch_preserves_delivery_receipt(self):
        with patch.object(interact, '_call', side_effect=[{'ok': True, 'data': {'ref': REF, 'value': []}}, action_response()]), \
             patch.object(interact, 'snapshot', side_effect=EngineError('snapshot_unavailable')):
            result = interact.run_act(args(session=str(self.run)))
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(result['status'], 'uncertain')
        persisted = json.loads((self.run / 'dispatch-receipt.json').read_text())
        self.assertTrue(persisted['input_dispatched'])
        self.assertTrue((self.run / 'action.json').exists())

    def test_keyboard_interrupt_during_readback_preserves_known_delivery(self):
        with patch.object(interact, '_call', side_effect=[{'ok': True, 'data': {'ref': REF, 'value': []}}, action_response()]), \
             patch.object(interact, 'snapshot', side_effect=KeyboardInterrupt):
            result = interact.run_act(args(session=str(self.run)))
        self.assertEqual(result['status'], 'interrupted')
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(result['retry'], 'unsafe')
        self.assertTrue(json.loads((self.run / 'dispatch-receipt.json').read_text())['input_dispatched'])

    def test_simultaneous_duplicate_cannot_get_past_atomic_consumption(self):
        nested = []
        def call(command, run):
            if command[0] == 'get':
                nested.append(interact.run_act(args(session=str(self.run))))
                return {'ok': True, 'data': {'ref': REF, 'value': []}}
            return action_response()
        with patch.object(interact, '_call', side_effect=call) as engine, \
             patch.object(interact, 'snapshot', return_value=raw('N')):
            result = interact.run_act(args(session=str(self.run)))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(nested[0]['diagnostic'], 'session_consumed')
        self.assertEqual(engine.call_count, 2)

    def test_readback_contradiction_is_not_success(self):
        result, _ = self.invoke(after=raw('Y'))
        self.assertEqual(result['status'], 'verification_failed')
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(result['effect_verification'], 'mismatch')

    def test_ok_without_data_does_not_claim_execution_complete(self):
        result, _ = self.invoke(response={'ok': True})
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(result['effect_verification'], 'matched')

    def test_ok_without_disposition_does_not_claim_execution_complete(self):
        result, _ = self.invoke(response={'ok': True, 'data': {}})
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(result['effect_verification'], 'matched')

    def test_unknown_delivery_remains_uncertain_despite_matched_value(self):
        result, _ = self.invoke(response=action_response('unknown'))
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['delivery'], 'possibly_dispatched')
        self.assertEqual(result['effect_verification'], 'matched')

    def test_known_no_delivery_can_confirm_already_requested_state(self):
        result, _ = self.invoke(response=action_response('not_delivered'))
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['effect_verification'], 'matched')

    def test_clear_missing_value_requires_fresh_get_and_accepts_observed_empty(self):
        after = raw()
        del after['data']['tree']['children'][0]['value']
        preflight = {'ok': True, 'data': {'ref': REF, 'value': []}}
        value = {'ok': True, 'data': {'ref': REF, 'property': 'value', 'value': ''}}
        with patch.object(interact, '_call', side_effect=[preflight, action_response(), value]) as call, \
             patch.object(interact, 'snapshot', return_value=after):
            result = interact.run_act(args(session=str(self.run), action='clear', value=None))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(call.call_args.args[0], ['get', REF, '--property', 'value'])
        self.assertTrue((self.run / 'after-value.json').exists())

    def test_missing_value_is_never_inferred_to_be_empty(self):
        after = raw()
        del after['data']['tree']['children'][0]['value']
        preflight = {'ok': True, 'data': {'ref': REF, 'value': []}}
        value = {'ok': True, 'data': {'ref': REF, 'property': 'value', 'value': None}}
        with patch.object(interact, '_call', side_effect=[preflight, action_response(), value]), \
             patch.object(interact, 'snapshot', return_value=after):
            result = interact.run_act(args(session=str(self.run), action='clear', value=None))
        self.assertEqual(result['status'], 'executed_unverified')
        self.assertEqual(result['effect_verification'], 'unavailable')

    def test_missing_value_get_wrong_ref_cannot_verify(self):
        after = raw()
        del after['data']['tree']['children'][0]['value']
        preflight = {'ok': True, 'data': {'ref': REF, 'value': []}}
        value = {'ok': True, 'data': {'ref': '@sother:e1', 'property': 'value', 'value': ''}}
        with patch.object(interact, '_call', side_effect=[preflight, action_response(), value]), \
             patch.object(interact, 'snapshot', return_value=after):
            result = interact.run_act(args(session=str(self.run), action='clear', value=None))
        self.assertEqual(result['effect_verification'], 'unavailable')

    def test_click_delivery_is_not_business_success(self):
        result, _ = self.invoke(action='click', value=None)
        self.assertEqual(result['status'], 'executed_unverified')
        self.assertEqual(result['business_commit'], 'not_verified')

    def test_not_delivered_can_be_proven_only_without_prep_side_effects(self):
        response = action_response('not_delivered', False)
        self.assertEqual(interact._delivery(response, 'click', False), (False, 'not_delivered'))
        for action, headed in (('select', False), ('click', True)):
            self.assertEqual(interact._delivery(response, action, headed), (None, 'possibly_dispatched'))
        response['error']['details'] = {'after_action': {'steps': [
            {'mechanism': 'semantic_api', 'outcome': 'succeeded', 'label': 'Expand'}]}}
        self.assertEqual(interact._delivery(response, 'click', False), (None, 'possibly_dispatched'))

    def test_contradictory_disposition_cannot_prove_no_input(self):
        response = action_response('not_delivered', False)
        response['error']['disposition']['retry'] = 'unsafe'
        self.assertEqual(interact._delivery(response, 'click', False), (None, 'possibly_dispatched'))

    def test_duplicate_control_identity_cannot_verify_effect(self):
        before = interact.derive(raw())['nodes'][1]
        after = raw('N')
        after['data']['tree']['children'].append(dict(after['data']['tree']['children'][0]))
        self.assertEqual(interact.verify_effect(before, after, 'set-value', 'N')['status'], 'unavailable')

    def test_absent_checked_state_is_not_assumed_unchecked(self):
        before = interact.derive(raw())['nodes'][1]
        self.assertEqual(interact.verify_effect(before, raw(states=[]), 'uncheck', None)['status'], 'unavailable')

    def test_bad_cli_never_echoes_private_arguments_or_paths(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = interact.main(['act', '--session', 'PRIVATE PATH', '--ref', REF, '--action', 'PRIVATE'])
        self.assertEqual(code, 2)
        self.assertNotIn('PRIVATE', stdout.getvalue() + stderr.getvalue())

    def test_unexpected_pre_dispatch_exception_does_not_echo_content(self):
        stdout = io.StringIO()
        with patch.object(interact, '_load_session', side_effect=RuntimeError('PRIVATE DATA')), contextlib.redirect_stdout(stdout):
            code = interact.main(['act', '--session', str(self.run), '--ref', REF, '--action', 'click'])
        self.assertEqual(code, 2)
        self.assertNotIn('PRIVATE', stdout.getvalue())
        self.assertFalse(json.loads(stdout.getvalue())['input_dispatched'])

    @unittest.skipUnless(os.name == 'nt', 'Windows reparse attributes')
    def test_regular_file_reader_rejects_reparse_attribute(self):
        real = Path.lstat
        target = self.run / 'session.json'
        def stat(path):
            if path == target:
                return SimpleNamespace(st_file_attributes=1024)
            return real(path)
        with patch.object(Path, 'lstat', stat), self.assertRaisesRegex(EngineError, 'reparse_file_rejected'):
            interact._read(target)


if __name__ == '__main__':
    unittest.main()
