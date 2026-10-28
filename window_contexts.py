"""Small saved window contexts; launch policy and popup ownership stay separate.

No stored process IDs, HWNDs, runtime IDs or native observation leases. The one
MenuAgent title adapter removes the user suffix only from its observed format;
it never infers control identities or turns a launcher into a permitted app.
"""
from __future__ import annotations

import copy
import ntpath
import re

from business_windows import validate_window_selector
from operations import OperationError

MENU_AGENT = 'skhynix.smartfx.clientfx.menuagenthost.4.0.exe'
MENU_TITLE = re.compile(r'^(.{1,500}) - \[([^,\[\]\r\n]{1,100}), ([^\[\]\r\n]{1,100})\] - FxMenuAgent \(PNT\)$')


def validate_contexts(contexts, program_ids):
    if not isinstance(contexts, dict) or len(contexts) > 100 or set(contexts) - set(program_ids):
        raise OperationError('작업에 등록된 프로그램의 업무 창 조건만 저장하세요.', 'invalid_window_context')
    result = {}
    for program, value in contexts.items():
        if (not isinstance(value, dict) or 'window_selector' not in value
                or set(value) - {'window_selector', 'ready_when'}):
            raise OperationError('업무 창 선택 조건과 선택적인 준비 조건만 저장할 수 있습니다.', 'invalid_window_context')
        item = {'window_selector': validate_window_selector(value['window_selector'])}
        if 'ready_when' in value:
            from window_readiness import validate_ready_when
            item['ready_when'] = validate_ready_when(value['ready_when'])
        result[program] = item
    return copy.deepcopy(result)


def inferred_menuagent_context(runtime, target):
    """Authoring-only observed stable title; no inference for other host kinds."""
    from business_windows import select_business_window, BusinessWindowError
    from interaction import _identity
    guard = runtime.guard
    resolver = getattr(guard, 'target_executable', None)
    executable = resolver(target) if callable(resolver) else guard.process_resolver(target['pid'])
    if ntpath.basename(executable).casefold() != MENU_AGENT:
        return None
    probe = runtime.create_transition_probe(dict(target))
    try:
        first = probe.capture()
        identity = _identity(first, target['window_id'])
        if ntpath.basename(identity['executable']).casefold() != MENU_AGENT:
            raise OperationError('연결 확인 중 실행 프로그램이 바뀌었습니다.', 'business_window_process_changed')
        row = next((item for item in first['windows'] if item.get('window_id') == target['window_id']), {})
        match = MENU_TITLE.fullmatch(row.get('title', ''))
        if not match:
            return None
        # The delimiter includes the observed system and stops before the user.
        selector = {'title_prefix': f'{match[1]} - [{match[2]}, ', 'class_name': row['class_name']}
        try:
            selected = select_business_window(first['windows'], selector, pid=target['pid'])
        except BusinessWindowError as error:
            if error.code == 'business_window_ambiguous':
                return None  # Keep explicit current binding; do not save an ambiguous rule.
            raise
        if not selected or selected['window_id'] != target['window_id']:
            return None
        runtime.check_active()
        after = probe.snapshot()
        if _identity(after, target['window_id']) != identity:
            raise OperationError('업무 창 조건을 확인하던 창이 바뀌었습니다.', 'business_window_identity_changed')
        latest = select_business_window(after['windows'], selector, pid=target['pid'])
        if not latest or latest['window_id'] != target['window_id']:
            raise OperationError('업무 창 제목이나 후보가 바뀌었습니다.', 'business_window_identity_changed')
        return {'window_selector': validate_window_selector(selector)}
    finally:
        probe.close()
