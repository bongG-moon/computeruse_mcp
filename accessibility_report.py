"""Explicit, local accessibility comparison. Never a source of input handles."""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
from pathlib import Path
import time
import uuid

from operations import OperationError


def collect(runtime, target):
    """Compare independent readers without activating or changing the UI.

    These are sequential observations, not an atomic snapshot. Provider absence
    cannot be inferred from missing Driver metadata or from a partial traversal.
    """
    import accessibility_diagnostics
    from accessibility_diagnostics import diagnose
    from inspection import inspect_window
    from interaction import _identity
    from consent import _plain_chain
    from vendor.guard import validate_arguments

    runtime.check_active()
    guard = runtime.guard
    validate_arguments('get_window_state', target, guard.policy,
                       guard.process_resolver, guard.window_resolver)
    probe = runtime.create_transition_probe(dict(target))
    started = time.monotonic()
    try:
        original = _identity(probe.capture(), target['window_id'])
        sources = {}
        application_strategy = None
        timings = {}
        intervals = {}
        for name, read in (
            ('provider', lambda: diagnose(runtime, target, max_depth=12, max_elements=600, timeout=8)),
            ('driver', lambda: inspect_window(runtime, target, observation='uia', diagnostics=True,
                                              max_controls=200, max_depth=12, max_elements=600,
                                              host_executable=original['executable'])),
        ):
            runtime.check_active()
            if _identity(probe.snapshot(), target['window_id']) != original:
                raise OperationError('진단 중 대상 창이 바뀌었습니다. 입력하지 않았습니다.', 'diagnostic_target_changed')
            tick = time.monotonic()
            intervals[name] = {'started_at': datetime.now(timezone.utc).isoformat()}
            try:
                value = read()
                if name == 'driver':
                    data = value.get('structuredContent', {})
                    application_strategy = data.get('inspection', {}).get('application_strategy')
                    value = data.get('inspection', {}).get('diagnostics', data.get('accessibility_diagnostics')) or {
                        'status': 'observation_error', 'diagnostic': data.get('diagnostic', {}),
                        'completeness': 'unknown', 'count': None}
                sources[name] = value
            except OperationError as error:
                runtime.check_active()
                if error.code not in {'accessibility_helper_missing', 'accessibility_helper_start_failed',
                                      'accessibility_provider_timeout', 'driver_timeout', 'observation_timeout'}:
                    raise
                sources[name] = {'status': 'timeout' if 'timeout' in error.code else 'observation_error',
                                 'diagnostic': {'code': error.code, 'message': str(error)},
                                 'completeness': 'unknown', 'count': None}
            timings[name + '_ms'] = round((time.monotonic() - tick) * 1000, 2)
            intervals[name]['finished_at'] = datetime.now(timezone.utc).isoformat()
        runtime.check_active()
        validate_arguments('get_window_state', target, guard.policy,
                           guard.process_resolver, guard.window_resolver)
        if _identity(probe.snapshot(), target['window_id']) != original:
            raise OperationError('진단 중 대상 창이 바뀌었습니다. 입력하지 않았습니다.', 'diagnostic_target_changed')
    finally:
        # The ordinary Driver reader grants a one-action observation lease.
        # Diagnostic inventory is never an action observation: discard this
        # exact window's lease even when a later reader/identity check fails.
        # Do not restore a pre-diagnostic lease or affect unrelated windows.
        observed = getattr(guard, 'observed_targets', None)
        if observed is not None:
            observed.discard((target['pid'], target['window_id']))
        frame = getattr(guard, '_native_popup_frame', None)
        if frame is not None and frame[:2] == (target['pid'], target['window_id']):
            guard._native_popup_frame = None
        probe.close()

    identifier = uuid.uuid4().hex
    report = {'format': 'computer-accessibility-comparison/v1', 'diagnostic_id': identifier,
              'observed_at': datetime.now(timezone.utc).isoformat(), 'target': copy.deepcopy(target),
              'read_only': True, 'authorizes_input': False, 'input_dispatched': False, 'task_verified': False,
              'scope': {'max_depth': 12, 'max_elements_per_view': 600, 'target_only': True},
              'comparison': {'atomic': False, 'window_identity_unchanged': True,
                             'provider_driver_equivalence': 'not_assumed',
                             'missing_provider_children_inferred': False,
                             'reason': 'Independent sequential reads; counts are not equivalent across views.'},
              'sources': sources, 'source_intervals': intervals,
              'timings': {**timings, 'total_ms': round((time.monotonic()-started)*1000, 2)},
              'next_steps': [
                  'Provider Raw/Control/Content와 직접 MSAA를 각각 비교하세요. partial/timeout은 0개와 다릅니다.',
                  '이름 없는 요소의 MCP 제외 사유와 원본 메타데이터를 확인하세요. 정적 필드명은 AutomationId가 아닙니다.',
                  '콤보 펼침·동적 편집기·하위 메뉴가 바뀐 뒤 새로 진단하세요. HWND/RuntimeId/인덱스는 저장 선택자가 아닙니다.',
                  '진단 노드와 추정 라벨로 입력하지 않습니다. 실제 Driver 능력과 유일한 새 관찰이 필요합니다.'
              ]}
    if application_strategy is not None:
        report['application_strategy'] = copy.deepcopy(application_strategy)
    helper = Path(accessibility_diagnostics.__file__).with_name(accessibility_diagnostics.HELPER)
    report['build'] = {'helper_sha256': hashlib.sha256(helper.read_bytes()).hexdigest() if helper.is_file() else None,
                       'helper_file_present': helper.is_file(), 'source': 'local_helper_file_after_observation',
                       'mcp_session_code': copy.deepcopy(getattr(runtime, 'code_identity', None))}
    # Serialize before creating a file; invalid provider numbers must not leave
    # a truncated JSON report that appears to be a completed diagnostic.
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    root = Path(runtime.run_dir)
    if not root.is_dir() or not _plain_chain(root):
        raise OperationError('로컬 진단 저장 위치를 확인하지 못했습니다.', 'diagnostic_storage_unavailable')
    folder = root / 'accessibility-diagnostics'
    folder.mkdir(exist_ok=True)
    if not _plain_chain(folder):
        raise OperationError('연결된 경로에 진단을 저장하지 않습니다.', 'diagnostic_storage_unavailable')
    path = folder / (identifier + '.json')
    with path.open('x', encoding='utf-8') as stream:
        stream.write(encoded)
    return {'status': 'ok', 'diagnostic_id': identifier, 'read_only': True,
            'input_dispatched': False, 'task_verified': False, 'diagnostic_report': report,
            'report_path': str(path),
            'message': '요소 진단을 이 PC에 저장했습니다. 원본/Driver/MCP 결과를 구분하며 입력은 하지 않았습니다.'}
