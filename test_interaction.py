"""Unified facade contract checks, including actual guarded visual dispatch."""
import copy
import threading
import os
import unittest
from unittest import mock
from types import SimpleNamespace

from image_pixels import crop_png, decode_png, encode_png, pixel_digest, target_from_region
from interaction import InteractionEngine
from inspection import inspect_window
from operations import OperationError
import test_image_steps as image_fixtures
from test_image_steps import TARGET, png
from test_inspection import Runtime as InspectionRuntime


class PixelTests(unittest.TestCase):
    def test_region_crop_preserves_pixels_and_template_contract(self):
        source = png(100, 100)
        region = {'x': 20, 'y': 30, 'width': 16, 'height': 16}
        result = target_from_region(source, region)
        width, height, color, channels, rows = decode_png(result['template_png'])
        full = decode_png(source)
        self.assertEqual((width, height), (16, 16))
        expected = full[-1][30][20*full[3]:36*full[3]]
        if channels == 4 and full[3] == 3:
            expected = b''.join(expected[index:index+3]+b'\xff' for index in range(0, len(expected), 3))
        self.assertEqual(rows[0], expected)
        self.assertEqual(result['capture_window'], {'width': 100, 'height': 100})

    def test_reencoded_image_uses_pixel_equality(self):
        image = png(100, 100)
        width, height, color, channels, rows = decode_png(image)
        self.assertEqual(pixel_digest(image), pixel_digest(encode_png(width, height, color, rows)))

    def test_bad_scope_and_too_small_template_rejected(self):
        for region in ({'x': -1, 'y': 0, 'width': 16, 'height': 16},
                       {'x': 90, 'y': 0, 'width': 16, 'height': 16},
                       {'x': True, 'y': 0, 'width': 16, 'height': 16},
                       {'x': 1, 'y': 1, 'width': 2, 'height': 2}):
            with self.subTest(region=region), self.assertRaises(OperationError):
                target_from_region(png(100, 100), region)


class InputPrivilegeTests(unittest.TestCase):
    def test_only_measured_lower_token_without_uiaccess_blocks(self):
        from privileges import input_integrity_preflight
        low = {'checked': True, 'integrity': 'medium', 'rid': 0x2000, 'ui_access': False}
        high = {'checked': True, 'integrity': 'high', 'rid': 0x3000, 'ui_access': False}
        blocked = input_integrity_preflight(1, 2, resolver=lambda pid: low if pid == 1 else high)
        self.assertEqual(blocked['code'], 'elevation_required')
        self.assertFalse(blocked['input_sent'])
        self.assertIsNone(input_integrity_preflight(1, 2, resolver=lambda pid: high))
        self.assertIsNone(input_integrity_preflight(1, 2, resolver=lambda pid: {'checked': False} if pid == 1 else high))
        self.assertIsNone(input_integrity_preflight(1, 2, resolver=lambda pid: {**low, 'ui_access': True} if pid == 1 else high))

    def test_real_current_token_matches_existing_readonly_status(self):
        from privileges import process_integrity, execution_privileges
        current, previous = process_integrity(os.getpid()), execution_privileges()
        if current['checked'] and previous['checked']:
            self.assertEqual(current['integrity'], previous['integrity'])
        self.assertFalse(process_integrity(True)['checked'])

    def test_real_uipi_error_has_actionable_category_without_noinput_invention(self):
        from vendor.guard import diagnostic_code, input_failure_evidence
        text = 'UIPI: target is at High integrity; cua-driver is at Medium integrity.'
        self.assertEqual(diagnostic_code(text), 'elevation_required')
        proof = input_failure_evidence({'isError': True, 'content': [{'type': 'text', 'text': text}]})
        self.assertNotIn('input_sent', proof)


class Probe:
    def __init__(self):
        self.closed = False
        self.state = {'process_exited': False, 'target_present': True, 'creation_time': 123,
            'executable': 'allowed.exe', 'windows': [{**TARGET, 'class_name': 'test', 'thread_id': 1,
            'owner_window_id': 0, 'title': 'Title'}]}
    def capture(self): return copy.deepcopy(self.state)
    def snapshot(self): return copy.deepcopy(self.state)
    def close(self): self.closed = True


