import json
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from server import ComputerManager,MANAGEMENT,SessionError,validate_management
from test_server import config_at


class CloseToolsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.manager=ComputerManager(config_at(tmp.name))
        self.runtime=Mock()
        self.runtime.execution_lock=threading.RLock()
        self.runtime.closures.prepare.return_value={'close_id':'a'*32,'scope':'window','target':{'pid':7,'window_id':12}}
        self.runtime.closures.verify.return_value={'status':'pending','task_verified':False}
        self.manager.session=self.runtime

    def test_new_tools_require_active_session(self):
        self.manager.session=None
        for name,args in (
            ('computer_prepare_close',{'pid':7,'window_id':12}),
            ('computer_verify_closed',{'close_id':'a'*32}),
            ('computer_close',{'pid':7,'window_id':12,'close_action':{'operation':'click','selector':{'name':'종료'}}})):
            with self.assertRaises(SessionError):self.manager.call(name,args)

    def test_prepare_and_verify_route_through_session_without_driver_calls(self):
        prepared=self.manager.call('computer_prepare_close',{'pid':7,'window_id':12,'scope':'process'})
        self.runtime.closures.prepare.assert_called_once_with({'pid':7,'window_id':12},scope='process')
        self.assertFalse(prepared['isError'])
        answer=self.manager.call('computer_verify_closed',{'close_id':'a'*32,'timeout_ms':0})
        self.assertFalse(answer['isError'])
        self.runtime.closures.verify.assert_called_once_with('a'*32,timeout_ms=0)
        self.runtime.call.assert_not_called()

    def test_unknown_is_error_and_pending_is_never_verified(self):
        self.runtime.closures.verify.return_value={'status':'unknown','task_verified':False}
        answer=self.manager.call('computer_verify_closed',{'close_id':'a'*32})
        self.assertTrue(answer['isError'])
        self.assertFalse(json.loads(answer['content'][0]['text'])['task_verified'])

    def test_schema_forbids_termination_save_guesses_and_bad_waits(self):
        for name,args in (
            ('computer_prepare_close',{'pid':7,'window_id':12,'scope':'all_apps'}),
            ('computer_verify_closed',{'close_id':'a'*32,'timeout_ms':True}),
            ('computer_verify_closed',{'close_id':'a'*32,'timeout_ms':10001}),
            ('computer_verify_closed',{'close_id':'a'*32,'force':True}),
            ('computer_close',{'pid':7,'window_id':12,'close_action':{},'save_policy':'discard'})):
            with self.assertRaises(SessionError):validate_management(name,args)
        self.assertTrue(MANAGEMENT['computer_prepare_close']['annotations']['readOnlyHint'])
        self.assertTrue(MANAGEMENT['computer_verify_closed']['annotations']['readOnlyHint'])
        self.assertFalse(MANAGEMENT['computer_close']['annotations']['readOnlyHint'])

    def test_end_is_explicitly_not_application_exit(self):
        self.runtime.state='stopped'; self.runtime.status.return_value={'state':'stopped'}
        answer=self.manager.call('computer_end',{})
        data=json.loads(answer['content'][0]['text'])
        self.assertFalse(data['application_exit_checked'])
        self.assertEqual(data['scope'],'automation_session_only')
        self.runtime.closures.verify.assert_not_called()


if __name__=='__main__':unittest.main()
