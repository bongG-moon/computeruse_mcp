"""Saved model review binds run, iteration, window identity and fresh pixels."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

import test_interaction as fixtures
from operations import OperationError
from visual_review import WorkflowVisualReviews
import transient_popup as popup


class WorkflowVisualReviewTests(unittest.TestCase):
    def setUp(self):
        fixtures.FacadeTests.setUp(self)
        self.runtime.id = 'session-test'
        self.now = 100.0
        self.manager = WorkflowVisualReviews(clock=lambda: self.now)
        self.addCleanup(self.manager.close)
        self.record = {'run_id': 'run-test', 'pending_step': 2}
        self.step = {'operation': 'checkpoint', 'review_mode': 'visual', 'message': 'First device shows Running',
                     'step_id': 'verify-start', 'iteration_id': 'device-one'}

    def mutations(self):
        return fixtures.FacadeTests.mutations(self)

    def open(self):
        return self.manager.open(self.runtime, self.record, self.step, fixtures.TARGET)

    def submission(self, result):
        fields = {'review_id', 'run_id', 'step_index', 'step_id', 'iteration_id', 'expected_condition_id', 'observation_id', 'frame_id'}
        return {**{key: result['visual_review'][key] for key in fields}, 'verdict': 'pass',
                'evidence': 'The first row label is Device One and its status displays Running'}

    def verify(self, submission):
        return self.manager.verify(self.runtime, self.record, self.step, fixtures.TARGET, submission)

    def test_open_and_verify_are_read_only_and_evidence_is_visual_assessed(self):
        opened = self.open()
        self.assertEqual(len(opened['image_content']), 1)
        verified = self.verify(self.submission(opened))
        self.assertEqual(verified['review_state'], 'passed')
        self.assertTrue(verified['task_verified'])
        self.assertFalse(verified['human_reviewed'])
        self.assertFalse(verified['input_replayed'])
        self.assertEqual(verified['evidence_level'], 'visual_assessed')
        self.assertEqual(self.mutations(), [])

    def test_wrong_run_iteration_condition_or_frame_is_rejected(self):
        opened = self.open()
        submitted = self.submission(opened)
        for key in ('review_id', 'run_id', 'step_index', 'step_id', 'iteration_id', 'expected_condition_id', 'observation_id', 'frame_id'):
            altered = {**submitted, key: 'different'}
            with self.subTest(key=key), self.assertRaises(OperationError):
                self.verify(altered)
        self.assertEqual(self.mutations(), [])

    def test_existing_human_checkpoint_cannot_be_downgraded(self):
        self.step.pop('review_mode')
        with self.assertRaises(OperationError): self.open()
        self.assertEqual(self.mutations(), [])

    def test_expiration_and_refresh_never_replay_input(self):
        first = self.open()
        self.now += 121
        with self.assertRaises(OperationError): self.verify(self.submission(first))
        second = self.open()
        self.assertNotEqual(first['visual_review']['review_id'], second['visual_review']['review_id'])
        self.assertTrue(self.verify(self.submission(second))['task_verified'])
        self.assertEqual(self.mutations(), [])

    def test_same_looking_new_session_cannot_approve_old_challenge(self):
        opened = self.open()
        old = self.runtime
        self.runtime = SimpleNamespace(**vars(self.runtime))
        with self.assertRaises(OperationError): self.verify(self.submission(opened))
        self.runtime = old
        self.assertEqual(self.mutations(), [])

    def test_false_or_uncertain_visual_result_is_not_completion(self):
        for verdict in ('fail', 'uncertain'):
            opened = self.open()
            result = self.verify({**self.submission(opened), 'verdict': verdict})
            self.assertFalse(result['task_verified'])
            self.assertFalse(result['model_image_verified'])
        self.assertEqual(self.mutations(), [])

    def test_replacement_window_is_rejected_before_assessment(self):
        opened = self.open()
        self.probe.state['creation_time'] = 456
        with self.assertRaises(OperationError): self.verify(self.submission(opened))
        self.assertEqual(self.mutations(), [])

    def test_unknown_extra_argument_cannot_override_stored_evidence(self):
        opened = self.open()
        with self.assertRaises(OperationError):
            self.verify({**self.submission(opened), 'task_verified': True})
        self.assertEqual(self.mutations(), [])

    def test_checkpoint_capture_never_activates_main_window(self):
        with mock.patch('interaction.prepare_image_foreground') as activate:
            self.open()
        activate.assert_not_called()

    def test_declared_popup_return_reobserves_transient_ambiguity_without_input(self):
        self.step['return_from'] = 'export-child'
        with mock.patch.object(popup, 'current_popup', side_effect=[
                OperationError('Menus are still closing', 'popup_observation_ambiguous'), None]) as probe, \
                mock.patch('visual_review.time.sleep') as sleep, \
                mock.patch.object(self.runtime, 'capture_checkpoint', wraps=self.runtime.capture_checkpoint) as capture:
            opened = self.open()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(sleep.call_count, 1)
        transition = opened['review_metrics']['transition_observation']
        self.assertEqual(transition['attempts'], 2)
        self.assertEqual(transition['last_reason'], 'popup_observation_ambiguous')
        self.assertFalse(transition['individual_native_timeout_enforced'])
        self.assertFalse(opened['task_verified'])
        self.assertEqual(self.mutations(), [])

    def test_persistent_return_ambiguity_stops_after_three_probes(self):
        self.step['return_from'] = 'export-child'
        with mock.patch.object(popup, 'current_popup', side_effect=OperationError(
                'Multiple menus remain', 'popup_observation_ambiguous')) as probe, \
                mock.patch('visual_review.time.sleep'), mock.patch.object(self.runtime, 'capture_checkpoint') as capture:
            with self.assertRaises(OperationError) as error:
                self.open()
        self.assertEqual(probe.call_count, 3)
        capture.assert_not_called()
        self.assertEqual(error.exception.code, 'popup_observation_ambiguous')
        self.assertEqual(error.exception.review_diagnostic['transition_observation']['attempts'], 3)
        self.assertEqual(self.mutations(), [])

    def test_generic_main_checkpoint_does_not_retry_ambiguity(self):
        with mock.patch.object(popup, 'current_popup', side_effect=OperationError(
                'Multiple menus', 'popup_observation_ambiguous')) as probe, mock.patch('visual_review.time.sleep') as sleep:
            with self.assertRaises(OperationError): self.open()
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()

    def test_return_does_not_retry_owner_or_identity_error(self):
        self.step['return_from'] = 'export-child'
        with mock.patch.object(popup, 'current_popup', side_effect=OperationError(
                'Owner changed', 'popup_owner_changed')) as probe, mock.patch('visual_review.time.sleep') as sleep:
            with self.assertRaises(OperationError) as error: self.open()
        self.assertEqual(error.exception.code, 'popup_owner_changed')
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()

    def test_return_stop_check_prevents_next_probe(self):
        self.step['return_from'] = 'export-child'
        def ambiguous(*args):
            self.runtime.check_active = mock.Mock(side_effect=OperationError('Stopped', 'stopped'))
            raise OperationError('Multiple menus', 'popup_observation_ambiguous')
        with mock.patch.object(popup, 'current_popup', side_effect=ambiguous) as probe, \
                mock.patch('visual_review.time.sleep') as sleep:
            with self.assertRaises(OperationError) as error: self.open()
        self.assertEqual(error.exception.code, 'stopped')
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_slow_native_probe_exhausts_budget_without_new_probe(self):
        self.step['return_from'] = 'export-child'
        with mock.patch.object(popup, 'current_popup', side_effect=OperationError(
                'Multiple menus', 'popup_observation_ambiguous')) as probe, \
                mock.patch('visual_review.time.monotonic', side_effect=[0, 1.2, 1.2]), \
                mock.patch('visual_review.time.sleep') as sleep:
            with self.assertRaises(OperationError) as error: self.open()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(error.exception.review_diagnostic['transition_observation']['elapsed_ms'], 1200)
        sleep.assert_not_called()

    def test_main_visual_checkpoint_and_recheck_never_read_uia(self):
        self.runtime.call = mock.Mock(side_effect=AssertionError('UIA is unresponsive'))
        with mock.patch.object(self.runtime, 'capture_checkpoint', wraps=self.runtime.capture_checkpoint) as capture:
            opened = self.open()
            result = self.verify(self.submission(opened))
        self.runtime.call.assert_not_called()
        self.assertEqual(capture.call_count, 2)
        self.assertTrue(result['task_verified'])
        self.assertEqual(result['review_metrics']['comparison_scope'], 'full_frame')

    def test_capture_refusal_never_falls_back_or_passes(self):
        self.runtime.call = mock.Mock(side_effect=AssertionError('UIA must not be a fallback'))
        self.runtime.capture_checkpoint = mock.Mock(return_value={
            'isError': True, 'structuredContent': {'error_code': 'checkpoint_requires_foreground'}, 'content': []})
        with self.assertRaises(OperationError) as error:
            self.open()
        self.assertEqual(error.exception.code, 'visual_review_capture_failed')
        self.assertEqual(error.exception.review_diagnostic['reason'], 'capture_failed')
        self.runtime.call.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_recheck_capture_refusal_retains_pending_challenge(self):
        opened = self.open()
        self.runtime.capture_checkpoint = mock.Mock(return_value={
            'isError': True, 'structuredContent': {'error_code': 'checkpoint_requires_foreground'}, 'content': []})
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened))
        self.assertEqual(error.exception.review_diagnostic['reason'], 'capture_failed')
        self.assertEqual(error.exception.review_diagnostic['capture_cause'], 'checkpoint_requires_foreground')
        self.assertIn(opened['visual_review']['review_id'], self.manager.jobs)
        self.assertEqual(self.mutations(), [])

    def test_current_request_reissues_exact_ids_without_capture_or_ttl_extension(self):
        opened = self.open()
        original_expires = self.manager.jobs[opened['visual_review']['review_id']]['expires']
        self.now += 11
        with mock.patch.object(self.runtime, 'capture_checkpoint') as capture:
            repeated = self.manager.current_request(self.runtime, self.record, self.step, fixtures.TARGET)
        capture.assert_not_called()
        self.assertEqual(repeated['observation_review_template'], opened['observation_review_template'])
        self.assertEqual(repeated['image_content'], opened['image_content'])
        self.assertEqual(repeated['review_metrics']['remaining_seconds'], 109)
        self.assertTrue(repeated['review_metrics']['request_reused'])
        self.assertFalse(repeated['review_metrics']['new_capture_performed'])
        self.assertEqual(self.manager.jobs[opened['visual_review']['review_id']]['expires'], original_expires)
        self.assertTrue(self.verify(self.submission(repeated))['task_verified'])

    def test_current_request_never_reuses_other_session_run_or_expired_frame(self):
        self.open()
        other_runtime = SimpleNamespace(**vars(self.runtime))
        self.assertIsNone(self.manager.current_request(other_runtime, self.record, self.step, fixtures.TARGET))
        self.assertIsNone(self.manager.current_request(self.runtime, {**self.record, 'run_id': 'other'}, self.step, fixtures.TARGET))
        self.now += 121
        self.assertIsNone(self.manager.current_request(self.runtime, self.record, self.step, fixtures.TARGET))
        self.assertEqual(self.mutations(), [])

    def test_current_request_rejects_replaced_window(self):
        self.open()
        self.probe.state['creation_time'] += 1
        with self.assertRaises(OperationError):
            self.manager.current_request(self.runtime, self.record, self.step, fixtures.TARGET)

    def change_pixel(self, x, y):
        width, height, color, channels, rows = fixtures.decode_png(self.source_png)
        row = bytearray(rows[y]); row[x*channels] ^= 255; rows[y] = bytes(row)
        self.source_png = fixtures.encode_png(width, height, color, rows)

    def test_unrelated_clock_pixel_only_allowed_with_explicit_evidence_regions(self):
        opened = self.open()
        self.change_pixel(90, 90)
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened))
        self.assertEqual(error.exception.review_diagnostic['reason'], 'full_frame_changed')
        # The same still-live frame can support a bounded assessment, but only
        # when the caller explicitly supplies the unchanged condition region.
        result = self.verify({**self.submission(opened), 'evidence_regions': [
            {'x': 10, 'y': 10, 'width': 30, 'height': 30}]})
        self.assertTrue(result['task_verified'])
        self.assertEqual(result['review_metrics']['comparison_scope'], 'evidence_regions')
        self.assertEqual(self.mutations(), [])

    def test_condition_region_change_cannot_pass_with_evidence_regions(self):
        opened = self.open()
        self.change_pixel(20, 20)
        with self.assertRaises(OperationError) as error:
            self.verify({**self.submission(opened), 'evidence_regions': [
                {'x': 10, 'y': 10, 'width': 30, 'height': 30}]})
        self.assertEqual(error.exception.review_diagnostic['reason'], 'evidence_regions_changed')
        self.assertEqual(error.exception.review_diagnostic['changed_region_indices'], [0])
        self.assertEqual(self.mutations(), [])

    def test_geometry_change_during_capture_is_not_pixel_success(self):
        opened = self.open()
        original = self.runtime.capture_checkpoint
        def moved(target):
            result = original(target)
            self.geometry = (2, 0, 102, 100, 2)
            return result
        self.runtime.capture_checkpoint = moved
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened))
        self.assertEqual(error.exception.review_diagnostic['reason'], 'capture_geometry_changed')
        self.assertEqual(self.mutations(), [])

    def test_popup_review_restores_exact_proof_on_same_run_request(self):
        owner=fixtures.TARGET; child={**owner,'window_id':owner['window_id']+1}
        proof={'process':(123,'allowed.exe'),'path':[child['window_id'],owner['window_id']],
               'window':{'thread_id':7},'bounds':(0,0,100,100),'foreground':owner['window_id']}
        engine=mock.Mock()
        engine.bind.side_effect=lambda t:{'window_ref':'main' if t==owner else 'popup'}
        engine.request_review.return_value={'action_id':'action-test'}
        engine.observe.return_value={'structuredContent':{'frame_id':'frame-test','observation_id':'obs-test',
            'image_size':{'width':100,'height':100}},'content':[{'type':'image','mimeType':'image/png','data':'fixture'}]}
        engine.review.return_value={'task_verified':True}
        manager=WorkflowVisualReviews(clock=lambda:self.now,engine_factory=lambda *a,**k:engine)
        self.addCleanup(manager.close)
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value=proof), \
                mock.patch.object(popup,'current_popup',return_value=child):
            self.assertTrue(popup.register_popup(child,owner))
            opened=manager.open(self.runtime,self.record,self.step,owner)
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value=proof):
            self.assertFalse(popup.proven_popup(child))
            result=manager.verify(self.runtime,self.record,self.step,owner,self.submission(opened))
            self.assertTrue(popup.proven_popup(child))
        self.assertTrue(result['task_verified'])
        engine.target.assert_any_call('main')
        engine.target.assert_any_call('popup')
        engine.review.assert_called_once()

    def test_popup_review_rejects_replacement_before_accepting_assessment(self):
        owner=fixtures.TARGET; child={**owner,'window_id':owner['window_id']+1}
        engine=mock.Mock()
        engine.bind.side_effect=lambda t:{'window_ref':'main' if t==owner else 'popup'}
        engine.request_review.return_value={'action_id':'action-test'}
        engine.observe.return_value={'structuredContent':{'frame_id':'frame-test','observation_id':'obs-test',
            'image_size':{'width':100,'height':100}},'content':[{'type':'image','mimeType':'image/png','data':'fixture'}]}
        manager=WorkflowVisualReviews(clock=lambda:self.now,engine_factory=lambda *a,**k:engine)
        self.addCleanup(manager.close)
        proof={'process':(123,'allowed.exe'),'path':[2,1],'window':{'thread_id':7},'bounds':(0,0,100,100)}
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value=proof), \
                mock.patch.object(popup,'current_popup',return_value=child):
            popup.register_popup(child,owner)
            opened=manager.open(self.runtime,self.record,self.step,owner)
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value={**proof,'path':[2,3,1]}):
            with self.assertRaises(OperationError) as caught:
                manager.verify(self.runtime,self.record,self.step,owner,self.submission(opened))
            self.assertEqual(caught.exception.code,'review_observation_changed')
        engine.review.assert_not_called()
        self.assertIn(opened['visual_review']['review_id'],manager.jobs)

    def test_full_template_round_trip_and_changed_metadata(self):
        opened = self.open()
        submitted = {**opened['observation_review_template'], 'verdict': 'pass',
                     'evidence': 'The first row shows Running'}
        for key, value in [('image_size', {'width': 1, 'height': 1}),
                           ('expires_in_seconds', 1), ('expected_condition', 'different'),
                           ('step_index', 2.0)]:
            with self.subTest(key=key), self.assertRaises(OperationError):
                self.verify({**submitted, key: value})
        self.assertTrue(self.verify(submitted)['task_verified'])


if __name__ == '__main__': unittest.main()
