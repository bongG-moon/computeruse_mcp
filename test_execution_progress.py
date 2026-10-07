import io
import json
import threading
import time
import unittest
from unittest import mock

from execution_progress import ExecutionProgress
from image_delivery import ImageDelivery, image_png
import test_server as fixtures


class ProgressStateTests(unittest.TestCase):
    def test_late_old_updates_do_not_change_new_operation_or_leak_content(self):
        now=[10.0]
        progress=ExecutionProgress(clock=lambda:now[0])
        first=progress.start('computer_perform')
        progress.update(first,'verifying',value='private',selector='private',step_index=2,step_total=5)
        now[0]=12.5
        self.assertEqual(progress.snapshot()['elapsed_ms'],2500)
        self.assertNotIn('private',json.dumps(progress.snapshot()))
        second=progress.start('computer_run_task')
        progress.update(first,'failed')
        progress.finish(first,error=True)
        self.assertTrue(progress.snapshot()['active'])
        self.assertEqual(progress.snapshot()['operation_id'],second)
        progress.finish(second)
        self.assertFalse(progress.snapshot()['active'])
        self.assertFalse(progress.snapshot()['task_verified'])

    def test_notification_failure_never_fails_or_repeats_execution(self):
        def bad_sink(value):
            raise OSError('closed client')
        progress=ExecutionProgress()
        identity=progress.start('click',sink=bad_sink)
        progress.update(identity,'acting')
        progress.finish(identity)
        self.assertFalse(progress.snapshot()['active'])

    def test_final_notification_cannot_overtake_delayed_callback(self):
        entered, release, finished=threading.Event(), threading.Event(), threading.Event()
        seen=[]
        def sink(value):
            if value['stage']=='observing':
                entered.set()
                self.assertTrue(release.wait(2))
            seen.append((value['sequence'],value['stage']))
        progress=ExecutionProgress()
        identity=progress.start('inspect',sink=sink)
        observer=threading.Thread(target=lambda:progress.update(identity,'observing'))
        observer.start()
        self.assertTrue(entered.wait(1))
        def finish():
            progress.finish(identity)
            finished.set()
        finisher=threading.Thread(target=finish)
        finisher.start()
        self.assertFalse(finished.wait(.03))
        release.set()
        observer.join(2);finisher.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual([row[0] for row in seen],sorted(row[0] for row in seen))
        self.assertEqual(seen[-1][1],'finished')

    def test_late_callback_cannot_replace_terminal_state_while_notification_waits(self):
        progress=ExecutionProgress()
        seen=[]
        identity=progress.start('inspect',sink=lambda snapshot:seen.append(snapshot))
        with progress.delivery_lock:
            finisher=threading.Thread(target=lambda:progress.finish(identity))
            finisher.start()
            deadline=time.monotonic()+1
            while progress.snapshot()['active'] and time.monotonic()<deadline:
                time.sleep(.001)
            self.assertFalse(progress.snapshot()['active'])
            progress.update(identity,'verifying')
            self.assertEqual(progress.snapshot()['stage'],'finished')
        finisher.join(2)
        self.assertEqual(seen[-1]['stage'],'finished')
        self.assertFalse(seen[-1]['active'])


class ProtocolProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProtocolTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_activity_and_checkpoint_reads_respond_during_blocked_ui_worker(self):
        f=self.fixture
        f.begin()
        started=time.monotonic()
        f.input.send({'jsonrpc':'2.0','id':30,'method':'tools/call','params':{'name':'computer_activity','arguments':{}}})
        value=f.wait_for(30)['result']['structuredContent']
        self.assertTrue(value['active'])
        self.assertEqual(value['tool'],'computer_begin')
        self.assertLess(time.monotonic()-started,.5)
        f.input.send({'jsonrpc':'2.0','id':31,'method':'tools/call','params':{'name':'computer_task_progress','arguments':{}}})
        self.assertFalse(f.wait_for(31)['result']['isError'])
        self.assertFalse(f.broker.closed.is_set(),'status read must not stop or activate an app')

    def test_progress_notifications_are_correlated_and_finish_before_result(self):
        f=self.fixture
        f.input.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_begin',
            'arguments':{'program_ids':['editor'],'task_description':'private business input'},'_meta':{'progressToken':'progress-2'}}})
        self.assertTrue(f.broker.entered.wait(1))
        notices=[row for row in map(json.loads,f.output.getvalue().splitlines()) if row.get('method')=='notifications/progress']
        self.assertTrue(notices)
        self.assertEqual(notices[0]['params']['progressToken'],'progress-2')
        self.assertNotIn('private business input',json.dumps(notices))
        f.input.send({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'computer_stop','arguments':{}}})
        f.wait_for(3)
        f.wait_for(2)
        lines=[json.loads(line) for line in f.output.getvalue().splitlines()]
        result_index=next(i for i,row in enumerate(lines) if row.get('id')==2)
        self.assertTrue(any(row.get('method')=='notifications/progress' for row in lines[:result_index]))
        self.assertFalse(f.manager.activity.snapshot()['active'])

    def test_activity_does_not_allow_duplicate_active_request_id(self):
        f=self.fixture
        f.begin()
        f.input.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_activity','arguments':{}}})
        self.assertEqual(f.wait_for(2)['error']['code'],-32600)

    def test_invalid_progress_token_is_rejected_before_any_work(self):
        f=self.fixture
        f.input.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_begin','_meta':{'progressToken':{}},'arguments':{}}})
        self.assertEqual(f.wait_for(2)['error']['code'],-32602)
        self.assertIsNone(f.manager.session)

    def test_failed_tool_emits_final_progress_before_response(self):
        f=self.fixture
        f.input.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_check_image',
            'arguments':{'answer':'no challenge'},'_meta':{'progressToken':'failed-image'}}})
        self.assertTrue(f.wait_for(2)['result']['isError'])
        rows=[json.loads(line) for line in f.output.getvalue().splitlines()]
        end=next(i for i,row in enumerate(rows) if row.get('id')==2)
        notices=[row for row in rows[:end] if row.get('method')=='notifications/progress']
        self.assertGreaterEqual(len(notices),2)
        self.assertEqual(notices[-1]['params']['message'],'도구 오류')
        self.assertFalse(f.manager.activity.snapshot()['active'])


