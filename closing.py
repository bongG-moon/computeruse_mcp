"""Read-only proof of an explicitly observed window/process closing.

No input, close message, save/discard decision, or process termination occurs
here. Tickets and retained process handles exist only within one MCP session.
"""
from __future__ import annotations

import copy
import ctypes
from ctypes import wintypes
import os
import threading
import time
import uuid

from vendor.guard import check_app, _same_windows_user_session


INPUT_METHOD_HELPER_CLASSES = {"IME", "MSCTFIME UI"}
TRANSIENT_WINDOW_READ_ERRORS = {
    "window_class_read_failed", "window_changed_during_read", "window_identity_read_failed",
    "window_title_read_failed", "window_owner_read_failed", "target_changed_during_read",
    "window_enumeration_failed",
}


def _protect_window_identity(row):
    return row.get("class_name") not in INPUT_METHOD_HELPER_CLASSES or any(
        row.get(key) is True for key in ("visible", "enabled", "minimized"))


class ClosureError(ValueError):
    def __init__(self, message, code="invalid_closure_request"):
        super().__init__(message)
        self.code = code


class ProbeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class NativeClosureProbe:
    """Retain the original kernel object; never infer exit from PID lookup."""
    def __init__(self, pid, window_id, *, kernel=None, user=None):
        if kernel is None or user is None:
            if os.name != "nt":
                raise ProbeError("windows_required")
            kernel = kernel or ctypes.WinDLL("kernel32", use_last_error=True)
            user = user or ctypes.WinDLL("user32", use_last_error=True)
        self.pid, self.window_id = pid, window_id
        self.kernel, self.user = kernel, user
        self.handle = None
        self.creation_time = None
        self.executable = None
        self._lock = threading.RLock()
        self.callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = [
            (kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (kernel.WaitForSingleObject, [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            (kernel.GetProcessId, [wintypes.HANDLE], wintypes.DWORD),
            (kernel.GetProcessTimes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            (kernel.QueryFullProcessImageNameW, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (user.IsWindow, [wintypes.HWND], wintypes.BOOL),
            (user.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (user.EnumWindows, [self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            (user.GetWindow, [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (user.GetAncestor, [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (user.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
            (user.IsWindowEnabled, [wintypes.HWND], wintypes.BOOL),
            (user.IsIconic, [wintypes.HWND], wintypes.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def _process_exited(self):
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state == 0:
            return True
        if state == 258:
            return False
        raise ProbeError("process_wait_failed")

    def _creation(self):
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(x) for x in values)):
            raise ProbeError("process_identity_read_failed")
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def _window_pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            raise ProbeError("window_identity_read_failed")
        return pid.value

    def _window(self, hwnd):
        if not self.user.IsWindow(hwnd) or self._window_pid(hwnd) != self.pid:
            raise ProbeError("window_changed_during_read")
        title, class_name = ctypes.create_unicode_buffer(1025), ctypes.create_unicode_buffer(257)
        ctypes.set_last_error(0)
        count = self.user.GetWindowTextW(hwnd, title, len(title))
        if not count and ctypes.get_last_error():
            raise ProbeError("window_title_read_failed")
        if not self.user.GetClassNameW(hwnd, class_name, len(class_name)):
            raise ProbeError("window_class_read_failed")
        owner = int(self.user.GetWindow(hwnd, 4) or 0)  # GW_OWNER
        root_owner = int(self.user.GetAncestor(hwnd, 3) or 0)  # GA_ROOTOWNER
        if not root_owner:
            raise ProbeError("window_owner_read_failed")
        thread_pid = wintypes.DWORD()
        thread_id = int(self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(thread_pid)))
        if not thread_id or thread_pid.value != self.pid:
            raise ProbeError("window_identity_read_failed")
        row = {"window_id": hwnd, "pid": self.pid, "thread_id": thread_id, "owner_window_id": owner,
               "root_owner_window_id": root_owner, "title": title.value,
               "class_name": class_name.value, "visible": bool(self.user.IsWindowVisible(hwnd)),
               "enabled": bool(self.user.IsWindowEnabled(hwnd)), "minimized": bool(self.user.IsIconic(hwnd))}
        if not self.user.IsWindow(hwnd) or self._window_pid(hwnd) != self.pid:
            raise ProbeError("window_changed_during_read")
        return row

    def capture(self):
        with self._lock:
            if self.handle is not None:
                raise ProbeError("probe_already_captured")
            self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, self.pid)
            if not self.handle:
                self.handle = None
                raise ProbeError("process_open_failed")
            try:
                if self.kernel.GetProcessId(self.handle) != self.pid:
                    raise ProbeError("process_identity_mismatch")
                if self._process_exited():
                    raise ProbeError("process_already_exited")
                if not _same_windows_user_session(self.pid, self.handle):
                    raise ProbeError("process_user_session_mismatch")
                self.creation_time = self._creation()
                size, buffer = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
                if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
                    raise ProbeError("process_executable_read_failed")
                self.executable = buffer.value
                initial = self.snapshot()
                if initial["process_exited"] or not initial["target_present"]:
                    raise ProbeError("target_not_live_at_prepare")
                return initial
            except Exception:
                self.close()
                raise

    def snapshot(self):
        with self._lock:
            if self.handle is None:
                raise ProbeError("probe_closed")
            if self._process_exited():
                return {"process_exited": True, "target_present": False, "windows": [],
                        "creation_time": self.creation_time, "executable": self.executable}
            if self.kernel.GetProcessId(self.handle) != self.pid or self._creation() != self.creation_time:
                raise ProbeError("process_identity_mismatch")
            present = bool(self.user.IsWindow(self.window_id))
            if present and self._window_pid(self.window_id) != self.pid:
                raise ProbeError("target_handle_reused")
            handles = []
            @self.callback_type
            def collect(hwnd, _):
                handles.append(int(hwnd))
                return True
            if not self.user.EnumWindows(collect, 0):
                raise ProbeError("window_enumeration_failed")
            rows = []
            for hwnd in handles:
                # EnumWindows is intentionally not filtered by visibility/title.
                # An unrelated desktop window can disappear after enumeration.
                # Its confirmed absence is harmless; identity/read failures for
                # a still-existing HWND remain unknown. Target gets final check.
                if hwnd != self.window_id and not self.user.IsWindow(hwnd):
                    continue
                try:
                    pid = self._window_pid(hwnd)
                except ProbeError:
                    if hwnd != self.window_id and not self.user.IsWindow(hwnd):
                        continue
                    raise
                if pid == self.pid:
                    rows.append(self._window(hwnd))
            if present and self.window_id not in {row["window_id"] for row in rows}:
                rows.append(self._window(self.window_id))
            # Recheck after enumeration: transient races are unknown, not success.
            if self._process_exited():
                return {"process_exited": True, "target_present": False, "windows": [],
                        "creation_time": self.creation_time, "executable": self.executable}
            final_present = bool(self.user.IsWindow(self.window_id))
            if final_present != present:
                raise ProbeError("target_changed_during_read")
            if final_present and self._window_pid(self.window_id) != self.pid:
                raise ProbeError("target_handle_reused")
            return {"process_exited": False, "target_present": present, "windows": rows,
                    "creation_time": self.creation_time, "executable": self.executable}

    def close(self):
        with self._lock:
            if self.handle is not None:
                if not self.kernel.CloseHandle(self.handle):
                    raise ProbeError("process_handle_close_failed")
                self.handle = None


class ClosureManager:
    MAX_TICKETS = 32
    POLL_SECONDS = 0.1

    def __init__(self, runtime, probe_factory=None):
        self.runtime = runtime
        self.session_id = runtime.id
        self.probe_factory = probe_factory or NativeClosureProbe
        self.tickets = {}
        self._lock = threading.RLock()
        self._closed = False

    def _active(self):
        self.runtime.check_active()
        if self._closed or self.runtime.id != self.session_id:
            raise ClosureError("종료 확인 기록은 만든 화면 작업 세션에서만 사용할 수 있습니다.", "closure_session_changed")

    def prepare(self, target, scope="window"):
        self._active()
        if not isinstance(target, dict) or set(target) != {"pid", "window_id"} or any(
                type(target[k]) is not int or target[k] <= 0 for k in ("pid", "window_id")):
            raise ClosureError("종료하기 전에 현재 창의 pid/window_id를 지정하세요.")
        if scope not in {"window", "process"}:
            raise ClosureError("scope는 window 또는 process여야 합니다.")
        with self._lock:
            self._active()
            if len(self.tickets) >= self.MAX_TICKETS:
                raise ClosureError("이 세션의 종료 확인 기록 32개를 모두 사용했습니다. 새 세션에서 현재 창을 다시 관찰하세요.", "closure_ticket_limit")
            guard = self.runtime.guard
            executable = check_app(guard.process_resolver(target["pid"]))
            if executable not in guard.policy["allowed_apps"] or guard.window_resolver(target["window_id"]) != target["pid"]:
                raise ClosureError("이번 세션에 승인된 실행 중 프로그램의 현재 창만 종료 확인할 수 있습니다.", "closure_target_not_allowed")
            probe = None
            try:
                probe = self.probe_factory(target["pid"], target["window_id"])
                initial = probe.capture()
                self._validate_snapshot(initial, target)
                if initial["process_exited"] or not initial["target_present"]:
                    raise ProbeError("target_not_live_at_prepare")
                if check_app(initial["executable"]) != executable or check_app(guard.process_resolver(target["pid"])) != executable or guard.window_resolver(target["window_id"]) != target["pid"]:
                    raise ProbeError("target_changed_during_prepare")
                self._active()
                close_id = uuid.uuid4().hex
                self.tickets[close_id] = {"probe": probe, "scope": scope, "target": copy.deepcopy(target),
                    "creation_time": initial["creation_time"], "baseline": copy.deepcopy(initial["windows"]),
                    "known_related": set(), "protected_hwnds": {row["window_id"] for row in initial["windows"]
                        if _protect_window_identity(row)}}
                return {"close_id": close_id, "scope": scope, "target": copy.deepcopy(target)}
            except Exception as exc:
                if probe is not None:
                    probe.close()
                if isinstance(exc, ProbeError):
                    raise ClosureError("현재 창의 종료 확인 기록을 만들지 못했습니다. 창을 새로 관찰하세요.", exc.code) from exc
                raise

    @staticmethod
    def _validate_snapshot(snapshot, target):
        if not isinstance(snapshot, dict) or type(snapshot.get("process_exited")) is not bool or type(snapshot.get("target_present")) is not bool or not isinstance(snapshot.get("windows"), list) or type(snapshot.get("creation_time")) is not int or not isinstance(snapshot.get("executable"), str):
            raise ProbeError("invalid_native_snapshot")
        seen = set()
        for row in snapshot["windows"]:
            if not isinstance(row, dict) or type(row.get("window_id")) is not int or row["window_id"] <= 0 or row.get("pid") != target["pid"] or row["window_id"] in seen:
                raise ProbeError("invalid_native_window_list")
            seen.add(row["window_id"])
        if snapshot["target_present"] != (target["window_id"] in seen) or snapshot["process_exited"] and (snapshot["windows"] or snapshot["target_present"]):
            raise ProbeError("inconsistent_native_snapshot")

    @staticmethod
    def _candidates(ticket, windows):
        target = ticket["target"]["window_id"]
        baseline = {row["window_id"] for row in ticket["baseline"]}
        by_id = {row["window_id"]: row for row in windows}
        # Keep both ownership graphs: closing/reparenting the target can erase
        # current owner links. Previously related surviving windows still block.
        owners = {}
        for row in ticket["baseline"] + windows:
            edges = owners.setdefault(row["window_id"], set())
            edges.update(value for value in (row.get("owner_window_id"), row.get("root_owner_window_id"))
                         if type(value) is int and value > 0 and value != row["window_id"])
        known_related = set(ticket.get("known_related", ()))
        protected_hwnds = set(ticket.get("protected_hwnds", ()))
        protected_hwnds.update(row["window_id"] for row in windows if _protect_window_identity(row))
        candidates, background_helpers = [], []
        for hwnd, row in by_id.items():
            if hwnd == target:
                continue
            linked = hwnd in known_related
            pending, visited = [hwnd], set()
            while not linked and pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                edges = owners.get(current, ())
                linked = target in edges
                pending.extend(parent for parent in edges if parent not in visited)
            if linked:
                known_related.add(hwnd)
            if linked or hwnd not in baseline:
                # Windows creates these input-method helper windows lazily and
                # reparents them after a dialog closes. Exclude only this strict
                # inert state, never a HWND previously seen interactive or with
                # another class, a minimized/unknown window, or original target.
                helper = row.get("class_name") in INPUT_METHOD_HELPER_CLASSES and hwnd not in protected_hwnds and all(
                    row.get(key) is False for key in ("visible", "enabled", "minimized"))
                if helper:
                    background_helpers.append({**copy.deepcopy(row), "reason": "inactive_windows_input_method_helper"})
                else:
                    candidates.append({**copy.deepcopy(row), "reason": "owned_by_target" if linked else "new_same_process_window"})
        ticket["known_related"].update(known_related)
        ticket["protected_hwnds"].update(protected_hwnds)
        return candidates, background_helpers

    def _result(self, close_id, ticket, snapshot):
        self._validate_snapshot(snapshot, ticket["target"])
        if snapshot["creation_time"] != ticket["creation_time"]:
            raise ProbeError("process_identity_mismatch")
        process_exited = snapshot["process_exited"]
        window_closed = not snapshot["target_present"]
        candidates, background_helpers = self._candidates(ticket, snapshot["windows"])
        verified = process_exited or (ticket["scope"] == "window" and window_closed and not candidates)
        status = "verified" if verified else "needs_dialog" if candidates else "pending"
        code = "original_process_exited" if process_exited else "target_window_closed" if verified else "possible_dialog_remains" if candidates else "process_still_running" if window_closed else "target_window_still_open"
        return {"close_id": close_id, "status": status, "task_verified": verified,
                "window_closed": window_closed, "process_exited": process_exited, "scope": ticket["scope"],
                "target": copy.deepcopy(ticket["target"]), "remaining_windows": copy.deepcopy(snapshot["windows"]),
                "dialog_candidates": candidates, "background_helpers": background_helpers, "diagnostic": {"code": code,
                    "next_step": "종료 범위의 완료를 확인했습니다. 저장 여부는 별도 확인이 필요합니다." if verified else
                    "후보 창을 읽고 사용자가 요청한 저장/종료 방침에 따라 처리한 뒤 같은 close_id로 다시 확인하세요. 저장·삭제·강제 종료는 자동 수행하지 않습니다." if candidates else
                    "창 또는 프로세스가 아직 열려 있습니다. 현재 화면을 확인한 뒤 같은 close_id로 다시 확인하세요."}}

    def verify(self, close_id, timeout_ms=1500):
        self._active()
        if not isinstance(close_id, str) or not close_id or len(close_id) > 128:
            raise ClosureError("현재 세션에서 만든 close_id를 지정하세요.")
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 10000:
            raise ClosureError("timeout_ms는 0~10000 범위의 정수여야 합니다.")
        with self._lock:
            self._active()
            ticket = self.tickets.get(close_id)
            if ticket is None:
                raise ClosureError("이 세션의 종료 확인 기록이 없습니다. 종료 전 현재 창을 새로 관찰하고 기록해야 합니다.", "closure_ticket_not_found")
        # Never hold the manager lock while waiting: stop must release every
        # handle promptly. A probe serializes only its short native reads.
        deadline = time.monotonic() + timeout_ms / 1000
        polls = 0
        read_error_count, last_read_error = 0, None
        while True:
            self._active()
            polls += 1
            try:
                result = self._result(close_id, ticket, ticket["probe"].snapshot())
            except Exception as exc:
                read_error_count += 1
                last_read_error = exc.code if isinstance(exc, ProbeError) else "closure_read_failed"
                result = {"close_id": close_id, "status": "unknown", "task_verified": False,
                    "window_closed": None, "process_exited": None, "scope": ticket["scope"],
                    "target": copy.deepcopy(ticket["target"]), "remaining_windows": [], "dialog_candidates": [], "background_helpers": [],
                    "diagnostic": {"code": last_read_error,
                        "next_step": "종료 여부를 확인하지 못했습니다. 성공으로 보고하지 말고 현재 창과 세션을 다시 확인하세요."}}
            self._active()  # A stop racing with a successful read must win.
            remaining = deadline - time.monotonic()
            result["poll_count"] = polls
            result["read_error_count"] = read_error_count
            result["last_read_error"] = last_read_error
            # Windows can destroy an HWND between IsWindow and metadata reads
            # during normal close. Only these lifecycle reads are retried, with
            # the same deadline and retained process handle. No input is replayed.
            transient_read = result["status"] == "unknown" and result["diagnostic"]["code"] in TRANSIENT_WINDOW_READ_ERRORS
            if not (result["status"] in {"pending", "needs_dialog"} or transient_read) or remaining <= 0:
                return result
            self.runtime.stop_event.wait(min(self.POLL_SECONDS, remaining))

    def close(self):
        with self._lock:
            self._closed = True
            tickets, self.tickets = self.tickets, {}
        first_error = None
        failed = {}
        for close_id, ticket in tickets.items():
            try:
                ticket["probe"].close()
            except Exception as exc:
                first_error = first_error or exc
                failed[close_id] = ticket
        if failed:
            with self._lock:
                self.tickets.update(failed)
        if first_error is not None:
            raise first_error
