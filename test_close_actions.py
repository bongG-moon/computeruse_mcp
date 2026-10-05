import threading
import unittest
from unittest.mock import Mock

from close_actions import request_close, validate_close_action
from operations import OperationError
from test_operations import Runtime, TARGET, element, snapshot


def observed(status='pending', **extra):
    return dict(close_id='a'*32, status=status, task_verified=status=='verified',
                window_closed=status=='verified', process_exited=status=='verified', **extra)


class CloseActionTests(unittest.TestCase):
    def runtime(self, states=None, actions=None):
        runtime = Runtime([snapshot(element(name='종료', role='Button'))], actions=actions)
        runtime.execution_lock = threading.RLock()
        runtime.closures = Mock()
        runtime.closures.prepare.return_value = {'close_id':'a'*32, 'scope':'window', 'target':TARGET}
        runtime.closures.verify.side_effect = states or [observed(), observed(), observed('verified')]
        return runtime

    def click(self, runtime, **kw):
        return request_close(runtime, TARGET, {'operation':'click','selector':{'name':'종료','role':'Button'}}, **kw)

    def test_close_checks_native_evidence_without_rereading_closed_window(self):
        runtime=self.runtime()
        result=self.click(runtime)
        self.assertTrue(result['task_verified'])
        self.assertEqual([name for name,_ in runtime.calls], ['get_window_state','click'])
        self.assertEqual(runtime.calls[-1][1]['element_token'],'s1:0')
        runtime.closures.prepare.assert_called_once_with(TARGET,scope='window')
        self.assertEqual(runtime.closures.verify.call_args.kwargs['timeout_ms'],1500)

    def test_owned_dialog_is_pending_and_close_request_is_not_repeated(self):
        runtime=self.runtime([observed(),observed(),observed('needs_dialog')])
        result=self.click(runtime)
        self.assertFalse(result['task_verified'])
        self.assertEqual(result['status'],'needs_dialog')
        self.assertEqual([name for name,_ in runtime.calls].count('click'),1)
        self.assertFalse(result['close_request_replayed'])

    def test_existing_dialog_or_unknown_state_never_sends_close(self):
        for status in ('needs_dialog','unknown','verified'):
            runtime=self.runtime([observed(status)])
            result=self.click(runtime)
            self.assertEqual(runtime.calls,[])
            self.assertFalse(result['input_dispatched'])

    def test_after_observation_dialog_or_exit_prevents_stale_action(self):
        for status in ('needs_dialog','unknown','verified'):
            runtime=self.runtime([observed(),observed(status)])
            result=self.click(runtime)
            self.assertEqual([c[0] for c in runtime.calls],['get_window_state'])
            self.assertFalse(result['input_dispatched'])

    def test_enabled_parent_with_owned_modeless_window_can_close_both_once(self):
        state=observed('needs_dialog',remaining_windows=[{**TARGET,'enabled':True},
            {'pid':TARGET['pid'],'window_id':99,'owner_window_id':TARGET['window_id'],'enabled':True}])
        runtime=self.runtime([state,state,observed('verified')])
        answer=self.click(runtime)
        self.assertTrue(answer['task_verified'])
        self.assertEqual([c[0] for c in runtime.calls],['get_window_state','click'])

    def test_disabled_parent_never_receives_close_even_without_known_dialog(self):
        state=observed(remaining_windows=[{**TARGET,'enabled':False}])
        runtime=self.runtime([state])
        answer=self.click(runtime)
        self.assertFalse(answer['input_dispatched'])
        self.assertEqual(runtime.calls,[])

    def test_driver_ack_error_does_not_override_proven_closure_or_cause_retry(self):
        runtime=self.runtime(actions=[{'isError':True,'content':[{'type':'text','text':'target disappeared'}]}])
        result=self.click(runtime)
        self.assertTrue(result['task_verified'])
        self.assertIsNotNone(result['action_error'])
        self.assertEqual([c[0] for c in runtime.calls].count('click'),1)

    def test_refused_input_is_reported_and_not_retried(self):
        runtime=self.runtime([observed(),observed(),observed()],actions=[{
            'isError':True,'structuredContent':{'input_sent':False,'error_code':'foreground_unavailable'}}])
        result=self.click(runtime)
        self.assertFalse(result['task_verified'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['action_error']['code'],'foreground_unavailable')
        self.assertEqual([c[0] for c in runtime.calls].count('click'),1)

    def test_explicit_hotkey_forwards_once_through_runtime(self):
        runtime=self.runtime()
        result=request_close(runtime,TARGET,{'operation':'hotkey','keys':['alt','f4']},scope='process',delivery_mode='foreground')
        self.assertTrue(result['task_verified'])
        self.assertEqual(runtime.calls[-1],('hotkey',{**TARGET,'keys':['alt','f4'],'delivery_mode':'foreground'}))
        runtime.closures.prepare.assert_called_once_with(TARGET,scope='process')

    def test_invalid_action_mode_timeout_never_prepares_or_mutates(self):
        for action in ({'operation':'click'}, {'operation':'click','selector':{'name':'종료'},'keys':['a']},
                       {'operation':'terminate'}, {'operation':'hotkey','keys':['a','a']},
                       {'operation':'hotkey','keys':['']}, {'operation':'click','selector':{'role':'Button'}}):
            with self.assertRaises(OperationError): validate_close_action(action)
        for kw in ({'timeout_ms':True},{'timeout_ms':10001},{'delivery_mode':'auto'}):
            runtime=self.runtime()
            with self.assertRaises(OperationError): self.click(runtime,**kw)
            runtime.closures.prepare.assert_not_called()
        runtime=self.runtime();runtime.mode='visual'
        with self.assertRaises(OperationError):self.click(runtime)
        runtime.closures.prepare.assert_not_called()

    def test_failed_accessibility_observation_never_closes_and_still_reports_native_state(self):
        runtime=self.runtime([observed(),observed()])
        runtime.observations=[RuntimeError('unavailable')]
        result=self.click(runtime)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual([c[0] for c in runtime.calls],['get_window_state'])
        self.assertFalse(result['task_verified'])


if __name__=='__main__': unittest.main()
