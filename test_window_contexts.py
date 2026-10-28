"""Saved modeless-window context integration; no desktop input or app launch."""
import copy
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from business_windows import BusinessWindowError
from operations import OperationError
from server import ComputerManager, TaskStore, SessionError
from task_revision import TaskRevisions
from test_business_windows import row
from test_easy_api import Runtime as EasyRuntime
from test_image_steps import image_target
from test_server import config_at
from test_window_transitions import Probe, state
from window_contexts import MENU_AGENT, inferred_menuagent_context, validate_contexts


TITLE = 'Device Setup Standard (CJTEST) - [smartMMDM, user-secret] - FxMenuAgent (PNT)'
SELECTOR = {'title_prefix': 'Device Setup Standard (CJTEST) - [smartMMDM, '}
READY = {'controls': [{'selector': {'name': '조회', 'role': 'Button',
    'within': {'automation_id': 'observed-body'}}, 'enabled': True}], 'timeout_ms': 0}
CONTEXT = {'window_selector': SELECTOR, 'ready_when': READY}


class ContextValidationTests(unittest.TestCase):
    def test_round_trip_copies_only_durable_window_and_explicit_ready_conditions(self):
        source = {'editor': copy.deepcopy(CONTEXT)}
        result = validate_contexts(source, ['editor'])
        self.assertEqual(result, source)
        result['editor']['ready_when']['controls'][0]['enabled'] = False
        self.assertTrue(source['editor']['ready_when']['controls'][0]['enabled'])
        self.assertEqual(validate_contexts({}, ['editor']), {})

    def test_invalid_program_or_ephemeral_window_metadata_cannot_be_saved(self):
        invalid = [None, [], {'other': CONTEXT}, {'editor': {}}, {'editor': {'ready_when': READY}},
            {'editor': {**CONTEXT, 'pid': 7}},
            {'editor': {'window_selector': {'title': 'A', 'window_id': 10}}},
            {'editor': {'window_selector': {'title': 'A', 'runtime_id': [1, 2]}}},
            {'editor': {**CONTEXT, 'ready_when': {'controls': []}}}]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises((OperationError, BusinessWindowError)):
                validate_contexts(candidate, ['editor'])


class InferredContextTests(unittest.TestCase):
    def runtime(self, rows=None, *, executable=None, current=None, use_target_resolver=True):
        rows = rows if rows is not None else [row(10, title=TITLE)]
        executable = executable or 'C:/installed/' + MENU_AGENT.upper()
        initial = {**state(*rows, present=True), 'executable': executable}
        current = {**current, 'executable': executable} if current is not None else initial
        probe = Probe(initial, current)
        guard = SimpleNamespace(process_resolver=mock.Mock(return_value=executable))
        if use_target_resolver:
            guard.target_executable = mock.Mock(return_value=executable)
        runtime = SimpleNamespace(guard=guard, check_active=mock.Mock(),
            create_transition_probe=mock.Mock(return_value=probe))
        return runtime, probe

    def test_exact_executable_and_observed_title_remove_user_suffix_only(self):
        runtime, probe = self.runtime()
        result = inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10})
        self.assertEqual(result['window_selector']['title_prefix'], SELECTOR['title_prefix'])
        self.assertEqual(result['window_selector']['class_name'], row()['class_name'])
        serialized = json.dumps(result)
        for forbidden in ('user-secret', 'window_id', '"pid"', 'runtime_id', 'ready_when'):
            self.assertNotIn(forbidden, serialized)
        runtime.guard.target_executable.assert_called_once_with({'pid': 7, 'window_id': 10})
        runtime.guard.process_resolver.assert_not_called()
        self.assertTrue(probe.closed)

    def test_executable_gate_is_exact_and_does_not_probe_other_hosts(self):
        for executable in ('C:/PNTLauncher.exe', 'C:/FxMenuAgent.exe',
                           'C:/prefix-' + MENU_AGENT, 'C:/' + MENU_AGENT + '.bak'):
            runtime, _ = self.runtime(executable=executable)
            self.assertIsNone(inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10}))
            runtime.create_transition_probe.assert_not_called()

    def test_process_resolver_fallback_still_requires_exact_executable(self):
        runtime, _ = self.runtime(use_target_resolver=False)
        self.assertEqual(inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10})
                         ['window_selector']['title_prefix'], SELECTOR['title_prefix'])
        runtime.guard.process_resolver.assert_called_once_with(7)

    def test_different_executable_between_resolver_and_probe_is_rejected(self):
        runtime, probe = self.runtime()
        probe.initial['executable'] = 'C:/installed/SKhynix.SmartFX.PNTLauncher.exe'
        with self.assertRaises(OperationError) as caught:
            inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10})
        self.assertEqual(caught.exception.code, 'business_window_process_changed')
        self.assertEqual(probe.reads, 0)
        self.assertTrue(probe.closed)

    def test_hidden_duplicate_owned_or_nonmatching_window_has_no_inferred_context(self):
        cases = [[row(10, title=TITLE, visible=False)],
                 [row(10, title=TITLE), row(11, title=TITLE.replace('user-secret', 'another-user'))],
                 [row(10, title=TITLE, owner_window_id=1, root_owner_window_id=1)],
                 [row(10, title='Other independent work')]]
        for rows in cases:
            with self.subTest(rows=rows):
                runtime, probe = self.runtime(rows)
                self.assertIsNone(inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10}))
                self.assertTrue(probe.closed)

    def test_window_or_process_replacement_is_rejected_and_probe_closed(self):
        current_states = [state(row(10, title=TITLE, thread_id=99), present=True),
            state(row(10, title=TITLE), present=True, creation=101),
            state(row(10, title=TITLE.replace('CJTEST', 'Other')), present=True)]
        for current in current_states:
            with self.subTest(current=current):
                runtime, probe = self.runtime(current=current)
                with self.assertRaises(OperationError) as caught:
                    inferred_menuagent_context(runtime, {'pid': 7, 'window_id': 10})
                self.assertEqual(caught.exception.code, 'business_window_identity_changed')
                self.assertTrue(probe.closed)


class StoredContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(self.temp.name, {'programs': [{'id': 'editor', 'name': 'Editor'}]})
        self.revisions = TaskRevisions(self.store)
        self.args = {'id': 'recording', 'name': 'Original recording', 'instructions': 'Use preserved recording',
            'expected': 'Result checked', 'program_ids': ['editor'], 'window_contexts': {'editor': CONTEXT},
            'steps': [{'step_id': 'image', 'program_id': 'editor', 'operation': 'image_click',
                       'image_target': image_target()},
                      {'step_id': 'review', 'program_id': 'editor', 'operation': 'checkpoint',
                       'message': 'Check actual result', 'review_mode': 'human'}]}
        self.original = self.store.save(self.args)

    def test_store_reload_and_text_save_preserve_context_image_steps_and_old_revision(self):
        reloaded = TaskStore(self.temp.name, self.store.config).get('recording')
        self.assertEqual(reloaded['window_contexts'], self.original['window_contexts'])
        updated = self.store.save({key: value for key, value in {**self.args, 'name': 'Renamed'}.items()
                                  if key not in {'steps', 'window_contexts'}})
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['steps'], self.original['steps'])
        self.assertEqual(updated['window_contexts'], self.original['window_contexts'])
        self.assertEqual(self.revisions.get('recording', 1)['steps'], self.original['steps'])

    def test_revision_context_edit_preserves_recording_and_immutable_previous_context(self):
        replacement = copy.deepcopy(CONTEXT)
        replacement['window_selector'] = {'title': 'Explicit alternate work'}
        changed = self.revisions.save({'id': 'recording', 'window_contexts': {'editor': replacement}}, expected_revision=1)
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['steps'], self.original['steps'])
        self.assertEqual(changed['window_contexts']['editor'], replacement)
        self.assertEqual(self.revisions.get('recording', 1)['window_contexts'], self.original['window_contexts'])
        text_edit = self.revisions.update('recording', 2, [{'op': 'set_text', 'fields': {'name': 'Renamed again'}}])['task']
        self.assertEqual(text_edit['window_contexts'], changed['window_contexts'])
        self.assertEqual(text_edit['steps'], self.original['steps'])

    def test_invalid_context_revision_is_atomic(self):
        before = self.store.path.read_bytes()
        with self.assertRaises((OperationError, ValueError, SessionError)):
            self.revisions.save({'id': 'recording', 'window_contexts': {'not-registered': CONTEXT}}, expected_revision=1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get('recording')['revision'], 1)

    def test_legacy_task_without_context_is_not_rewritten_by_read(self):
        self.store.save({key: value for key, value in {**self.args, 'id': 'legacy'}.items()
                         if key != 'window_contexts'})
        before = self.store.path.read_bytes()
        self.assertNotIn('window_contexts', self.revisions.get('legacy'))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_reopened_editor_preserves_saved_readiness_without_automatic_reinference(self):
        from process_editor import ProcessEditors
        runtime = SimpleNamespace(check_active=mock.Mock(), mode='uia',
            programs=[{'id': 'editor', 'name': 'Editor'}], guard=SimpleNamespace())
        editors = ProcessEditors(SimpleNamespace(_program=mock.Mock()), self.store)
        def initialized(job):
            job['started'].set()
            job['done'].set()
        with mock.patch.object(editors, '_work', side_effect=initialized), \
             mock.patch('window_contexts.inferred_menuagent_context') as infer:
            answer = editors.start(runtime, {'task_id': 'recording',
                'targets': [{'program_id': 'editor', 'pid': 7, 'window_id': 10}]})
        infer.assert_not_called()
        draft = editors.jobs[answer['editor_id']]['draft']
        self.assertEqual(draft.window_contexts, self.original['window_contexts'])
        self.assertEqual(draft.steps, self.original['steps'])
        self.assertEqual(self.store.get('recording'), self.original)

    def test_explicit_editor_context_change_only_affects_draft_until_save(self):
        from process_editor import ProcessEditors
        runtime = SimpleNamespace(check_active=mock.Mock(), mode='uia',
            programs=[{'id': 'editor', 'name': 'Editor'}], guard=SimpleNamespace())
        editors = ProcessEditors(SimpleNamespace(_program=mock.Mock()), self.store)
        replacement = {'window_selector': {'title': 'User-selected alternate work'}}
        def initialized(job):
            job['started'].set()
            job['done'].set()
        with mock.patch.object(editors, '_work', side_effect=initialized), \
             mock.patch('window_contexts.inferred_menuagent_context') as infer:
            answer = editors.start(runtime, {'task_id': 'recording', 'targets': [
                {'program_id': 'editor', 'pid': 7, 'window_id': 10, 'window_context': replacement}]})
        infer.assert_not_called()
        draft = editors.jobs[answer['editor_id']]['draft']
        self.assertEqual(draft.window_contexts, {'editor': replacement})
        self.assertEqual(draft.steps, self.original['steps'])
        self.assertEqual(self.store.get('recording'), self.original)

    def test_existing_recording_without_context_is_not_inferred_on_editor_open_or_save(self):
        from process_editor import ProcessEditors
        legacy = self.store.save({key: value for key, value in {**self.args, 'id': 'legacy'}.items()
                                 if key != 'window_contexts'})
        runtime = SimpleNamespace(check_active=mock.Mock(), mode='uia', stop_event=threading.Event(),
            programs=[{'id': 'editor', 'name': 'Editor'}], guard=SimpleNamespace())
        editors = ProcessEditors(SimpleNamespace(_program=mock.Mock()), self.store)
        def initialized(job):
            job['started'].set()
            job['done'].set()
        with mock.patch.object(editors, '_work', side_effect=initialized), \
             mock.patch('window_contexts.inferred_menuagent_context', return_value=CONTEXT) as infer:
            answer = editors.start(runtime, {'task_id': 'legacy',
                'targets': [{'program_id': 'editor', 'pid': 7, 'window_id': 10}]})
            job = editors.jobs[answer['editor_id']]
            self.assertEqual(job['draft'].window_contexts, {})
            self.assertEqual(self.store.get('legacy'), legacy)
            editors._command(job, {'action': 'save', 'payload': {
                'name': legacy['name'], 'description': legacy['instructions'], 'save_mode': 'update'}})
        infer.assert_not_called()
        saved = self.store.get('legacy')
        self.assertEqual(saved['revision'], 2)
        self.assertNotIn('window_contexts', saved)
        self.assertEqual(saved['steps'], legacy['steps'])
        self.assertEqual(self.revisions.get('legacy', 1)['steps'], legacy['steps'])


