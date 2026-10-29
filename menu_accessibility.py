"""Bounded, local menu evidence and one-shot native actions.

This module is an internal adapter, not an authorization boundary. Callers must
run the existing Guard approval, budget, lifetime and stop checks around act().
Observed locators are short-lived provider evidence, never Driver handles or
durable workflow selectors. UIA and direct MSAA are sampled independently.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import time

from operations import OperationError

HELPER = 'Computer Use MCP 메뉴 접근성.exe'
FORMAT = 'computer-menu-accessibility/v1'
SOURCES = ('uia_raw', 'direct_msaa')
EVIDENCE_LIFETIME_MS = 10000


def _invalid(code='menu_accessibility_invalid_request'):
    return OperationError('메뉴 접근성 대상 또는 관찰 증거를 확인하세요.', code)


def _target(target):
    if (not isinstance(target, dict) or set(target) != {'pid', 'window_id'}
            or any(type(value) is not int or value <= 0 for value in target.values())):
        raise _invalid()


def _limits(max_depth, max_elements, timeout):
    if (type(max_depth) is not int or not 1 <= max_depth <= 32
            or type(max_elements) is not int or not 1 <= max_elements <= 2000
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not .2 <= timeout <= 30):
        raise _invalid()


def _empty(source, code, status='error'):
    return {'source': source, 'status': status, 'complete': False, 'truncated': False,
            'elements': [], 'provider_errors': [], 'diagnostic': {'code': code},
            'read_only': True, 'authorizes_input': False}


def _reject_constant(_):
    raise ValueError('nonfinite JSON')


def _native(request, timeout, check):
    """Exactly one helper process; cancellation never retries a possible input."""
    check()
    path = Path(__file__).with_name(HELPER)
    if not path.is_file():
        raise _invalid('menu_accessibility_helper_missing')
    request = {**request, 'time_budget_ms': max(1, int(timeout * 700))}
    try:
        process = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as error:
        raise _invalid('menu_accessibility_helper_start_failed') from error
    try:
        deadline = time.monotonic() + timeout
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False) + '\n'
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _invalid('menu_accessibility_timeout')
            try:
                output, _ = process.communicate(payload, timeout=min(.05, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        check()
        if len(output) > 8 * 1024 * 1024:
            raise _invalid('menu_accessibility_invalid_response')
        try:
            data = json.loads(output, parse_constant=_reject_constant)
        except (TypeError, ValueError) as error:
            raise _invalid('menu_accessibility_invalid_response') from error
        if (not isinstance(data, dict) or data.get('source') != request['source']
                or any(data.get(key) != request[key] for key in ('pid', 'window_id'))
                or data.get('authorizes_input') is not False):
            raise _invalid('menu_accessibility_invalid_response')
        if process.returncode and data.get('status') != 'error':
            raise _invalid('menu_accessibility_helper_failed')
        return data
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=.5)
        except subprocess.TimeoutExpired as error:
            raise _invalid('menu_accessibility_cleanup_pending') from error


def _validate_source(data, max_elements):
    if (data.get('status') not in {'ok', 'empty', 'partial', 'error', 'unavailable'}
            or type(data.get('complete')) is not bool or type(data.get('truncated')) is not bool
            or data.get('read_only') is not True or not isinstance(data.get('elements'), list)
            or len(data['elements']) > max_elements):
        raise _invalid('menu_accessibility_invalid_response')
    rows = data['elements']
    for index, row in enumerate(rows):
        if (not isinstance(row, dict) or row.get('element_index') != index
                or type(row.get('parent_index')) is not int or not -1 <= row['parent_index'] < index
                or row.get('verification_only') is not True or row.get('authorizes_input') is not False
                or row.get('identity_lifetime') != 'current_menu_observation_only'
                or not isinstance(row.get('name'), (str, type(None)))
                or not isinstance(row.get('role'), str)
                or (row.get('enabled') is not None and type(row.get('enabled')) is not bool)
                or (row.get('visible') is not None and type(row.get('visible')) is not bool)
                or not isinstance(row.get('locator'), dict) or not isinstance(row.get('expected'), dict)
                or not isinstance(row.get('supported_actions'), list)
                or any(value not in {'expand', 'invoke', 'hover', 'click_parent', 'click_leaf'} for value in row['supported_actions'])
                or any(key in row for key in ('element_token', 'snapshot_id', 'actions', 'handle'))):
            raise _invalid('menu_accessibility_invalid_response')
    if data['complete'] and (data['status'] in {'partial', 'error', 'unavailable'} or data['truncated']
                             or data.get('provider_errors')):
        raise _invalid('menu_accessibility_invalid_response')


def _normalize_source(data):
    """Normalize presentation fields without rewriting native evidence."""
    states = {'Collapsed': 'collapsed', 'Expanded': 'expanded',
              'PartiallyExpanded': 'partially_expanded', 'LeafNode': 'leaf_node'}
    for row in data['elements']:
        bounds = row.get('bounds')
        if isinstance(bounds, dict):
            row['coordinate_space'] = bounds.get('coordinate_space')
            row['bounds'] = {key: bounds.get(key) for key in ('x', 'y', 'width', 'height')}
        row['expansion_state'] = states.get(row.get('expansion_state'), row.get('expansion_state'))
        menu_children = row.get('has_children')
        row['provider_has_children'] = row.get('provider_has_children', menu_children)
        row['has_children'] = (True if menu_children is True or row.get('has_popup') is True
                               else False if menu_children is False and row.get('has_popup') is False else None)
    return data


def observe(target, *, max_depth=12, max_elements=500, timeout=6, check=lambda: None):
    """Read only this HWND's provider subtree. Owned popups need their own target."""
    _target(target)
    _limits(max_depth, max_elements, timeout)
    started = time.monotonic()
    sources = {}
    for source in SOURCES:
        check()
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            sources[source] = _empty(source, 'menu_accessibility_timeout', 'timeout')
            continue
        try:
            data = _native({**target, 'source': source, 'operation': 'observe',
                            'max_depth': max_depth, 'max_elements': max_elements},
                           min(remaining, timeout / len(SOURCES)), check)
            _validate_source(data, max_elements)
            sources[source] = _normalize_source(data)
        except OperationError as error:
            # Guard stop/lifetime/permission errors must propagate, never become
            # a provider absence that could trigger another backend's action.
            if not error.code.startswith('menu_accessibility_') or error.code.endswith('cleanup_pending'):
                raise
            status = 'timeout' if error.code.endswith('timeout') else 'unavailable' if error.code.endswith(('missing', 'start_failed')) else 'error'
            sources[source] = _empty(source, error.code, status)
    check()
    return {'format': FORMAT, 'target': dict(target), 'read_only': True,
            'authorizes_input': False, 'input_dispatched': False,
            'sources': sources, 'complete': all(value['complete'] for value in sources.values()),
            'scope': 'requested_window_descendants_only', 'sample_order': list(SOURCES),
            'evidence_lifetime_ms': EVIDENCE_LIFETIME_MS,
            'elapsed_ms': round((time.monotonic() - started) * 1000, 2)}


