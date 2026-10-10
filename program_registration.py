"""Explicit chat registration, without desktop input or broad config reloads."""
from __future__ import annotations

import copy
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time
import uuid
import re
from urllib.parse import urlsplit

import programs
from settings import validate_config
from program_launch import validate_launch_profile
from vendor.guard import check_app, _same_windows_user_session, GuardError


class RegistrationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def protocol_handler(uri, command_reader=None):
    """Read association metadata only; never execute its command or trust it
    as the UI process. Launchers, browsers and updater chains often differ.
    """
    validate_launch_profile({'kind': 'uri', 'target': uri})
    scheme = urlsplit(uri).scheme.lower()
    info = {'scheme': scheme, 'registered_handler': None, 'handler_is_control_target': False,
            'registry_modified': False, 'application_launched': False}
    if scheme in {'http', 'https'}:
        return {**info, 'status': 'browser_route', 'message': '웹 주소의 처리 프로그램과 업무 창은 다를 수 있습니다. 실제 열린 업무 창을 선택하세요.'}
    if command_reader is None:
        if os.name != 'nt':
            return {**info, 'status': 'unavailable'}
        def command_reader(scheme):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme + r'\shell\open\command') as key:
                value, kind = winreg.QueryValueEx(key, None)
                if kind not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
                    raise ValueError('unsupported association')
                return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value
    try:
        command = command_reader(scheme)
        if not isinstance(command, str) or len(command) > 32768 or any(ord(c) < 32 for c in command):
            raise ValueError('invalid command')
        # Parse only an unambiguous first executable token. Never interpolate
        # URI placeholders or pass the association through a shell.
        match = re.match(r'^\s*(?:"([^"\r\n]+)"|([^\s"]+))(?:\s|$)', command)
        if not match:
            raise ValueError('ambiguous command')
        exe = match.group(1) or match.group(2)
        check_app(exe)
        info.update(status='association_found', registered_handler=exe)
    except (OSError, ValueError, GuardError):
        info.update(status='unavailable', message='주소 연결 정보를 확인하지 못했습니다. 실제 업무 창을 열고 목록에서 선택하면 됩니다.')
    return info


def live_candidates():
    """Only visible top-level metadata belonging to this token user/session.

    No UIA, screenshots, command lines, registry or disk search. An inaccessible
    process is omitted; elevation is never requested to populate this list.
    """
    if os.name != "nt":
        raise RegistrationError("windows_required", "열린 프로그램 목록은 Windows에서 사용할 수 있습니다. EXE 경로로 등록할 수 있습니다.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    signatures = [
        (kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        (kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        (kernel.GetProcessTimes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        (kernel.QueryFullProcessImageNameW, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        (user.EnumWindows, [callback_type, wintypes.LPARAM], wintypes.BOOL),
        (user.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        (user.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
        (user.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ]
    for function, arguments, result in signatures:
        function.argtypes, function.restype = arguments, result
    rows = []

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd) or len(rows) >= 100:
            return True
        pid = wintypes.DWORD()
        if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return True
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return True
        try:
            if not _same_windows_user_session(pid.value, handle):
                return True
            timestamps = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in timestamps)):
                return True
            size, executable = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
            if not kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size)):
                return True
            # Check app type before exposing its window title.
            check_app(executable.value)
            title, class_name = ctypes.create_unicode_buffer(1025), ctypes.create_unicode_buffer(257)
            if not user.GetWindowTextW(hwnd, title, len(title)) or not user.GetClassNameW(hwnd, class_name, len(class_name)):
                return True
            current_pid = wintypes.DWORD()
            if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid)) or current_pid.value != pid.value:
                return True
            rows.append({"pid": pid.value, "window_id": int(hwnd), "exe": executable.value,
                         "name": Path(executable.value).stem, "window_title": title.value,
                         "class_name": class_name.value,
                         "creation_time": (timestamps[0].dwHighDateTime << 32) | timestamps[0].dwLowDateTime})
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            kernel.CloseHandle(handle)
        return True

    if not user.EnumWindows(visit, 0):
        raise RegistrationError("candidate_read_failed", "열린 창 목록을 읽지 못했습니다. EXE 경로로 등록하거나 목록을 다시 확인하세요.")
    return sorted(rows, key=lambda row: (row["name"].casefold(), row["window_title"], row["window_id"]))


