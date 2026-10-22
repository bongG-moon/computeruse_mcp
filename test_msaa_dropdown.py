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
from closing import ProbeError


def data(*rects):
    return {'frames':[{'x':100,'y':50,'width':100,'height':100}],
            'lists':[{'role':'List','screen_rect':r} for r in rects]}


class MsaaTests(unittest.TestCase):
    def test_native_hwnd_failure_is_distinct_from_provider_error_and_not_retried(self):
        for native_code,expected in [('target_handle_reused','msaa_target_changed'),
                ('window_class_read_failed','msaa_native_probe_failed')]:
            runtime=Runtime(Probe([]))
            runtime.probe.snapshot=mock.Mock(side_effect=ProbeError(native_code))
            with mock.patch.object(msaa,'_read_once') as read:
                with self.assertRaises(OperationError) as caught:
                    msaa.read_lists(runtime,{'pid':42,'window_id':10})
                self.assertEqual(caught.exception.code,expected)
                self.assertIn(native_code,str(caught.exception))
                read.assert_not_called()
            self.assertTrue(runtime.probe.closed)

    def test_helper_malformed_output_has_bounded_diagnostic(self):
        runtime=SimpleNamespace(check_active=lambda:None,guard=SimpleNamespace(policy={},process_resolver=None,window_resolver=None))
        for output in ('not json','[]','null'):
            process=mock.Mock(returncode=0)
            process.communicate.return_value=(output,'')
            process.poll.return_value=0
            with mock.patch('vendor.guard.validate_arguments'), mock.patch.object(msaa.Path,'is_file',return_value=True), \
                    mock.patch.object(msaa.subprocess,'Popen',return_value=process):
                with self.assertRaises(OperationError) as caught:
                    msaa._read_once(runtime,{'pid':42,'window_id':10})
                self.assertEqual(caught.exception.code,'msaa_invalid_response')

    def test_transient_provider_retry_is_read_only_and_bounded(self):
        runtime = Runtime(Probe([]))
        with mock.patch.object(msaa, '_read_once', side_effect=[
                OperationError('provider range', 'msaa_provider_range'), {'lists':[]}]) as read:
            result = msaa.read_lists(runtime, {'pid':42,'window_id':10})
        self.assertEqual(result['read_attempts'], 2)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(runtime.probe.closed)

    def test_changed_hwnd_never_retries_provider(self):
        runtime = Runtime(Probe([]))
        def read(*args, **kwargs):
            runtime.probe.state['creation_time'] = 99
            raise OperationError('provider', 'msaa_provider_error')
        with mock.patch.object(msaa, '_read_once', side_effect=read) as call:
            with self.assertRaises(OperationError) as caught:
                msaa.read_lists(runtime, {'pid':42,'window_id':10})
        self.assertEqual(caught.exception.code, 'msaa_target_changed')
        self.assertEqual(call.call_count, 1)
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
