"""Independent business-window selection and lifecycle; no desktop input."""
import copy
from types import SimpleNamespace
import unittest
from unittest import mock

from business_windows import (BusinessWindowError, business_window_candidates, class_matches,
                              discover_business_windows, select_business_window, validate_window_selector)
from window_transitions import WindowTransition, TransitionError, validate_transition
from test_window_transitions import Probe, state


SELECTOR = {'title_prefix': 'Device Setup Standard (CJTEST) - ['}
CLASS = 'WindowsForms10.Window.8.app.0.abc_r7_ad1'


def row(hwnd=10, *, pid=7, title='Device Setup Standard (CJTEST) - [smartMMDM, user] - Agent', **extra):
    return {'pid': pid, 'window_id': hwnd, 'title': title, 'class_name': CLASS,
        'thread_id': 30, 'owner_window_id': 0, 'root_owner_window_id': hwnd,
        'visible': True, 'minimized': False, 'enabled': True, **extra}


class BusinessWindowTests(unittest.TestCase):
    def test_hidden_resident_and_owned_popup_are_not_business_windows(self):
        rows = [row(1, visible=False), row(2), row(3, owner_window_id=2, root_owner_window_id=2),
                row(4, root_owner_window_id=2), row(5, minimized=True)]
        evidence = business_window_candidates(rows, SELECTOR, pid=7)
        self.assertEqual([r['window_id'] for r in evidence['matches']], [2])
        self.assertEqual({r['reason'] for r in evidence['rejected']},
            {'hidden_or_visibility_unknown', 'owned_popup_requires_owner_binding',
             'nested_child_not_business_window', 'minimized_or_state_unknown'})

    def test_two_windows_require_stable_title_context_not_pid_or_first(self):
        rows = [row(2, title='Lot Summary'), row(3)]
        self.assertEqual(select_business_window(rows, SELECTOR, pid=7)['window_id'], 3)
        with self.assertRaises(BusinessWindowError) as caught:
            select_business_window([row(2), row(3)], SELECTOR, pid=7)
        self.assertEqual(caught.exception.code, 'business_window_ambiguous')
        self.assertEqual(len(caught.exception.candidates), 2)

    def test_exact_title_and_runtime_suffix_are_independent_constraints(self):
        title = 'Stable title'
        current = row(2, title=title, class_name='WindowsForms10.Window.8.app.0.def_r9_ad2')
        self.assertEqual(select_business_window([current], {'title': title, 'class_name': CLASS})['window_id'], 2)
        self.assertIsNone(select_business_window([current], {'title': title, 'class_name': CLASS.replace('.8.', '.808.')}))
        self.assertFalse(class_matches('WindowsForms10.*', CLASS))

    def test_user_suffix_can_change_without_persisting_user_or_handle(self):
        spec = validate_window_selector(SELECTOR)
        original = copy.deepcopy(spec)
        for user, hwnd in [('user-one', 22), ('user-two', 400)]:
            found = select_business_window([row(hwnd, title=SELECTOR['title_prefix'] + user + ']')], spec)
            self.assertEqual(found['window_id'], hwnd)
        self.assertEqual(spec, original)
        self.assertEqual(set(spec), {'title_prefix'})

    def test_invalid_durable_selectors_reject_identifiers_and_wildcards_are_literal(self):
        for selector in ({}, {'title': 'A', 'title_prefix': 'A'}, {'title_prefix': ''},
                         {'title': 'A', 'window_id': 7}, {'title': 'A', 'pid': 7},
                         {'title': 'A\x00B'}, {'title': 'A', 'runtime_id': [1, 2]},
                         {'class_name': CLASS}):
            with self.subTest(selector=selector), self.assertRaises(BusinessWindowError):
                validate_window_selector(selector)
        self.assertIsNone(select_business_window([row()], {'title_prefix': 'Device*'}))

    def test_missing_identity_cannot_make_another_window_look_unique(self):
        broken = row(3); del broken['thread_id']
        with self.assertRaises(BusinessWindowError) as caught:
            select_business_window([row(2), broken], SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_identity_unavailable')

    def test_same_selector_different_pid_is_not_a_same_process_target(self):
        self.assertIsNone(select_business_window([row(2, pid=999)], SELECTOR, pid=7))
        self.assertEqual(select_business_window([row(2, pid=999), row(3)], SELECTOR, pid=7)['window_id'], 3)

    def test_duplicate_window_inventory_is_not_unique(self):
        with self.assertRaises(BusinessWindowError) as caught:
            select_business_window([row(2), row(2)], SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_inventory_invalid')


class BusinessTransitionTests(unittest.TestCase):
    def tracker(self, initial, current, spec=None):
        probe = Probe(initial=initial, current=current)
        runtime = SimpleNamespace(check_active=lambda: None, create_transition_probe=lambda target: probe)
        transition = WindowTransition(runtime, {'pid': 7, 'window_id': 10},
            validate_transition(spec or {'mode': 'business_window', 'window_selector': SELECTOR}))
        transition.prepare()
        self.addCleanup(transition.close)
        self.assertIsNone(transition.error)
        return transition, probe

    def test_new_independent_window_can_appear_while_original_remains(self):
        source = row(10, title='Launcher work')
        tracker, probe = self.tracker(state(source, present=True), state(source, row(22), present=True))
        selected = tracker.inspect()
        self.assertEqual(selected['window_id'], 22)
        tracker.confirm({'pid': 7, 'window_id': 22}, selected)
        self.assertEqual(probe.reads, 2)

    def test_already_open_work_window_can_be_explicitly_reselected(self):
        source, target = row(10, title='Launcher work'), row(22)
        snapshot = state(source, target, present=True)
        tracker, _ = self.tracker(snapshot, snapshot)
        self.assertEqual(tracker.inspect()['window_id'], 22)

    def test_original_business_window_can_be_explicit_destination(self):
        original = state(row(10), present=True)
        tracker, _ = self.tracker(original, original)
        self.assertEqual(tracker.inspect()['window_id'], 10)

    def test_original_auto_mode_stays_strict(self):
        source = row(10, title='Launcher work')
        tracker, _ = self.tracker(state(source, present=True), state(source, row(22), present=True), {'mode': 'auto'})
        with self.assertRaises(TransitionError) as caught: tracker.inspect()
        self.assertEqual(caught.exception.code, 'transition_original_present')

    def test_changed_process_or_window_thread_is_rejected(self):
        source, target = row(10, title='Launcher work'), row(22)
        initial = state(source, target, present=True)
        for current, expected in ((state(source, target, present=True, creation=101), 'transition_process_changed'),
                (state(source, row(22, thread_id=31), present=True), 'transition_target_identity_changed')):
            tracker, _ = self.tracker(initial, current)
            with self.assertRaises(TransitionError) as caught: tracker.inspect()
            self.assertEqual(caught.exception.code, expected)

    def test_confirmation_rechecks_selected_window_and_does_not_pick_replacement(self):
        source = row(10, title='Launcher work')
        tracker, probe = self.tracker(state(source, present=True), state(source, row(22), present=True))
        selected = tracker.inspect()
        probe.current = state(source, row(23), present=True)
        with self.assertRaises(TransitionError) as caught:
            tracker.confirm({'pid': 7, 'window_id': 22}, selected)
        self.assertEqual(caught.exception.code, 'transition_target_changed')

    def test_duplicate_work_windows_return_candidates_without_guessing(self):
        source = row(10, title='Launcher work')
        tracker, _ = self.tracker(state(source, present=True), state(source, row(22), row(23), present=True))
        with self.assertRaises(TransitionError) as caught: tracker.inspect()
        self.assertEqual(caught.exception.code, 'business_window_ambiguous')
        self.assertEqual(len(tracker.candidates), 2)

    def test_business_selector_is_not_accepted_by_owned_popup_modes(self):
        for spec in ({'mode': 'business_window'}, {'mode': 'business_window', 'title': 'A'},
                {'mode': 'auto', 'window_selector': SELECTOR}, {'mode': 'same_window', 'window_selector': SELECTOR}):
            with self.assertRaises(TransitionError): validate_transition(spec)


class BusinessDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.probes = {}
        self.runtime = SimpleNamespace(check_active=mock.Mock(), call=mock.Mock(),
            guard=SimpleNamespace(policy={}, process_resolver=None, window_resolver=None, check_stop=mock.Mock()),
            create_transition_probe=lambda target: self.probes[target['pid']])
        self.authorize = mock.patch('vendor.guard.validate_arguments').start()
        self.addCleanup(mock.patch.stopall)

    def add(self, pid, rows, current=None):
        initial = state(*rows, present=True)
        self.probes[pid] = Probe(initial, current or initial)
        return [{'pid': r['pid'], 'window_id': r['window_id'], 'title': r['title']} for r in rows if r['visible']]

    def test_retained_native_selection_uses_no_driver_or_focus(self):
        listed = self.add(7, [row(1, visible=False), row(10, title='Other work'), row(22)])
        selected = discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(selected['window_id'], 22)
        self.assertEqual(selected['creation_time'], self.probes[7].initial['creation_time'])
        self.assertEqual(selected['executable'], self.probes[7].initial['executable'])
        self.assertTrue(self.probes[7].closed)
        self.assertEqual(self.probes[7].reads, 1)
        self.runtime.call.assert_not_called()
        self.assertGreaterEqual(self.authorize.call_count, 4)

    def test_ambiguous_across_approved_processes_is_not_first_pid(self):
        listed = self.add(7, [row(10)]) + self.add(8, [row(20, pid=8)])
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_ambiguous')
        self.assertTrue(all(p.closed for p in self.probes.values()))

    def test_new_matching_window_outside_discovery_is_not_silently_added(self):
        listed = self.add(7, [row(10, title='Other work')], state(row(10, title='Other work'), row(22), present=True))
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_inventory_changed')
        self.assertTrue(self.probes[7].closed)

    def test_changed_process_creation_is_not_reused(self):
        listed = self.add(7, [row(10)], state(row(10), present=True, creation=101))
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_process_changed')
        self.assertTrue(self.probes[7].closed)

    def test_same_hwnd_changed_thread_is_not_reused(self):
        listed = self.add(7, [row(10)], state(row(10, thread_id=100), present=True))
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(caught.exception.code, 'business_window_inventory_changed')

    def test_guard_denial_precedes_native_capture(self):
        listed = self.add(7, [row(10)])
        self.authorize.side_effect = RuntimeError('denied')
        with self.assertRaises(RuntimeError): discover_business_windows(self.runtime, listed, SELECTOR)
        self.assertEqual(self.probes[7].reads, 0)
        self.runtime.call.assert_not_called()

    def test_owned_popup_matching_title_is_not_main(self):
        listed = self.add(7, [row(10, owner_window_id=1, root_owner_window_id=1)])
        self.assertIsNone(discover_business_windows(self.runtime, listed, SELECTOR))
        self.assertTrue(self.probes[7].closed)

    def test_explicit_selected_window_ignores_other_matching_windows(self):
        listed = self.add(7, [row(10), row(22)])
        listed += self.add(8, [row(30, pid=8)])
        selected = discover_business_windows(self.runtime, listed, SELECTOR,
                                            explicit_target={'pid': 7, 'window_id': 22})
        self.assertEqual(selected['window_id'], 22)
        self.assertIn('creation_time', selected)
        self.assertTrue(self.probes[7].closed)
        self.assertFalse(self.probes[8].closed)
        self.assertEqual(self.probes[8].reads, 0)
        self.runtime.call.assert_not_called()

    def test_explicit_selection_can_use_single_approved_row(self):
        listed = self.add(7, [row(10), row(22)], state(row(10), row(22), row(23), present=True))
        selected = discover_business_windows(self.runtime, listed[:1], SELECTOR,
                                            explicit_target={'pid': 7, 'window_id': 10})
        self.assertEqual(selected['window_id'], 10)

    def test_explicit_target_must_be_in_approved_inventory(self):
        listed = self.add(7, [row(10), row(22)])
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed[:1], SELECTOR,
                                      explicit_target={'pid': 7, 'window_id': 22})
        self.assertEqual(caught.exception.code, 'business_window_inventory_changed')
        self.authorize.assert_not_called()
        self.assertFalse(self.probes[7].closed)

    def test_explicit_target_still_requires_selector_and_independent_window(self):
        for target in (row(10, title='Different work'), row(10, visible=False),
                       row(10, owner_window_id=22, root_owner_window_id=22)):
            self.add(7, [target, row(22)])
            listed = [{'pid': 7, 'window_id': 10, 'title': target['title']}]
            self.assertIsNone(discover_business_windows(self.runtime, listed, SELECTOR,
                                                       explicit_target={'pid': 7, 'window_id': 10}))

    def test_explicit_target_still_rejects_changed_thread(self):
        listed = self.add(7, [row(10), row(22)], state(row(10, thread_id=31), row(22), present=True))
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR,
                                      explicit_target={'pid': 7, 'window_id': 10})
        self.assertEqual(caught.exception.code, 'business_window_inventory_changed')

    def test_explicit_target_still_rejects_process_reuse(self):
        listed = self.add(7, [row(10), row(22)], state(row(10), row(22), present=True, creation=101))
        with self.assertRaises(BusinessWindowError) as caught:
            discover_business_windows(self.runtime, listed, SELECTOR,
                                      explicit_target={'pid': 7, 'window_id': 10})
        self.assertEqual(caught.exception.code, 'business_window_process_changed')


