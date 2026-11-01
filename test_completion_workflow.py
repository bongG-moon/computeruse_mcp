"""Actual runner/operation verification with scripted provider and menu transport.

No desktop input or real SmartFX/Excel is performed by this test.
"""
import copy
import tempfile
import threading
import unittest
from unittest import mock

from test_workflows import FakeRuntime
from test_grid_workflow import recipe as grid_recipe
from test_semantic_menus import target as menu_target
from workflows import WorkflowRunner, WorkflowError, validate_recipe

TARGET = {'pid': 100, 'window_id': 200}
FIELD = {'role': 'Edit', 'name': 'Run result'}


class Runtime(FakeRuntime):
    def __init__(self, folder):
        super().__init__(folder)
        self.stop_event = threading.Event()
        self.values = []
        self.calls = []
        self.menu_calls = []
        self.popup_alive = True
        self.menu_sent = True
        self.guard.window_resolver = lambda hwnd: 100 if hwnd == 200 or hwnd == 201 and self.popup_alive else 0

    def call(self, name, args):
        self.check_active()
        self.calls.append((name, copy.deepcopy(args)))
        if name == 'get_window_state':
            value = self.values.pop(0) if self.values else 'Ready'
            return {'structuredContent': {**TARGET, 'snapshot_id': 'current', 'elements': [
                {'element_index': 0, 'element_token': 'status', 'name': 'Run result', 'role': 'Edit',
                 'value': value, 'enabled': True, 'actions': ['set_value']},
                {'element_index': 1, 'element_token': 'search', 'name': 'Search', 'role': 'Button',
                 'enabled': True, 'actions': ['invoke']}]}}
        return {'structuredContent': {'input_sent': True, 'effect': 'confirmed'}}

    def menu_action(self, target, menu, operation, **kwargs):
        self.check_active()
        self.menu_calls.append((copy.deepcopy(target), copy.deepcopy(menu), operation))
        self.popup_alive = False
        return {'input_dispatched': self.menu_sent}


def menu_step(**kwargs):
    return {'program_id': 'editor', 'step_id': 'export', 'operation': 'menu_command',
        'menu_target': menu_target('Export to excel', 'Active sheet'), 'completion_mode': 'conditions',
        'expect': [{'selector': FIELD, 'property': 'value', 'equals': 'Ready', 'require_change': True}],
        'verification_timeout_ms': 0, **kwargs}


def task(steps):
    return {'id': 'completion-test', 'revision': 1, 'name': 'Completion', 'instructions': 'Test',
        'expected': 'Current result', 'program_ids': ['editor'], 'variables': {}, 'steps': steps}


class CompletionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = Runtime(self.folder.name)
        self.runner = WorkflowRunner(self.folder.name)
        self.targets = [{'program_id': 'editor', **TARGET}]

    def run_task(self, recipe, resume=None):
        return self.runner.run(self.runtime, recipe, {}, self.targets, resume_run_id=resume)

    def test_authored_current_result_completes_without_human_checkpoint(self):
        self.runtime.values = ['Pending', 'Ready']
        result = self.run_task(task([menu_step()]))
        self.assertTrue(result['task_verified'], result)
        self.assertEqual(len(self.runtime.menu_calls), 1)
        self.assertEqual(self.runtime.calls[0][0], 'get_window_state')
        self.assertNotIn('checkpoint', result)

    def test_existing_ready_result_is_not_new_export_success(self):
        self.runtime.values = ['Ready', 'Ready']
        recipe = task([menu_step()])
        first = self.run_task(recipe)
        self.assertFalse(first['task_verified'])
        self.assertEqual(first['step_delivery']['state'], 'sent')
        self.runner = WorkflowRunner(self.folder.name)
        second = self.run_task(recipe, first['run_id'])
        self.assertFalse(second['task_verified'])
        self.assertEqual(len(self.runtime.menu_calls), 1)

    def test_busy_then_ready_resume_uses_saved_change_evidence_without_command(self):
        self.runtime.values = ['Ready', 'Busy']
        recipe = task([menu_step()])
        first = self.run_task(recipe)
        self.assertFalse(first['task_verified'], first)
        self.assertTrue(first['operation_verification']['state']['change_seen'])
        self.runner = WorkflowRunner(self.folder.name)
        self.runtime.values = ['Ready']
        second = self.run_task(recipe, first['run_id'])
        self.assertTrue(second['task_verified'], second)
        self.assertEqual(second['run_id'], first['run_id'])
        self.assertEqual(len(self.runtime.menu_calls), 1)

    def test_unknown_delivery_still_only_observes_result_on_resume(self):
        self.runtime.menu_sent = None
        self.runtime.values = ['Pending', 'Ready']
        recipe = task([menu_step()])
        first = self.run_task(recipe)
        self.assertFalse(first['task_verified'])
        self.assertEqual(first['step_delivery']['state'], 'unknown')
        second = self.run_task(recipe, first['run_id'])
        self.assertTrue(second['task_verified'], second)
        self.assertEqual(len(self.runtime.menu_calls), 1)

    def test_closed_popup_resumes_at_prebound_owner_only(self):
        wait = {'program_id': 'editor', 'operation': 'wait_for_window', 'window_ref': 'menu',
            'owner_ref': 'main', 'title': '', 'class_name': 'MenuFixture', 'timeout_ms': 0}
        recipe = task([wait, menu_step(window_ref='menu', completion_window_ref='main')])
        self.runtime.values = ['Ready', 'Busy']
        with mock.patch('workflows.execute_window_wait', return_value={'task_verified': True, 'target': {'pid': 100, 'window_id': 201}}):
            first = self.run_task(recipe)
        self.assertEqual(first['completed_steps'], 1, first)
        self.assertFalse(self.runtime.popup_alive)
        self.runner = WorkflowRunner(self.folder.name)
        self.runtime.values = ['Ready']
        with mock.patch('workflows.execute_window_wait', side_effect=AssertionError('closed popup must not be rediscovered')):
            second = self.run_task(recipe, first['run_id'])
        self.assertTrue(second['task_verified'], second)
        self.assertEqual(self.runtime.menu_calls[0][0]['window_id'], 201)
        self.assertTrue(all(args['window_id'] == 200 for name, args in self.runtime.calls if name == 'get_window_state'))

    def test_changed_result_lifetime_does_not_adopt_saved_proof(self):
        self.runtime.values = ['Ready', 'Busy']
        recipe = task([menu_step()])
        first = self.run_task(recipe)
        self.runtime.id = 'replacement-window-session'
        second = self.run_task(recipe, first['run_id'])
        self.assertFalse(second['task_verified'])
        self.assertEqual(second['last_result']['diagnostic']['code'], 'operation_verification_unavailable')
        self.assertEqual(len(self.runtime.menu_calls), 1)

    def test_new_app_or_future_result_binding_is_not_implicitly_allowed(self):
        for ref in ('excel', 'later', '${window}'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                validate_recipe([menu_step(completion_window_ref=ref)], {}, ['editor'])

    def test_query_busy_evidence_survives_before_grid_without_reclick(self):
        recipe = grid_recipe()
        condition = {'selector': FIELD, 'property': 'value', 'equals': 'Ready'}
        recipe['steps'][0]['expect'] = [{**condition, 'require_change': True}]
        recipe['steps'][0]['verification_timeout_ms'] = 0
        recipe['steps'][1]['grid_intent']['ready_when'] = [condition]
        self.runtime.values = ['Ready', 'Busy']
        first = self.run_task(recipe)
        self.assertEqual(first['completed_steps'], 0, first)
        self.runner = WorkflowRunner(self.folder.name)
        self.runtime.values = ['Ready']
        with mock.patch('grid_context.execute_grid_menu', return_value={'task_verified': False, 'input_dispatched': False}) as opening:
            second = self.run_task(recipe, first['run_id'])
        self.assertEqual(second['completed_steps'], 1, second)
        self.assertTrue(second['step_receipts'][0]['query_input_verified'])
        self.assertTrue(opening.call_args.kwargs['query_evidence']['require_change_verified'])
        self.assertEqual(sum(name == 'click' for name, _ in self.runtime.calls), 1)

    def test_completed_generation_and_ready_condition_can_be_separate(self):
        recipe = grid_recipe()
        generation = {'selector': {'role': 'Text', 'name': 'Completed generation'},
            'property': 'value', 'equals': 'next-query', 'require_change': True}
        readiness = copy.deepcopy(recipe['steps'][1]['grid_intent']['ready_when'][0])
        recipe['steps'][0]['expect'] = [generation, readiness]
        validate_recipe(recipe['steps'], {}, ['editor'])
        recipe['steps'][0]['expect'] = [generation]
        with self.assertRaises(ValueError):
            validate_recipe(recipe['steps'], {}, ['editor'])

    def test_local_receipt_does_not_expose_persisted_completion_values(self):
        import json
        from local_delivery import receipt
        self.runtime.values = ['PRIVATE_OLD_WORK_VALUE', 'Busy']
        result = self.run_task(task([menu_step()]))
        encoded = json.dumps(receipt(result))
        self.assertNotIn('PRIVATE_OLD_WORK_VALUE', encoded)
        self.assertNotIn('operation_verification', encoded)

    def test_editing_one_completion_keeps_independent_scope_check_in_new_revision(self):
        from process_editor import ProcessDraft
        from server import TaskStore
        from task_revision import TaskRevisions
        scope = {'role': 'Edit', 'name': 'Output scope'}

        class ScopeRuntime(Runtime):
            def call(self, name, args):
                answer = super().call(name, args)
                if name == 'get_window_state':
                    answer['structuredContent']['elements'].append({
                        'element_index': 2, 'element_token': 'scope', **scope, 'enabled': True,
                        'value': 'selected_ranges', 'actions': ['set_value']})
                return answer

        store = TaskStore(self.folder.name, {'programs': [{'id': 'editor', 'name': 'Fixture'}]})
        command = menu_step(completion_window_ref='main')
        scope_check = {'selector': scope, 'property': 'value', 'equals': 'whole_active_sheet'}
        command['expect'].append(scope_check)
        args = {**task([command]), 'id': 'completion-edit'}
        args.pop('revision')
        saved = store.save(args)
        draft = ProcessDraft(self.targets, saved)
        draft.selections['result'] = {'recognition': 'uia', 'target': ('editor', 'main'), 'selector': FIELD}
        # Legacy IPC payload must edit its matching condition, not replace all.
        draft.completion({'index': 0, 'selection_id': 'result', 'property': 'value',
            'equals': 'Ready', 'completion_window_ref': 'main'})
        updated = TaskRevisions(store).save({**args, 'steps': draft.steps}, expected_revision=saved['revision'])
        self.assertEqual(updated['revision'], saved['revision'] + 1)
        self.assertEqual(updated['steps'][0]['expect'][1], scope_check)
        self.assertEqual(updated['steps'][0]['step_id'], saved['steps'][0]['step_id'])
        self.assertEqual(TaskRevisions(store).get(saved['id'], saved['revision'])['steps'], saved['steps'])
        runtime = ScopeRuntime(self.folder.name)
        runtime.values = ['Pending', 'Ready']
        result = WorkflowRunner(self.folder.name).run(runtime, updated, {}, self.targets)
        self.assertFalse(result['task_verified'], result)
        self.assertEqual(len(runtime.menu_calls), 1)


if __name__ == '__main__': unittest.main()
