"""Exact UWP frame bindings without granting a shared Windows host process.

The Driver targets top-level frames, while Windows gives the app its own
CoreWindow child. Bind the two only while the exact host, child, registered EXE,
package and process lifetimes still match. No capture or UI input occurs here.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os


HOST_CLASS = "ApplicationFrameWindow"
APP_CLASS = "Windows.UI.Core.CoreWindow"
FIELDS = frozenset({"pid", "window_id", "exe", "host_exe", "host_started", "host_thread",
                    "app_pid", "app_window_id", "app_started", "app_thread", "app_package"})


def _path(value):
    if not isinstance(value, str) or not ntpath.isabs(value) or value.startswith(("\\\\", "//")):
        raise ValueError("invalid executable path")
    return ntpath.normcase(ntpath.normpath(value))


class NativeWindows:
    """Small read-only Win32 provider. Process ownership is checked separately."""
    def __init__(self):
        if os.name != "nt": raise OSError("hosted windows require Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.user.EnumChildWindows.argtypes = [wintypes.HWND, self.callback, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.GetPackageFullName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        self.kernel.GetPackageFullName.restype = wintypes.LONG
        self.kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]

    def trusted_host_exe(self):
        text = ctypes.create_unicode_buffer(32768)
        count = self.kernel.GetWindowsDirectoryW(text, len(text))
        if not 0 < count < len(text): raise OSError("cannot identify Windows directory")
        return ntpath.join(text.value, "System32", "ApplicationFrameHost.exe")

    def _enumerate(self, parent=None):
        result = []
        @self.callback
        def collect(hwnd, _):
            if len(result) >= 4096: return False
            result.append(int(hwnd))
            return True
        if parent is None:
            if not self.user.EnumWindows(collect, 0): raise OSError("window enumeration failed")
        else:
            self.user.EnumChildWindows(parent, collect, 0)
        if len(result) >= 4096: raise OSError("window enumeration limit exceeded")
        return result

    def windows(self): return self._enumerate()
    def children(self, hwnd): return self._enumerate(hwnd)
    def owner(self, hwnd):
        pid = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread or not pid.value: raise OSError("window disappeared")
        return pid.value, int(thread)
    def root(self, hwnd): return int(self.user.GetAncestor(hwnd, 2) or 0)
    def visible(self, hwnd): return bool(self.user.IsWindowVisible(hwnd))
    def class_name(self, hwnd):
        text = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, text, len(text)): raise OSError("window class unavailable")
        return text.value
    def focus(self, thread):
        class GUIThreadInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        self.user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
        self.user.GetGUIThreadInfo.restype = wintypes.BOOL
        info = GUIThreadInfo(); info.cbSize = ctypes.sizeof(info)
        if not self.user.GetGUIThreadInfo(thread, ctypes.byref(info)):
            raise OSError("hosted app focus unavailable")
        return int(info.hwndFocus or 0)
    def password(self, hwnd):
        name = self.class_name(hwnd).casefold()
        self.user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetWindowLongW.restype = wintypes.LONG
        ctypes.set_last_error(0)
        style = self.user.GetWindowLongW(hwnd, -16)
        if not style and ctypes.get_last_error(): raise OSError("focused input style unavailable")
        return (name == "edit" or name.startswith("windowsforms10.edit.")) and bool(style & 0x0020)
    def identity(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process: raise OSError("process identity unavailable")
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(process, *map(ctypes.byref, (created, exited, kernel, user))):
                raise OSError("process creation time unavailable")
            length = wintypes.UINT()
            status = self.kernel.GetPackageFullName(process, ctypes.byref(length), None)
            package = ""
            if status == 122 and 0 < length.value <= 32768:  # ERROR_INSUFFICIENT_BUFFER
                name = ctypes.create_unicode_buffer(length.value)
                if self.kernel.GetPackageFullName(process, ctypes.byref(length), name) != 0:
                    raise OSError("package identity changed")
                package = name.value
            elif status != 15700:  # APPMODEL_ERROR_NO_PACKAGE is normal for the host.
                raise OSError("package identity unavailable")
            return {"started": (created.dwHighDateTime << 32) | created.dwLowDateTime, "package": package}
        finally:
            self.kernel.CloseHandle(process)


def _dependencies(native, process_resolver):
    if process_resolver is None:
        # Also verifies the current Windows SID and session. Keep lazy to avoid
        # a cycle when Guard imports this adapter during native discovery.
        from vendor.guard import windows_process_exe
        process_resolver = windows_process_exe
    return native or NativeWindows(), process_resolver


def _binding(frame, allowed, native, process_resolver):
    if type(frame) is not int or frame <= 0 or native.class_name(frame) != HOST_CLASS:
        return None
    if native.root(frame) != frame or not native.visible(frame): return None
    host_pid, host_thread = native.owner(frame)
    host_exe = _path(process_resolver(host_pid))
    if host_exe != _path(native.trusted_host_exe()): return None
    host = native.identity(host_pid)
    if host.get("package") or type(host.get("started")) is not int or host["started"] <= 0:
        return None
    # Do not silently select one visible app from a frame containing several.
    children = [child for child in native.children(frame)
                if native.class_name(child) == APP_CLASS and native.visible(child)]
    if len(children) != 1: return None
    child = children[0]
    if native.root(child) != frame: return None
    app_pid, app_thread = native.owner(child)
    if app_pid == host_pid: return None
    app_exe = _path(process_resolver(app_pid))
    if app_exe not in allowed: return None
    app = native.identity(app_pid)
    if (type(app.get("started")) is not int or app["started"] <= 0 or
            not isinstance(app.get("package"), str) or not 1 <= len(app["package"]) <= 1000):
        return None
    # Detect close, reparent, PID reuse and identity changes during the read.
    if (native.owner(frame) != (host_pid, host_thread) or native.owner(child) != (app_pid, app_thread)
            or native.root(frame) != frame or native.root(child) != frame
            or native.class_name(frame) != HOST_CLASS or native.class_name(child) != APP_CLASS
            or not native.visible(frame) or not native.visible(child)
            or [item for item in native.children(frame) if native.class_name(item) == APP_CLASS and native.visible(item)] != [child]
            or _path(process_resolver(host_pid)) != host_exe or _path(process_resolver(app_pid)) != app_exe
            or native.identity(host_pid) != host or native.identity(app_pid) != app):
        return None
    return {"pid": host_pid, "window_id": frame, "exe": app_exe, "host_exe": host_exe,
            "host_started": host["started"], "host_thread": host_thread,
            "app_pid": app_pid, "app_window_id": child, "app_started": app["started"],
            "app_thread": app_thread, "app_package": app["package"]}


def discover(allowed_executables, *, native=None, process_resolver=None):
    """Find exact frame bindings to registered package EXEs; no titles are read."""
    try:
        native, process_resolver = _dependencies(native, process_resolver)
        allowed = {_path(path) for path in allowed_executables}
        frames = native.windows()
    except (OSError, RuntimeError, ValueError, AttributeError): return []
    result = []
    for frame in frames:
        try:
            binding = _binding(frame, allowed, native, process_resolver)
            if binding is not None: result.append(binding)
        except (OSError, RuntimeError, ValueError, AttributeError): continue
    return result


def validate(record, allowed_executables, *, native=None, process_resolver=None):
    """Re-prove one original frame binding; never substitute a replacement."""
    if not isinstance(record, dict) or set(record) != FIELDS: return False
    if any(type(record[key]) is not int or record[key] <= 0 for key in
           ("pid", "window_id", "host_started", "host_thread", "app_pid", "app_window_id", "app_started", "app_thread")):
        return False
    try:
        native, process_resolver = _dependencies(native, process_resolver)
        current = _binding(record["window_id"], {_path(path) for path in allowed_executables}, native, process_resolver)
        return current is not None and current == record
    except (OSError, RuntimeError, ValueError, AttributeError): return False


def focus_window(record, allowed_executables, *, native=None, process_resolver=None):
    """Return the original package's focused HWND; never focus or type itself."""
    native, process_resolver = _dependencies(native, process_resolver)
    allowed = tuple(allowed_executables)
    if not validate(record, allowed, native=native, process_resolver=process_resolver):
        raise RuntimeError("hosted window binding changed before keyboard input")
    focus = native.focus(record["app_thread"])
    if (type(focus) is not int or focus <= 0 or native.owner(focus)[0] != record["app_pid"]
            or native.root(focus) != record["window_id"]):
        raise RuntimeError("image keyboard focus is outside the approved hosted app")
    if native.password(focus):
        from vendor.guard import ProtectedImageInput
        raise ProtectedImageInput("비밀번호 입력칸이 선택되어 글자와 키 전송을 중지했습니다. 일반 입력칸을 다시 선택하세요.")
    if (not validate(record, allowed, native=native, process_resolver=process_resolver)
            or native.focus(record["app_thread"]) != focus):
        raise RuntimeError("hosted app or keyboard focus changed during inspection")
    return focus
