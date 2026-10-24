"""Selection appearance is fresh runtime evidence, never a saved target flag."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import airtest_backend
from session_runtime import SessionRuntime
from operations import OperationError
from test_image_steps import TARGET, image_target, png


class SelectionCapabilityTests(unittest.TestCase):
    def test_session_image_action_reproves_popup_for_each_comparison(self):
        runtime = SimpleNamespace(execution_lock=threading.RLock(),check_active=mock.Mock(),
            _driver_ended=lambda:False,msaa_replay_target=None)
        image = image_target()
        def guard_action(step, target, compare, active):
            compare(image,png(100,100))
            return {'input_dispatched':False,'diagnostic':{}}
        runtime.guard = SimpleNamespace(image_action=guard_action)
        with mock.patch('transient_popup.proven_popup',side_effect=[False,True,False]) as proof, \
                mock.patch('image_targets.match_image',return_value={'status':'not_found'}) as match:
            for _ in range(3):
                SessionRuntime.image_action(runtime,{'operation':'wait_for_image'},TARGET)
        self.assertEqual([c.kwargs['allow_selection_normalization'] for c in match.call_args_list],[False,True,False])
        self.assertEqual(proof.call_args_list,[mock.call(TARGET)]*3)

    def test_msaa_capability_follows_fresh_read_and_never_survives_failed_read(self):
        runtime = SimpleNamespace(execution_lock=threading.RLock(),check_active=mock.Mock(),
            _driver_ended=lambda:False,msaa_replay_target=copy.deepcopy(TARGET))
        def guard_action(step,target,compare,active):
            compare(image_target(),png(100,100))
            return {'input_dispatched':False,'diagnostic':{}}
        runtime.guard = SimpleNamespace(image_action=guard_action)
        with mock.patch('msaa_dropdown.read_lists',return_value={'lists':[]}) as read, \
                mock.patch('msaa_dropdown.match_lists',return_value={'status':'not_found'}) as match:
            SessionRuntime.image_action(runtime,{'operation':'wait_for_image'},TARGET)
            read.assert_called_once_with(runtime,TARGET)
            self.assertTrue(match.call_args.kwargs['allow_selection_normalization'])
            self.assertIsNone(runtime.msaa_replay_target)
        runtime.msaa_replay_target = copy.deepcopy(TARGET)
        with mock.patch('msaa_dropdown.read_lists',side_effect=OperationError('changed','msaa_target_changed')), \
                mock.patch('msaa_dropdown.match_lists') as match:
            with self.assertRaises(OperationError):
                SessionRuntime.image_action(runtime,{'operation':'wait_for_image'},TARGET)
            match.assert_not_called()
            self.assertIsNone(runtime.msaa_replay_target)

    def test_worker_schema_preserves_boolean_capability_and_rejects_extra_fields(self):
        value = {'nonce':'a'*64,'target':image_target(),'screenshot_png':png(100,100),
                 'allow_selection_normalization':True}
        with tempfile.TemporaryDirectory() as directory:
            request,response = Path(directory)/'in.json',Path(directory)/'out.json'
            for capability in (False,True):
                payload = {**value,'allow_selection_normalization':capability}
                request.write_text(json.dumps(payload),encoding='utf-8')
                with mock.patch.object(airtest_backend,'match_candidates',return_value={'status':'not_found'}) as compare:
                    self.assertEqual(airtest_backend._worker_main(['--image-worker',str(request),str(response)]),0)
                    self.assertIs(compare.call_args.kwargs['allow_selection_normalization'],capability)
                response.unlink()
            for changed in ({'allow_selection_normalization':'true'}, {'unknown':True}):
                request.write_text(json.dumps({**value,**changed}),encoding='utf-8')
                with mock.patch.object(airtest_backend,'match_candidates') as compare:
                    self.assertEqual(airtest_backend._worker_main(['--image-worker',str(request),str(response)]),2)
                    compare.assert_not_called()

    def test_real_isolated_worker_receives_capability(self):
        from test_menu_appearance import MenuAppearanceTests
        MenuAppearanceTests.setUpClass()
        fixture = MenuAppearanceTests()
        pattern,current = fixture.menu(),fixture.menu(selected=1)
        target = {'format':'computer-image-target/v1','template_png':fixture.png(pattern),
                  'width':269,'height':99,'anchor':{'x':.4,'y':.5},
                  'capture_window':{'width':269,'height':99},'min_score':.94,'ambiguity_margin':.03}
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(run_dir=directory,check_active=mock.Mock(),stop_event=threading.Event())
            self.assertNotEqual(airtest_backend.match_isolated(runtime,target,fixture.png(current))['status'],'matched')
            result = airtest_backend.match_isolated(runtime,target,fixture.png(current),allow_selection_normalization=True)
            self.assertEqual(result['status'],'matched')
            self.assertEqual(list((Path(directory)/'im').iterdir()),[])


if __name__ == '__main__':
    unittest.main()
