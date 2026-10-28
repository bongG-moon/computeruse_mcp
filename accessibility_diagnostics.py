"""Opt-in, local, read-only accessibility diagnostics with bounded native reads.

This inventory is evidence, never an input selector. Each backend is sampled
independently; matching counts cannot prove identical providers or screen state.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import subprocess
import time
import uuid

from operations import OperationError

HELPER = 'Computer Use MCP 접근성 진단.exe'
SOURCES = ('uia_raw', 'uia_control', 'uia_content', 'direct_msaa')
FORMAT = 'computer-accessibility-diagnostic/v1'


def _reject_constant(value):
    raise ValueError('Non-finite JSON value')


def _empty(source, status, reason, *, hresult=None):
    return {'source': source, 'status': status, 'complete': False, 'elements': [],
            'observed_count': None, 'diagnostic': {'code': reason, 'hresult': hresult}}


def _read_source(target, source, max_depth, max_elements, timeout, check=lambda: None):
    path = Path(__file__).with_name(HELPER)
    if not path.is_file():
        return _empty(source, 'unavailable', 'accessibility_helper_missing')
    # Native traversal's soft deadline must leave time for process startup,
    # JSON serialization and pipe delivery. A 300 ms reserve lost partial
    # observations in the measured 2 s source budget; reserve 40%, capped at
    # one second for longer reads. The external hard deadline stays unchanged.
    hard_budget_ms = max(1, int(timeout * 1000))
    reserve_ms = min(1000, max(100, int(hard_budget_ms * .4)))
    request = {**target, 'source': source, 'max_depth': max_depth, 'max_elements': max_elements,
               'time_budget_ms': max(1, hard_budget_ms - reserve_ms)}
    try:
        process = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        return _empty(source, 'unavailable', 'accessibility_helper_start_failed')
    try:
        deadline = time.monotonic() + timeout
        payload = json.dumps(request) + '\n'
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _empty(source, 'timeout', 'accessibility_provider_timeout')
            try:
                output, _ = process.communicate(payload, timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        if len(output) > 16 * 1024 * 1024:
            return _empty(source, 'error', 'accessibility_response_too_large')
        try:
            data = json.loads(output, parse_constant=_reject_constant)
        except (TypeError, ValueError):
            return _empty(source, 'error', 'accessibility_invalid_response')
        if (not isinstance(data, dict) or data.get('source') != source
                or data.get('pid') != target['pid'] or data.get('window_id') != target['window_id']
                or data.get('read_only') is not True or data.get('authorizes_input') is not False
                or data.get('status') not in {'ok', 'empty', 'partial', 'unavailable', 'error'}
                or type(data.get('complete')) is not bool or not isinstance(data.get('elements'), list)
                or len(data['elements']) > max_elements
                or any(not isinstance(row, dict) for row in data['elements'])):
            return _empty(source, 'error', 'accessibility_invalid_response')
        if process.returncode and data.get('status') not in {'error', 'unavailable'}:
            return _empty(source, 'error', 'accessibility_helper_failed')
        return data
    except subprocess.TimeoutExpired:
        return _empty(source, 'timeout', 'accessibility_provider_timeout')
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=.5)
        except subprocess.TimeoutExpired as error:
            raise OperationError('접근성 진단 도우미 종료를 확인하지 못했습니다.',
                                 'accessibility_cleanup_pending') from error


def diagnose(runtime, target, *, max_depth=12, max_elements=600, timeout=8):
    """Read an approved window without focus, images, mutations or model calls.

Cancellation and identity checks surround every independent backend. The total
native read allowance is bounded, and a stuck provider is killed with its helper.
    """
    if (not isinstance(target, dict) or set(target) != {'pid', 'window_id'}
            or any(type(value) is not int or value < 1 for value in target.values())):
        raise OperationError('현재 창의 PID와 창 참조가 필요합니다.', 'invalid_target')
    if (type(max_depth) is not int or not 1 <= max_depth <= 32
            or type(max_elements) is not int or not 1 <= max_elements <= 2000
            or type(timeout) not in (int, float) or not 1 <= timeout <= 30):
        raise OperationError('진단 깊이·요소 수·시간 제한을 확인하세요.', 'invalid_accessibility_diagnostic')
    from vendor.guard import validate_arguments
    from interaction import _identity
    from closing import ProbeError
    guard = runtime.guard
    call_id = uuid.uuid4().hex

    def log(kind, **fields):
        writer = getattr(guard, 'log', None)
        if callable(writer):
            writer(kind, request_id=call_id, tool='get_window_state',
                   route='accessibility_diagnostic', **fields)

    def authorize():
        runtime.check_active()
        stopper = getattr(guard, 'check_stop', None)
        if callable(stopper):
            stopper()
        validate_arguments('get_window_state', target, guard.policy,
                           guard.process_resolver, guard.window_resolver)

    authorize()
    log('accessibility_diagnostic_request', pid=target['pid'])
    probe = runtime.create_transition_probe(dict(target))
    started = time.monotonic()
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        before = _identity(probe.capture(), target['window_id'])
        sources = {}
        for source in SOURCES:
            authorize()
            if _identity(probe.snapshot(), target['window_id']) != before:
                raise OperationError('진단 중 창이 교체됐습니다. 다시 연결하세요.', 'accessibility_target_changed')
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                sources[source] = _empty(source, 'timeout', 'accessibility_total_timeout')
                continue
            source_started = time.monotonic()
            sources[source] = _read_source(target, source, max_depth, max_elements,
                min(remaining, timeout / len(SOURCES)), check=authorize)
            sources[source]['elapsed_ms'] = round((time.monotonic() - source_started) * 1000, 2)
            authorize()
            if _identity(probe.snapshot(), target['window_id']) != before:
                raise OperationError('진단 중 창이 교체됐습니다. 이전 결과를 사용하지 마세요.', 'accessibility_target_changed')
        log('accessibility_diagnostic_result', success=True,
            summary='Read-only inventory completed; no input authorization or screen data in this log.')
        return {'format': FORMAT, 'diagnostic_id': 'accessibility-' + uuid.uuid4().hex,
            'observed_at': observed_at, 'target': dict(target), 'read_only': True,
            'authorizes_input': False, 'input_dispatched': False, 'focus_dispatched': False,
            'backend': 'windows-accessibility-helper', 'scope': {'kind': 'approved_window_descendants',
                'owned_popups': 'not_traversed_use_their_separately_approved_window',
                'max_depth': max_depth, 'max_elements_per_source': max_elements},
            'identity_verified': True, 'screen_state': 'independent_sequential_samples',
            'complete': all(row['complete'] for row in sources.values()), 'sources': sources,
            'elapsed_ms': round((time.monotonic() - started) * 1000, 2),
            'limitations': ['Provider results are observations, not guaranteed control capabilities.',
                'Native handles, runtime IDs and child IDs expire; do not save them as selectors.',
                'Absent data in an incomplete source does not prove provider absence.']}
    except ProbeError as error:
        log('accessibility_diagnostic_failed', success=False, summary='Native window identity could not be verified.')
        raise OperationError('진단 대상 창의 수명을 확인하지 못했습니다.', 'accessibility_native_probe_failed') from error
    except BaseException:
        log('accessibility_diagnostic_failed', success=False, summary='Read-only inventory stopped; no input sent.')
        raise
    finally:
        probe.close()
