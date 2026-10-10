"""Public two-workflow contract through ComputerManager, no desktop input."""
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from easy_api import schemas
from server import ComputerManager, MANAGEMENT, result
from session_runtime import SessionError
from test_server import config_at
from test_image_steps import png


class Probe:
    def __init__(self, target, executable): self.target, self.executable = target, executable
    def capture(self): return self.snapshot()
    def snapshot(self):
        return {'process_exited': False, 'target_present': True, 'creation_time': 123, 'executable': self.executable,
            'windows': [{**self.target, 'thread_id': 8, 'class_name': 'TestWindow', 'owner_window_id': 0}]}
    def close(self): pass


class Runtime:
    def __init__(self, config, programs, mode, task, **kwargs):
        self.config, self.programs, self.mode, self.task = config, programs, mode, task
        self.id, self.state = 'session', 'starting'
        self.deadline = time.monotonic()+kwargs.get('max_minutes', config['max_minutes'])*60
        self.max_actions = kwargs.get('max_actions', config['max_actions'])
        self.execution_lock, self.stop_event = threading.RLock(), threading.Event()
        self.windows = [{'pid': 100, 'window_id': 200, 'title': 'Test app'}]
        self.launches, self.calls = [], []
        self.guard = SimpleNamespace(process_resolver=lambda pid: programs[0]['exe'], action_count=0)
        self.closures = SimpleNamespace(verify=mock.Mock(return_value={'status': 'verified', 'window_closed': True}))
    def start(self): self.state = 'active'; return self.status()
    def status(self): return {'session_id': self.id, 'state': self.state}
    def check_active(self):
        if self.state != 'active': raise SessionError('inactive')
    def stop(self, reason): self.state = 'stopped'; self.stop_event.set()
    def wait_stopped(self, timeout): return self.state == 'stopped'
    def create_transition_probe(self, target): return Probe(target, self.programs[0]['exe'])
    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return {'structuredContent': {'windows': copy.deepcopy(self.windows)}}
    def launch(self, program):
        self.launches.append(program)
        return {'launch_status': 'waiting_for_window', 'input_dispatched': True}


class EasyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = config_at(self.temp.name, tool_profile='simple', approval='client')
        self.manager = ComputerManager(self.config, runtime_factory=Runtime)
        self.addCleanup(self.manager.close)

    def open(self, **args):
        return self.manager.call('computer_open', {'program_id': 'editor', **args})['structuredContent']

    def task(self):
        return self.manager.tasks.save({'id': 'demo', 'name': 'Demo', 'instructions': 'Read current app',
            'expected': 'Done', 'program_ids': ['editor'], 'steps': [{'operation': 'delay', 'duration_ms': 0, 'program_id': 'editor'}]})

    def test_default_schema_has_twelve_tools_and_concrete_action_contract(self):
        tools = schemas(MANAGEMENT)
        self.assertEqual(len(tools), 12)
        self.assertNotIn('computer_teach_element', [tool['name'] for tool in tools])
        act = next(tool for tool in tools if tool['name'] == 'computer_act')
        props = act['inputSchema']['properties']['step']['properties']
        self.assertIn('set_value', props['operation']['enum'])
        self.assertIn('scroll', props['operation']['enum'])
        self.assertIn('expect', props)
        with self.assertRaises(SessionError): self.manager.call('click', {})

    def test_open_creates_one_bounded_session_and_reuses_live_window_ref(self):
        first = self.open(image_delivery='vision')
        runtime = self.manager.session
        second = self.open()
        self.assertEqual(first['window_ref'], second['window_ref'])
        self.assertIs(runtime, self.manager.session)
        self.assertEqual(runtime.launches, [])
        self.assertEqual(self.manager.image_delivery.status()['delivery_mode'], 'vision')

    def test_same_program_id_refreshed_registration_renews_frozen_runtime(self):
        opened = self.open()
        original = self.manager.session
        prior_scope = copy.deepcopy(original.programs)
        config = copy.deepcopy(self.manager.config)
        updated = config['programs'][0]
        updated.update(exe=str(Path(self.temp.name)/'NewVersion.exe'),
            control_exes=[str(Path(self.temp.name)/'NewHost.exe')], launch={'kind': 'exe', 'arguments': ['--new-version']})
        self.manager.config = config
        answer = self.open()
        self.assertIsNot(self.manager.session, original)
        self.assertEqual(original.state, 'stopped')
        self.assertEqual(original.programs, prior_scope)
        self.assertEqual(self.manager.session.programs, [updated])
        self.assertNotEqual(answer['window_ref'], opened['window_ref'])
        self.assertEqual(original.launches, [])
        self.assertEqual(self.manager.session.launches, [])

    def test_registration_label_only_does_not_restart_or_expand_session(self):
        self.open()
        original = self.manager.session
        changed = copy.deepcopy(self.manager.config)
        changed['programs'][0].update(name='New friendly name', hints='Updated help')
        self.manager.config = changed
        self.open()
        self.assertIs(self.manager.session, original)

    def test_registration_scope_change_waits_for_open_editor(self):
        self.open()
        original = self.manager.session
        changed = copy.deepcopy(self.manager.config)
        changed['programs'][0]['control_exes'] = [str(Path(self.temp.name)/'NewHost.exe')]
        self.manager.config = changed
        with mock.patch.object(self.manager.process_editors, 'pending', return_value={'id': 'editing'}):
            with self.assertRaises(SessionError): self.open()
        self.assertIs(self.manager.session, original)
        self.assertEqual(original.state, 'active')

    def test_ambiguous_windows_return_choices_without_guessing_or_launching(self):
        self.open()
        self.manager.session.windows.append({'pid': 100, 'window_id': 201, 'title': 'Other document'})
        answer = self.open()
        self.assertEqual(answer['status'], 'choose_window')
        self.assertEqual(len(answer['candidates']), 2)
        chosen = self.open(window_id=201)
        self.assertEqual(chosen['target']['window_id'], 201)
        self.assertEqual(self.manager.session.launches, [])

    def test_slow_start_is_launched_once_across_repeated_open_requests(self):
        self.open()
        self.manager.session.windows = []
        first, second = self.open(), self.open()
        self.assertEqual(first['status'], 'window_unavailable')
        self.assertEqual(second['status'], 'window_unavailable')
        self.assertEqual(self.manager.session.launches, ['editor'])
        self.manager.session.windows = [{'pid': 100, 'window_id': 202, 'title': 'Started'}]
        self.assertEqual(self.open()['status'], 'connected')

    def test_new_hosted_frame_renews_exact_scope_without_extending_budget_or_launching(self):
        opened = self.open()
        previous = self.manager.session
        previous.deadline = time.monotonic()+130
        previous.guard.action_count = 4
        previous.hosted_scope_needs_refresh = mock.Mock(return_value=True)
        answer = self.open()
        current = self.manager.session
        self.assertEqual(answer['status'], 'connected')
        self.assertIsNot(current, previous)
        self.assertEqual(current.programs, previous.programs)
        self.assertLessEqual(current.deadline, previous.deadline)
        self.assertEqual(current.max_actions, previous.max_actions-4)
        self.assertNotEqual(answer['window_ref'], opened['window_ref'])
        self.assertEqual(previous.launches+current.launches, [])

    def test_frame_created_by_launch_renews_once_without_relaunch(self):
        self.open()
        previous = self.manager.session
        previous.windows = []
        previous.hosted_scope_needs_refresh = mock.Mock(side_effect=[False, True])
        answer = self.open()
        self.assertEqual(answer['status'], 'connected')
        self.assertIsNot(self.manager.session, previous)
        self.assertEqual(previous.launches, ['editor'])
        self.assertEqual(self.manager.session.launches, [])

    def test_hosted_scope_refresh_waits_for_editor_without_mutating_live_scope(self):
        self.open()
        previous = self.manager.session
        previous.hosted_scope_needs_refresh = mock.Mock(return_value=True)
        with mock.patch.object(self.manager.process_editors, 'pending', return_value={'editor_id': 'active'}):
            with self.assertRaises(SessionError): self.open()
        self.assertIs(self.manager.session, previous)
        self.assertEqual(previous.state, 'active')

    def test_known_program_management_delegates_to_supported_registration(self):
        with mock.patch.object(self.manager, '_call', return_value=result({'registered': True})) as call:
            answer = self.manager.call('computer_register_program', {'name': 'New app', 'exe': str(Path(self.temp.name)/'App.exe')})
        self.assertTrue(answer['structuredContent']['registered'])
        self.assertEqual(call.call_args.args[0], 'computer_register_program')

    def test_candidate_discovery_uses_registered_connector_no_disk_search(self):
        with mock.patch.object(self.manager, '_call', return_value=result({'candidates': []})) as call:
            self.manager.call('computer_programs', {'action': 'candidates'})
        self.assertEqual(call.call_args.args[:2], ('computer_program_candidates', {}))

    def test_registration_guidance_uses_public_open_and_keeps_user_program_data(self):
        program = {'id': 'new', 'name': 'computer_begin', 'hints': 'computer_end is a literal example'}
        for legacy in ('computer_begin', 'computer_end'):
            with mock.patch.object(self.manager, '_call', return_value=result({'program': program, 'next_tool': legacy,
                    'message': 'Use computer_begin'})):
                answer = self.manager.call('computer_register_program', {'exe': str(Path(self.temp.name)/'App.exe')})['structuredContent']
            self.assertEqual(answer['next_tool'], 'computer_open')
            self.assertEqual(answer['next_arguments'], {'program_id': 'new'})
            self.assertEqual(answer['program'], program)
            self.assertNotIn('computer_begin', answer['message'])
        with mock.patch.object(self.manager, '_call', return_value=result({'next_tool': 'computer_program_candidates'})):
            answer = self.manager.call('computer_register_program', {})['structuredContent']
        self.assertEqual((answer['next_tool'], answer['next_arguments']), ('computer_programs', {'action': 'candidates'}))

    def test_status_and_program_list_expose_only_public_navigation(self):
        answer = self.manager.call('computer_status', {})['structuredContent']
        self.assertEqual(answer['image_delivery']['tool'], 'computer_open')
        self.assertEqual(answer['image_delivery']['local_review_tool'], 'computer_status')
        self.assertEqual(answer['adaptive_observation']['tool'], 'computer_observe')
        self.assertEqual(answer['program_registration']['candidates_tool'], 'computer_programs')
        for section in ('direct_picker', 'image_process'):
            self.assertEqual(answer['teaching_support'][section]['tool'], 'computer_process_editor')
            self.assertEqual(answer['teaching_support'][section]['status_tool'], 'computer_status')
        programs = self.manager.call('computer_programs', {})['structuredContent']
        self.assertEqual(programs['next_tool_if_path_unknown'], 'computer_programs')
        self.assertEqual(programs['next_arguments_if_path_unknown'], {'action': 'candidates'})

    def test_editor_and_local_review_guidance_keep_exact_identity(self):
        with mock.patch.object(self.manager.process_editors, 'status', return_value={'editor_id': 'e', 'next_tool': 'computer_process_status'}):
            answer = self.manager.call('computer_status', {'editor_id': 'e'})['structuredContent']
        self.assertEqual((answer['next_tool'], answer['next_arguments']), ('computer_status', {'editor_id': 'e'}))
        review = {'review_id': 'r', 'next_tool': 'computer_review_checkpoint', 'next_arguments': {'review_id': 'r'}}
        with mock.patch.object(self.manager.checkpoint_reviews, 'status', return_value=review):
            answer = self.manager.call('computer_status', {'review_id': 'r'})['structuredContent']
        self.assertEqual((answer['next_tool'], answer['next_arguments']), ('computer_status', {'review_id': 'r'}))
        self.manager.easy.editor_reviews['r'] = 'e'
        review.update(checkpoint_id='c', next_tool='computer_run_task', next_arguments={'resume_run_id': 'run', 'acknowledge_checkpoint': 'c'})
        with mock.patch.object(self.manager.checkpoint_reviews, 'status', return_value=review):
            answer = self.manager.call('computer_status', {'review_id': 'r'})['structuredContent']
        self.assertEqual(answer['next_tool'], 'computer_process_editor')
        self.assertEqual(answer['next_arguments'], {'action': 'resume_test', 'editor_id': 'e', 'acknowledge_checkpoint': 'c'})

    def test_post_input_rebind_guidance_never_launches_replacement_program(self):
        opened = self.open()
        response = result({'next_tool': 'computer_open', 'next_action': 'rebind_and_observe', 'dispatch': 'sent'})
        answer = self.manager.easy.normalize_response('computer_act', {'window_ref': opened['window_ref']}, response)['structuredContent']
        self.assertEqual(answer['next_arguments'], {'program_id': 'editor', 'launch_if_missing': False})
        self.assertEqual(answer['dispatch'], 'sent')

    def test_task_user_text_and_fields_are_not_navigation_rewritten(self):
        task = self.task()
        task['instructions'] = 'computer_begin then computer_end'
        task['steps'][0]['message'] = 'computer_process_status'
        response = result({'task': task, 'history': [{'next_tool': 'computer_begin'}]})
        answer = self.manager.easy.normalize_response('computer_tasks', {'action': 'get'}, response)
        self.assertEqual(answer, response)

    def test_run_binds_current_window_and_forwards_visual_review_unchanged(self):
        self.task(); opened = self.open(image_delivery='vision')
        submission = {'review_id': 'review-identity'}
        with mock.patch.object(self.manager.workflows, 'run', return_value={'status': 'verified', 'task_verified': True}) as run:
            self.manager.call('computer_run_task', {'task_id': 'demo', 'observation_review': submission})
        self.assertEqual(run.call_args.kwargs['delivery_mode'], 'foreground')
        self.assertTrue(run.call_args.kwargs['image_delivery_enabled'])
        self.assertEqual(run.call_args.kwargs['observation_review'], submission)
        self.assertEqual(run.call_args.args[3], [{'program_id': 'editor', **opened['target']}])

    def test_explicit_human_checkpoint_opens_local_review_even_in_vision_profile(self):
        self.task(); self.open(image_delivery='vision')
        answer = {'status': 'needs_review', 'task_verified': False, 'checkpoint': {'id': 'id', 'capture_available': True},
                  'checkpoint_content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]}
        with mock.patch.object(self.manager.workflows, 'run', return_value=answer), mock.patch.object(
                self.manager.checkpoint_reviews, 'open', return_value={'review_id': 'human-review'}) as review:
            result = self.manager.call('computer_run_task', {'task_id': 'demo'})
        review.assert_called_once()
        self.assertEqual(result['structuredContent']['local_review']['review_id'], 'human-review')
        self.assertEqual(result['structuredContent']['next_tool'], 'computer_status')

    def test_saved_target_review_schema_and_submission_reach_bound_workflow(self):
        self.open(image_delivery='vision'); self.task()
        schema = next(item for item in schemas(MANAGEMENT) if item['name'] == 'computer_run_task')
        contract = schema['inputSchema']['properties']['target_review']
        self.assertIn('frame_id', contract['required'])
        self.assertIn('target_region', contract['required'])
        self.assertFalse(contract['additionalProperties'])
        submission = {**{key: 'exact-id' for key in
            ('recovery_id', 'run_id', 'step_id', 'iteration_id', 'observation_id', 'frame_id')},
            'step_index': 2, 'target_region': {'x': 5, 'y': 5, 'width': 20, 'height': 20}, 'evidence': 'same icon'}
        with mock.patch.object(self.manager.workflows, 'resume_snapshot', return_value=None), mock.patch.object(
                self.manager.workflows, 'run', return_value={'status': 'needs_review'}) as run:
            self.manager.call('computer_run_task', {'task_id': 'demo', 'resume_run_id': 'a'*32,
                'target_review': submission})
        self.assertEqual(run.call_args.kwargs['target_review'], submission)
        self.assertEqual(run.call_args.kwargs['resume_run_id'], 'a'*32)
        self.assertTrue(run.call_args.kwargs['image_delivery_enabled'])

    def test_target_review_images_never_become_human_checkpoint_dialog(self):
        self.open(image_delivery='vision'); self.task()
        for marker in ({'target_review': {'recovery_id': 'target'}}, {'state': 'needs_target_review'}):
            answer = {'status': 'needs_review', 'checkpoint': {'capture_available': True}, **marker,
                'observation_content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]}
            with self.subTest(marker=marker), mock.patch.object(self.manager.workflows, 'run', return_value=answer), mock.patch.object(
                    self.manager.checkpoint_reviews, 'open') as review:
                response = self.manager.call('computer_run_task', {'task_id': 'demo'})
            review.assert_not_called()
            self.assertEqual(sum(item['type'] == 'image' for item in response['content']), 1)

    def test_visual_checkpoint_never_opens_human_dialog(self):
        self.task(); self.open(image_delivery='vision')
        answer = {'status': 'needs_review', 'checkpoint': {'capture_available': True, 'review_mode': 'visual'},
            'visual_review': {'review_id': 'model-review'},
            'observation_content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]}
        with mock.patch.object(self.manager.workflows, 'run', return_value=answer), mock.patch.object(self.manager.checkpoint_reviews, 'open') as review:
            result = self.manager.call('computer_run_task', {'task_id': 'demo'})
        review.assert_not_called()
        self.assertEqual(result['content'][-1]['type'], 'image')
        self.assertNotIn('observation_content', result['structuredContent'])

    def test_human_acknowledgement_cannot_be_fabricated_by_model(self):
        self.task(); self.open(image_delivery='vision')
        with mock.patch.object(self.manager.workflows, 'resume_snapshot', return_value=None), mock.patch.object(self.manager.checkpoint_reviews, 'acknowledgement', return_value={'human_reviewed': False}), mock.patch.object(self.manager.workflows, 'run') as run:
            result = self.manager.call('computer_run_task', {'task_id': 'demo', 'resume_run_id': 'a'*32, 'acknowledge_checkpoint': 'b'*32})
        self.assertFalse(result['structuredContent']['task_verified'])
        run.assert_not_called()

    def test_resume_uses_immutable_original_revision_instead_of_new_edits(self):
        original = self.task(); self.open()
        frozen = {**original, 'instructions': 'Original frozen instructions'}
        with mock.patch.object(self.manager.workflows, 'resume_snapshot', return_value=(frozen, {'condition': 'W'})), mock.patch.object(
                self.manager.workflows, 'run', return_value={'status': 'verified'}) as run:
            self.manager.call('computer_run_task', {'task_id': 'demo', 'resume_run_id': 'a'*32})
        self.assertEqual(run.call_args.args[1], frozen)
        self.assertEqual(run.call_args.kwargs['resume_run_id'], 'a'*32)

    def test_editor_status_is_single_public_poll_and_passes_cancel(self):
        with mock.patch.object(self.manager.process_editors, 'status', return_value={'status': 'editing'}) as status:
            result = self.manager.call('computer_status', {'editor_id': 'editor-job', 'wait_ms': 100, 'cancel': True})
        self.assertEqual(status.call_args.args, ('editor-job',))
        self.assertEqual(status.call_args.kwargs, {'wait_ms': 100, 'cancel': True})
        self.assertEqual(result['structuredContent']['status'], 'editing')

    def test_editor_trial_visual_frame_is_delivered_once_outside_metadata(self):
        self.open(image_delivery='vision')
        answer = {'status': 'editing', 'visual_review': {'review_id': 'r'},
                  'observation_content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]}
        with mock.patch.object(self.manager.process_editors, 'status', return_value=answer):
            output = self.manager.call('computer_status', {'editor_id': 'e'})
        self.assertNotIn('observation_content', output['structuredContent'])
        self.assertEqual(len([item for item in output['content'] if item['type'] == 'image']), 1)
        self.assertNotIn(png(), output['content'][0]['text'])
        self.manager.image_delivery.delivery_mode = 'text'
        with mock.patch.object(self.manager.process_editors, 'status', return_value=answer):
            output = self.manager.call('computer_status', {'editor_id': 'e'})
        self.assertFalse(any(item['type'] == 'image' for item in output['content']))
        self.assertNotIn(png(), str(output))

    def test_editor_resume_forwards_only_bound_trial_review_without_reattachment(self):
        self.open(image_delivery='vision')
        review = {'review_id': 'r', 'verdict': 'pass', 'evidence': 'Ready'}
        answer = {'status': 'editing', 'last_test': {'status': 'verified', 'task_verified': True}}
        with mock.patch.object(self.manager.process_editors, 'resume_test', return_value=answer) as resume, mock.patch.object(self.manager.easy, '_targets') as targets:
            output = self.manager.call('computer_process_editor', {'action': 'resume_test', 'editor_id': 'e', 'observation_review': review})
        resume.assert_called_once_with('e', self.manager.session, observation_review=review)
        targets.assert_not_called()
        self.assertTrue(output['structuredContent']['last_test']['task_verified'])
        with self.assertRaises(SessionError):
            self.manager.call('computer_process_editor', {'action': 'resume_test', 'editor_id': 'e', 'name': 'Changed draft'})

    def test_editor_human_checkpoint_requires_real_receipt_and_opens_viewer(self):
        self.open(image_delivery='vision')
        answer = {'status': 'editing', 'last_test': {'run_id': 'r', 'checkpoint': {'id': 'c', 'capture_available': True}},
                  'checkpoint_content': [{'type': 'image', 'mimeType': 'image/png', 'data': png()}]}
        with mock.patch.object(self.manager.process_editors, 'status', return_value=answer), mock.patch.object(
                self.manager.checkpoint_reviews, 'acknowledgement', return_value={'human_reviewed': False}) as acknowledge, mock.patch.object(
                self.manager.checkpoint_reviews, 'open', return_value={'review_id': 'native-review'}) as viewer, mock.patch.object(
                self.manager.process_editors, 'resume_test') as resume:
            output = self.manager.call('computer_process_editor', {'action': 'resume_test', 'editor_id': 'e', 'acknowledge_checkpoint': 'c'})
        acknowledge.assert_called_once_with(self.manager.session, 'r', 'c')
        viewer.assert_called_once()
        resume.assert_not_called()
        self.assertEqual(output['structuredContent']['local_review']['review_id'], 'native-review')
        self.assertNotIn('checkpoint_content', output['structuredContent'])
        self.assertFalse(output['structuredContent']['task_verified'])

    def test_malformed_coordinates_are_rejected_before_input(self):
        opened = self.open()
        with self.assertRaises(SessionError):
            self.manager.call('computer_act', {'window_ref': opened['window_ref'], 'step': {'operation': 'click'},
                'image_region': {'x': True, 'y': 0, 'width': 20, 'height': 20}})
        self.assertTrue(all(call[0] == 'list_windows' for call in self.manager.session.calls))


if __name__ == '__main__': unittest.main()
