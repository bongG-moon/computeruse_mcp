"""Native expansion proof regression; fake HWNDs, no desktop input."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from combo_popup import ComboPopupDiscovery
from operations import OperationError, Operations
from test_operations import Runtime, TARGET, COMBO, element, snapshot


CLASS = 'WindowsForms10.Window.808.app.0.abc_r10_ad1'


def row(hwnd=12, **extra):
    return {'pid': 7, 'window_id': hwnd, 'title': '', 'class_name': CLASS if hwnd != 12 else 'Main',
            'thread_id': 2, 'owner_window_id': 30 if hwnd != 12 else 0,
            'root_owner_window_id': 30 if hwnd != 12 else 12, 'visible': True, **extra}


def state(rows, **extra):
    return {'process_exited': False, 'target_present': True, 'creation_time': 123,
            'executable': 'C:/fixture.exe', 'windows': copy.deepcopy(rows), **extra}


class Probe:
    def __init__(self, before=None, after=None):
        self.before = before or state([row()])
        self.after = after or state([row(), row(99)])
        self.reads, self.closed, self.path_changed = 0, False, False
        self.desktop_parent = 0

    def capture(self):
        return copy.deepcopy(self.before)

    def snapshot(self):
        return self.transition_snapshot()

    def transition_snapshot(self):
        self.reads += 1
        data = self.before if self.reads == 1 else self.after
        return copy.deepcopy(data(self.reads) if callable(data) else data)

    def ownership_path(self, hwnd, owner):
        if hwnd == 77:
            return []
        return [{'window_id': hwnd, 'pid': 7, 'thread_id': 2, 'owner_window_id': 30,
                 'parent_window_id': self.desktop_parent},
                {'window_id': 30, 'pid': 7, 'thread_id': 3 if self.path_changed else 2,
                 'owner_window_id': 0, 'parent_window_id': owner}]

    def close(self):
        self.closed = True


class DiscoveryTests(unittest.TestCase):
    def prepare(self, probe=None):
        probe = probe or Probe()
        runtime = SimpleNamespace(check_active=mock.Mock(), create_transition_probe=mock.Mock(return_value=probe))
        tracker = ComboPopupDiscovery(runtime, TARGET)
        self.addCleanup(tracker.close)
        tracker.prepare()
        return tracker, probe

    def test_embedded_owner_parent_chain_and_hidden_reused_hwnd(self):
        tracker, probe = self.prepare(Probe(before=state([row(), row(99, visible=False)])))
        self.assertEqual(tracker.discover(), {'pid': 7, 'window_id': 99})
        tracker.confirm()
        self.assertEqual(tracker.selected_path[1]['parent_window_id'], 12)

    def test_preexisting_visible_popup_is_not_used(self):
        tracker, _ = self.prepare(Probe(before=state([row(), row(99)])))
        self.assertIsNone(tracker.discover())

    def test_unrelated_same_pid_and_other_pid_are_not_used(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(77), row(88, pid=9)])))
        self.assertIsNone(tracker.discover())

    def test_multiple_lists_are_ambiguous(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99), row(98)])))
        with self.assertRaisesRegex(OperationError, '여러 개'):
            tracker.discover()

    def test_search_modal_is_not_a_combobox_option_list(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99, class_name='#32770', title='Search')])) )
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_popup_kind_unsupported')

    def test_untitled_related_menu_is_not_a_combobox_option_list(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99, class_name=CLASS.replace('.808.', '.20808.'))])))
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_popup_kind_unsupported')

    def test_related_nonlist_with_desktop_parent_is_not_silently_ignored(self):
        probe = Probe(after=state([row(), row(99, class_name=CLASS.replace('.808.', '.20808.'))]))
        probe.desktop_parent = 65552
        tracker, _ = self.prepare(probe)
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_popup_kind_unsupported')

    def test_only_explicit_native_child_evidence_excludes_child_editor(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99),
            row(88, class_name='Edit', title='N', is_child=True), row(87, class_name=CLASS, is_child=True)])))
        self.assertEqual(tracker.discover(), {'pid':7, 'window_id':99})

    def test_named_winforms_808_search_form_is_not_a_combobox_list(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99, title='Searchable list')])) )
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_popup_kind_unsupported')

    def test_untitled_808_with_disabled_owner_is_not_a_combobox_list(self):
        tracker, _ = self.prepare(Probe(after=state([row(enabled=False), row(99)])))
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_modal_owner_disabled')

    def test_process_replacement_is_rejected(self):
        tracker, _ = self.prepare(Probe(after=state([row(), row(99)], creation_time=124)))
        with self.assertRaises(OperationError):
            tracker.discover()

    def test_hidden_hwnd_replacement_is_rejected(self):
        tracker, _ = self.prepare(Probe(before=state([row(), row(99, visible=False, thread_id=8)])))
        with self.assertRaises(OperationError) as error:
            tracker.discover()
        self.assertEqual(error.exception.code, 'combo_popup_changed')

    def test_owner_path_change_after_observation_is_rejected(self):
        tracker, probe = self.prepare()
        tracker.discover()
        probe.path_changed = True
        with self.assertRaises(OperationError) as error:
            tracker.confirm()
        self.assertEqual(error.exception.code, 'combo_popup_changed')

    def test_popup_disappearing_or_changing_identity_is_rejected(self):
        for rows in ([row()], [row(), row(99, thread_id=9)], [row(), row(99, visible=False)]):
            tracker, probe = self.prepare()
            tracker.discover()
            probe.after = state(rows)
            with self.assertRaises(OperationError):
                tracker.confirm()

    def test_stop_prevents_further_discovery(self):
        tracker, _ = self.prepare()
        tracker.runtime.check_active.side_effect = OperationError('stopped', 'stopped')
        with self.assertRaises(OperationError):
            tracker.discover()


class OperationIntegrationTests(unittest.TestCase):
    def setup_runtime(self, *, probe=None, popup_error=None, actions=None):
        combo = element(0, COMBO['name'], 'ComboBox', 'P', actions_unused=None)
        combo['actions'] = ['expand']
        menu = element(0, 'List', 'DataGrid', '')
        option = element(1, 'N', 'Row', '', parent_index=0, selected=False)
        option['actions'] = ['select']
        observed_popup = {**snapshot(menu, option), 'window_id': 99}
        runtime = Runtime([snapshot(combo), popup_error or observed_popup, snapshot({**combo, 'value': 'N'})], actions=actions)
        runtime.probe = probe or Probe()
        runtime.create_transition_probe = mock.Mock(return_value=runtime.probe)
        return runtime

    def run_selection(self, runtime):
        return Operations(runtime).execute({'operation': 'select_option', 'selector': COMBO, 'value': 'N',
            'verification_timeout_ms': 0, 'window_transition': {'mode': 'same_window'}}, TARGET)

    def test_native_chain_selects_fresh_driver_row_then_verifies_parent_value(self):
        runtime = self.setup_runtime(probe=Probe(before=state([row(), row(99, visible=False)])))
        result = self.run_selection(runtime)
        self.assertTrue(result['task_verified'], result)
        self.assertEqual([args['window_id'] for name, args in runtime.calls if name == 'click'], [12, 99])
        self.assertFalse(any(name == 'list_windows' for name, _ in runtime.calls))
        self.assertTrue(runtime.probe.closed)
        self.assertLessEqual(result['metrics']['combo_popup_native_reads'], 4)

    def test_default_transition_probe_is_reused_and_closed(self):
        runtime = self.setup_runtime()
        result = Operations(runtime).execute({'operation': 'select_option', 'selector': COMBO,
            'value': 'N', 'verification_timeout_ms': 0}, TARGET)
        self.assertTrue(result['task_verified'], result)
        runtime.create_transition_probe.assert_called_once_with(TARGET)
        self.assertTrue(runtime.probe.closed)

    def test_live_popup_omitted_by_driver_has_explicit_diagnosis_no_second_click(self):
        runtime = self.setup_runtime(popup_error=RuntimeError('No window with window_id 99 exists.'))
        result = self.run_selection(runtime)
        self.assertEqual(result['diagnostic']['code'], 'combo_popup_driver_unavailable')
        self.assertEqual(sum(name == 'click' for name, _ in runtime.calls), 1)
        self.assertEqual([args['window_id'] for name, args in runtime.calls if name == 'get_window_state'], [12, 99])
        self.assertFalse(result['task_verified'])

    def test_ambiguous_or_modal_popup_never_receives_option_click(self):
        for extra in ([row(99), row(98)], [row(99, class_name='#32770', title='Search')],
                      [row(99, title='Searchable list')]):
            runtime = self.setup_runtime(probe=Probe(after=state([row(), *extra])))
            result = self.run_selection(runtime)
            self.assertFalse(result['task_verified'])
            self.assertEqual(sum(name == 'click' for name, _ in runtime.calls), 1)
            self.assertEqual(sum(name == 'get_window_state' for name, _ in runtime.calls), 1)

    def test_unknown_expansion_is_not_retried_or_parent_observed(self):
        runtime = self.setup_runtime(actions=[RuntimeError('unknown delivery')])
        result = self.run_selection(runtime)
        self.assertFalse(result['task_verified'])
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state', 'click'])
        self.assertTrue(runtime.probe.closed)

    def test_unknown_selection_is_not_retried_or_parent_observed(self):
        runtime = self.setup_runtime(actions=[{'ok': True}, RuntimeError('unknown selection delivery')])
        result = self.run_selection(runtime)
        self.assertFalse(result['task_verified'])
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state', 'click', 'get_window_state', 'click'])
        self.assertTrue(runtime.probe.closed)

    def test_popup_replacement_during_driver_read_blocks_selection(self):
        runtime = self.setup_runtime()
        original = runtime.observations[1]
        def replace(name, args):
            runtime.probe.after = state([row(), row(99, thread_id=9)])
            return original
        runtime.observations[1] = replace
        result = self.run_selection(runtime)
        self.assertEqual(result['diagnostic']['code'], 'combo_popup_changed')
        self.assertEqual(sum(name == 'click' for name, _ in runtime.calls), 1)

    def test_permission_denial_does_not_trigger_native_input_or_retry(self):
        denial = {'isError': True, 'structuredContent': {'error_code': 'target_denied'},
                  'content': [{'type': 'text', 'text': 'Policy refused popup observation'}]}
        runtime = self.setup_runtime(popup_error=denial)
        result = self.run_selection(runtime)
        self.assertFalse(result['task_verified'])
        self.assertIn('target_denied', result['diagnostic']['message'])
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state', 'click', 'get_window_state'])

    def test_probe_failure_blocks_expansion_input(self):
        runtime = self.setup_runtime(probe=Probe(before=state([row(thread_id=None)])))
        result = self.run_selection(runtime)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state'])


if __name__ == '__main__':
    unittest.main()
