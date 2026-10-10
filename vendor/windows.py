"""Small Windows environment helpers. No UI input or automation is performed here."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess


def hidden_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def python_console() -> str:
    import sys
    path = Path(sys.executable)
    console = path.with_name("python.exe")
    return str(console if console.is_file() else path)


def native_claude(value: str = "") -> str:
    """Resolve the current user's CLI without executing a shell wrapper."""
    raw = value or shutil.which("claude.exe") or shutil.which("claude") or ""
    if not raw:
        return ""
    path = Path(raw.strip('"'))
    if path.suffix.lower() == ".exe" and path.is_file():
        return str(path.resolve())
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        for candidate in (
            path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe",
            path.parent / "node_modules/@anthropic-ai/claude-code/vendor/claude.exe",
            path.with_suffix(".exe"),
        ):
            if candidate.is_file():
                return str(candidate.resolve())
    return ""


def _app_path(name: str) -> str:
    if os.name != "nt":
        return ""
    import winreg
    key = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for extra in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | extra) as handle:
                    value = str(winreg.QueryValue(handle, None)).strip('"')
                    if Path(value).is_file():
                        return value
            except OSError:
                pass
    return ""


def _first(*paths: str | Path) -> str:
    for path in paths:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    return ""


def process_executable(pid: int) -> str:
    if os.name != "nt":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
    finally:
        kernel.CloseHandle(handle)


def running_apps() -> list[dict]:
    if os.name != "nt":
        return []
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    rows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        size = user.GetWindowTextLengthW(hwnd)
        if size <= 0:
            return True
        text = ctypes.create_unicode_buffer(size + 1)
        user.GetWindowTextW(hwnd, text, len(text))
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = process_executable(pid.value)
        if exe:
            rows.append({"name": Path(exe).stem, "exe": exe, "pid": pid.value,
                         "window_title": text.value, "window_id": int(hwnd)})
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(callback, 0)
    return sorted(rows, key=lambda row: (row["name"].lower(), row["window_title"]))


def discover() -> dict:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    from builtin_programs import builtin_catalog
    defaults = builtin_catalog()
    notepad, notepad_controls = defaults["notepad"]["exe"], defaults["notepad"]["control_exes"]
    # Browser defaults must be Chrome, not whichever browser Windows prefers.
    # Leave it unavailable when Chrome is absent; never silently allow Edge.
    browser = defaults["chrome"]["exe"]
    excel = _first(_app_path("excel.exe"), pf / "Microsoft Office/root/Office16/EXCEL.EXE",
                   pf86 / "Microsoft Office/root/Office16/EXCEL.EXE")
    apps = [
        {"id": "browser", "name": "Google Chrome", "exe": browser, "hints": "Chrome의 시험용 별도 창과 탭만 사용합니다. Chrome이 없으면 실제 설치 경로를 지정하세요. Edge로 자동 대체하지 않습니다."},
        {"id": "notepad", "name": "메모장", "exe": notepad, "hints": "시험용 새 문서만 사용합니다."},
        {"id": "explorer", "name": "파일 탐색기", "exe": _first(windir / "explorer.exe"), "hints": "이번 시험 결과 폴더만 확인합니다. 주소창에서 명령을 실행하지 않습니다."},
        {"id": "excel", "name": "Excel", "exe": excel, "hints": "시험용 새 통합문서만 사용합니다. 기존 문서나 매크로를 실행하지 않습니다."},
    ]
    for app in apps:
        app["available"] = bool(app["exe"])
        app["control_exes"] = notepad_controls if app["id"] == "notepad" else []
    apps.extend({k: v for k, v in defaults[name].items() if k != "builtin"} for name in ("calculator", "store"))
    return {"claude": native_claude(), "apps": apps}


class OwnedProcess:
    """Own only the runner tree; kill-on-close contains orphaned MCP children."""
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.job = None
        self.job_error = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not job:
            self.job_error = {"stage": "create_job", "winerror": ctypes.get_last_error()}
            return
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.job_error = {"stage": "set_job_limits", "winerror": ctypes.get_last_error()}
            kernel.CloseHandle(job)
            return
        if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
            self.job_error = {"stage": "assign_process", "winerror": ctypes.get_last_error()}
            kernel.CloseHandle(job)
            return
        self.job = job
        self.kernel = kernel

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif self.process.poll() is None:
            if os.name == "nt":
                # The PID is obtained from this exact process, never from a name scan.
                subprocess.run([str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/taskkill.exe"),
                                "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                               creationflags=hidden_flags(), timeout=10)
            else:
                self.process.terminate()
