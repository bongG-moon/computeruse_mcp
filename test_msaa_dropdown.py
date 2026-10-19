import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import msaa_dropdown as msaa
from operations import OperationError
from test_image_steps import png, image_target
from test_recording_windows import Probe, Runtime, step
from recording_windows import execute_window_wait


def data(*rects):
    return {'frames':[{'x':100,'y':50,'width':100,'height':100}],
            'lists':[{'role':'List','screen_rect':r} for r in rects]}


class MsaaTests(unittest.TestCase):
    def test_geometry_dpi_and_multiple_frame_contracts(self):
        d=data({'x':110,'y':60,'width':30,'height':30})
        self.assertEqual(msaa.regions(d,(100,100)),[{'x':10,'y':10,'width':30,'height':30}])
        with self.assertRaises(OperationError): msaa.regions(d,(150,150))
        d['frames'].append({'x':101,'y':50,'width':100,'height':100})
        with self.assertRaises(OperationError): msaa.regions(d,(100,100))

    def test_old_template_retained_and_crop_coordinates_translated(self):
        old=image_target(); original=copy.deepcopy(old)
        proof={'status':'matched','x':3,'y':4,'rect':{'x':1,'y':2,'width':16,'height':16},'score':.99}
        with mock.patch.object(msaa,'match_image',return_value=proof) as matcher:
            answer=msaa.match_lists(SimpleNamespace(),old,png(100,100),data({'x':110,'y':60,'width':40,'height':40}))
        self.assertEqual(answer['x'],13); self.assertEqual(answer['y'],14)
        self.assertEqual(answer['rect']['x'],11)
        self.assertEqual(answer['screenshot'],{'width':100,'height':100})
        self.assertEqual(old,original); self.assertEqual(matcher.call_args.args[1],old)

    def test_two_lists_or_ambiguous_match_never_provide_click(self):
        d=data({'x':100,'y':50,'width':40,'height':40},{'x':150,'y':100,'width':40,'height':40})
        proof={'status':'matched','x':3,'y':4,'rect':{'x':1,'y':2,'width':16,'height':16},'score':.99}
        with mock.patch.object(msaa,'match_image',return_value=proof):
            result=msaa.match_lists(SimpleNamespace(),image_target(),png(100,100),d)
        self.assertEqual(result['status'],'ambiguous'); self.assertNotIn('x',result)

    def test_old_wait_recipe_recovers_without_mutation_or_click_replay(self):
        runtime=Runtime(Probe([])); runtime.recover_msaa_dropdown=mock.Mock(return_value={'pid':42,'window_id':10})
        original=step(); saved=copy.deepcopy(original)
        answer=execute_window_wait(runtime,original,{('editor','main'):{'pid':42,'window_id':10}},image_hint=image_target())
        self.assertTrue(answer['task_verified']); self.assertEqual(answer['accessibility_scope'],'msaa_list')
        self.assertFalse(answer['input_dispatched']); self.assertEqual(original,saved)

    def test_no_image_never_enables_legacy_recovery(self):
        runtime=Runtime(Probe([])); runtime.recover_msaa_dropdown=mock.Mock()
        answer=execute_window_wait(runtime,step(),{('editor','main'):{'pid':42,'window_id':10}})
        self.assertFalse(answer['task_verified']); runtime.recover_msaa_dropdown.assert_not_called()

    def test_msaa_timeout_survives_as_actionable_diagnostic(self):
        runtime=Runtime(Probe([]))
        runtime.recover_msaa_dropdown=mock.Mock(side_effect=OperationError('timeout','msaa_timeout'))
        result=execute_window_wait(runtime,step(),{('editor','main'):{'pid':42,'window_id':10}},image_hint=image_target())
        self.assertEqual(result['diagnostic']['code'],'msaa_timeout'); self.assertFalse(result['input_dispatched'])

if __name__=='__main__': unittest.main()
