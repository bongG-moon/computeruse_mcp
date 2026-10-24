"""Timeout retains the latest bounded image evidence, never sends input."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from image_steps import execute_image_step
from test_image_steps import TARGET, image_target


class ImageWaitDiagnosticTests(unittest.TestCase):
    def runtime(self, answer):
        return SimpleNamespace(mode='uia', check_active=mock.Mock(), stop_event=threading.Event(),
                               image_action=mock.Mock(return_value=answer))

    def test_timeout_preserves_cause_scores_backend_and_dimensions(self):
        answer = {'status':'needs_review','input_dispatched':False,'task_verified':False,
                  'diagnostic':{'code':'image_not_found','message':'unchanged original reason',
                                'appearance_rejected':'foreground_ink_changed'},
                  'image_match':{'status':'not_found','score':.85,'second_score':.82,
                                 'backend':'airtest-template-rgb','candidate_count':0,
                                 'screenshot':{'width':269,'height':99},'original_score':.72}}
        original = copy.deepcopy(answer)
        runtime = self.runtime(answer)
        result = execute_image_step(runtime, {'operation':'wait_for_image','timeout_ms':0,'image_target':image_target()},TARGET)
        self.assertEqual(result['diagnostic']['code'],'image_wait_timeout')
        self.assertEqual(result['diagnostic']['cause'],answer['diagnostic'])
        self.assertEqual(result['diagnostic']['last_match'],answer['image_match'])
        self.assertEqual(result['diagnostic']['template']['capture_window'],{'width':100,'height':100})
        self.assertNotIn('template_png',result['diagnostic']['template'])
        self.assertEqual(answer,original)
        self.assertFalse(result['input_dispatched'])
        self.assertFalse(result['business_result_verified'])
        runtime.image_action.assert_called_once()

    def test_ambiguity_and_capture_refusal_are_not_relabelled_or_retried(self):
        for code in ('image_ambiguous','image_target_unavailable','image_capture_refused'):
            answer = {'input_dispatched':False,'diagnostic':{'code':code}}
            runtime = self.runtime(answer)
            result = execute_image_step(runtime, {'operation':'wait_for_image','timeout_ms':0,'image_target':image_target()},TARGET)
            self.assertEqual(result['diagnostic'],answer['diagnostic'])
            runtime.image_action.assert_called_once()

    def test_real_guard_nested_match_contract_is_preserved(self):
        match = {'score':.85,'backend':'airtest-template-rgb','screenshot':{'width':269,'height':99},
                 'appearance_score':.99,'appearance_rejected':'foreground_ink_changed'}
        answer = {'input_dispatched':False,'diagnostic':{'code':'image_not_found','image_match':match}}
        result = execute_image_step(self.runtime(answer),
            {'operation':'wait_for_image','timeout_ms':0,'image_target':image_target()},TARGET)
        self.assertEqual(result['diagnostic']['last_match'],match)
        self.assertEqual(result['diagnostic']['cause']['image_match'],match)


if __name__ == '__main__':
    unittest.main()