class ProgramRegistration:
    CANDIDATE_SECONDS = 300

    def __init__(self, manager, *, candidate_source=None, clock=time.monotonic):
        self.manager = manager
        self.candidate_source = candidate_source or live_candidates
        self.clock = clock
        self.candidates = {}

    @staticmethod
    def _identity(row):
        return tuple(row[key] for key in ("pid", "window_id", "creation_time", "exe", "class_name", "window_title"))

    def list_candidates(self):
        items, now = [], self.clock()
        with self.manager.lock:
            self.candidates = {key: value for key, value in self.candidates.items() if value[0] > now}
            rows = self.candidate_source()
            for row in rows[:100]:
                token = "program-" + uuid.uuid4().hex
                self.candidates[token] = (now + self.CANDIDATE_SECONDS, self._identity(row), copy.deepcopy(row))
                items.append({key: row[key] for key in ("name", "exe", "window_title")} | {"candidate_id": token})
            # Bound memory across repeated inventories; tokens are transient.
            self.candidates = dict(list(self.candidates.items())[-300:])
        return {"ok": True, "status": "program_candidates", "candidates": items,
                "expires_in_seconds": self.CANDIDATE_SECONDS, "screen_accessed": False,
                "window_metadata_read": True, "driver_started": False,
                "next_tool": "computer_register_program", "input_dispatched": False,
                "message": "추가할 창의 candidate_id를 computer_register_program에 전달하세요. 같은 이름의 창은 제목으로 구분하세요."}

    def uri_candidates(self, args, cancel_event=None):
        uri = args['launch_uri']
        validate_launch_profile({'kind': 'uri', 'target': uri})
        if args.get('arguments') or args.get('working_directory'):
            raise RegistrationError('invalid_launch', '주소 실행과 EXE 인자·시작 폴더는 함께 지정하지 않습니다.')
        # Previously confirmed URI + program identity is already sufficient.
        known = [p for p in self.manager.config['programs'] if p.get('launch') == {'kind': 'uri', 'target': uri}]
        if len(known) == 1:
            return self.register({**args, 'exe': known[0]['exe'], 'program_id': known[0]['id']}, cancel_event)
        choices = self.list_candidates()
        association = protocol_handler(uri)
        return {**choices, 'status': 'choose_program_window', 'saved': False,
                'applied_to_next_session': False, 'reconnect_required': False,
                'launch_uri': uri, 'association': association, 'automatic_retry': False,
                'next_arguments': {'launch_uri': uri, **{key: args[key] for key in ('name', 'program_id', 'hints') if key in args}},
                'message': '주소를 등록할 실제 업무 창을 목록에서 선택하세요. 선택한 candidate_id와 launch_uri를 함께 전달하면 바로 등록됩니다. 창이 없으면 사용자가 평소 방식으로 한 번 연 뒤 목록을 다시 확인하세요. 설정 파일을 찾거나 편집할 필요가 없습니다.'}

    def _options(self, args, loaded):
        if bool(args.get("exe")) == bool(args.get("candidate_id")):
            raise RegistrationError("choose_program", "EXE 전체 경로 또는 열린 창의 candidate_id 중 하나를 지정하세요.")
        exe = args.get("exe")
        if args.get("candidate_id"):
            remembered = self.candidates.get(args["candidate_id"])
            if remembered is None or remembered[0] <= self.clock():
                raise RegistrationError("candidate_stale", "열린 창 선택이 만료되었습니다. computer_program_candidates로 현재 목록을 다시 확인하세요.")
            matches = [row for row in self.candidate_source() if self._identity(row) == remembered[1]]
            if len(matches) != 1:
                raise RegistrationError("candidate_stale", "선택했던 창이 바뀌거나 닫혔습니다. computer_program_candidates로 현재 목록을 다시 확인하세요.")
            exe = matches[0]["exe"]
        if args.get("launch_uri") and (args.get("arguments") or args.get("working_directory")):
            raise RegistrationError("invalid_launch", "주소 실행과 EXE 인자·시작 폴더는 함께 지정하지 않습니다.")
        launch = ({"kind": "uri", "target": args["launch_uri"]} if args.get("launch_uri") else
                  {"kind": "exe", "arguments": args.get("arguments", [])} if "arguments" in args or args.get("working_directory") else None)
        if args.get("working_directory"):
            launch["cwd"] = args["working_directory"]
        same_executable = [item for item in loaded["programs"] if item.get("exe") and check_app(item["exe"]) == check_app(exe)]
        # Explicit [] means no arguments, not "reuse the old arguments". A
        # single plain registration already has exactly that default behavior.
        if (launch == {"kind": "exe", "arguments": []} and len(same_executable) == 1
                and "launch" not in same_executable[0]):
            launch = None
        matches = [item for item in loaded["programs"] if item.get("exe") and check_app(item["exe"]) == check_app(exe)
                   and (launch is None or item.get("launch") == launch)]
        existing = matches[0] if len(matches) == 1 else {}
        # An omitted friendly label/hint must not rename or erase an existing entry.
        return {"exe": exe, "name": args.get("name", existing.get("name", Path(exe).stem)),
                "program_id": args.get("program_id"), "hints": args.get("hints", existing.get("hints", "")),
                "control_exes": args.get("control_exes", existing.get("control_exes", [])),
                "launch": launch if launch is not None else existing.get("launch")}

    def _apply(self, updated):
        manager = self.manager
        # Prepare all independent copies before publishing any. Existing stores
        # stay connected to teaching/editing services; their locks are retained.
        values = [copy.deepcopy(updated) for _ in range(3)]
        manager.config, manager.tasks.config, manager.elements.config = values

    def register(self, args, cancel_event=None):
        manager = self.manager
        with manager.lock:
            if args.get('launch_uri') and not args.get('exe') and not args.get('candidate_id'):
                return self.uri_candidates(args, cancel_event)
            if manager.config_path is None:
                raise RegistrationError("configuration_untracked", "연결된 설정 파일 경로가 없어 저장할 수 없습니다. 설치 도구로 연결한 MCP에서 다시 요청하세요.")
            loaded = validate_config(copy.deepcopy(manager.config))
            _, _, disk, initial_hash = programs._read(manager.config_path)
            if disk != loaded:
                raise RegistrationError("configuration_changed", "다른 곳에서 설정이 변경되어 덮어쓰지 않았습니다. 현재 화면 작업을 마친 뒤 MCP를 다시 연결하고 등록하세요.")
            options = self._options(args, loaded)
            preview = programs.preview_add(manager.config_path, **options)
            if preview["expected_config_sha256"] != initial_hash:
                raise RegistrationError("configuration_changed", "등록 확인 중 설정이 변경되었습니다. 다른 변경을 덮어쓰지 않았습니다. 목록을 다시 확인하세요.")
            updated = copy.deepcopy(loaded)
            if preview["status"] != "already_present":
                updated["programs"].append(preview["program"])
            updated = validate_config(updated)
            if cancel_event is not None and cancel_event.is_set():
                raise RegistrationError("registration_cancelled", "등록 요청이 취소되어 설정을 변경하지 않았습니다.")
            saved = programs.add_program(manager.config_path, expected_config_sha256=preview["expected_config_sha256"], **options)
            original = manager.config, manager.tasks.config, manager.elements.config
            try:
                _, _, after, _ = programs._read(manager.config_path)
                if after != updated:
                    raise RegistrationError("configuration_changed", "저장 직후 다른 설정 변경이 감지되었습니다.")
                self._apply(updated)
            except Exception:
                manager.config, manager.tasks.config, manager.elements.config = original
                return {"ok": False, "status": "saved_restart_required", "saved": True,
                        "applied_to_next_session": False, "reconnect_required": True,
                        "active_session_scope_changed": False, "program": saved["program"],
                        "screen_accessed": False, "driver_started": False,
                        "diagnostic": {"code": "registration_apply_failed"},
                        "message": "등록은 저장했지만 이 연결에 반영하지 못했습니다. 중복 등록하지 말고 현재 작업을 마친 뒤 MCP를 다시 연결하세요."}
            active = manager.session is not None and manager.session.state != "stopped"
            return {"ok": True, "status": "registered" if saved["changed"] else "already_present",
                    "saved": True, "changed": saved["changed"], "program": saved["program"],
                    "applied_to_next_session": True, "reconnect_required": False,
                    "active_session_scope_changed": False, "screen_accessed": False, "driver_started": False,
                    "next_tool": "computer_end" if active else "computer_begin",
                    "message": ("등록했습니다. 현재 화면 작업을 끝내고 새 작업에서 이 프로그램을 선택하세요. MCP 재접속은 필요 없습니다."
                                if active else "사용할 수 있습니다. computer_begin에서 이 프로그램 ID를 선택하세요. MCP 재접속은 필요 없습니다.")}