class FacadeTests(unittest.TestCase):
    # Base case owns a real Guard with injectable process/window resolvers and
    # the Driver transport contract. No global input/UIA/desktop is exercised.
    def setUp(self):
        image_fixtures.ImageGuardTests.setUp(self)
        self.probe = Probe()
        self.runtime = SimpleNamespace(mode='uia', guard=self.guard, stop_event=threading.Event(),
            execution_lock=threading.RLock(), check_active=lambda: None,
            create_transition_probe=lambda target: self.probe,
            capture_checkpoint=lambda target: self.guard.capture_checkpoint(target),
            call=lambda name, args: {'structuredContent': {**TARGET, 'elements': [
                {'role': 'Button', 'label': 'Query', 'actions': ['invoke'], 'automation_id': 'query'}]}})
        self.engine = InteractionEngine(self.runtime)
        self.addCleanup(self.engine.close)
        self.ref = self.engine.bind(TARGET)['window_ref']
        self.region = {'x': 20, 'y': 30, 'width': 16, 'height': 16}
        self.scope = {'x': 10, 'y': 20, 'width': 50, 'height': 50}

    def mutations(self):
        return [call for call in self.transport.calls if call['name'] != 'get_window_state']

    def observe(self, vision=True):
        return self.engine.observe(self.ref, goal='Click the first row start button', image_delivery_enabled=vision)['structuredContent']

    def act(self, observed, **kwargs):
        return self.engine.act(self.ref, {'operation': 'click'}, observation_id=observed['observation_id'],
            image_region=self.region, scope_region=self.scope, image_delivery_enabled=True,
            completion='First row is running', **kwargs)

    def test_logical_window_identity_reuse_and_handle_replacement(self):
        self.assertEqual(self.engine.bind(TARGET)['window_ref'], self.ref)
        self.probe.state['windows'][0]['title'] = 'New document'
        self.assertEqual(self.engine.target(self.ref), TARGET)
        self.probe.state['windows'][0]['thread_id'] = 2
        with self.assertRaisesRegex(OperationError, '교체'):
            self.engine.target(self.ref)
        self.assertEqual(self.mutations(), [])

    def test_text_profile_never_returns_or_authorizes_pixels(self):
        observed = self.observe(False)
        self.assertFalse(observed['capability']['visual_targeting'])
        self.assertNotIn('frame_id', observed)
        with self.assertRaises(OperationError): self.act(observed)
        self.assertEqual(self.mutations(), [])

    def test_goal_captures_even_with_toolbar_control(self):
        observed = self.observe()
        self.assertEqual(observed['inspection']['observed_modalities'], ['uia', 'visual'])
        self.assertIn('frame_id', observed)
        self.assertFalse(observed['inspection']['goal_target_verified'])
        self.assertIn('target_ref', observed['inspection']['controls'][0])

    def test_image_scope_dispatches_once_foreground_and_never_calls_completed(self):
        observed = self.observe()
        answer = self.act(observed)
        self.assertEqual(answer['state'], 'needs_observation_review')
        self.assertEqual(answer['dispatch'], 'sent')
        self.assertFalse(answer['task_verified'])
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.mutations()[0]['arguments']['delivery_mode'], 'foreground')
        self.assertEqual((self.mutations()[0]['arguments']['x'], self.mutations()[0]['arguments']['y']), (28, 38))
        with self.assertRaises(OperationError): self.act(observed)
        self.assertEqual(len(self.mutations()), 1)

    def test_unrelated_animation_does_not_invalidate_scoped_target(self):
        observed = self.observe()
        width, height, color, channels, rows = decode_png(png(100, 100))
        rows[0] = b'\xff' * len(rows[0])
        changed = encode_png(width, height, color, rows)
        self.transport.after = lambda request, answer: answer['content'][0].update(data=changed) if request['name'] == 'get_window_state' else None
        answer = self.act(observed)
        self.assertEqual(answer['dispatch'], 'sent')
        self.assertEqual(len(self.mutations()), 1)

    def test_row_content_change_prevents_wrong_row_input(self):
        observed = self.observe()
        width, height, color, channels, rows = decode_png(png(100, 100))
        rows[22] = b'\xff' * len(rows[22])
        changed = encode_png(width, height, color, rows)
        self.transport.after = lambda request, answer: answer['content'][0].update(data=changed) if request['name'] == 'get_window_state' else None
        answer = self.act(observed)
        self.assertEqual(answer['dispatch'], 'not_sent')
        self.assertEqual(answer['diagnostic']['code'], 'observation_scope_changed')
        self.assertEqual(self.mutations(), [])

    def test_duplicate_icon_without_context_never_chooses_first(self):
        observed = self.observe()
        with mock.patch('interaction.match_image', return_value={'status': 'ambiguous'}):
            answer = self.engine.act(self.ref, {'operation': 'click'}, observation_id=observed['observation_id'],
                image_region=self.region, image_delivery_enabled=True, completion='started')
        self.assertEqual(answer['dispatch'], 'not_sent')
        self.assertEqual(self.mutations(), [])

    def test_driver_failure_cause_and_explicit_non_delivery_survive_image_facade(self):
        observed = self.observe()
        def refuse(request, answer):
            if request['name'] == 'click':
                answer.update(isError=True, structuredContent={'input_sent': False, 'error_code': 'native_pointer_unavailable',
                    'effect': 'not_applied'}, content=[{'type': 'text', 'text': 'UIA target provider failed before input.'}])
        self.transport.after = refuse
        answer = self.act(observed)
        evidence = answer['diagnostic']['driver_failure']
        self.assertEqual(evidence['error_code'], 'native_pointer_unavailable')
        self.assertFalse(evidence['input_sent'])
        self.assertIn('UIA target provider', evidence['message'])
        self.assertEqual(answer['dispatch'], 'not_sent')
        self.assertEqual(len(self.mutations()), 1)

    def test_new_observation_revokes_old_refs_and_geometry_change_prevents_input(self):
        old = self.observe()
        current = self.observe()
        with self.assertRaises(OperationError): self.act(old)
        self.geometry = (2, 0, 102, 100, 2)
        answer = self.act(current)
        self.assertEqual(answer['dispatch'], 'not_sent')
        self.assertEqual(self.mutations(), [])

    def test_uia_ref_uses_foreground_and_no_parent_fallback(self):
        observed = self.observe(False)
        ref = observed['inspection']['controls'][0]['target_ref']
        with mock.patch.object(self.engine.operations, 'execute', return_value={'task_verified': False, 'input_dispatched': True, 'verification_deferred': True}) as execute:
            self.engine.act(self.ref, {'operation': 'click'}, target_ref=ref, completion='Query completed')
        args, kwargs = execute.call_args
        self.assertEqual(args[0]['selector'], {'automation_id': 'query'})
        self.assertEqual(kwargs['delivery_mode'], 'foreground')

    def test_visual_review_is_bound_readonly_and_distinct_from_deterministic(self):
        answer = self.act(self.observe())
        observed = self.observe()
        count = len(self.mutations())
        result = self.engine.review(self.ref, action_id=answer['action_id'], observation_id=observed['observation_id'],
            frame_id=observed['frame_id'], verdict='pass', evidence='The first row displays Running')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['status'], 'verified_visual')
        self.assertFalse(result['verification_deferred'])
        self.assertNotIn('diagnostic', result)
        self.assertNotIn('next_step', result)
        self.assertIn('prior_action_diagnostic', result)
        self.assertEqual(result['evidence_level'], 'visual_assessed')
        self.assertTrue(result['task_verified'])
        self.assertFalse(result['input_replayed'])
        self.assertEqual(len(self.mutations()), count)
        with self.assertRaises(OperationError):
            self.engine.review(self.ref, action_id=answer['action_id'], observation_id=observed['observation_id'],
                frame_id=observed['frame_id'], verdict='pass', evidence='duplicate')

    def test_closed_window_after_dispatched_input_preserves_receipt(self):
        for receipt, dispatch in (({'input_dispatched': True, 'verification_deferred': True}, 'sent'),
                                  ({'input_dispatched': None}, 'unknown')):
            self.probe.state['target_present'] = True
            def execute(*args, **kwargs):
                self.probe.state['target_present'] = False
                return {'status': 'needs_review', 'task_verified': False, **receipt}
            with mock.patch.object(self.engine.operations, 'execute', side_effect=execute) as callback:
                answer = self.engine.act(self.ref, {'operation': 'click', 'selector': {'automation_id': 'query'}}, completion='Dialog closes')
            callback.assert_called_once()
            self.assertEqual(answer['state'], 'uncertain')
            self.assertEqual(answer['dispatch'], dispatch)
            self.assertEqual(answer['input_dispatched'], receipt['input_dispatched'])
            self.assertEqual(answer['diagnostic']['stage'], 'post_action_review')
            self.assertFalse(answer['automatic_replay'])
            self.assertEqual(answer['current_binding']['window_ref'], self.ref)
            self.assertEqual(answer['next_action'], 'rebind_and_observe')

    def test_review_rejects_old_frame_and_newly_changed_pixels(self):
        old = self.observe()
        answer = self.act(old)
        with self.assertRaises(OperationError):
            self.engine.review(self.ref, action_id=answer['action_id'], observation_id=old['observation_id'],
                frame_id=old['frame_id'], verdict='pass', evidence='Old state')
        fresh = self.observe()
        self.transport.after = lambda request, value: value['content'][0].update(data=png(99, 100)) if request['name'] == 'get_window_state' else None
        with self.assertRaises(OperationError):
            self.engine.review(self.ref, action_id=answer['action_id'], observation_id=fresh['observation_id'],
                frame_id=fresh['frame_id'], verdict='pass', evidence='No longer current')
        self.assertEqual(len(self.mutations()), 1)

    def test_failed_binding_of_verified_transition_preserves_input_receipt(self):
        changed = {**TARGET, 'window_id': TARGET['window_id']+1}
        receipt = {'task_verified': True, 'input_dispatched': True, 'target': changed,
                   'transition': {'state': 'verified'}, 'checks': [{'passed': True}]}
        with mock.patch.object(self.engine.operations, 'execute', return_value=receipt) as execute, \
                mock.patch.object(self.runtime, 'create_transition_probe', side_effect=OperationError('new window gone', 'target_unavailable')):
            answer = self.engine.act(self.ref, {'operation': 'click', 'selector': {'automation_id': 'query'}}, completion='Result dialog appears')
        execute.assert_called_once()
        self.assertEqual(answer['dispatch'], 'sent')
        self.assertTrue(answer['input_dispatched'])
        self.assertFalse(answer['task_verified'])
        self.assertEqual(answer['state'], 'uncertain')
        self.assertEqual(answer['diagnostic']['stage'], 'post_action_rebind')
        self.assertTrue(answer['prior_action_verification']['task_verified'])
        self.assertEqual(answer['checks'], receipt['checks'])
        self.assertEqual(answer['transition'], receipt['transition'])
        self.assertEqual(answer['next_action'], 'rebind_and_observe')
        self.assertFalse(answer['binding_current'])
        self.assertFalse(answer['automatic_replay'])
        self.assertEqual(self.engine.actions, {})

    def test_cleared_bindings_after_dispatch_cannot_erase_receipt(self):
        def execute(*args, **kwargs):
            self.engine.close()
            return {'task_verified': False, 'input_dispatched': True, 'verification_deferred': True}
        with mock.patch.object(self.engine.operations, 'execute', side_effect=execute):
            answer = self.engine.act(self.ref, {'operation': 'click', 'selector': {'automation_id': 'query'}}, completion='Dialog closes')
        self.assertEqual(answer['dispatch'], 'sent')
        self.assertTrue(answer['input_dispatched'])
        self.assertEqual(answer['current_binding']['window_ref'], self.ref)
        self.assertEqual(answer['next_action'], 'rebind_and_observe')


class GoalInspectionTests(unittest.TestCase):
    def test_arbitrary_toolbar_never_proves_requested_icon_is_accessible(self):
        runtime = InspectionRuntime([{'role': 'Button', 'label': 'Settings', 'actions': ['invoke']}])
        runtime.capture_checkpoint = mock.Mock(return_value={'structuredContent': {'pid': 11, 'window_id': 22},
            'content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]})
        answer = inspect_window(runtime, {'pid': 11, 'window_id': 22}, requested_goal='First row Start')
        self.assertEqual(answer['structuredContent']['inspection']['image_capture']['reason'], 'goal_target_not_verified')
        runtime.capture_checkpoint.assert_called_once()


if __name__ == '__main__': unittest.main()
