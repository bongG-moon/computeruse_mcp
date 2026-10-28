"""Explicit selection of independent work windows, not owned popup recovery.

This module only filters freshly observed native metadata. A selector is durable
context; PID/HWND/thread are current evidence and never selector fields. The
caller retains and revalidates the approved process/window around observation
and input. No framework or executable name implies a particular host topology.
"""
from __future__ import annotations

import copy
import re


WINDOW_SELECTOR_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'title': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'title_prefix': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
            'description': 'Explicit stable observed title prefix, without user/session suffix. No wildcard matching.'},
        'class_name': {'type': 'string', 'minLength': 1, 'maxLength': 256},
    },
    'oneOf': [{'required': ['title']}, {'required': ['title_prefix']}],
    'description': 'Unique visible independent top-level business window. Use exactly one title condition. Owned popups use their separate owner-bound path.',
}

_WINFORMS = re.compile(r'(WindowsForms10\.[A-Za-z0-9_]+\.[0-9a-fA-F]+)\.app\.0\.[0-9a-fA-F]+_r[0-9]+_ad[0-9]+\Z')
_IDENTITY = ('pid', 'window_id', 'class_name', 'thread_id', 'owner_window_id', 'root_owner_window_id')


class BusinessWindowError(ValueError):
    def __init__(self, message, code='invalid_business_window_selector', *, candidates=None):
        super().__init__(message)
        self.code = code
        self.candidates = copy.deepcopy(candidates or [])


