import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from operations import OperationError
import transient_popup as popup
from test_image_steps import ImageGuardTests, TARGET, image_target


def missing(hwnd=TARGET['window_id'], **extra):
    return {'isError': True, 'structuredContent': {
        'code': 'tool_invocation_failed', 'diagnostic_code': 'target_unavailable',
        'message': f'No window with window_id {hwnd} exists.', **extra}}


class PopupContractTests(unittest.TestCase):
    def test_main_review_discovers_child_hwnd_from_transition_snapshot(self):
        owner={'pid':42,'window_id':1}; target={'pid':42,'window_id':2}
        baseline={'process_exited':False,'target_present':True,'creation_time':123,'executable':'allowed.exe','windows':[]}
        probe=mock.Mock()
        probe.capture.return_value=baseline
        probe.transition_snapshot.return_value={**baseline,'windows':[{**target,'visible':True,'class_name':'ComboLBox'}]}
        probe.ownership_path.return_value=[2,1]
        proof={'process':(123,'allowed.exe'),'path':[2,1],'window':{'thread_id':7},'bounds':(0,0,100,100)}
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value=proof):
            self.assertEqual(popup.current_popup(owner,SimpleNamespace(create_transition_probe=lambda _:probe)),target)
        probe.transition_snapshot.assert_called_once()
        probe.close.assert_called_once()

    def test_child_discovery_rejects_owner_process_replacement(self):
        owner={'pid':42,'window_id':1}
        baseline={'process_exited':False,'target_present':True,'creation_time':123,'executable':'allowed.exe','windows':[]}
        probe=mock.Mock()
        probe.capture.return_value=baseline
        probe.transition_snapshot.return_value={**baseline,'creation_time':456}
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'register_popup') as register:
            with self.assertRaises(OperationError) as caught:
                popup.current_popup(owner,SimpleNamespace(create_transition_probe=lambda _:probe))
            self.assertEqual(caught.exception.code,'popup_owner_changed')
            register.assert_not_called()
        probe.close.assert_called_once()

    def test_review_restore_rejects_changed_proof_and_rebuilds_fresh_scope(self):
        owner={'pid':42,'window_id':1}; target={'pid':42,'window_id':2}
        proof={'process':(123,'allowed.exe'),'path':[2,1],'window':{'thread_id':7},'bounds':(0,0,100,100),'foreground':1}
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value=proof):
            self.assertTrue(popup.register_popup(target,owner))
            saved=popup.popup_proof(target)
        for key,value in [('process',(124,'allowed.exe')),('path',[2,3,1]),('window',{'thread_id':8}),('bounds',(0,0,200,100))]:
            with self.subTest(key=key), popup.recorded_window_scope({'steps':[]}), \
                    mock.patch.object(popup,'_snapshot',return_value={**proof,key:value}):
                self.assertFalse(popup.register_popup(target,owner,expected_proof=saved))
                self.assertEqual(popup.popup_diagnostic(target)['code'],'popup_identity_changed')
        with popup.recorded_window_scope({'steps':[]}), mock.patch.object(popup,'_snapshot',return_value={**proof,'foreground':2}):
            self.assertTrue(popup.register_popup(target,owner,expected_proof=saved))
            self.assertEqual(popup.popup_owner(target),owner)

    def test_actual_driver_0282_split_code_and_plain_text(self):
        answer={"isError":True,"structuredContent":{"code":"tool_invocation_failed"},
                "content":[{"type":"text","text":f'No window with window_id {TARGET["window_id"]} exists. Call `list_windows({{"pid": 42}})` for candidates.'}]}
        self.assertTrue(popup.exact_missing(answer,TARGET["window_id"]))
        self.assertFalse(popup.exact_missing(answer,98765))
        answer["structuredContent"]["diagnostic_code"]="stale_observation"
        self.assertFalse(popup.exact_missing(answer,TARGET["window_id"]))

    def test_only_exact_driver_missing_error_qualifies(self):
        self.assertTrue(popup.exact_missing(missing(), TARGET['window_id']))
        for answer in (missing(hwnd=99), missing(code='denied'),
                       missing(diagnostic_code='stale_observation'), missing(input_sent=True),
                       missing(message='No window exists'), {'isError':False}):
            self.assertFalse(popup.exact_missing(answer, TARGET['window_id']))

    def test_missing_message_never_overrides_refusal_or_delivery_evidence(self):
        for extra in ({'input_dispatched':True}, {'input_sent':None}, {'input_dispatched':None},
                      {'dispatch':'unknown'}, {'delivery':{'delivered_count':1}},
                      {'delivery':{'state':'unknown'}}, {'error_code':'access_denied'},
                      {'diagnostic':{'code':'stale_observation'}}, {'error':{'code':'access_denied'}}):
            with self.subTest(extra=extra):
                self.assertFalse(popup.exact_missing(missing(**extra),TARGET['window_id']))
        self.assertTrue(popup.exact_missing(missing(input_sent=False),TARGET['window_id']))

    def test_failed_registration_retains_reason_and_revokes_old_proof(self):
        owner={'pid':42,'window_id':1};target={'pid':42,'window_id':2}
        with popup.recorded_window_scope({'steps':[]}):
            with mock.patch.object(popup,'_snapshot',return_value={'proof':True}):
                self.assertTrue(popup.register_popup(target,owner))
            with mock.patch.object(popup,'_snapshot',side_effect=OperationError('focus','popup_foreground_mismatch')):
                self.assertFalse(popup.register_popup(target,owner))
            self.assertEqual(popup.popup_diagnostic(target)['code'],'popup_foreground_mismatch')
            self.assertFalse(popup._scope.get()['bindings'])
        self.assertEqual(popup.popup_diagnostic(target)['code'],'popup_scope_unverified')

    def test_multiple_proven_popups_are_not_arbitrarily_selected(self):
        owner={'pid':42,'window_id':1}
        with popup.recorded_window_scope({'steps':[]}):
            with mock.patch.object(popup,'_snapshot',return_value={'proof':True}):
                popup.register_popup({'pid':42,'window_id':2},owner)
                popup.register_popup({'pid':42,'window_id':3},owner)
            with mock.patch.object(popup,'proven_popup',return_value=True):
                with self.assertRaises(OperationError) as error: popup.current_popup(owner)
                self.assertEqual(error.exception.code,'popup_observation_ambiguous')

    def test_scope_cleanup_and_class_style(self):
        for style in ('808', '20808'):
            self.assertTrue(popup._allowed_class(f'WindowsForms10.Window.{style}.app.0.aaa_r10_ad1', set()))
        self.assertFalse(popup._allowed_class('WindowsForms10.EDIT.808.app.0.aaa_r10_ad1', set()))
        with popup.recorded_window_scope({'steps': []}):
            self.assertIsNotNone(popup._scope.get())
        self.assertIsNone(popup._scope.get())

    def test_hung_renderer_does_not_spawn_more_threads_and_scope_is_copied(self):
        gate, started = threading.Event(), threading.Event()
        def slow():
            started.set(); gate.wait(2)
            return popup._scope.get()['classes']
        with popup.recorded_window_scope({'steps':[{'operation':'wait_for_window','class_name':'Fixture'}]}):
            try:
                with self.assertRaises(OperationError) as caught: popup._bounded_render(slow, .01)
                self.assertEqual(caught.exception.code, 'popup_render_timeout')
                self.assertTrue(started.wait(.1))
                with self.assertRaises(OperationError) as caught: popup._bounded_render(lambda: None, .01)
                self.assertEqual(caught.exception.code, 'popup_render_busy')
            finally: gate.set()
        for _ in range(100):
            if not popup._render_lock.locked(): break
            threading.Event().wait(.01)
        with popup.recorded_window_scope({'steps':[]}):
            self.assertEqual(popup._bounded_render(lambda: popup._scope.get()['classes']), set())


