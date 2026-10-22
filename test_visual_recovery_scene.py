"""Rendered business-grid regression through the real recovery/Guard contract.

Only the Driver capture transport and OS window resolvers are fixture doubles.
Pixels, crops, Airtest/OpenCV candidate discovery, match validation, identity
checks and recovery policy are real. No desktop capture or input is performed.
"""
import base64
import copy
import unittest
from unittest import mock

import airtest_backend
import test_interaction as fixtures
from image_pixels import crop_regions, pixel_digest, target_from_region
from image_targets import validate_match
from operations import OperationError
from visual_targets import WorkflowVisualTargets


class RenderedRecoverySceneTests(unittest.TestCase):
    WIDTH, HEIGHT = 480, 320
    CONDITION = {'x': 12, 'y': 5, 'width': 130, 'height': 33}
    CLOCK = {'x': 410, 'y': 7, 'width': 66, 'height': 22}
    CELL = {'x': 304, 'y': 109, 'width': 48, 'height': 12}
    REPEATED_SCOPE = {'x': 280, 'y': 96, 'width': 100, 'height': 39}
    UNIQUE_SCOPE = {'x': 16, 'y': 96, 'width': 372, 'height': 39}

    @classmethod
    def setUpClass(cls):
        cls.cv, cls.np, _ = airtest_backend._load()

    def render(self, *, clock='10:20', mes='N', family='Hybrid'):
        frame = self.np.full((self.HEIGHT, self.WIDTH, 3), 248, self.np.uint8)
        def text(value, point, scale=.5):
            self.cv.putText(frame, value, point, self.cv.FONT_HERSHEY_SIMPLEX,
                            scale, (25,25,25), 1, self.cv.LINE_AA)
        text('MES: '+mes, (20,27))
        text('Family: '+family, (180,27))
        text(clock, (414,23), .42)
        text('Device / empty value cells', (20,68), .6)
        for index, label in enumerate(('DEVICE ALPHA', 'MODULE BRAVO', 'PART CHARLIE', 'LOT DELTA')):
            y = 100 + 44*index
            text(label, (24,y+22), .55)
            self.cv.rectangle(frame, (285,y), (375,y+30), (40,40,40), 1)
            self.cv.rectangle(frame, (286,y+1), (374,y+29), (255,255,255), -1)
        return base64.b64encode(self.cv.imencode('.png', frame)[1]).decode('ascii')

    def setUp(self):
        fixtures.FacadeTests.setUp(self)
        self.runtime.id = 'rendered-scene-session'
        self.runtime.run_dir = self.tmp.name
        self.geometry = (0,0,self.WIDTH,self.HEIGHT,2)
        self.now = 100.0
        self.manager = WorkflowVisualTargets(clock=lambda:self.now)
        self.addCleanup(self.manager.close)
        self.current = self.render()
        def capture(request, response):
            if request['name'] == 'get_window_state':
                response['content'][0]['data'] = self.current
                response['structuredContent'].update(screenshot_width=self.WIDTH, screenshot_height=self.HEIGHT)
        self.transport.after = capture
        # Choose the real pure-Python PNG crop path; avoid starting the native
        # crop helper per region. This does not substitute image/match results.
        crop_patch = mock.patch('image_pixels._native_crop', return_value=None)
        crop_patch.start(); self.addCleanup(crop_patch.stop)
        self.matches = []
        def actual_match(runtime, target, screenshot):
            answer = airtest_backend.match_candidates(target, screenshot,
                                                      check_active=runtime.check_active)
            validated = validate_match(answer, target, screenshot)
            self.matches.append(validated)
            return validated
        match_patch = mock.patch('visual_targets.match_image', side_effect=actual_match)
        match_patch.start(); self.addCleanup(match_patch.stop)
        self.record = {'run_id':'scene-run', 'task_id':'scene-task', 'revision':1,
                       'recipe_hash':'original-recipe', 'inputs_hash':'original-inputs',
                       'snapshot_hash':'original-snapshot', 'pending_step':2,
                       'step_delivery':{'step_index':2, 'state':'not_sent'}}
        self.step = {'operation':'image_click', 'image_target':target_from_region(self.current,self.REPEATED_SCOPE),
                     'program_id':'allowed', 'window_ref':'main', 'step_id':'choose-row', 'iteration_id':'row-one'}
        self.original_step = copy.deepcopy(self.step)

    def proof(self):
        png = crop_regions(self.current,[self.CONDITION])[0]
        self.runtime._workflow_visual_evidence = {self.record['run_id']:{
            'step_index':1, 'target':dict(fixtures.TARGET), 'expires':self.now+120,
            'identity':copy.deepcopy(self.engine.windows[self.ref]['identity']),
            'geometry':self.geometry, 'regions':[dict(self.CONDITION)], 'digests':[pixel_digest(png)]}}

    def open(self, *, prior_proof=False):
        if prior_proof: self.proof()
        opened = self.manager.open(self.runtime,self.record,self.step,fixtures.TARGET,
                   failure={'input_dispatched':False,'diagnostic':{'code':'image_ambiguous'}},
                   image_delivery_enabled=True)
        if prior_proof:
            self.assertIn('condition_proof',self.manager.jobs[opened['target_review']['recovery_id']])
        return opened

    def submission(self, opened, *, scope=None, clock=False):
        fields = ('recovery_id','run_id','step_index','step_id','iteration_id','observation_id','frame_id')
        value = {key:opened['target_review'][key] for key in fields}
        value.update(target_region=dict(self.CELL), scope_region=dict(scope or self.UNIQUE_SCOPE),
                     evidence='The empty value cell belongs to DEVICE ALPHA, identified by the visible row label.')
        if clock:
            value['volatile_regions'] = [{'kind':'clock','region':dict(self.CLOCK),
                'evidence':'Only the displayed HH:MM clock changes; it is separate from MES, Family and row labels.'}]
        return value

    def verify(self, submission):
        return self.manager.verify(self.runtime,self.record,self.step,fixtures.TARGET,
                                   submission,image_delivery_enabled=True)

    def assert_no_input_or_recipe_change(self):
        self.assertEqual(fixtures.FacadeTests.mutations(self),[])
        self.assertEqual(self.step,self.original_step)
        self.assertEqual(self.record['step_delivery']['state'],'not_sent')

    def test_repeated_blank_cells_are_rejected_by_actual_candidate_search(self):
        submitted = self.submission(self.open(),scope=self.REPEATED_SCOPE)
        with self.assertRaises(OperationError) as error:
            self.verify(submitted)
        self.assertEqual(error.exception.code,'target_recovery_ambiguous')
        self.assertEqual(self.matches[-1]['status'],'ambiguous')
        self.assertGreaterEqual(self.matches[-1]['candidate_count'],2)
        self.assert_no_input_or_recipe_change()

    def test_unique_row_label_scope_is_accepted_by_actual_candidate_search(self):
        result = self.verify(self.submission(self.open()))
        self.assertEqual(result['state'],'target_review_ready')
        self.assertEqual(self.matches[-1]['status'],'matched')
        self.assertEqual(self.matches[-1]['rect'],self.UNIQUE_SCOPE)
        self.assertGreaterEqual(self.matches[-1]['score'],.98)
        self.assert_no_input_or_recipe_change()

    def test_clock_only_change_requires_explicit_narrow_exception_and_retained_proof(self):
        opened = self.open(prior_proof=True)
        self.current = self.render(clock='10:21')
        result = self.verify(self.submission(opened,clock=True))
        self.assertEqual(result['state'],'target_review_ready')
        self.assertEqual(self.matches[-1]['rect'],self.UNIQUE_SCOPE)
        self.assertEqual(result['volatile_regions'][0]['region'],self.CLOCK)
        self.assert_no_input_or_recipe_change()

    def test_clock_without_explicit_exception_is_still_rejected(self):
        opened = self.open(prior_proof=True)
        self.current = self.render(clock='10:21')
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened))
        self.assertEqual(error.exception.code,'target_recovery_context_changed')
        self.assert_no_input_or_recipe_change()

    def test_clock_exception_without_prior_condition_proof_is_rejected(self):
        opened = self.open()
        self.current = self.render(clock='10:21')
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened,clock=True))
        self.assertEqual(error.exception.code,'invalid_target_recovery')
        self.assert_no_input_or_recipe_change()

    def test_business_condition_outside_latest_evidence_cannot_hide_behind_clock(self):
        opened = self.open(prior_proof=True)  # retained proof covers MES, not Family
        self.current = self.render(clock='10:21',family='Standard')
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened,clock=True))
        self.assertEqual(error.exception.code,'target_recovery_context_changed')
        self.assert_no_input_or_recipe_change()

    def test_changed_retained_business_condition_is_rejected(self):
        opened = self.open(prior_proof=True)
        self.current = self.render(clock='10:21',mes='W')
        with self.assertRaises(OperationError) as error:
            self.verify(self.submission(opened,clock=True))
        self.assertEqual(error.exception.code,'target_recovery_context_changed')
        self.assert_no_input_or_recipe_change()

    def test_clock_mask_cannot_overlap_protected_regions_or_exceed_budget(self):
        opened = self.open(prior_proof=True)
        for region in (self.CELL, {'x':20,'y':100,'width':50,'height':12},
                       {'x':20,'y':10,'width':50,'height':12},
                       {'x':400,'y':50,'width':80,'height':60}):
            submitted = self.submission(opened,clock=True)
            submitted['volatile_regions'][0]['region'] = dict(region)
            with self.subTest(region=region), self.assertRaises(OperationError) as error:
                self.verify(submitted)
            self.assertEqual(error.exception.code,'invalid_target_recovery')
        self.assert_no_input_or_recipe_change()


if __name__ == '__main__':
    unittest.main()
