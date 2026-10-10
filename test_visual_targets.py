"""Saved image recovery uses real guarded capture/input contracts, no desktop."""
import copy
import unittest
from unittest import mock

import test_interaction as fixtures
from image_pixels import decode_png, encode_png
from operations import OperationError
from test_image_steps import image_target, png
from visual_targets import WorkflowVisualTargets


class WorkflowVisualTargetTests(unittest.TestCase):
    def setUp(self):
        fixtures.FacadeTests.setUp(self)
        self.runtime.id = 'recovery-session'
        self.now = 100.0
        self.manager = WorkflowVisualTargets(clock=lambda: self.now)
        self.addCleanup(self.manager.close)
        self.record = {'run_id': 'run-test', 'task_id': 'task-test', 'revision': 1,
            'recipe_hash': 'recipe-original', 'inputs_hash': 'inputs-original',
            'snapshot_hash': 'snapshot-original', 'pending_step': 2,
            'step_delivery': {'step_index': 2, 'state': 'not_sent'}}
        self.step = {'operation': 'image_click', 'image_target': image_target(),
            'program_id': 'allowed', 'window_ref': 'main', 'step_id': 'open-first', 'iteration_id': 'row-one'}
        self.failure = {'input_dispatched': False, 'diagnostic': {'code': 'image_not_found'}}

    def mutations(self): return fixtures.FacadeTests.mutations(self)

    def open(self, vision=True):
        return self.manager.open(self.runtime, self.record, self.step, fixtures.TARGET,
            failure=self.failure, image_delivery_enabled=vision)

    def submission(self, opened):
        challenge = opened['target_review']
        return {**{key: challenge[key] for key in ('recovery_id', 'run_id', 'step_index', 'step_id',
            'iteration_id', 'observation_id', 'frame_id')}, 'target_region': dict(self.region),
            'scope_region': dict(self.scope), 'evidence': 'The first named row has the same open icon at the saved reference anchor.'}

    def verify(self, submitted, vision=True):
        return self.manager.verify(self.runtime, self.record, self.step, fixtures.TARGET,
            submitted, image_delivery_enabled=vision)

    def execute(self, identity, vision=True):
        return self.manager.execute(self.runtime, self.record, self.step, fixtures.TARGET,
            identity, image_delivery_enabled=vision)

    def changed_frame(self, x=95, y=95):
        width, height, color, channels, rows = decode_png(png(100, 100))
        row = bytearray(rows[y]); row[x*channels] ^= 255; rows[y] = bytes(row)
        changed = encode_png(width, height, color, rows)
        self.transport.after = lambda request, result: result['content'][0].update(data=changed) if request['name'] == 'get_window_state' else None

    def test_reference_and_current_frame_are_explicit_without_mutating_saved_task(self):
        original = copy.deepcopy(self.step)
        result = self.open()
        self.assertEqual(result['state'], 'needs_target_review')
        self.assertEqual(len(result['image_content']), 2)
        self.assertEqual(result['image_content'][0]['data'], original['image_target']['template_png'])
        self.assertEqual(result['target_review']['saved_target']['anchor'], original['image_target']['anchor'])
        self.assertNotIn('template_png', result['target_review']['saved_target'])
        self.assertNotIn('data', result['target_review'])
        self.assertEqual(self.step, original)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(self.mutations(), [])

    def test_reference_and_current_images_require_explicit_vision_each_time(self):
        with self.assertRaises(OperationError): self.open(False)
        opened = self.open()
        with self.assertRaises(OperationError): self.verify(self.submission(opened), False)
        verified = self.verify(self.submission(opened))
        result = self.execute(verified['recovery_id'], False)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(self.mutations(), [])

    def test_sent_unknown_or_other_failure_cannot_open_recovery(self):
        for state in ('sent', 'unknown'):
            self.record['step_delivery']['state'] = state
            with self.subTest(state=state), self.assertRaises(OperationError): self.open()
        self.record['step_delivery']['state'] = 'not_sent'
        for failure in ({'input_dispatched': None, 'diagnostic': {'code': 'image_not_found'}},
                        {'input_dispatched': True, 'diagnostic': {'code': 'image_ambiguous'}},
                        {'input_dispatched': False, 'diagnostic': {'code': 'image_requires_foreground'}}):
            self.failure = failure
            with self.subTest(failure=failure), self.assertRaises(OperationError): self.open()
        self.assertEqual(self.mutations(), [])

    def test_every_challenge_identifier_and_task_snapshot_are_bound(self):
        opened = self.open(); submitted = self.submission(opened)
        for key in ('recovery_id', 'run_id', 'step_index', 'step_id', 'iteration_id', 'observation_id', 'frame_id'):
            with self.subTest(key=key), self.assertRaises(OperationError): self.verify({**submitted, key: 'wrong'})
        for key in ('recipe_hash', 'inputs_hash', 'snapshot_hash', 'revision'):
            original = self.record[key]; self.record[key] = 'other'
            with self.subTest(key=key), self.assertRaises(OperationError): self.verify(submitted)
            self.record[key] = original
        self.step['image_target']['anchor']['x'] = .25
        with self.assertRaises(OperationError): self.verify(submitted)
        self.assertEqual(self.mutations(), [])

    def test_expiry_refresh_and_window_replacement_never_deliver_input(self):
        first = self.open(); self.now += 121
        with self.assertRaises(OperationError): self.verify(self.submission(first))
        second = self.open()
        self.assertNotEqual(first['target_review']['recovery_id'], second['target_review']['recovery_id'])
        self.probe.state['creation_time'] += 1
        with self.assertRaises(OperationError): self.verify(self.submission(second))
        self.assertEqual(self.mutations(), [])

    def test_invalid_or_unbounded_region_scope_and_evidence_rejected(self):
        submitted = self.submission(self.open())
        for patch in ({'target_region': {'x': True, 'y': 0, 'width': 16, 'height': 16}},
                      {'target_region': {'x': 90, 'y': 0, 'width': 16, 'height': 16}},
                      {'target_region': {'x': 20, 'y': 30, 'width': 2, 'height': 2}},
                      {'scope_region': self.region}, {'scope_region': {'x': 50, 'y': 50, 'width': 20, 'height': 20}},
                      {'evidence': ''}, {'task_verified': True}):
            with self.subTest(patch=patch), self.assertRaises(OperationError): self.verify({**submitted, **patch})
        self.assertEqual(self.mutations(), [])

    def test_changes_outside_selected_region_invalidate_prior_scene_proof(self):
        submitted = self.submission(self.open())
        self.changed_frame()  # Outside target AND scope: previous step may differ.
        with self.assertRaisesRegex(OperationError, '화면 내용'): self.verify(submitted)
        self.assertEqual(self.mutations(), [])

    def test_no_scope_requires_global_unique_match_at_the_observed_location(self):
        submitted = self.submission(self.open()); submitted.pop('scope_region')
        for found in ({'status': 'ambiguous'}, {'status': 'matched', 'rect': {**self.region, 'x': 50}}):
            with self.subTest(found=found), mock.patch('visual_targets.match_image', return_value=found), self.assertRaises(OperationError):
                self.verify(submitted)
        with mock.patch('visual_targets.match_image', return_value={'status': 'matched', 'rect': self.region}):
            result = self.verify(submitted)
        self.assertEqual(result['state'], 'target_review_ready')
        self.assertEqual(self.mutations(), [])

    def test_verified_recovery_dispatches_once_from_fresh_pixels_and_preserves_snapshot(self):
        original = copy.deepcopy(self.step)
        verified = self.verify(self.submission(self.open()))
        self.record['step_delivery']['state'] = 'unknown'  # Durable crash-safety boundary.
        result = self.execute(verified['recovery_id'])
        self.assertTrue(result['input_dispatched'])
        self.assertTrue(result['verification_deferred'])
        self.assertFalse(result['task_verified'])
        self.assertEqual(self.step, original)
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.mutations()[0]['arguments']['x'], 28)
        self.assertEqual(self.mutations()[0]['arguments']['y'], 38)
        again = self.execute(verified['recovery_id'])
        self.assertFalse(again['input_dispatched'])
        self.assertEqual(len(self.mutations()), 1)

    def test_scene_change_after_verification_is_rechecked_by_guard_before_input(self):
        verified = self.verify(self.submission(self.open()))
        self.changed_frame(25, 35)
        result = self.execute(verified['recovery_id'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic']['code'], 'target_recovery_observation_changed')
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.execute(verified['recovery_id'])['diagnostic']['code'], 'target_recovery_expired')

    def test_unknown_transport_result_is_preserved_and_ticket_never_reused(self):
        verified = self.verify(self.submission(self.open()))
        with mock.patch.object(self.guard, 'image_action', side_effect=RuntimeError('driver unavailable after dispatch attempt')):
            result = self.execute(verified['recovery_id'])
        self.assertIsNone(result['input_dispatched'])
        self.assertFalse(result['automatic_replay'])
        self.assertEqual(self.execute(verified['recovery_id'])['diagnostic']['code'], 'target_recovery_expired')

    def test_unverified_ticket_and_disabled_or_expired_session_cannot_execute(self):
        opened = self.open()
        result = self.execute(opened['target_review']['recovery_id'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(self.mutations(), [])
        verified = self.verify(self.submission(self.open())); self.now += 121
        result = self.execute(verified['recovery_id'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(self.mutations(), [])


if __name__ == '__main__': unittest.main()