class WindowRuntime(EasyRuntime):
    """Full native identity rows plus scoped-property response; no real Driver."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.windows = [row(200, pid=100, title=TITLE)]
        self.enabled = True
        self.scoped_reads = 0
        self.guard.policy = {}
        self.guard.window_resolver = lambda hwnd: 100
        self.guard.observed_targets = set()

    def create_transition_probe(self, target):
        runtime = self
        class CurrentProbe:
            def snapshot(self):
                return {'process_exited': False,
                    'target_present': any(w['window_id'] == target['window_id'] for w in runtime.windows),
                    'creation_time': 123, 'executable': runtime.programs[0]['exe'],
                    'windows': copy.deepcopy(runtime.windows)}
            capture = snapshot
            def close(self): pass
        return CurrentProbe()

    def observe_completion_controls(self, target, selectors, timeout_ms):
        self.scoped_reads += 1
        return {'structuredContent': {**target, 'read_only': True, 'scoped_observation': True,
            'scope_complete': True, 'elements': [
                {'element_index': 1, 'automation_id': 'observed-body', 'role': 'Pane', 'verification_only': True},
                {'element_index': 2, 'parent_index': 1, 'name': '조회', 'role': 'Button',
                 'enabled': self.enabled, 'verification_only': True}]}}


class EasyContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        config = config_at(self.temp.name, tool_profile='simple', approval='client')
        self.manager = ComputerManager(config, runtime_factory=WindowRuntime)
        self.addCleanup(self.manager.close)
        patcher = mock.patch('vendor.guard.validate_arguments')
        self.authorize = patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, **args):
        return self.manager.call('computer_open', {'program_id': 'editor', **args})['structuredContent']

    def test_open_filters_work_window_and_checks_requested_controls_without_input(self):
        opened = self.open(window_selector=SELECTOR, ready_when=READY)
        self.assertEqual(opened['status'], 'connected', opened)
        self.assertTrue(opened['readiness']['ready'])
        self.assertFalse(opened['readiness']['task_verified'])
        self.assertEqual(opened['target'], {'pid': 100, 'window_id': 200})
        self.assertEqual(self.manager.easy.window_contexts[opened['window_ref']], CONTEXT)
        self.assertEqual(self.manager.session.scoped_reads, 1)
        self.assertEqual(self.manager.session.launches, [])
        self.assertTrue(all(name == 'list_windows' for name, _ in self.manager.session.calls))

    def test_ambiguous_open_returns_choices_before_readiness_without_guessing(self):
        self.open()
        self.manager.session.windows.append(row(201, pid=100, title=TITLE))
        opened = self.open(window_selector=SELECTOR, ready_when=READY)
        self.assertEqual(opened['status'], 'choose_window')
        self.assertEqual(len(opened['candidates']), 2)
        self.assertFalse(opened['input_dispatched'])
        self.assertEqual(self.manager.session.scoped_reads, 0)

    def test_explicit_window_disambiguates_duplicate_titles_but_does_not_expand_scope(self):
        self.open()
        self.manager.session.windows.append(row(201, pid=100, title=TITLE))
        opened = self.open(window_id=201, window_selector=SELECTOR, ready_when=READY)
        self.assertEqual(opened['status'], 'connected', opened)
        self.assertEqual(opened['target']['window_id'], 201)
        self.assertTrue(opened['readiness']['ready'])

    def test_unready_open_keeps_connected_reference_but_returns_no_input(self):
        self.open(); self.manager.session.enabled = False
        opened = self.open(window_selector=SELECTOR, ready_when=READY)
        self.assertEqual(opened['status'], 'window_not_ready', opened)
        self.assertFalse(opened['readiness']['ready'])
        self.assertFalse(opened['input_dispatched'])
        self.assertIn('window_ref', opened)
        self.assertEqual(opened['readiness']['checks'][0]['reason'], 'control_disabled')

    def test_targets_fresh_execution_requires_ready_but_resume_does_not_repeat_start_condition(self):
        opened = self.open(window_selector=SELECTOR)
        self.manager.session.enabled = False
        refs = {'editor': opened['window_ref']}
        contexts = {'editor': CONTEXT}
        targets, pending = self.manager.easy._targets(['editor'], refs, contexts=contexts)
        self.assertIsNone(targets)
        self.assertEqual(pending['status'], 'window_not_ready')
        reads = self.manager.session.scoped_reads
        targets, pending = self.manager.easy._targets(['editor'], refs, contexts=contexts, check_ready=False)
        self.assertIsNone(pending)
        self.assertEqual(targets, [{'program_id': 'editor', 'pid': 100, 'window_id': 200}])
        self.assertEqual(self.manager.session.scoped_reads, reads)

    def test_cached_open_context_is_rechecked_for_editor_and_is_copied(self):
        opened = self.open(window_selector=SELECTOR, ready_when=READY)
        self.manager.session.enabled = False
        targets, pending = self.manager.easy._targets(['editor'], {'editor': opened['window_ref']}, for_editor=True)
        self.assertIsNone(targets)
        self.assertEqual(pending['status'], 'window_not_ready')
        self.manager.session.enabled = True
        targets, pending = self.manager.easy._targets(['editor'], {'editor': opened['window_ref']}, for_editor=True)
        self.assertIsNone(pending)
        self.assertEqual(targets[0]['window_context'], CONTEXT)
        targets[0]['window_context']['ready_when']['controls'][0]['enabled'] = False
        self.assertTrue(self.manager.easy.window_contexts[opened['window_ref']]['ready_when']['controls'][0]['enabled'])

    def test_explicit_saved_context_mismatch_does_not_run_readiness_or_input(self):
        opened = self.open()
        with self.assertRaises(OperationError) as caught:
            self.manager.easy._targets(['editor'], {'editor': opened['window_ref']},
                contexts={'editor': {**CONTEXT, 'window_selector': {'title': 'Different work'}}})
        self.assertEqual(caught.exception.code, 'business_window_context_mismatch')
        self.assertEqual(self.manager.session.scoped_reads, 0)

    def test_process_replacement_between_discovery_and_binding_leaves_no_reference(self):
        api = self.manager.easy
        api._session(['editor'], 'Read existing work', None)
        runtime = self.manager.session
        factory = runtime.create_transition_probe
        count = 0
        def replacement(target):
            nonlocal count
            count += 1
            probe = factory(target)
            if count == 2:
                capture = probe.capture
                probe.capture = lambda: {**capture(), 'creation_time': 999}
            return probe
        with mock.patch.object(runtime, 'create_transition_probe', side_effect=replacement):
            with self.assertRaises(OperationError) as caught:
                api._attach('editor', window_selector=SELECTOR, ready_when=READY)
        self.assertEqual(caught.exception.code, 'business_window_identity_changed')
        self.assertEqual(api.bindings, {})
        self.assertEqual(api.engine.windows, {})
        self.assertEqual(runtime.scoped_reads, 0)

    def test_public_run_routes_new_and_resumed_task_context_without_mutating_steps(self):
        task = self.manager.tasks.save({'id': 'demo', 'name': 'Demo', 'instructions': 'Read existing work',
            'expected': 'Ready', 'program_ids': ['editor'], 'window_contexts': {'editor': CONTEXT},
            'steps': [{'step_id': 'delay', 'operation': 'delay', 'duration_ms': 0, 'program_id': 'editor'}]})
        self.open()
        api = self.manager.easy
        with mock.patch.object(api, '_targets', wraps=api._targets) as targets, \
             mock.patch.object(api, '_renew_workflow_budget', return_value=None), \
             mock.patch.object(self.manager.workflows, 'resume_snapshot', return_value=(task, {})), \
             mock.patch.object(self.manager.workflows, 'run', return_value={
                 'run_id': 'existing-run', 'status': 'verified', 'task_verified': True}):
            self.manager.call('computer_run_task', {'task_id': 'demo'})
            self.assertTrue(targets.call_args.kwargs['check_ready'])
            reads = self.manager.session.scoped_reads
            self.manager.session.enabled = False
            self.manager.call('computer_run_task', {'task_id': 'demo', 'resume_run_id': 'existing-run'})
            self.assertFalse(targets.call_args.kwargs['check_ready'])
            self.assertEqual(self.manager.session.scoped_reads, reads)
            self.assertEqual(targets.call_args.kwargs['contexts'], {'editor': CONTEXT})
        self.assertEqual(self.manager.tasks.get('demo')['steps'], task['steps'])


if __name__ == '__main__': unittest.main()
