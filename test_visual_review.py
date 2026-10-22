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