class PopupNativeContractTests(unittest.TestCase):
    def native(self, *, foreign=False, replaced=False, timeout=False):
        user=mock.Mock()
        user.SetThreadDpiAwarenessContext.return_value=0
        user.ScreenToClient.return_value=True
        user.ChildWindowFromPointEx.return_value=300
        user.WindowFromPoint.return_value=300
        user.SetCursorPos.return_value=True
        user.PostMessageW.return_value=True
        user.IsWindowEnabled.return_value=True
        def thread(hwnd,pid):
            pid._obj.value=999 if foreign else TARGET['pid']
            return 7
        user.GetWindowThreadProcessId.side_effect=thread
        user.SendMessageTimeoutW.return_value=0 if timeout else 1
        probe=mock.Mock()
        probe.ownership_path.side_effect=([ [300,200], [301,200] ] if replaced else None)
        probe.ownership_path.return_value=[300,200]
        proof={'bounds':(0,0,100,100)}
        with mock.patch.object(popup,'_user',return_value=user), \
                mock.patch.object(popup,'_binding',return_value=({},proof)), \
                mock.patch('privileges.input_integrity_preflight',return_value=None), \
                mock.patch('closing.NativeClosureProbe',return_value=probe):
            try:
                result=popup.click(TARGET,{'x':30,'y':40},(0,0,100,100,2),lambda:None)
            except OperationError as error:
                result=error
        return result,user,probe

    def test_message_timeout_is_unknown_and_never_retried(self):
        result,user,probe=self.native(timeout=True)
        self.assertTrue(result['isError'])
        self.assertIsNone(result['structuredContent']['input_sent'])
        self.assertFalse(result['structuredContent']['task_verified'])
        self.assertEqual(user.SendMessageTimeoutW.call_count,1)
        probe.close.assert_called_once()

    def test_foreign_child_and_changed_path_never_receive_messages(self):
        for options,code in [({'foreign':True},'popup_child_mismatch'),({'replaced':True},'popup_identity_changed')]:
            result,user,_=self.native(**options)
            self.assertIsInstance(result,OperationError)
            self.assertEqual(result.code,code)
            user.SendMessageTimeoutW.assert_not_called()


