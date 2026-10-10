"""Text model, real checkpoint identity and chat URI registration contracts."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from image_delivery import ImageDelivery, image_png
from checkpoint_review import CheckpointReviews
from program_registration import protocol_handler
from server import ComputerManager, result
from settings import atomic_json
from test_server import config_at


class ClientCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = ComputerManager(config_at(self.tmp.name))
        self.image = {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(image_png('ABCDEF')).decode()}

    def test_unknown_model_check_does_not_send_image_or_start_driver(self):
        answer = self.manager.call('computer_check_image', {})
        self.assertEqual(answer['structuredContent']['status'], 'text_safe')
        self.assertEqual([c['type'] for c in answer['content']], ['text'])
        self.assertIsNone(self.manager.session)

    def test_central_policy_applies_to_lowlevel_inspection_and_checkpoint(self):
        for tool in ('get_window_state', 'computer_inspect', 'computer_run_task'):
            original = result({'status': 'visual_observation', 'input_dispatched': False})
            original['content'].append(self.image)
            before = copy.deepcopy(original)
            with patch.object(self.manager, '_call', return_value=original):
                answer = self.manager.call(tool, {})
            self.assertEqual(original, before, 'internal pixels must remain available to matching')
            self.assertFalse(any(c['type'] == 'image' for c in answer['content']))
            self.assertNotIn(self.image['data'], json.dumps(answer))
            self.assertFalse(answer['structuredContent']['model_visual_evidence_available'])
            self.assertFalse(answer['structuredContent']['image_delivery']['images_sent_to_model'])

    def test_vision_opt_in_and_text_switch_are_connection_only(self):
        original_config = copy.deepcopy(self.manager.config)
        enabled = self.manager.call('computer_check_image', {'delivery_mode': 'vision'})
        self.assertTrue(any(c['type'] == 'image' for c in enabled['content']))
        self.assertEqual(self.manager.image_delivery.status()['delivery_mode'], 'vision')
        self.manager.call('computer_check_image', {'delivery_mode': 'text'})
        self.assertEqual(self.manager.image_delivery.status()['delivery_mode'], 'text')
        self.assertEqual(self.manager.config, original_config)

    def test_legacy_structured_json_and_image_resources_never_leak_encoded_pixels(self):
        pixels = self.image['data']
        metadata = {'screenshot': pixels, 'screenshot_width': 376, 'screenshot_height': 120,
                    'nested': {'png_base64': pixels}, 'error_code': 'capture_incomplete'}
        cases = [result(metadata, error=True), result(metadata),
                 {'content': [{'type': 'text', 'text': json.dumps({'image_base64': pixels})}]},
                 {'content': [{'type': 'resource', 'resource': {'uri': 'local:screen', 'mimeType': 'image/png', 'blob': pixels}}]},
                 {'content': [{'type': 'resource_link', 'uri': 'local:screen', 'mimeType': 'image/png', 'name': 'screen'}]},
                 {'content': [{'type': 'text', 'text': pixels}]}]
        for value in cases:
            before = copy.deepcopy(value)
            with patch.object(self.manager, '_call', return_value=value):
                answer = self.manager.call('get_window_state', {})
            self.assertEqual(value, before)
            self.assertNotIn(pixels, json.dumps(answer))
            self.assertTrue(all(c.get('type') == 'text' for c in answer['content']))
            self.assertFalse(answer['structuredContent']['image_delivery']['images_sent_to_model'])
        answer = self.manager.image_delivery.filter_response(result(metadata))
        self.assertEqual(answer['structuredContent']['screenshot_width'], 376)
        self.assertEqual(answer['structuredContent']['error_code'], 'capture_incomplete')
        embedded = result({'screenshot': {**self.image, 'width': 376, 'height': 120,
                           'window_bounds': {'x': -1920, 'y': 0, 'width': 1280, 'height': 900}}})
        answer = self.manager.image_delivery.filter_response(embedded)
        kept = answer['structuredContent']['screenshot']
        self.assertEqual(kept['width'], 376)
        self.assertEqual(kept['window_bounds']['x'], -1920)
        self.assertNotIn(pixels, json.dumps(answer))

    def test_failed_roundtrip_returns_to_safe_text(self):
        delivery = ImageDelivery()
        initial = delivery.check(delivery_mode='vision')
        self.assertFalse(delivery.check(initial['challenge_id'], 'WRONG')['roundtrip_verified'])
        self.assertEqual(delivery.check()['status'], 'text_safe')

    def test_image_recipe_selects_foreground_only_when_delivery_omitted(self):
        self.manager.session = SimpleNamespace(state='active')
        args = {'task_id': 'demo', 'targets': [{'program_id': 'editor', 'pid': 10, 'window_id': 20}]}
        with patch.object(self.manager.tasks, 'get', return_value={'steps': [{'operation': 'image_click'}]}), \
                patch.object(self.manager.workflows, 'run', return_value={'task_verified': True}) as run:
            self.manager.call('computer_run_task', args)
            self.assertEqual(run.call_args.kwargs['delivery_mode'], 'foreground')
            self.manager.call('computer_run_task', {**args, 'delivery_mode': 'background'})
            self.assertEqual(run.call_args.kwargs['delivery_mode'], 'background')
        with patch.object(self.manager.tasks, 'get', return_value={'steps': [{'operation': 'click'}]}), \
                patch.object(self.manager.workflows, 'run', return_value={'task_verified': True}) as run:
            self.manager.call('computer_run_task', args)
            self.assertEqual(run.call_args.kwargs['delivery_mode'], 'background')
        with patch.object(self.manager.tasks, 'get', return_value={'steps': [{'operation': 'click', 'completion_mode': 'human'}]}), \
                patch.object(self.manager.workflows, 'run', return_value={'task_verified': True}) as run:
            self.manager.call('computer_run_task', args)
            self.assertEqual(run.call_args.kwargs['delivery_mode'], 'foreground')
            self.manager.call('computer_run_task', {**args, 'delivery_mode': 'background'})
            self.assertEqual(run.call_args.kwargs['delivery_mode'], 'background')

    def test_text_checkpoint_cannot_be_acknowledged_without_native_confirmation(self):
        self.manager.session = SimpleNamespace(id='s', state='active')
        args = {'task_id': 'demo', 'targets': [{'program_id': 'editor', 'pid': 10, 'window_id': 20}],
                'resume_run_id': 'run1', 'acknowledge_checkpoint': 'checkpoint1'}
        with patch.object(self.manager.workflows, 'run') as run:
            answer = self.manager.call('computer_run_task', args)
        run.assert_not_called()
        self.assertFalse(answer['structuredContent']['input_dispatched'])
        self.assertEqual(answer['structuredContent']['status'], 'needs_review')

    def test_switching_to_vision_cannot_override_existing_native_rejection(self):
        self.manager.session = SimpleNamespace(id='s', state='active')
        self.manager.image_delivery.check(delivery_mode='vision')
        args = {'task_id': 'demo', 'targets': [{'program_id': 'editor', 'pid': 10, 'window_id': 20}],
                'resume_run_id': 'run1', 'acknowledge_checkpoint': 'checkpoint1'}
        with patch.object(self.manager.workflows, 'run') as run, \
                patch.object(self.manager.checkpoint_reviews, 'acknowledgement', return_value={'review_id': 'r', 'status': 'rejected', 'human_reviewed': False}):
            answer = self.manager.call('computer_run_task', args)
        run.assert_not_called()
        self.assertEqual(answer['structuredContent']['status'], 'needs_review')


class CheckpointReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = SimpleNamespace(id='session1', state='active', run_dir=Path(self.tmp.name), check_active=Mock())
        self.child = Mock()
        self.child.poll.return_value = None
        self.reviews = CheckpointReviews(process_factory=self.spawn)
        self.image = {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(image_png('ABCDEF')).decode()}
        self.record = {'run_id': 'run1', 'checkpoint': {'id': 'check1', 'message': '결과를 확인하세요.', 'capture_available': True}}
        self.args = {'task_id': 'task1', 'targets': [{'program_id': 'demo', 'pid': 1, 'window_id': 2}]}

    def spawn(self, argv, **kwargs):
        self.assertFalse(kwargs['shell'])
        request = json.loads(Path(argv[1]).read_text(encoding='utf-8'))
        atomic_json(Path(argv[1]).with_name('ready.json'), {**request, 'visible': True})
        return self.child

    def start(self):
        # Only helper existence is stubbed; all IPC files and identity checks
        # are real. No desktop process is started by these tests.
        original = Path.is_file
        with patch.object(Path, 'is_file', lambda p: True if p.name == 'Computer Use MCP 화면 확인.exe' else original(p)):
            return self.reviews.open(self.runtime, self.record, [self.image], self.args)

    def decision(self, identity, state, **changes):
        job = self.reviews.jobs[identity]
        atomic_json(job['folder'] / 'result.json', {'review_id': identity, 'nonce': job['nonce'], 'status': state, **changes})

    def test_confirmation_is_bound_to_exact_session_run_checkpoint_and_does_not_replay(self):
        started = self.start()
        self.assertEqual(started['status'], 'pending')
        self.assertFalse(started['human_reviewed'])
        self.assertFalse(self.reviews.acknowledgement(self.runtime, 'run1', 'check1')['human_reviewed'])
        self.decision(started['review_id'], 'confirmed')
        done = self.reviews.status(started['review_id'], self.runtime)
        self.assertTrue(done['human_reviewed'])
        self.assertFalse(done['task_verified'])
        self.assertFalse(done['input_dispatched'])
        self.assertEqual(done['next_arguments']['resume_run_id'], 'run1')
        self.assertEqual(done['next_arguments']['acknowledge_checkpoint'], 'check1')
        self.assertFalse(self.reviews.acknowledgement(self.runtime, 'run1', 'another')['human_reviewed'])
        self.assertFalse((self.reviews.jobs[started['review_id']]['folder'] / 'screen.png').exists())

    def test_rejection_and_invalid_nonce_never_authorize_resume(self):
        for state, changes in [('rejected', {}), ('confirmed', {'nonce': '0'*64})]:
            self.record['checkpoint']['id'] += 'x'
            started = self.start()
            self.decision(started['review_id'], state, **changes)
            self.assertFalse(self.reviews.status(started['review_id'], self.runtime)['human_reviewed'])

    def test_closed_session_invalidates_even_prior_confirmation(self):
        started = self.start()
        self.decision(started['review_id'], 'confirmed')
        self.runtime.state = 'stopped'
        self.assertEqual(self.reviews.status(started['review_id'], self.runtime)['status'], 'cancelled')

    def test_new_session_object_cannot_reuse_confirmation_even_if_id_is_copied(self):
        started = self.start()
        self.decision(started['review_id'], 'confirmed')
        other = SimpleNamespace(id=self.runtime.id, state='active')
        value = self.reviews.status(started['review_id'], other)
        self.assertEqual(value['status'], 'cancelled')
        self.assertFalse(value['human_reviewed'])

    def test_rejected_decision_is_terminal_and_later_file_cannot_approve_it(self):
        started = self.start()
        self.decision(started['review_id'], 'rejected')
        self.assertEqual(self.reviews.status(started['review_id'], self.runtime)['status'], 'rejected')
        self.decision(started['review_id'], 'confirmed')
        self.assertEqual(self.reviews.status(started['review_id'], self.runtime)['status'], 'rejected')
        self.assertFalse(self.reviews.acknowledgement(self.runtime, 'run1', 'check1')['human_reviewed'])

    def test_expired_review_cannot_be_confirmed_and_pixels_are_removed(self):
        started = self.start()
        job = self.reviews.jobs[started['review_id']]
        self.decision(started['review_id'], 'confirmed')
        self.reviews.clock = lambda: job['expires'] + 1
        answer = self.reviews.status(started['review_id'], self.runtime)
        self.assertEqual(answer['status'], 'cancelled')
        self.assertFalse(answer['human_reviewed'])
        self.assertFalse((job['folder'] / 'screen.png').exists())

    def test_stop_only_closes_exact_owned_viewer_and_removes_pixels(self):
        started = self.start()
        self.reviews.stop(self.runtime)
        self.child.wait.assert_called_once_with(timeout=.6)
        self.assertEqual(self.reviews.status(started['review_id'], self.runtime)['status'], 'cancelled')
        self.assertFalse((self.reviews.jobs[started['review_id']]['folder'] / 'screen.png').exists())

    def test_process_alive_without_visible_ready_is_bounded_startup_failure(self):
        started = self.start()
        job = self.reviews.jobs[started['review_id']]
        job['status'] = 'starting'
        job['startup_expires'] = self.reviews.clock() - 1
        (job['folder'] / 'ready.json').unlink()
        answer = self.reviews.status(started['review_id'], self.runtime)
        self.assertEqual(answer['status'], 'unavailable')
        self.assertEqual(answer['diagnostic']['code'], 'checkpoint_review_not_visible')
        self.assertFalse(answer['human_reviewed'])

    def test_session_deadline_or_stop_flag_invalidates_a_late_confirmation(self):
        started = self.start()
        self.decision(started['review_id'], 'confirmed')
        self.runtime.check_active.side_effect = RuntimeError('session deadline expired')
        answer = self.reviews.status(started['review_id'], self.runtime)
        self.assertEqual(answer['status'], 'cancelled')
        self.assertFalse(answer['human_reviewed'])

    def test_repeated_request_reuses_viewer_and_no_missing_helper_claims_visible(self):
        first = self.start()
        self.assertEqual(self.start()['review_id'], first['review_id'])
        self.record['checkpoint']['id'] = 'missing'
        with patch.object(Path, 'is_file', return_value=False):
            answer = self.reviews.open(self.runtime, self.record, [self.image], self.args)
        self.assertEqual(answer['status'], 'unavailable')
        self.assertFalse(answer['review_visible'])


class ProtocolAssociationTests(unittest.TestCase):
    def test_association_is_metadata_not_control_target_or_executed_command(self):
        uri = 'testapp://company/menu'
        with patch('program_registration.check_app', return_value='c:\\apps\\launcher.exe'):
            value = protocol_handler(uri, lambda _: '"C:\\Apps\\Launcher.exe" --launch "%1"')
        self.assertEqual(value['registered_handler'], 'C:\\Apps\\Launcher.exe')
        self.assertFalse(value['handler_is_control_target'])
        self.assertFalse(value['application_launched'])
        self.assertFalse(value['registry_modified'])

    def test_web_route_does_not_assume_browser_is_business_target(self):
        reader = Mock()
        answer = protocol_handler('https://example.invalid/updater.application', reader)
        reader.assert_not_called()
        self.assertEqual(answer['status'], 'browser_route')
        self.assertIsNone(answer['registered_handler'])

    def test_malformed_or_shell_association_never_returned_as_target(self):
        for command in ['"unfinished.exe', '"C:\\x.exe"\nwhoami']:
            self.assertEqual(protocol_handler('testapp://company/menu', lambda _: command)['status'], 'unavailable')


@unittest.skipUnless(os.name == 'nt' and Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe').is_file(), 'Windows .NET compiler required')
class CheckpointReviewLayoutTests(unittest.TestCase):
    """Lay out the real native form without showing it or acknowledging work."""
    PROBE = r'''
using System; using System.Collections.Generic; using System.Drawing; using System.IO;
using System.Reflection; using System.Runtime.InteropServices; using System.Windows.Forms;
using System.Web.Script.Serialization;
internal static class ReviewLayoutProbe {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
 [STAThread] static int Main(string[] args) {
  SetProcessDpiAwarenessContext(new IntPtr(-4)); Application.EnableVisualStyles();
  Application.SetCompatibleTextRenderingDefault(false);
  string message="요소 클릭 뒤 업무 결과가 맞는지 확인하세요.";
  if(args[1]=="minimum") for(int i=0;i<3;i++) message += " 현재 단계의 처리 결과와 선택한 조건을 확인하고 계속하세요.";
  Type kind=typeof(CheckpointReview).GetNestedType("ReviewWindow",BindingFlags.NonPublic);
  using(Form form=(Form)Activator.CreateInstance(kind,BindingFlags.Instance|BindingFlags.NonPublic,null,
      new object[]{args[0],new Dictionary<string,object>{{"message",message}},new string('a',64),new string('b',32)},null)) {
   if(args[1]=="minimum") form.Size=form.MinimumSize;
   form.PerformLayout(); foreach(Control control in form.Controls) control.PerformLayout();
   var heading=(Label)form.Controls.Find("ReviewHeading",true)[0];
   var description=(Label)form.Controls.Find("ReviewDescription",true)[0];
   var picture=form.Controls.Find("ReviewImage",true)[0];
   int requiredHeading=heading.GetPreferredSize(new Size(heading.Width,0)).Height;
   int requiredDescription=description.GetPreferredSize(new Size(description.Width,0)).Height;
   Rectangle working=Screen.FromHandle(form.Handle).WorkingArea;
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {
    visible=form.Visible,heading_height=heading.Height,heading_required=requiredHeading,
    description_height=description.Height,description_required=requiredDescription,
    no_overlap=heading.Bottom<=description.Top && description.Bottom<=picture.Top,
    image_height=picture.Height,inside_work_area=form.Width<=working.Width && form.Height<=working.Height,
    wrote_ready=File.Exists(Path.Combine(args[0],"ready.json")),wrote_result=File.Exists(Path.Combine(args[0],"result.json"))
   }));
  } return 0;
 }
}'''

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='checkpoint-layout-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.folder = Path(cls.directory.name)
        (cls.folder / 'screen.png').write_bytes(image_png('ABCDEF'))
        probe = cls.folder / 'probe.cs'
        probe.write_text(cls.PROBE, encoding='utf-8')
        cls.helper = cls.folder / 'probe.exe'
        compiler = r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        command = [compiler, '/nologo', '/target:exe', '/main:ReviewLayoutProbe', '/codepage:65001',
                   '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll',
                   '/reference:System.Web.Extensions.dll', '/out:' + str(cls.helper),
                   str(Path(__file__).with_name('CheckpointReview.cs')), str(probe)]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def check_layout(self, mode):
        run = subprocess.run([str(self.helper), str(self.folder), mode], capture_output=True,
                             text=True, encoding='utf-8-sig', check=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        value = json.loads(run.stdout)
        self.assertGreaterEqual(value['heading_height'], value['heading_required'], value)
        self.assertGreaterEqual(value['description_height'], value['description_required'], value)
        self.assertTrue(value['no_overlap'], value)
        self.assertGreater(value['image_height'], 100, value)
        self.assertTrue(value['inside_work_area'], value)
        self.assertFalse(value['visible'], value)
        self.assertFalse(value['wrote_ready'], value)
        self.assertFalse(value['wrote_result'], value)

    def test_dpi_sized_heading_and_description_do_not_clip(self):
        self.check_layout('initial')

    def test_minimum_size_wraps_long_description_above_image(self):
        self.check_layout('minimum')


if __name__ == '__main__':
    unittest.main()