class BusinessOperationTests(unittest.TestCase):
    def make(self, *, existing=False, duplicate=False, after='after'):
        from test_operations import Runtime, TARGET, FIELD, element, snapshot
        source, destination = row(TARGET['window_id'], title='Origin work'), row(22)
        initial = state(source, *([destination] if existing else []), present=True)
        current = state(source, destination, *([row(23)] if duplicate else []), present=True)
        runtime = Runtime([snapshot(element()), {**snapshot(element(value=after)), 'window_id': 22}])
        probe = Probe(initial, current)
        runtime.create_transition_probe = lambda target: probe
        step = {'operation': 'click', 'selector': FIELD,
            'expect': [{'selector': FIELD, 'property': 'value', 'equals': 'after'}],
            'verification_timeout_ms': 0,
            'window_transition': {'mode': 'business_window', 'window_selector': SELECTOR, 'timeout_ms': 0}}
        return runtime, step, TARGET

    def test_new_and_existing_work_windows_verify_after_one_input_only(self):
        from operations import Operations
        for existing in (False, True):
            with self.subTest(existing=existing):
                runtime, step, target = self.make(existing=existing)
                answer = Operations(runtime).execute(step, target)
                self.assertTrue(answer['task_verified'], answer)
                self.assertEqual(answer['target']['window_id'], 22)
                self.assertEqual([name for name, _ in runtime.calls], ['get_window_state', 'click', 'get_window_state'])
                self.assertEqual(runtime.calls[-1][1]['window_id'], 22)

    def test_window_existence_is_not_completion(self):
        from operations import Operations
        runtime, step, target = self.make(existing=True, after='not done')
        answer = Operations(runtime).execute(step, target)
        self.assertFalse(answer['task_verified'])
        self.assertEqual([name for name, _ in runtime.calls].count('click'), 1)

    def test_ambiguous_work_window_does_not_send_or_repeat_input_there(self):
        from operations import Operations
        runtime, step, target = self.make(duplicate=True)
        answer = Operations(runtime).execute(step, target)
        self.assertFalse(answer['task_verified'])
        self.assertEqual(answer['diagnostic']['code'], 'business_window_ambiguous')
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state', 'click'])

    def test_missing_process_proof_blocks_initial_input(self):
        from operations import Operations
        runtime, step, target = self.make()
        del runtime.create_transition_probe
        answer = Operations(runtime).execute(step, target)
        self.assertFalse(answer['input_dispatched'])
        self.assertEqual(answer['diagnostic']['code'], 'transition_identity_unavailable')
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state'])


if __name__ == '__main__': unittest.main()
