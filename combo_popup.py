"""Read-only, per-expansion HWND proof for native ComboBox lists.

This does not implement a new input backend. A native window proof still needs
fresh Driver element handles, and cannot convert a search dialog into a list.
Untitled WinForms 808 windows have no universal modal marker in the current
native snapshot. Known dialog evidence is rejected, but class alone does not
prove a vendor window's semantics; fresh selectable list evidence is required.
"""
from __future__ import annotations

import copy
import re
import time

from operations import OperationError


_IDENTITY = ('pid', 'window_id', 'class_name', 'thread_id', 'owner_window_id',
             'root_owner_window_id')


class ComboPopupDiscovery:
    def __init__(self, runtime, owner, *, probe=None):
        self.runtime, self.owner = runtime, dict(owner)
        self.probe, self.owns_probe = probe, probe is None
        self.initial = self.owner_identity = self.selected = self.selected_path = None
        self.elapsed_ms = 0.0
        self.reads = 0

    def _state(self):
        self.runtime.check_active()
        state = getattr(self.probe, 'transition_snapshot', self.probe.snapshot)()
        self.reads += 1
        from interaction import _identity
        identity = _identity(state, self.owner['window_id'])
        if identity.get('pid') != self.owner['pid'] or (
                self.owner_identity is not None and identity != self.owner_identity):
            raise OperationError('선택 목록의 원래 창이나 프로세스가 바뀌었습니다.', 'combo_owner_changed')
        rows = state.get('windows', [])
        if len(rows) > 4096 or any(not isinstance(row, dict) for row in rows):
            raise OperationError('선택 목록의 창 관찰 범위가 불완전합니다.', 'combo_inventory_invalid')
        ids = [row.get('window_id') for row in rows]
        if len(ids) != len(set(ids)):
            raise OperationError('선택 목록 창 식별자가 중복됩니다.', 'combo_inventory_invalid')
        self.owner_identity = identity
        return rows

    def prepare(self):
        began = time.monotonic()
        try:
            if self.probe is None:
                self.probe = self.runtime.create_transition_probe(self.owner)
                self.probe.capture()
            self.initial = copy.deepcopy(self._state())
        except Exception:
            self.close()
            raise
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def _path(self, row, rows):
        # NativeClosureProbe rechecks every same-process owner/parent edge.
        path_reader = getattr(self.probe, 'ownership_path', None)
        if callable(path_reader):
            path = path_reader(row['window_id'], self.owner['window_id'])
            if path:
                return path
        from recording_windows import owned_by, combo_list_owned_by
        if not callable(path_reader) and owned_by(row, self.owner['window_id'], rows):
            return [{key: row.get(key) for key in _IDENTITY}]
        if row.get('class_name') == 'ComboLBox' and combo_list_owned_by(
                self.owner['window_id'], row['window_id'], self.owner['pid']):
            return [{'combo_list_owner': self.owner['window_id'], 'window_id': row['window_id']}]
        return None

    @staticmethod
    def _list_class(name):
        return isinstance(name, str) and (name == 'ComboLBox' or re.fullmatch(
            r'WindowsForms10\.Window\.808\.app\.0\.[0-9a-fA-F]+_r\d+_ad\d+', name) is not None)

    def _candidates(self, rows):
        before = {row['window_id']: row for row in self.initial}
        candidates = []
        for row in rows:
            hwnd = row.get('window_id')
            if (row.get('pid') != self.owner['pid'] or hwnd == self.owner['window_id']
                    or row.get('visible') is not True or before.get(hwnd, {}).get('visible') is True
                    or row.get('is_child') is True
                    or row.get('class_name') in {'IME', 'MSCTFIME UI'}):
                continue
            if (any(type(row.get(key)) is not int or row[key] < 1 for key in ('pid', 'window_id', 'thread_id'))
                    or not row.get('class_name') or any(type(row.get(key)) is not int
                        for key in ('owner_window_id', 'root_owner_window_id'))):
                raise OperationError('새 목록 후보의 창 식별 근거가 부족합니다.', 'combo_identity_unavailable')
            path = self._path(row, rows)
            if not path:
                continue
            if any(item.get('enabled') is False for item in rows
                   if item.get('window_id') in {edge.get('window_id') for edge in path} | {self.owner['window_id']}):
                raise OperationError('원래 창이 비활성화된 관련 창은 일반 드롭다운으로 처리하지 않습니다. 검색 모달을 새로 관찰하세요.',
                                     'combo_modal_owner_disabled')
            if row['class_name'] != 'ComboLBox' and row.get('title'):
                raise OperationError('제목이 있는 관련 창은 일반 드롭다운으로 추정하지 않습니다. 검색 모달/편집기를 새로 관찰하세요.',
                                     'combo_popup_kind_unsupported')
            # Child editors are not popup candidates. Only a known list HWND
            # is auto-connected; an actual related dialog needs explicit review.
            if not self._list_class(row['class_name']):
                # GA_PARENT on a top-level popup may be the desktop HWND, not
                # zero. Only explicit native WS_CHILD evidence can exclude a
                # new child editor. Never infer that from a nonzero parent.
                raise OperationError('일반 목록과 다른 관련 창이 열렸습니다. 검색 모달/편집기를 새로 관찰하세요.',
                                     'combo_popup_kind_unsupported')
            old = before.get(hwnd)
            if old is not None and any(old.get(key) != row.get(key) for key in _IDENTITY):
                raise OperationError('숨겨진 목록의 창 식별 정보가 바뀌었습니다.', 'combo_popup_changed')
            candidates.append((row, path))
        if len(candidates) > 1:
            raise OperationError('새로 표시된 관련 목록이 여러 개입니다. 첫 후보를 선택하지 않습니다.', 'ambiguous_popup')
        return candidates

    def discover(self):
        began = time.monotonic()
        try:
            candidates = self._candidates(self._state())
            if not candidates:
                return None
            self.selected, self.selected_path = copy.deepcopy(candidates[0])
            self._confirm()
            return {key: self.selected[key] for key in ('pid', 'window_id')}
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def confirm(self):
        began = time.monotonic()
        try:
            self._confirm()
        finally:
            self.elapsed_ms += (time.monotonic() - began) * 1000

    def _confirm(self):
        candidates = self._candidates(self._state())
        if len(candidates) != 1 or any(candidates[0][0].get(key) != self.selected.get(key)
                                      for key in _IDENTITY) or candidates[0][1] != self.selected_path:
            raise OperationError('선택 목록이 닫혔거나 소유 관계가 바뀌었습니다. 입력하지 않았습니다.', 'combo_popup_changed')

    def close(self):
        if self.owns_probe and self.probe is not None:
            self.probe.close()
        self.probe = None
