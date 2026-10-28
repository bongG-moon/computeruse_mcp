"""Opt-in readiness of an already bound window; never activates or authorizes input.

Only requested current properties are read. The existing scoped helper may walk
window identity metadata to resolve them, but no Driver tree or screenshot is
collected and no values are cached between requests.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
import math
import time

from operations import OperationError, SELECTOR_SCHEMA, _elements, _payload, _unique, validate_selector


READY_WHEN_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['controls'],
    'properties': {
        'controls': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['selector'],
            'properties': {'selector': copy.deepcopy(SELECTOR_SCHEMA),
                'enabled': {'type': 'boolean'}, 'visible': {'type': 'boolean'},
                'value': {'type': 'string', 'maxLength': 16000}}}},
        'timeout_ms': {'type': 'integer', 'minimum': 0, 'maximum': 10000,
            'description': 'Default 2000ms total. Zero performs one scoped read with a 2000ms ceiling.'}},
    'description': 'Explicit read-only preparation, not business completion. Require only observed controls needed for the next action, not every toolbar. Uses exact name/automation_id and optional within; row/label readiness requires a separate fresh observation.'}
# The compact native property reader cannot resolve label or row semantics.
READY_WHEN_SCHEMA['properties']['controls']['items']['properties']['selector']['properties'].pop('row')
READY_WHEN_SCHEMA['properties']['controls']['items']['properties']['selector']['properties'].pop('label')


def validate_ready_when(spec):
    if (not isinstance(spec, dict) or set(spec) - {'controls', 'timeout_ms'}
            or not isinstance(spec.get('controls'), list) or not 1 <= len(spec['controls']) <= 8
            or type(spec.get('timeout_ms', 2000)) is not int or not 0 <= spec.get('timeout_ms', 2000) <= 10000):
        raise OperationError('준비 조건은 controls 1~8개와 timeout_ms 0~10000으로 지정하세요.', 'invalid_readiness')
    for condition in spec['controls']:
        if (not isinstance(condition, dict) or 'selector' not in condition
                or set(condition) - {'selector', 'enabled', 'visible', 'value'}):
            raise OperationError('준비 조건에는 selector와 enabled/visible/value만 사용할 수 있습니다.', 'invalid_readiness')
        selector = validate_selector(condition['selector'])
        if {'label', 'row'} & set(selector):
            raise OperationError('행 키와 라벨 관계는 준비 대기 전에 새 화면 관찰로 확인하세요.', 'readiness_selector_unsupported')
        if any(type(condition[key]) is not bool for key in ('enabled', 'visible') if key in condition):
            raise OperationError('enabled와 visible은 true/false여야 합니다.', 'invalid_readiness')
        if 'value' in condition and (not isinstance(condition['value'], str)
                or len(condition['value']) > 16000 or '\0' in condition['value']):
            raise OperationError('준비 조건 value는 16000자 이하 문자열이어야 합니다.', 'invalid_readiness')
    return copy.deepcopy(spec)


def _checks(data, conditions):
    checks = []
    for index, condition in enumerate(conditions):
        try:
            element = _unique(data, condition['selector'])
        except OperationError as error:
            if error.code not in {'selector_not_found', 'ambiguous_selector', 'ambiguous_scope'}:
                raise
            checks.append({'control': index, 'ready': False, 'reason': {
                'selector_not_found': 'control_missing', 'ambiguous_selector': 'control_ambiguous',
                'ambiguous_scope': 'scope_ambiguous'}[error.code]})
            continue
        reason = None
        if any(element.get(key) is True for key in ('protected', 'is_password', 'is_protected')):
            reason = 'control_protected'
        for field in ('enabled', 'visible', 'value'):
            if reason or field not in condition:
                continue
            observed = element.get(field)
            if field == 'visible' and type(observed) is not bool:
                offscreen = element.get('offscreen', element.get('is_offscreen'))
                observed = not offscreen if type(offscreen) is bool else None
            if observed is None or field in {'enabled', 'visible'} and type(observed) is not bool:
                reason = 'control_property_unavailable'
            elif observed != condition[field]:
                reason = {'enabled': 'control_disabled' if condition[field] else 'control_enabled',
                          'visible': 'control_hidden' if condition[field] else 'control_visible',
                          'value': 'control_value_mismatch'}[field]
        checks.append({'control': index, 'ready': reason is None, 'reason': reason or 'ready'})
    return checks


def wait_ready(runtime, target, spec, *, clock=time.monotonic, wait=None):
    """Wait on explicit prerequisites of this exact HWND, without switching windows.

    Scope/permission/cancellation/lifetime errors propagate. Missing or disabled
    controls can become ready; incomplete or erroneous reads never pass and do
    not trigger a full-tree fallback. ``task_verified`` remains false even when
    preparation is ready; this function did not perform the user's task.
    """
    from interaction import _identity
    from closing import ProbeError
    from vendor.guard import validate_arguments

    spec = validate_ready_when(spec)
    if (not isinstance(target, dict) or set(target) != {'pid', 'window_id'}
            or any(type(value) is not int or value <= 0 for value in target.values())):
        raise OperationError('준비 상태는 현재 연결된 정확한 창에서 확인하세요.', 'invalid_target')
    guard = runtime.guard
    started = clock()
    one_read = spec.get('timeout_ms', 2000) == 0
    deadline = started + (2000 if one_read else spec.get('timeout_ms', 2000)) / 1000
    metrics = {'scoped_reads': 0, 'native_identity_reads': 0, 'driver_full_tree_reads': 0,
               'scope_lookup_metadata_reads': 0, 'observation_ms': 0.0}
    sleeper = wait or getattr(getattr(runtime, 'stop_event', None), 'wait', None) or time.sleep
    observer = getattr(runtime, 'observe_completion_controls', None)
    probe = None

    def check():
        runtime.check_active()
        stop = getattr(guard, 'check_stop', None)
        if callable(stop):
            stop()
        validate_arguments('get_window_state', target, guard.policy,
                           guard.process_resolver, guard.window_resolver)

    def identity(state):
        try:
            return _identity(state, target['window_id'])
        except OperationError as error:
            raise OperationError('준비 대상 창이 없어졌거나 식별 정보를 읽지 못했습니다.',
                                 'readiness_window_unavailable') from error

    def window_reason(state):
        row = next(item for item in state['windows'] if item.get('window_id') == target['window_id'])
        return ('window_visibility_unknown' if type(row.get('visible')) is not bool else
            'window_hidden' if not row['visible'] else
            'window_minimized' if row.get('minimized') is True else
            'window_disabled' if row.get('enabled') is False else None)

    def result(code, checks=None, *, underlying=None):
        return {'format': 'computer-window-readiness/v1', 'status': 'ready' if code == 'ready' else 'not_ready',
            'ready': code == 'ready',
            'read_only': True, 'authorizes_input': False, 'input_dispatched': False, 'focus_dispatched': False,
            'task_verified': False, 'checks': checks or [],
            'diagnostic': {'code': 'readiness_' + code, **({'cause': underlying} if underlying else {})},
            'metrics': {**metrics, 'elapsed_ms': round((clock() - started) * 1000, 2)}}

    try:
        with getattr(runtime, 'execution_lock', None) or nullcontext():
            check()
            if not callable(observer):
                return result('reader_unavailable')
            probe = runtime.create_transition_probe(dict(target))
            initial_state = probe.capture(); metrics['native_identity_reads'] += 1
            initial = identity(initial_state)
            if initial.get('pid') != target['pid']:
                raise OperationError('준비 대상의 프로세스가 바뀌었습니다.', 'readiness_window_changed')
            attempts = 0
            last_checks = []
            while True:
                check()
                state = probe.snapshot(); metrics['native_identity_reads'] += 1
                if identity(state) != initial:
                    raise OperationError('준비 중 업무 창이 교체됐습니다. 새 창으로 연결하세요.', 'readiness_window_changed')
                unavailable = window_reason(state)
                checks = [{'ready': False, 'reason': unavailable}] if unavailable else last_checks
                if not unavailable:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        return result('timeout', checks)
                    tick = clock()
                    metrics['scoped_reads'] += 1
                    metrics['scope_lookup_metadata_reads'] += 1
                    try:
                        answer = observer(dict(target), [copy.deepcopy(c['selector']) for c in spec['controls']],
                                          timeout_ms=max(1, math.floor(remaining * 1000)))
                    except NotImplementedError:
                        return result('reader_unavailable')
                    except OperationError as error:
                        if error.code not in {'ambiguous_selector', 'ambiguous_scope', 'selector_not_found',
                            'scope_lookup_limit', 'scoped_observation_timeout', 'scoped_read_failed',
                            'uia_property_read_failed', 'scoped_helper_exited', 'scoped_response_invalid'}:
                            raise
                        return result('read_error', underlying=error.code)
                    finally:
                        metrics['observation_ms'] += round((clock() - tick) * 1000, 2)
                    check()
                    after = probe.snapshot(); metrics['native_identity_reads'] += 1
                    if identity(after) != initial:
                        raise OperationError('준비 관찰 중 업무 창이 교체됐습니다.', 'readiness_window_changed')
                    if window_reason(after):
                        return result('window_not_ready', [{'ready': False, 'reason': window_reason(after)}])
                    data = _payload(answer)
                    elements = _elements(data)
                    if (answer.get('isError') or any(data.get(key) != value for key, value in target.items())
                            or data.get('read_only') is not True or data.get('scoped_observation') is not True
                            or data.get('scope_complete') is not True
                            or any(data.get(key) is True for key in ('truncated', 'max_elements_reached', 'max_depth_reached'))
                            or len(elements) > 80 or any(e.get('verification_only') is not True
                                or any(key in e for key in ('element_token', 'snapshot_id', 'actions')) for e in elements)):
                        return result('incomplete_observation')
                    checks = _checks(data, spec['controls'])
                    last_checks = checks
                    if clock() > deadline:
                        return result('timeout', checks)
                    if all(item['ready'] for item in checks):
                        return result('ready', checks)
                    if any(item['reason'] in {'control_ambiguous', 'scope_ambiguous',
                                             'control_property_unavailable', 'control_protected'} for item in checks):
                        return result('controls_unresolved', checks)
                if one_read:
                    return result('not_ready', checks)
                remaining = deadline - clock()
                if remaining <= 0:
                    return result('timeout', checks)
                sleeper(min(remaining, .1 * 2 ** min(attempts, 3)))
                attempts += 1
    except ProbeError as error:
        raise OperationError('준비 상태를 확인할 창의 수명을 읽지 못했습니다.', 'readiness_window_unavailable') from error
    finally:
        observed = getattr(guard, 'observed_targets', None)
        if observed is not None:
            observed.discard((target['pid'], target['window_id']))
        frame = getattr(guard, '_native_popup_frame', None)
        if frame is not None and frame[:2] == (target['pid'], target['window_id']):
            guard._native_popup_frame = None
        if probe is not None:
            probe.close()