def act(target, locator, expected, operation, *, timeout=3, check=lambda: None):
    """Reobserve and dispatch at most one action; the caller owns Guard approval.

    Timeout/cancellation after helper launch is uncertain delivery. The caller
    must stop and observe the result, never repeat the final command blindly.
    """
    _target(target)
    _limits(12, 500, timeout)
    if (operation not in {'expand', 'invoke', 'hover', 'click_parent', 'click_leaf'} or not isinstance(locator, dict)
            or set(locator) != {'source', 'path', 'root_object_id'} or locator.get('source') not in SOURCES
            or not isinstance(locator.get('path'), list) or len(locator['path']) > 32
            or any(type(value) is not int or not 0 <= value < 2000 for value in locator['path'])
            or locator.get('root_object_id') != -4 or not isinstance(expected, dict)
            or set(expected) != {'target', 'path', 'observed_at_ms'}):
        raise _invalid()
    stamp = expected['observed_at_ms']
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not -500 <= time.time() * 1000 - stamp <= EVIDENCE_LIFETIME_MS
            or not isinstance(expected['target'], dict) or not isinstance(expected['path'], list)
            or len(expected['path']) != len(locator['path']) + 1):
        raise _invalid('menu_accessibility_stale_evidence')
    check()
    try:
        data = _native({**target, 'source': locator['source'], 'operation': operation,
                        'locator': locator, 'expected': expected, 'max_depth': 32, 'max_elements': 2000}, timeout, check)
    except OperationError as error:
        # No success inference or automatic fallback after a dispatched helper.
        # Missing/start failures are known not sent; other failures may have
        # occurred after the provider accepted the action.
        if error.code in {'menu_accessibility_helper_missing', 'menu_accessibility_helper_start_failed'}:
            raise
        error.input_dispatched = None
        error.delivery = 'unknown'
        raise
    if (data.get('status') not in {'ok', 'error'} or data.get('operation') != operation
            or data.get('delivery') not in {'sent', 'not_sent', 'not_needed', 'unknown'}
            or (data.get('input_dispatched') is not None and type(data.get('input_dispatched')) is not bool)
            or data.get('business_result_verified') is not False):
        raise _invalid('menu_accessibility_invalid_response')
    return data