class GuardPopupTests(ImageGuardTests):
    def test_native_fallback_rechecks_image_after_driver_missing(self):
        def after(request,result):
            if request['name']=='click': result.clear(); result.update(missing())
        self.transport.after=after
        self.matcher.side_effect=[dict(self.match), {'status':'not_found'}]
        with mock.patch.object(popup,'click') as click:
            result=self.run_step()
        self.assertFalse(result['input_dispatched'])
        self.assertFalse(result['task_verified'])
        self.assertEqual(self.matcher.call_count,2)
        self.assertEqual(len(self.mutations()),1)
        click.assert_not_called()

    def test_fallback_does_not_use_moved_target_or_ignore_stop(self):
        self.transport.close=mock.Mock()
        for stopped in (False,True):
            self.guard.action_count=0
            self.transport.calls.clear()
            def after(request,result):
                if request['name']=='click':
                    result.clear(); result.update(missing())
                    if stopped: (self.guard.run_dir/'stop.flag').write_text('stop',encoding='utf-8')
            self.transport.after=after
            self.matcher.side_effect=[dict(self.match),{**self.match,'x':self.match['x']+2}]
            with mock.patch.object(popup,'click') as click:
                result=self.run_step()
            self.assertFalse(result['input_dispatched'])
            self.assertFalse(result['task_verified'])
            click.assert_not_called()

    def test_guard_exact_missing_click_uses_one_internal_backend(self):
        def after(request, result):
            if request['name'] == 'click':
                result.clear(); result.update(missing())
        self.transport.after = after
        with mock.patch.object(popup, 'click', return_value={'structuredContent': {
                'backend':'owned-popup-messages','input_sent':True,'task_verified':False}}) as click:
            result = self.run_step()
        self.assertTrue(result['input_dispatched'])
        self.assertFalse(result['task_verified'])
        self.assertEqual(result['input_backend'], 'owned-popup-messages')
        self.assertEqual(self.guard.action_count, 1)
        click.assert_called_once()
        journal=[json.loads(line) for line in (self.guard.run_dir/'actions.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(row.get('event')=='request' and row.get('tool')=='click' for row in journal))
        self.assertTrue(any(row.get('event')=='result' and row.get('summary',{}).get('backend')=='owned-popup-messages' for row in journal))

    def test_denied_approval_never_reaches_driver_or_fallback(self):
        from vendor.guard import GuardError
        self.guard.approve=mock.Mock(side_effect=GuardError('approval denied'))
        with mock.patch.object(popup,'click') as click:
            result=self.run_step()
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(self.guard.action_count,0)
        self.assertEqual(self.mutations(),[])
        click.assert_not_called()

    def test_wrong_error_and_text_focus_never_use_fallback(self):
        for error, operation in ((missing(diagnostic_code='access_denied'), 'image_click'),
                                 (missing(hwnd=99), 'image_click'), (missing(), 'image_type_text')):
            self.transport.calls.clear()
            def after(request, result):
                if request['name'] == 'click':
                    result.clear(); result.update(error)
            self.transport.after = after
            step = {'operation':operation,'image_target':image_target()}
            if operation == 'image_type_text': step['value'] = 'not delivered'
            with mock.patch.object(popup, 'click') as click:
                self.run_step(step)
            click.assert_not_called()

    def test_budget_and_stale_geometry_prevent_fallback(self):
        self.guard.action_count = self.policy['max_actions']
        with mock.patch.object(popup, 'click') as click:
            self.run_step()
        click.assert_not_called()
