"""Explicit prerequisite reads, with no desktop input or full Driver inventory."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from operations import OperationError
from window_readiness import READY_WHEN_SCHEMA, validate_ready_when, wait_ready


TARGET = {'pid': 10, 'window_id': 20}
QUERY = {'name': '조회', 'role': 'Button', 'within': {'automation_id': 'observed_body'}}
SPEC = {'controls': [{'selector': QUERY, 'enabled': True}], 'timeout_ms': 1000}


def data(*elements, **extra):
    return {'structuredContent': {**TARGET, 'read_only': True, 'scoped_observation': True,
        'scope_complete': True, 'elements': list(elements), **extra}}


def body(enabled=True, **extra):
    return data({'element_index': 1, 'name': 'Body', 'automation_id': 'observed_body',
        'role': 'Pane', 'verification_only': True},
        {'element_index': 2, 'parent_index': 1, 'name': '조회', 'role': 'Button',
         'enabled': enabled, 'verification_only': True, **extra})


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def wait(self, duration): self.now += duration


class Probe:
    def __init__(self):
        self.state = {'process_exited': False, 'target_present': True, 'creation_time': 1,
            'executable': 'approved.exe', 'windows': [{**TARGET, 'thread_id': 7, 'class_name': 'Work',
            'owner_window_id': 0, 'visible': True, 'enabled': True, 'minimized': False}]}
        self.closed = False
    def capture(self): return self.snapshot()
    def snapshot(self): return copy.deepcopy(self.state)
    def close(self): self.closed = True


class Runtime:
    def __init__(self, observations):
        self.probe = Probe(); self.observations = list(observations); self.calls = []
        self.guard = SimpleNamespace(policy={}, process_resolver=None, window_resolver=None,
            check_stop=mock.Mock(), observed_targets={(10, 20), (10, 30)})
        self.execution_lock = threading.RLock()
        self.cancelled = False
    def check_active(self):
        if self.cancelled: raise OperationError('cancelled', 'cancelled')
    def create_transition_probe(self, target): return self.probe
    def observe_completion_controls(self, target, selectors, timeout_ms):
        self.calls.append((copy.deepcopy(target), copy.deepcopy(selectors), timeout_ms))
        result = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        if callable(result): result = result()
        if isinstance(result, Exception): raise result
        return copy.deepcopy(result)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('vendor.guard.validate_arguments')
        self.validate = patch.start(); self.addCleanup(patch.stop)
        self.clock = Clock()
    def run_ready(self, runtime, spec=None):
        return wait_ready(runtime, TARGET, spec or SPEC, clock=self.clock, wait=self.clock.wait)

    def test_missing_then_attached_then_enabled_is_read_without_input(self):
        runtime = Runtime([data(), body(False), body(True)])
        result = self.run_ready(runtime)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['ready'])
        self.assertEqual(result['metrics']['scoped_reads'], 3)
        self.assertEqual(result['metrics']['driver_full_tree_reads'], 0)
        self.assertEqual(result['metrics']['scope_lookup_metadata_reads'], 3)
        self.assertFalse(result['task_verified']); self.assertFalse(result['input_dispatched'])
        self.assertFalse(result['focus_dispatched']); self.assertFalse(result['authorizes_input'])
        self.assertEqual(runtime.guard.observed_targets, {(10, 30)})
        self.assertTrue(runtime.probe.closed)

    def test_only_requested_inner_control_needed_no_toolbar_requirement(self):
        runtime = Runtime([body()])
        result = self.run_ready(runtime)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(runtime.calls[0][1], [QUERY])
        self.assertEqual(len(runtime.calls), 1)

    def test_same_named_toolbar_button_is_excluded_by_scope(self):
        answer = body()
        answer['structuredContent']['elements'].extend([
            {'element_index': 3, 'name': 'Commands', 'role': 'ToolBar', 'verification_only': True},
            {'element_index': 4, 'parent_index': 3, 'name': '조회', 'role': 'Button',
             'enabled': False, 'verification_only': True}])
        self.assertEqual(self.run_ready(Runtime([answer]))['status'], 'ready')

    def test_duplicate_controls_or_scope_never_become_ready(self):
        for duplicate in ({'element_index': 5, 'parent_index': 1, 'name': '조회', 'role': 'Button', 'enabled': True},
                          {'element_index': 6, 'automation_id': 'observed_body', 'name': 'Other', 'role': 'Pane'}):
            with self.subTest(duplicate=duplicate):
                answer = body()
                answer['structuredContent']['elements'].append({**duplicate, 'verification_only': True})
                runtime = Runtime([answer])
                result = self.run_ready(runtime)
                self.assertEqual(result['diagnostic']['code'], 'readiness_controls_unresolved')
                self.assertEqual(len(runtime.calls), 1)

    def test_partial_foreign_or_actionable_response_cannot_pass(self):
        for change in ({'truncated': True}, {'scope_complete': False}, {'pid': 11}, {'read_only': False}):
            answer=body(); answer['structuredContent'].update(change)
            self.assertEqual(self.run_ready(Runtime([answer]))['diagnostic']['code'], 'readiness_incomplete_observation')
        answer=body(); answer['structuredContent']['elements'][1]['element_token']='not-authorized'
        self.assertEqual(self.run_ready(Runtime([answer]))['diagnostic']['code'], 'readiness_incomplete_observation')

    def test_provider_failure_is_distinct_and_does_not_fallback_or_poll(self):
        runtime = Runtime([OperationError('provider failed', 'uia_property_read_failed')])
        result = self.run_ready(runtime)
        self.assertEqual(result['diagnostic'], {'code': 'readiness_read_error', 'cause': 'uia_property_read_failed'})
        self.assertEqual(len(runtime.calls), 1)

    def test_reader_unavailable_never_falls_back_to_driver(self):
        runtime=Runtime([NotImplementedError('not supported')])
        self.assertEqual(self.run_ready(runtime)['diagnostic']['code'], 'readiness_reader_unavailable')
        self.assertEqual(len(runtime.calls), 1)

    def test_cancel_is_not_swallowed_or_retried(self):
        runtime=Runtime([OperationError('cancelled', 'cancelled')])
        with self.assertRaises(OperationError) as caught: self.run_ready(runtime)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(len(runtime.calls), 1)
        self.assertTrue(runtime.probe.closed)
        self.assertEqual(runtime.guard.observed_targets, {(10, 30)})

    def test_window_replacement_between_read_and_result_stops(self):
        runtime=Runtime([body()])
        def replaced():
            runtime.probe.state['windows'][0]['thread_id'] += 1
            return body()
        runtime.observations=[replaced]
        with self.assertRaises(OperationError) as caught: self.run_ready(runtime)
        self.assertEqual(caught.exception.code, 'readiness_window_changed')
        self.assertTrue(runtime.probe.closed)

    def test_disappeared_window_is_distinct_from_missing_control(self):
        runtime=Runtime([body()]); runtime.probe.state['target_present']=False
        with self.assertRaises(OperationError) as caught: self.run_ready(runtime)
        self.assertEqual(caught.exception.code, 'readiness_window_unavailable')
        self.assertEqual(runtime.calls, [])

    def test_hidden_resident_is_never_read_or_treated_ready(self):
        runtime=Runtime([body()]); runtime.probe.state['windows'][0]['visible']=False
        result=self.run_ready(runtime, {**SPEC,'timeout_ms':0})
        self.assertEqual(result['status'], 'not_ready'); self.assertEqual(runtime.calls, [])
        self.assertEqual(result['checks'][0]['reason'], 'window_hidden')

    def test_window_hiding_during_read_cannot_pass(self):
        runtime=Runtime([body()])
        def hidden():
            runtime.probe.state['windows'][0]['visible']=False
            return body()
        runtime.observations=[hidden]
        self.assertEqual(self.run_ready(runtime)['diagnostic']['code'], 'readiness_window_not_ready')

    def test_timeout_retains_missing_reason_and_has_bounded_queries(self):
        runtime=Runtime([data()])
        result=self.run_ready(runtime, {**SPEC,'timeout_ms':1000})
        self.assertEqual(result['diagnostic']['code'], 'readiness_timeout')
        self.assertFalse(result['ready'])
        self.assertEqual(result['checks'][0]['reason'], 'control_missing')
        self.assertLessEqual(len(runtime.calls), 5)
        # Late deadline may be reached before a final read; no late ready result.
        self.assertLessEqual(result['metrics']['elapsed_ms'], 1000)

    def test_zero_timeout_is_one_bounded_read(self):
        runtime=Runtime([data(),body()])
        result=self.run_ready(runtime,{**SPEC,'timeout_ms':0})
        self.assertEqual(result['status'], 'not_ready'); self.assertEqual(len(runtime.calls),1)
        self.assertLessEqual(runtime.calls[0][2],2000)

    def test_late_read_cannot_claim_ready(self):
        runtime=Runtime([body()])
        def late(): self.clock.now+=2; return body()
        runtime.observations=[late]
        self.assertEqual(self.run_ready(runtime)['diagnostic']['code'],'readiness_timeout')

    def test_value_and_visible_require_actual_property_evidence(self):
        spec={'controls':[{'selector':QUERY,'value':'N','visible':True}],'timeout_ms':0}
        result=self.run_ready(Runtime([body(value='N',offscreen=False)]),spec)
        self.assertEqual(result['status'],'ready')
        result=self.run_ready(Runtime([body(value='N')]),spec)
        self.assertEqual(result['checks'][0]['reason'],'control_property_unavailable')
        result=self.run_ready(Runtime([body(value='P',offscreen=False)]),spec)
        self.assertEqual(result['checks'][0]['reason'],'control_value_mismatch')

    def test_protected_control_is_not_a_readiness_value(self):
        result=self.run_ready(Runtime([body(protected=True,value='private')]))
        self.assertEqual(result['checks'][0]['reason'],'control_protected')
        self.assertNotIn('private',str(result))

    def test_invalid_contract_and_unsupported_selectors_stop_before_read(self):
        cases=[{}, {'controls':[]}, {**SPEC,'timeout_ms':True}, {**SPEC,'timeout_ms':10001},
            {'controls':[{'selector':QUERY,'enabled':1}]}, {'controls':[{'selector':QUERY,'value':'x\0y'}]},
            {'controls':[{'selector':QUERY}]*9}, {'controls':[{'selector':QUERY,'click':True}]}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(OperationError): validate_ready_when(value)
        row={**QUERY,'row':{'key':{'name':'Device'},'property':'value','equals':'D1'}}
        with self.assertRaises(OperationError) as caught: validate_ready_when({'controls':[{'selector':row}]})
        self.assertEqual(caught.exception.code,'readiness_selector_unsupported')
        selector=READY_WHEN_SCHEMA['properties']['controls']['items']['properties']['selector']['properties']
        self.assertNotIn('row',selector); self.assertNotIn('label',selector)


if __name__ == '__main__': unittest.main()
