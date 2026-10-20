import copy
import threading
import unittest
from unittest import mock
from operations import OperationError
import transient_popup as popup
from test_image_steps import ImageGuardTests, TARGET, image_target


def missing(hwnd=TARGET['window_id'], **extra):
    return {'isError': True, 'structuredContent': {
        'code': 'tool_invocation_failed', 'diagnostic_code': 'target_unavailable',
        'message': f'No window with window_id {hwnd} exists.', **extra}}


class PopupContractTests(unittest.TestCase):
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


class GuardPopupTests(ImageGuardTests):
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
