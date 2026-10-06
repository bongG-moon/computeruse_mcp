"""Launch one allowed executable and report bounded, read-only startup evidence.

Creating a process is not proof of application readiness. Launchers can exit
after handing work to another process; existing windows are not startup proof.
This module never retries, terminates an application, or changes its token.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

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


def create_application(executable, process_factory=subprocess.Popen):
    """No shell, extra arguments, hidden console, or privilege fallback."""
    return process_factory([executable], cwd=str(Path(executable).parent),
                           env=launch_environment(), shell=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=0)


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
        result = {"process_started": True, "requested_pid": process.pid,
                  "process_running": exit_code is None, "exit_code": exit_code,
                  "launched": exit_code is None, "window_verified": False,
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
        else:
            result["launch_status"] = "process_running_unverified"
            result["next"] = "프로세스는 실행 중이지만 창이 열렸는지 확인하지 못했습니다. 잠시 후 list_windows로 확인하세요. 실행 완료로 보고하거나 자동 재실행하지 마세요."
        if self.last_error or self.initial is None:
            result["observation_error"] = self.last_error or "initial_window_discovery_failed"
        if exit_code is not None and rows:
            result["existing_window_count"] = len(set(rows) & set(self.initial or {}))
        return result