def validate_window_selector(value):
    if (not isinstance(value, dict) or set(value) - {'title', 'title_prefix', 'class_name'}
            or ('title' in value) == ('title_prefix' in value)):
        raise BusinessWindowError('업무 창은 정확한 title 또는 안정적인 title_prefix 중 하나로 지정하세요.')
    for key, text in value.items():
        if (not isinstance(text, str) or not text.strip() or len(text) > (256 if key == 'class_name' else 1000)
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise BusinessWindowError('업무 창의 제목 조건과 창 종류를 확인하세요.')
    return copy.deepcopy(value)


def class_matches(expected, actual):
    if expected == actual:
        return True
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    left, right = _WINFORMS.fullmatch(expected), _WINFORMS.fullmatch(actual)
    return bool(left and right and left[1] == right[1])


def window_identity(row):
    """Transient identity evidence, never a reusable selector or permission."""
    if not isinstance(row, dict):
        return None
    return tuple(row.get(key) for key in _IDENTITY)


def business_window_candidates(rows, selector, *, pid=None):
    selector = validate_window_selector(selector)
    if not isinstance(rows, list) or len(rows) > 2000 or (pid is not None and (type(pid) is not int or pid < 1)):
        raise BusinessWindowError('현재 업무 창의 관찰 범위를 확인하세요.', 'business_window_inventory_invalid')
    matches, rejected, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise BusinessWindowError('창 관찰 정보가 불완전합니다.', 'business_window_inventory_invalid')
        title = row.get('title')
        if not isinstance(title, str):
            continue
        title_matches = title == selector['title'] if 'title' in selector else title.startswith(selector['title_prefix'])
        if not title_matches or (pid is not None and row.get('pid') != pid):
            continue
        if 'class_name' in selector and not class_matches(selector['class_name'], row.get('class_name')):
            continue
        reason = None
        if row.get('visible') is not True:
            reason = 'hidden_or_visibility_unknown'
        elif row.get('minimized') is not False:
            reason = 'minimized_or_state_unknown'
        elif (any(type(row.get(key)) is not int or row[key] < 1 for key in ('pid', 'window_id', 'thread_id'))
                or not isinstance(row.get('class_name'), str) or not row['class_name']
                or type(row.get('owner_window_id')) is not int
                or type(row.get('root_owner_window_id')) is not int):
            # Missing identity in a matching visible candidate must not make
            # another candidate appear unique by silently filtering this one.
            raise BusinessWindowError('일치하는 업무 창의 수명·종류·스레드 근거가 부족합니다.',
                                      'business_window_identity_unavailable')
        elif row['owner_window_id'] != 0:
            reason = 'owned_popup_requires_owner_binding'
        elif row['root_owner_window_id'] != row['window_id'] or row.get('is_top_level') is False:
            reason = 'nested_child_not_business_window'
        elif row['class_name'] in {'IME', 'MSCTFIME UI', 'ComboLBox', '#32768'}:
            reason = 'transient_system_window'
        if reason:
            rejected.append({'pid': row.get('pid'), 'window_id': row.get('window_id'), 'reason': reason})
            continue
        key = (row['pid'], row['window_id'])
        if key in seen:
            raise BusinessWindowError('같은 창 식별자가 중복 관찰됐습니다. 새 목록을 확인하세요.',
                                      'business_window_inventory_invalid')
        seen.add(key)
        matches.append(copy.deepcopy(row))
    return {'matches': matches, 'rejected': rejected}


def select_business_window(rows, selector, *, pid=None):
    result = business_window_candidates(rows, selector, pid=pid)
    if len(result['matches']) > 1:
        raise BusinessWindowError('업무 창이 여러 개 일치합니다. 후보 창을 선택하거나 제목 문맥을 더 지정하세요.',
            'business_window_ambiguous', candidates=result['matches'][:20])
    return result['matches'][0] if result['matches'] else None


def discover_business_windows(runtime, discovery_rows, selector, *, explicit_target=None):
    """Recheck approved discovery metadata without a Driver/tree/focus request.

    One retained native process probe is used per already-discovered PID. Only
    windows from that authorized discovery list can bind. A matching newly
    appearing window makes discovery stale instead of silently broadening scope.
    All probes remain retained through the final identity/uniqueness check.
    A caller-selected exact target may bypass global uniqueness, but must be
    present in the approved discovery inventory and satisfy the same selector,
    independent-window and retained process/window lifetime checks.
    """
    from vendor.guard import validate_arguments

    selector = validate_window_selector(selector)
    if not isinstance(discovery_rows, list) or len(discovery_rows) > 2000:
        raise BusinessWindowError('현재 창 목록을 다시 확인하세요.', 'business_window_inventory_invalid')
    grouped, allowed = {}, set()
    for row in discovery_rows:
        if (not isinstance(row, dict)
                or any(type(row.get(key)) is not int or row[key] < 1 for key in ('pid', 'window_id'))):
            raise BusinessWindowError('현재 창 목록의 식별 정보를 확인하세요.', 'business_window_inventory_invalid')
        key = (row['pid'], row['window_id'])
        if key in allowed:
            raise BusinessWindowError('창 목록에 중복 식별자가 있습니다.', 'business_window_inventory_invalid')
        allowed.add(key)
        grouped.setdefault(row['pid'], []).append(row)
    explicit_key = None
    if explicit_target is not None:
        if (not isinstance(explicit_target, dict)
                or any(type(explicit_target.get(key)) is not int or explicit_target[key] < 1
                       for key in ('pid', 'window_id'))):
            raise BusinessWindowError('명시적으로 선택한 업무 창의 식별 정보를 확인하세요.',
                                      'business_window_inventory_invalid')
        explicit_key = tuple(explicit_target[key] for key in ('pid', 'window_id'))
        if explicit_key not in allowed:
            raise BusinessWindowError('선택한 업무 창이 승인된 현재 창 목록에 없습니다.',
                                      'business_window_inventory_changed')
        grouped = {explicit_key[0]: [row for row in grouped[explicit_key[0]]
                                    if row['window_id'] == explicit_key[1]]}
    guard = runtime.guard
    probes = []

    def authorize(target):
        runtime.check_active()
        checker = getattr(guard, 'check_stop', None)
        if callable(checker): checker()
        validate_arguments('get_window_state', target, guard.policy,
                           guard.process_resolver, guard.window_resolver)

    def process_identity(snapshot):
        if (not isinstance(snapshot, dict) or snapshot.get('process_exited') is not False
                or snapshot.get('target_present') is not True
                or not snapshot.get('creation_time') or not isinstance(snapshot.get('executable'), str)
                or not snapshot['executable'] or not isinstance(snapshot.get('windows'), list)
                or snapshot.get('windows_truncated') is True or snapshot.get('truncated') is True):
            raise BusinessWindowError('업무 프로세스 또는 창 목록의 수명을 확인하지 못했습니다.',
                                      'business_window_identity_unavailable')
        return snapshot['creation_time'], snapshot['executable']

    def candidates(snapshot, pid):
        rows = snapshot['windows']
        if explicit_key is not None:
            rows = [row for row in rows if isinstance(row, dict)
                    and (row.get('pid'), row.get('window_id')) == explicit_key]
        result = business_window_candidates(rows, selector, pid=pid)['matches']
        if any((row['pid'], row['window_id']) not in allowed for row in result):
            raise BusinessWindowError('업무 창 목록이 바뀌었습니다. 현재 후보를 다시 확인하세요.',
                                      'business_window_inventory_changed')
        return result

    found = []
    try:
        for pid, listed in grouped.items():
            seed = {key: listed[0][key] for key in ('pid', 'window_id')}
            authorize(seed)
            probe = runtime.create_transition_probe(seed)
            probes.append((probe, seed, None, None, None))
            snapshot = probe.capture()
            identity = process_identity(snapshot)
            seed_row = next((row for row in snapshot['windows']
                             if row.get('window_id') == seed['window_id'] and row.get('pid') == pid), None)
            if seed_row is None:
                raise BusinessWindowError('처음 관찰한 창을 다시 확인하지 못했습니다.', 'business_window_identity_unavailable')
            current = candidates(snapshot, pid)
            for row in current:
                authorize({key: row[key] for key in ('pid', 'window_id')})
            probes[-1] = (probe, seed, identity, window_identity(seed_row), current)
            # Keep selection and later engine.bind tied to the same process
            # lifetime. These fields are fresh evidence, never durable context.
            found.extend({**row, 'creation_time': identity[0], 'executable': identity[1]}
                         for row in current)
        for probe, seed, identity, seed_identity, previous in probes:
            authorize(seed)
            snapshot = probe.snapshot()
            if process_identity(snapshot) != identity:
                raise BusinessWindowError('업무 프로세스가 교체됐습니다. 새로 연결하세요.', 'business_window_process_changed')
            seed_row = next((row for row in snapshot['windows']
                             if row.get('window_id') == seed['window_id'] and row.get('pid') == seed['pid']), None)
            current = candidates(snapshot, seed['pid'])
            if (window_identity(seed_row) != seed_identity
                    or {window_identity(row) for row in current} != {window_identity(row) for row in previous}):
                raise BusinessWindowError('업무 창이 닫히거나 교체됐습니다. 현재 후보를 다시 연결하세요.',
                                          'business_window_inventory_changed')
            for row in current:
                authorize({key: row[key] for key in ('pid', 'window_id')})
        return select_business_window(found, selector)
    finally:
        for probe, *_ in probes:
            probe.close()