class ImageDeliveryTests(unittest.TestCase):
    def test_generated_image_roundtrip_has_no_text_answer_and_one_use_only(self):
        from unittest.mock import patch
        delivery=ImageDelivery()
        with patch('image_delivery.secrets.choice',side_effect=list('A2B3C4')):
            first=delivery.check()
        self.assertEqual(first['image_content']['mimeType'],'image/png')
        self.assertNotIn('A2B3C4',json.dumps(first))
        self.assertFalse(first['screen_accessed'])
        self.assertTrue(delivery.check(first['challenge_id'],'a2b3c4')['roundtrip_verified'])
        self.assertFalse(delivery.status()['model_vision_guaranteed'])
        self.assertEqual(delivery.check(first['challenge_id'],'a2b3c4')['status'],'expired_or_unknown')

    def test_wrong_or_expired_answer_never_claims_vision_success(self):
        now=[1.0]
        delivery=ImageDelivery(clock=lambda:now[0])
        first=delivery.check()
        result=delivery.check(first['challenge_id'],'!')
        self.assertFalse(result['roundtrip_verified'])
        self.assertEqual(delivery.status()['last_roundtrip'],'failed')
        second=delivery.check()
        now[0]=302.0
        self.assertEqual(delivery.check(second['challenge_id'],'ABCDEF')['status'],'expired_or_unknown')

    def test_non_ascii_readback_is_a_failed_check_without_server_error(self):
        delivery=ImageDelivery()
        first=delivery.check()
        self.assertEqual(delivery.check(first['challenge_id'],'잘 안 보여요')['status'],'failed')

    def test_mcp_preserves_image_content_as_image_and_metadata_as_text(self):
        f=fixtures.ProtocolTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.input.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_check_image','arguments':{}}})
        result=f.wait_for(2)['result']
        self.assertEqual([item['type'] for item in result['content']],['text','image'])
        self.assertNotIn('data',result['structuredContent'])
        self.assertFalse(result['structuredContent']['screen_accessed'])


class InspectionIntegrationTests(unittest.TestCase):
    def test_scoped_failure_preserves_code_and_does_not_fallback_to_input(self):
        from operations import OperationError
        f=fixtures.ManagerTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.manager.session=object()
        try:
            with mock.patch('inspection.inspect_window', side_effect=OperationError('범위 확인 시간 초과', 'scoped_observation_timeout')):
                answer=f.manager.call('computer_inspect', {'pid': 1, 'window_id': 2, 'within': {'name': 'area'}})
            self.assertTrue(answer['isError'])
            self.assertEqual(answer['structuredContent']['diagnostic']['code'], 'scoped_observation_timeout')
            self.assertFalse(answer['structuredContent']['input_dispatched'])
        finally:
            f.manager.session=None

    def test_fresh_completion_read_is_available_without_fast_profile_but_requires_uia(self):
        from session_runtime import SessionRuntime
        runtime=object.__new__(SessionRuntime)
        runtime.mode='uia'
        runtime.fast_verification_enabled=False
        runtime.execution_lock=threading.RLock()
        runtime.check_active=mock.Mock()
        runtime.scoped_reader=mock.Mock()
        target={'pid': 1, 'window_id': 2}
        selectors=[{'name': 'done'}]
        runtime.observe_completion_controls(target, selectors, timeout_ms=800)
        runtime.scoped_reader.observe.assert_called_once_with(target, selectors, timeout_ms=800)
        self.assertFalse(runtime.fast_verification_enabled)
        runtime.mode='visual'
        with self.assertRaises(NotImplementedError):
            runtime.observe_completion_controls(target, selectors)
        self.assertEqual(runtime.scoped_reader.observe.call_count,1)


if __name__=='__main__':
    unittest.main()
