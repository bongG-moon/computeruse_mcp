"""Launch one allowed executable and report bounded, read-only startup evidence.

Creating a process is not proof of application readiness. Launchers can exit
after handing work to another process; existing windows are not startup proof.
This module never retries, terminates an application, or changes its token.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit

from vendor.guard import GuardError, check_app


# These values are owned/overwritten by our portable runtime registration and
# AdministratorBridge. Do not point another application's embedded Python/Tcl
# at this MCP's runtime. Preserve unrelated application and Windows variables.
RUNTIME_ENVIRONMENT = {
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT",
    "PYTHONSAFEPATH", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONNOUSERSITE",
    "PYTHONDONTWRITEBYTECODE", "TCL_LIBRARY", "TK_LIBRARY",
}


def launch_environment(source=None):
    source = os.environ if source is None else source
    return {key: value for key, value in source.items() if key.upper() not in RUNTIME_ENVIRONMENT}


def validate_launch_profile(value):
    """Validate a saved exact target, never a command or per-call interpolation."""
    if not isinstance(value, dict) or value.get("kind") not in {"exe", "uri", "builtin"}:
        raise ValueError("실행 방식은 exe, uri 또는 등록된 기본 앱으로 지정하세요.")
    if value["kind"] == "builtin":
        from builtin_programs import BUILTIN_PROTOCOLS
        if set(value) != {"kind", "id"} or value.get("id") not in BUILTIN_PROTOCOLS:
            raise ValueError("기본 앱 실행은 확인된 calculator 또는 store 식별자만 지원합니다.")
        return dict(value)
    if value["kind"] == "exe":
        if set(value) - {"kind", "arguments", "cwd"}:
            raise ValueError("실행 방식에는 인자 목록(arguments)과 시작 폴더(cwd)만 지정할 수 있습니다.")
        arguments = value.get("arguments", [])
        if (not isinstance(arguments, list) or len(arguments) > 32 or
                any(not isinstance(arg, str) or len(arg) > 4096 or any(ord(ch) < 32 for ch in arg)
                    for arg in arguments) or sum(len(arg) for arg in arguments) > 8192):
            raise ValueError("실행 인자는 문자열 목록으로 최대 32개, 합계 8,192자까지 지정하세요. 제어 문자는 사용할 수 없습니다.")
        if "cwd" in value:
            cwd = value["cwd"]
            if (not isinstance(cwd, str) or not cwd or len(cwd) > 32767 or
                    not Path(cwd).is_absolute() or cwd.startswith(("\\\\", "//")) or
                    any(ord(ch) < 32 or ch in '\"<>|*?' for ch in cwd)):
                raise ValueError("시작 폴더는 이 PC에 있는 폴더의 전체 경로를 지정하세요.")
    else:
        if set(value) != {"kind", "target"}:
            raise ValueError("주소 실행은 정확한 target 주소만 지정하세요. 인자와 시작 폴더를 함께 쓰지 않습니다.")
        target = value["target"]
        if (not isinstance(target, str) or not 1 <= len(target) <= 8192 or target != target.strip() or
                any(ord(ch) < 32 or ch.isspace() or ch in '\"\\' for ch in target) or
                any(ord(ch) < 32 or ch in '\"\\' for ch in unquote(target))):
            raise ValueError("실행 주소에 공백·제어 문자·따옴표·역슬래시를 사용할 수 없습니다.")
        parsed = urlsplit(target)
        scheme = parsed.scheme.lower()
        blocked = {"file", "shell", "javascript", "vbscript", "data", "cmd", "powershell", "pwsh",
                   "wscript", "cscript", "search", "search-ms", "mshta", "about", "rundll32"}
        if (not re.fullmatch(r"[a-z][a-z0-9+.-]{1,63}", scheme) or scheme in blocked or scheme.startswith("ms-") or
                not target.lower().startswith(scheme + "://") or not parsed.netloc or
                parsed.username is not None or parsed.password is not None):
            raise ValueError("http(s):// 또는 설치된 프로그램의 전용 protocol:// 주소를 지정하세요. 파일·시스템 명령·사용자정보 포함 주소는 지원하지 않습니다.")
        # Force parsing of malformed ports/IPv6 while preserving the exact saved URL.
        if not parsed.hostname:
            raise ValueError("실행 주소의 대상 이름을 확인하세요.")
        parsed.port
    return {key: list(item) if isinstance(item, list) else item for key, item in value.items()}


def create_application(executable, process_factory=subprocess.Popen, *, arguments=(), cwd=None):
    """Start the registered EXE directly; arguments remain individual strings."""
    profile = {"kind": "exe", "arguments": list(arguments)}
    if cwd is not None:
        profile["cwd"] = cwd
    validate_launch_profile(profile)
    directory = cwd or str(Path(executable).parent)
    if cwd is not None and not Path(cwd).is_dir():
        raise ValueError("등록한 시작 폴더를 찾지 못했습니다. 자동으로 다른 폴더에서 실행하지 않습니다.")
    return process_factory([executable, *arguments], cwd=directory,
                           env=launch_environment(), shell=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=0)


class UriLaunchRequest:
    """Windows dispatch accepted; os.startfile provides no process handle/PID."""
    pid = None
    launch_kind = "uri"
    def poll(self):
        return None


def create_program(program, process_factory=subprocess.Popen, *, uri_launcher=None):
    profile = validate_launch_profile(program.get("launch", {"kind": "exe"}))
    if profile["kind"] == "exe":
        return create_application(program["exe"], process_factory,
                                  arguments=profile.get("arguments", []), cwd=profile.get("cwd"))
    if uri_launcher is None:
        if os.name != "nt":
            raise OSError("주소 실행은 Windows에서만 지원합니다.")
        uri_launcher = os.startfile
    if profile["kind"] == "builtin":
        from builtin_programs import BUILTIN_PROTOCOLS
        uri_launcher(BUILTIN_PROTOCOLS[profile["id"]], "open")
    else:
        uri_launcher(profile["target"], "open")
    return UriLaunchRequest()


class LaunchObservation:
    def __init__(self, executables, observe, *, check_active=lambda: None,
                 clock=time.monotonic, wait=time.sleep):
        self.executables = {check_app(path) for path in executables}
        self.observe, self.check_active = observe, check_active
        self.clock, self.wait = clock, wait
        self.initial = None
        self.last_error = None

    def _windows(self):
        # The caller's guard has already checked allowlist, SID and session
        # before reading any titles. Narrow this further to the selected app.
        answer = self.observe()
        data = answer.get("structuredContent", {}) if isinstance(answer, dict) else {}
        if (not isinstance(answer, dict) or answer.get("isError") or
                not isinstance(data, dict) or not isinstance(data.get("windows"), list)):
            self.last_error = "window_discovery_failed"
            return None
        rows = {}
        for row in data["windows"]:
            if not isinstance(row, dict):
                continue
            try:
                if check_app(row.get("exe", "")) not in self.executables:
                    continue
            except (GuardError, ValueError, OSError):
                continue
            pid, hwnd = row.get("pid"), row.get("window_id")
            if type(pid) is int and pid > 0 and type(hwnd) is int and hwnd > 0:
                rows[(pid, hwnd)] = {"pid": pid, "window_id": hwnd, "exe": row["exe"]}
        self.last_error = None
        return rows

    def capture(self):
        self.check_active()
        self.initial = self._windows()

    def finish(self, process, timeout_seconds=2.0):
        started = self.clock()
        stable = {}
        rows = None
        exit_code = None
        while True:
            self.check_active()
            rows = self._windows()
            exit_code = process.poll()
            now = self.clock()
            direct = {key: row for key, row in (rows or {}).items()
                      if key[0] == process.pid and exit_code is None}
            new = ({key: row for key, row in (rows or {}).items()
                    if key not in self.initial and key[0] != process.pid}
                   if self.initial is not None else {})
            candidates = {**direct, **new}
            stable = {key: stable.get(key, now) for key in candidates}
            steady = {key: row for key, row in candidates.items() if now - stable[key] >= .25}
            if steady or now - started >= timeout_seconds:
                break
            self.wait(min(.1, max(0, timeout_seconds - (now - started))))

        direct_rows = [row for key, row in steady.items() if key in direct]
        new_rows = [row for key, row in steady.items() if key in new]
        has_process = type(process.pid) is int and process.pid > 0
        result = {"request_accepted": True, "process_started": True if has_process else None, "requested_pid": process.pid,
                  "process_running": exit_code is None if has_process else None, "exit_code": exit_code,
                  "launched": exit_code is None if has_process else False, "window_verified": False,
                  "application_ready_verified": False, "task_verified": False,
                  "automatic_retry": False, "privilege_mode": "inherit_mcp",
                  "windows": direct_rows + new_rows,
                  "observed_ms": round((self.clock() - started) * 1000)}
        if direct_rows:
            result.update(launch_status="window_observed", window_verified=True)
            result["next"] = "표시된 창을 get_window_state로 새로 읽어 로그인·오류·시작 화면인지 확인한 뒤 작업하세요. 창이 보인다는 것만으로 업무 준비 완료는 아닙니다."
        elif new_rows:
            result.update(launch_status="handoff_unverified", window_verified=True,
                          handoff_verified=False)
            result["next"] = "실행 요청 뒤 같은 프로그램에 허용된 다른 프로세스의 새 창이 나타났습니다. 런처의 정상 인계는 아직 확정하지 않았습니다. 이 창을 새로 관찰해 확인하세요. 자동 재실행하지 않습니다."
        elif exit_code is not None:
            result["launch_status"] = "startup_failed" if exit_code != 0 else "launcher_exited_unverified"
            result["next"] = "요청한 프로세스가 종료됐고 새 창 실행을 확인하지 못했습니다. 종료 코드와 앱 로그를 확인하세요. 이미 열린 창이나 늦게 생성되는 자식 창은 list_windows로 다시 확인할 수 있습니다. 자동 재실행하지 않습니다."
        elif not has_process:
            result["launch_status"] = "dispatch_unverified"
            result["next"] = "등록한 주소를 Windows에 전달했습니다. 실제 프로세스 실행·창 열림은 확인하지 못했습니다. list_windows로 허용된 프로그램의 창을 다시 확인하세요. 자동 재실행하지 않습니다."
        else:
            result["launch_status"] = "process_running_unverified"
            result["next"] = "프로세스는 실행 중이지만 창이 열렸는지 확인하지 못했습니다. 잠시 후 list_windows로 확인하세요. 실행 완료로 보고하거나 자동 재실행하지 마세요."
        if self.last_error or self.initial is None:
            result["observation_error"] = self.last_error or "initial_window_discovery_failed"
        if exit_code is not None and rows:
            result["existing_window_count"] = len(set(rows) & set(self.initial or {}))
        return result
