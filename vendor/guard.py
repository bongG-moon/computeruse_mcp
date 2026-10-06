"""Narrow, local stdio MCP gate for the Cua RPA trial (Python stdlib only).

This is a tool/target boundary, not an OS sandbox. An allowed application's own
UI can navigate, launch programs, or change data. The caller must make that
limitation clear; a successful driver action is not proof of task success.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import math
import ntpath
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid


class GuardError(RuntimeError):
    pass


class CheckpointRequiresForeground(GuardError):
    """A screenshot may expose another app unless the exact window is visible."""
    pass


class BackgroundShortcutUnavailable(GuardError):
    """Known unsafe delivery mode; no request reached the driver."""
    pass


class ProtectedImageInput(GuardError):
    """A focused native Edit explicitly declares itself a password field."""
    code = "image_protected_input"


KEY_ALIASES = {alias: canonical for canonical, aliases in {
    "ctrl": {"ctrl", "control", "lctrl", "rctrl", "leftctrl", "rightctrl", "leftcontrol", "rightcontrol", "control_l", "control_r"},
    "shift": {"shift", "lshift", "rshift", "leftshift", "rightshift", "shift_l", "shift_r"},
    "alt": {"alt", "lalt", "ralt", "leftalt", "rightalt", "alt_l", "alt_r", "option"},
    "win": {"win", "windows", "super", "cmd", "meta", "command", "lwin", "rwin", "leftwin", "rightwin", "win_l", "win_r"},
}.items() for alias in aliases}
KEY_MODIFIERS = set(KEY_ALIASES) | {"altgr"}


def background_shortcut(name, args):
    if args.get("delivery_mode", "background") != "background":
        return False
    if name == "press_key":
        return bool(args.get("modifiers"))
    if name == "hotkey":
        keys = args.get("keys", [])
        return len(keys) > 1 and any(key.strip().lower() in KEY_MODIFIERS for key in keys)
    return False


OBSERVATIONS = {"list_apps", "list_windows", "get_window_state", "verify_state", "zoom"}
UIA_OBSERVATION_LIMITS = {"max_depth": (12, 100), "max_elements": (600, 20000)}
METRIC_FIELDS = {"duration_ms", "response_size_bytes", "element_count", "returned_element_count", "total_element_count"}
COMMON_TOOLS = {"list_apps", "list_windows", "get_window_state", "bring_to_front",
                "click", "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll"}
MODE_TOOLS = {"uia": {"set_value", "invoke_menu", "verify_state"}, "visual": {"zoom", "drag"}}
PIXEL_KEYS = {"x", "y", "x1", "x2", "y1", "y2", "from_x", "from_y", "to_x", "to_y", "from_zoom"}
ELEMENT_KEYS = {"element_index", "element_token", "snapshot_id"}
TARGET_KEYS = {"pid", "window_id", "session"}
INPUT_KEYS = TARGET_KEYS | {"scope", "target", "delivery_mode"} | ELEMENT_KEYS
ALLOWED_KEYS = {
    "list_apps": set(), "list_windows": {"pid", "on_screen_only"},
    "get_window_state": TARGET_KEYS | {"include_accessibility_tree", "include_screenshot", "max_depth", "max_dimension", "max_elements", "query"},
    "bring_to_front": {"pid", "window_id"},
    "click": INPUT_KEYS | {"x", "y", "from_zoom", "button", "count", "modifier"},
    "double_click": INPUT_KEYS - {"scope", "target"} | {"x", "y", "from_zoom", "modifier"},
    "right_click": INPUT_KEYS - {"scope", "target"} | {"x", "y", "from_zoom", "modifier"},
    "type_text": INPUT_KEYS | {"x", "y", "text", "delay_ms"},
    "press_key": INPUT_KEYS | {"x", "y", "key", "modifiers"},
    "hotkey": INPUT_KEYS | {"x", "y", "keys"},
    "scroll": INPUT_KEYS | {"x", "y", "direction", "amount", "by"},
    "set_value": TARGET_KEYS | ELEMENT_KEYS | {"value"},
    "invoke_menu": TARGET_KEYS | {"path"},
    "verify_state": TARGET_KEYS | {"expect", "include_screenshot", "stable_samples", "timeout_ms"},
    "zoom": {"pid", "window_id", "x1", "y1", "x2", "y2"},
    "drag": INPUT_KEYS - ELEMENT_KEYS | {"from_x", "from_y", "to_x", "to_y", "from_zoom", "duration_ms", "steps", "button", "modifier"},
}
BLOCKED_EXES = {
    "cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe", "windowsterminal.exe",
    "wt.exe", "conhost.exe", "openconsole.exe", "bash.exe", "wsl.exe", "sh.exe",
    "regedit.exe", "regedt32.exe", "mmc.exe", "taskmgr.exe", "control.exe", "systemsettings.exe",
    "securityhealthhost.exe", "sechealthui.exe", "credentialuibroker.exe", "consent.exe",
    "mshta.exe", "wscript.exe", "cscript.exe", "rundll32.exe", "regsvr32.exe", "msiexec.exe",
    "python.exe", "pythonw.exe", "py.exe", "node.exe", "diskpart.exe", "netsh.exe",
    "lockapp.exe", "claude.exe", "codex.exe",
}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_exe(path):
    if not isinstance(path, str) or not ntpath.isabs(path) or not path.lower().endswith(".exe"):
        raise GuardError("App must be an absolute Windows executable path.")
    drive, tail = ntpath.splitdrive(path)
    if "\x00" in path or path.startswith(("\\\\", "//")) or len(drive) != 2 or drive[1] != ":" or ":" in tail:
        raise GuardError("Network/device executable paths are not accepted.")
    canonical = ntpath.normcase(ntpath.normpath(path))
    if os.name == "nt":
        canonical = ntpath.normcase(os.path.realpath(canonical))
    return canonical


def check_app(path):
    canonical = normalize_exe(path)
    basename = ntpath.basename(canonical)
    if basename in BLOCKED_EXES or basename.startswith(("python3", "python2")):
        raise GuardError("Terminal, interpreter, system and security applications are not trial targets.")
    return canonical


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise GuardError("Policy must be a JSON object.")
    if policy.get("mode") not in MODE_TOOLS or policy.get("approval_mode") not in {"each", "run"}:
        raise GuardError("Invalid mode or approval_mode.")
    if policy.get("log_detail", "metadata") not in {"metadata", "content"}:
        raise GuardError("log_detail must be metadata or content.")
    apps = policy.get("allowed_apps")
    if not isinstance(apps, list) or not apps:
        raise GuardError("Choose at least one allowed application.")
    for app in apps:
        check_app(app)
    if type(policy.get("max_actions")) is not int or not 1 <= policy["max_actions"] <= 1000:
        raise GuardError("max_actions must be between 1 and 1000.")
    if not isinstance(policy.get("driver"), str) or not os.path.isabs(policy["driver"]):
        raise GuardError("driver must be an absolute path.")
    if not isinstance(policy.get("run_dir"), str) or not os.path.isabs(policy["run_dir"]):
        raise GuardError("run_dir must be an absolute path.")
    env = policy.get("driver_env", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise GuardError("driver_env must contain string keys and values.")
    for name, default in (("approval_timeout_seconds", 300), ("request_timeout_seconds", 90),
                          ("observation_timeout_seconds", 20)):
        value = policy.get(name, default)
        if type(value) not in (int, float) or not 0 < value <= 1800 or not math.isfinite(value):
            raise GuardError(name + " must be positive and at most 1800.")
    return policy


def windows_process_exe(pid):
    if os.name != "nt":
        raise GuardError("Live target resolution requires Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise GuardError("Cannot inspect target process; access denied or process exited.")
    try:
        if not _same_windows_user_session(pid, handle):
            raise GuardError("Target process belongs to a different user or session.")
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise GuardError("Cannot resolve target executable.")
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def _same_windows_user_session(pid, handle):
    """Fail closed for run-as-other-user/session processes on the same desktop."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    current_session, target_session = wintypes.DWORD(), wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(current_session)) or not kernel.ProcessIdToSessionId(pid, ctypes.byref(target_session)):
        return False
    if current_session.value != target_session.value:
        return False
    class SidAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]
    tokens, buffers = [], []
    try:
        for process in (kernel.GetCurrentProcess(), handle):
            token = wintypes.HANDLE()
            if not advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
                return False
            tokens.append(token)
            required = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            if not required.value:
                return False
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi.GetTokenInformation(token, 1, buffer, required, ctypes.byref(required)):
                return False
            buffers.append(buffer)
        left = ctypes.cast(buffers[0], ctypes.POINTER(SidAttributes)).contents.Sid
        right = ctypes.cast(buffers[1], ctypes.POINTER(SidAttributes)).contents.Sid
        return bool(advapi.EqualSid(left, right))
    finally:
        for token in tokens:
            kernel.CloseHandle(token)


def _win32_window_handles():
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    handles = []
    @callback_type
    def collect(hwnd, _):
        handles.append(int(hwnd))
        return True
    if not user.EnumWindows(collect, 0):
        raise GuardError("Cannot enumerate the current Windows desktop.")
    return handles


def _win32_window_metadata(hwnd):
    """Called only AFTER executable/user/session approval, never before."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetWindow.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    length = min(max(user.GetWindowTextLengthW(hwnd), 0), 8192)
    title = ctypes.create_unicode_buffer(length + 1)
    user.GetWindowTextW(hwnd, title, len(title))
    rect = wintypes.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise GuardError("Window disappeared during discovery.")
    minimized = bool(user.IsIconic(hwnd))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    visible = bool(user.IsWindowVisible(hwnd))
    on_screen = visible and not minimized and width > 0 and height > 0 and bool(user.MonitorFromWindow(hwnd, 0))
    return {"title": title.value, "bounds": {"x": rect.left, "y": rect.top, "width": width, "height": height},
            "minimized": minimized, "is_on_screen": on_screen,
            # These are untrusted candidates until discovery checks their PID,
            # executable, current user and Windows session below.
            "owner_window_id": int(user.GetWindow(hwnd, 4) or 0),
            "root_owner_window_id": int(user.GetAncestor(hwnd, 3) or 0)}


def discover_allowed_windows(policy, args, process_resolver=windows_process_exe,
                             window_resolver=None, handles_provider=None,
                             metadata_provider=None):
    """Local metadata discovery needs no desktop-capture grant from Cua.

    Providers are injectable to prove denied titles are never requested. Default
    process resolution also checks current Windows user SID and session.
    """
    handles_provider = handles_provider or _win32_window_handles
    metadata_provider = metadata_provider or _win32_window_metadata
    window_resolver = window_resolver or windows_window_pid
    rows = []
    for hwnd in handles_provider():
        try:
            pid = window_resolver(hwnd)
            if "pid" in args and args["pid"] != pid:
                continue
            exe = _allowed_pid(pid, policy, process_resolver)
            metadata = dict(metadata_provider(hwnd))
            if args.get("on_screen_only") and not metadata.get("is_on_screen"):
                continue
            for key in ("owner_window_id", "root_owner_window_id"):
                owner = metadata.pop(key, 0)
                metadata[key] = 0
                if type(owner) is not int or owner <= 0:
                    continue
                try:
                    # A relationship is discovery evidence only, never an input
                    # grant. Keep owners from other processes/sessions private.
                    if (window_resolver(owner) == pid and
                            _allowed_pid(pid, policy, process_resolver) == exe and
                            window_resolver(owner) == pid):
                        metadata[key] = owner
                except (GuardError, OSError, ValueError):
                    pass
            # Recheck after metadata retrieval to handle a closed/reused HWND.
            if window_resolver(hwnd) != pid or _allowed_pid(pid, policy, process_resolver) != exe:
                continue
            rows.append({"pid": pid, "window_id": hwnd, "exe": exe,
                         "app_name": ntpath.basename(exe), **metadata})
        except (GuardError, OSError, ValueError):
            continue
    return rows


def local_discovery_result(name, rows):
    if name == "list_windows":
        structured = {"windows": rows}
    else:
        by_pid = {}
        for row in rows:
            app = by_pid.setdefault(row["pid"], {"pid": row["pid"], "name": row["app_name"],
                "exe": row["exe"], "running": True, "windows": []})
            app["windows"].append(row)
        structured = {"apps": list(by_pid.values()),
                      "processes": [{"pid": app["pid"], "name": app["name"], "exe": app["exe"]} for app in by_pid.values()]}
    structured["discovery_source"] = "guard_win32_allowed_window_metadata"
    return {"isError": False, "structuredContent": structured,
            "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}]}


def windows_window_pid(window_id):
    if os.name != "nt":
        raise GuardError("Live window resolution requires Windows.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    owner = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(wintypes.HWND(window_id), ctypes.byref(owner)):
        raise GuardError("Window no longer exists.")
    return owner.value


def windows_checkpoint_ready(window_id):
    """Fail closed unless the exact target is visible, restored and foreground."""
    if os.name != "nt" or type(window_id) is not int or window_id <= 0:
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        function = getattr(user, name)
        function.argtypes, function.restype = [wintypes.HWND], wintypes.BOOL
    user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], wintypes.HWND
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    hwnd = wintypes.HWND(window_id)
    rect = wintypes.RECT()
    if (not user.IsWindow(hwnd) or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd)
            or int(user.GetForegroundWindow() or 0) != window_id
            or not user.GetWindowRect(hwnd, ctypes.byref(rect))
            or rect.right <= rect.left or rect.bottom <= rect.top
            or not user.MonitorFromWindow(hwnd, 0)):
        return False
    # IsWindowVisible can also be true for a cloaked window on another desktop.
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    cloaked = wintypes.DWORD()
    if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0 or cloaked.value:
        return False
    return int(user.GetForegroundWindow() or 0) == window_id and bool(user.IsWindowVisible(hwnd)) and not bool(user.IsIconic(hwnd))


def windows_image_geometry(window_id):
    """Physical rect and DPI awareness, for screenshot-to-input consistency."""
    if os.name != "nt":
        raise GuardError("Image coordinates require Windows.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
    user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        rect = wintypes.RECT()
        if not user.GetWindowRect(window_id, ctypes.byref(rect)):
            raise GuardError("Image target window no longer exists.")
        awareness = user.GetAwarenessFromDpiAwarenessContext(user.GetWindowDpiAwarenessContext(window_id))
        return (rect.left, rect.top, rect.right, rect.bottom, awareness)
    finally:
        if previous:
            user.SetThreadDpiAwarenessContext(previous)


def native_edit_password(class_name, style):
    """Recognize only known native Edit classes; custom fields stay unknown."""
    name = class_name.lower()
    return (name == "edit" or name.startswith("windowsforms10.edit.")) and bool(style & 0x0020)  # ES_PASSWORD


def windows_image_focus(target):
    """Read the exact focused native child without input or clipboard access."""
    if os.name != "nt":
        raise GuardError("Image keyboard focus requires Windows.")
    class GUIThreadInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
    user.GetGUIThreadInfo.restype = wintypes.BOOL
    user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    owner, info = wintypes.DWORD(), GUIThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    thread = user.GetWindowThreadProcessId(target["window_id"], ctypes.byref(owner))
    if not thread or owner.value != target["pid"] or not user.GetGUIThreadInfo(thread, ctypes.byref(info)):
        raise GuardError("Cannot confirm image keyboard focus.")
    focus = int(info.hwndFocus or 0)
    if not focus or int(user.GetAncestor(focus, 2) or 0) != target["window_id"] or windows_window_pid(focus) != target["pid"]:
        raise GuardError("Image keyboard focus is outside the approved window.")
    user.GetClassNameW.argtypes, user.GetClassNameW.restype = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int
    user.GetWindowLongW.argtypes, user.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], wintypes.LONG
    class_name = ctypes.create_unicode_buffer(256)
    if not user.GetClassNameW(focus, class_name, len(class_name)):
        raise GuardError("Cannot inspect the focused image input class.")
    ctypes.set_last_error(0)
    style = user.GetWindowLongW(focus, -16)  # GWL_STYLE is always a 32-bit value.
    if not style and ctypes.get_last_error():
        raise GuardError("Cannot inspect the focused image input style.")
    if native_edit_password(class_name.value, style):
        raise ProtectedImageInput("비밀번호 입력칸이 선택되어 글자와 키 전송을 중지했습니다. 일반 입력칸을 다시 선택하세요.")
    return focus


def _allowed_pid(pid, policy, resolver):
    if type(pid) is not int or pid <= 0:
        raise GuardError("An explicit positive pid is required.")
    exe = check_app(resolver(pid))
    if exe not in {check_app(p) for p in policy["allowed_apps"]}:
        raise GuardError("The current process executable is not an approved app.")
    return exe


def _guard_keys(args, target_exe=""):
    keys = []
    for field in ("keys", "modifiers", "modifier"):
        value = args.get(field, [])
        if not isinstance(value, list) or any(not isinstance(k, str) for k in value):
            raise GuardError("Key modifiers must be arrays of strings.")
        if any("+" in k or "\x00" in k for k in value):
            raise GuardError("Pass individual key names, not encoded key combinations.")
        keys.extend(k.lower().strip() for k in value)
    if "key" in args:
        if not isinstance(args["key"], str):
            raise GuardError("key must be text.")
        if "+" in args["key"] or "\x00" in args["key"]:
            raise GuardError("Pass one key name and a separate modifier list.")
        keys.append(args["key"].lower().strip())
    normalized = {KEY_ALIASES.get(k, k) for k in keys if k != "altgr"}
    if "altgr" in keys:
        normalized.update({"ctrl", "alt"})
    if "win" in normalized:
        raise GuardError("System-wide Windows shortcuts are unavailable.")
    if ({"alt", "tab"} <= normalized or {"alt", "escape"} <= normalized or {"alt", "esc"} <= normalized or
            {"ctrl", "alt", "delete"} <= normalized or {"ctrl", "alt", "del"} <= normalized):
        raise GuardError("System-wide switching/security shortcuts are unavailable.")
    if {"ctrl", "shift", "escape"} <= normalized or {"ctrl", "shift", "esc"} <= normalized:
        raise GuardError("Task Manager shortcut is unavailable.")
    excel_save_as = ntpath.basename(target_exe).lower() == "excel.exe" and normalized == {"f12"}
    if ("f12" in normalized and not excel_save_as) or ("ctrl" in normalized and "shift" in normalized and normalized & {"i", "j", "c"}):
        raise GuardError("Developer console shortcuts are unavailable in screen-only trials.")


def validate_arguments(name, args, policy, process_resolver=windows_process_exe,
                       window_resolver=windows_window_pid):
    """Return normalized driver arguments; fail closed before forwarding a call.

    Resolvers are injectable for tests. Production always resolves live process
    image paths and the HWND owner, rather than trusting discovery text or pids.
    """
    mode = policy.get("mode")
    if mode not in MODE_TOOLS or name not in COMMON_TOOLS | MODE_TOOLS[mode]:
        raise GuardError("Tool is unavailable in this trial mode: " + str(name))
    if not isinstance(args, dict):
        raise GuardError("Tool arguments must be an object.")
    args = copy.deepcopy(args)
    if set(args) - ALLOWED_KEYS[name]:
        raise GuardError("Unexpected or unsafe argument: " + ", ".join(sorted(set(args) - ALLOWED_KEYS[name])))
    if name == "press_key" and (not isinstance(args.get("key"), str) or not args["key"].strip()):
        raise GuardError("press_key requires a nonempty key name.")
    if name == "hotkey" and (not isinstance(args.get("keys"), list) or not args["keys"]):
        raise GuardError("hotkey requires a nonempty key list.")
    if name == "list_apps":
        return args
    if name == "list_windows":
        if "pid" in args:
            _allowed_pid(args["pid"], policy, process_resolver)
        return args
    if args.get("scope", "window") != "window":
        raise GuardError("Desktop scope is unavailable; select an exact approved window.")
    target = args.get("target")
    if target is not None:
        if not isinstance(target, dict) or set(target) != {"kind", "pid", "window_id"} or target["kind"] != "window":
            raise GuardError("target must describe exactly one approved window.")
        for field in ("pid", "window_id"):
            if field in args and args[field] != target[field]:
                raise GuardError("Conflicting target coordinates.")
            args[field] = target[field]
    target_exe = _allowed_pid(args.get("pid"), policy, process_resolver)
    hwnd = args.get("window_id")
    if type(hwnd) is not int or hwnd <= 0 or window_resolver(hwnd) != args["pid"]:
        raise GuardError("window_id must currently belong to the approved pid.")
    if "session" in args and (not isinstance(args["session"], str) or not 1 <= len(args["session"]) <= 128):
        raise GuardError("Invalid session label.")
    if mode == "uia" and set(args) & PIXEL_KEYS:
        raise GuardError("UIA trial does not accept pixel coordinates.")
    if mode == "visual" and set(args) & ELEMENT_KEYS:
        raise GuardError("Visual trial does not accept accessibility element handles.")
    if mode == "visual" and "query" in args:
        raise GuardError("Visual trial does not use accessibility queries.")
    if name == "get_window_state":
        args["include_screenshot"] = mode == "visual"
        args["include_accessibility_tree"] = mode == "uia"
        for field, (default, ceiling) in UIA_OBSERVATION_LIMITS.items():
            if mode == "uia":
                args.setdefault(field, default)
            if field in args and (type(args[field]) is not int or not 1 <= args[field] <= ceiling):
                raise GuardError(f"{field} must be an integer between 1 and {ceiling}.")
        if mode == "visual":
            args.setdefault("max_dimension", 1280)
        if "max_dimension" in args and (type(args["max_dimension"]) is not int or args["max_dimension"] < 1):
            raise GuardError("max_dimension must be a positive integer.")
    if name == "verify_state":
        args["include_screenshot"] = False
    if name in {"click", "double_click", "right_click", "set_value"}:
        if mode == "uia" and not set(args) & {"element_index", "element_token"}:
            raise GuardError("UIA actions need a fresh element handle.")
        if mode == "visual" and not {"x", "y"} <= set(args):
            raise GuardError("Visual click needs window-local screenshot coordinates.")
    if "element_index" in args and "snapshot_id" not in args and "element_token" not in args:
        raise GuardError("element_index requires its snapshot_id.")
    if ("x" in args) != ("y" in args):
        raise GuardError("x and y must be supplied together.")
    if args.get("delivery_mode", "background") not in {"background", "foreground"}:
        raise GuardError("Invalid delivery mode.")
    _guard_keys(args, target_exe)
    return args


def route_for(name, args, mode):
    if name in {"list_apps", "list_windows"}:
        return "filtered_window_discovery"
    if name == "get_window_state":
        return "screenshot_observation" if mode == "visual" else "accessibility_observation"
    if name in {"set_value", "invoke_menu", "verify_state"} or set(args) & ELEMENT_KEYS:
        return "accessibility_targeted"
    if set(args) & PIXEL_KEYS or name == "zoom":
        return "screenshot_coordinate_targeted"
    return "window_scoped_keyboard_or_focus"


def journal_route(name, args, mode):
    if name in OBSERVATIONS or name in {"list_apps", "list_windows"}:
        return "observation"
    if set(args) & ELEMENT_KEYS or name in {"set_value", "invoke_menu"}:
        return "uia"
    if set(args) & PIXEL_KEYS:
        return "visual"
    return "keyboard"


def driver_evidence_text(result):
    """Exclude our recovery advice when classifying the Driver's actual reply."""
    texts = []
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text", ""))
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and set(payload) == {"computer_use_guidance"}:
            continue
        texts.append(text)
    return "\n".join(texts)


def action_delivery_failed(result):
    """A normal JSON-RPC response may still explicitly refuse an action.

    `unverifiable` alone is the real Driver's normal successful SendInput
    response: it requires a later checkpoint, not an automatic input retry.
    Only explicit failure/no-op/partial-delivery evidence stops this pipeline.
    """
    if result.get("isError"):
        return True
    sources = [result.get("structuredContent", {})]
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                sources.append(json.loads(item.get("text", "")))
            except (ValueError, TypeError):
                pass
    for evidence in sources:
        if not isinstance(evidence, dict):
            continue
        if (evidence.get("success") is False or evidence.get("input_sent") is False
                or evidence.get("refusal") not in (None, False, "")
                or evidence.get("effect") in ("refused", "partial", "suspected_noop", "not_applied", "no_effect")
                or evidence.get("status") in ("failed", "error", "refused")
                or evidence.get("code") in ("background_unavailable", "delivery_failed", "permission_required")
                or evidence.get("error_code") in ("background_unavailable", "delivery_failed", "permission_required")):
            return True
        escalation = evidence.get("escalation")
        if isinstance(escalation, dict) and escalation.get("reason") in (
                "route_unavailable", "delivery_failed", "suspected_noop", "permission_required", "background_unavailable"):
            return True
        delivery = evidence.get("delivery")
        if isinstance(delivery, dict) and delivery.get("delivered_count") == 0:
            return True
    return False


def result_summary(result):
    """Bounded, image-free evidence; driver execution is not task verification."""
    text = driver_evidence_text(result)
    summary = {"text": str(redact_images(text))[:4000], "task_verified": False,
               "image_count": sum(c.get("type") == "image" for c in result.get("content", []) if isinstance(c, dict))}
    structured = result.get("structuredContent", {})
    if isinstance(structured, dict):
        for key in ("effect", "verified", "verification", "status", "outcome", "delivery_mode", "backend", "route"):
            if key in structured:
                value = redact_images(structured[key])
                summary[key] = value if isinstance(value, (bool, int, float, type(None))) else str(value)[:500]
        if isinstance(structured.get("escalation"), dict):
            summary["escalation"] = {key: str(redact_images(value))[:500] for key, value in structured["escalation"].items()
                                     if key in {"target", "reason"}}
    return summary


def diagnostic_code(value):
    """Classify diagnostics without copying screen/input text into persistent logs."""
    text = str(value).lower()
    if "no window with window_id" in text and "exists" in text:
        return "target_unavailable"
    for code, fragments in (
        ("stale_observation", ("stale", "reobserve", "expired snapshot", "invalid element token")),
        ("background_unavailable", ("background_unavailable", "background unavailable")),
        ("delivery_failed", ("delivery_failed", "delivery failed", "postmessage ignored", "postmessage wm_keydown/up is ignored")),
        ("driver_timeout", ("timed out", "timeout")),
        ("driver_ended", ("driver process ended", "driver process is not running", "driver connection closed", "driver transport closed", "broken pipe")),
        ("stopped", ("stopped", "취소", "중지")),
        ("action_limit", ("maximum action",)),
        ("target_unavailable", ("window no longer", "process exited", "cannot inspect target")),
        ("target_denied", ("not an approved", "different user", "desktop scope", "unsafe argument", "unavailable")),
    ):
        if any(part in text for part in fragments):
            return code
    return "request_failed"


def metadata_event(kind, fields):
    safe = {key: value for key, value in fields.items() if key in {
        "request_id", "route", "targeting", "mode", "action_count", "success", "pid", "exe", "discovery_source", "approval_id", "driver_tool"}}
    tool = fields.get("tool")
    safe["tool"] = tool if tool in COMMON_TOOLS | MODE_TOOLS["uia"] | MODE_TOOLS["visual"] | {"computer_launch"} else "unavailable"
    for key in METRIC_FIELDS:
        value = fields.get(key)
        if type(value) in (int, float) and 0 <= value <= 2**63 - 1 and math.isfinite(value):
            safe[key] = value
    if "arguments" in fields:
        args = fields["arguments"] if isinstance(fields["arguments"], dict) else {}
        # Keys, menu paths, selectors, values, tokens and predicates can reveal
        # user text. Log only numeric window/position limits and fixed enums.
        numeric = {"pid", "window_id", "x", "y", "x1", "y1", "x2", "y2", "from_x", "from_y", "to_x", "to_y",
                   "amount", "max_depth", "max_dimension", "max_elements", "count", "delay_ms", "duration_ms", "steps", "timeout_ms"}
        safe["arguments"] = {key: value for key, value in args.items()
                             if key in numeric and type(value) in (int, float)}
        for key, options in {"scope": {"window"}, "delivery_mode": {"background", "foreground"},
                             "direction": {"up", "down", "left", "right"}, "button": {"left", "right", "middle"}}.items():
            if isinstance(args.get(key), str) and args[key] in options:
                safe["arguments"][key] = args[key]
        safe["arguments_redacted"] = True
    if "summary" in fields:
        source = fields["summary"]
        summary = {"task_verified": False, "content_omitted": True}
        if isinstance(source, dict):
            for key in ("image_count", "verified"):
                if type(source.get(key)) in (bool, int):
                    summary[key] = source[key]
            for key in ("effect", "status", "outcome", "delivery_mode", "backend", "route"):
                value = source.get(key)
                if isinstance(value, str) and value.lower() in {
                    "confirmed", "unconfirmed", "unverifiable", "unknown", "no_effect", "applied", "not_applied",
                    "success", "failed", "error", "refused", "partial", "suspected_noop", "delivered", "background", "foreground",
                    "uia", "win32", "postmessage", "sendinput", "keyboard", "visual", "observation"}:
                    summary[key] = value.lower()
            if fields.get("success") is False or source.get("effect") in {"unverifiable", "unconfirmed", "unknown"}:
                summary["diagnostic_code"] = diagnostic_code(source)
                if (fields.get("success") is not False and summary["diagnostic_code"] == "request_failed"
                        and source.get("effect") in {"unverifiable", "unconfirmed", "unknown"}):
                    summary["diagnostic_code"] = "effect_unconfirmed"
            if isinstance(source.get("escalation"), dict):
                escalation = source["escalation"]
                summary["escalation"] = {key: value for key, value in escalation.items()
                                         if key in {"target", "reason"} and value in {
                                             "foreground", "background", "delivery_failed", "background_unavailable", "verification_failed"}}
        elif kind in {"denied", "error"}:
            summary["diagnostic_code"] = diagnostic_code(source)
        safe["summary"] = summary
    return safe


def action_guidance(result, name):
    """Preserve driver evidence and add recovery guidance, never replay an input."""
    if name in OBSERVATIONS and not result.get("isError"):
        return result
    result = copy.deepcopy(result)
    if name not in OBSERVATIONS and action_delivery_failed(result):
        result["isError"] = True
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        structured = {}
    texts = driver_evidence_text(result)
    guidance = {"task_verified": False,
                "next_step": "같은 창을 get_window_state로 다시 관찰하고 요청한 결과가 실제로 나타났는지 확인하세요. 전달 성공만으로 작업 완료를 판단하지 마세요."}
    if result.get("isError"):
        guidance["diagnostic_code"] = diagnostic_code(texts + json.dumps(structured, ensure_ascii=False))
        guidance["diagnostic"] = texts[:4000]
        if guidance["diagnostic_code"] == "stale_observation":
            guidance["next_step"] = "get_window_state로 해당 창을 다시 관찰한 뒤 새 element_token 또는 snapshot_id를 사용하세요. 이전 입력이 적용됐는지 확인한 후 필요한 동작만 요청하세요."
        elif guidance["diagnostic_code"] == "target_unavailable":
            guidance["next_step"] = "list_windows로 현재 창을 다시 식별하세요. 별도 대화상자의 창을 관찰할 수 없다면 허용된 부모 창을 get_window_state로 다시 관찰해 대화상자 요소가 포함되어 있는지 확인하세요. 자동 재시도하지 않습니다."
        elif guidance["diagnostic_code"] == "driver_ended" or "runtime was terminated" in texts.lower():
            guidance["recovery_kind"] = "restart_session"
            guidance["automatic_retry"] = False
            guidance["next_step"] = "Driver 연결이 종료됐습니다. 같은 연결로 다시 읽지 마세요. computer_begin으로 새 세션을 시작하고 list_windows로 창을 확인한 뒤 새로 관찰하세요. 이전 변경이 적용됐는지 확인하기 전에는 입력을 반복하지 마세요."
        elif guidance["diagnostic_code"] == "driver_timeout":
            guidance["recovery_kind"] = "fresh_observation" if name in OBSERVATIONS else "verify_before_action"
            guidance["automatic_retry"] = False
            guidance["next_step"] = "시간 초과만으로 동작 적용 여부를 판단하지 마세요. 세션이 active인지 확인하고 list_windows로 창을 다시 식별하세요. UIA 읽기는 max_depth/max_elements를 줄여 한 번 새로 관찰할 수 있습니다. 다시 실패하면 반복을 멈추고 연결 복구 또는 이미지 관찰을 검토하세요. 이전 변경 동작은 자동으로 재실행하지 마세요."
    if structured.get("effect") in {"unverifiable", "unconfirmed", "unknown"} or structured.get("escalation"):
        guidance["next_step"] += " background_unavailable로 명확히 거절됐거나 관찰 결과 동작이 적용되지 않았음을 확인한 경우에만 새 관찰 후 foreground 방식을 검토하세요. 텍스트 입력 등 중복 실행될 수 있는 동작을 자동 재시도하지 마세요."
    structured["computer_use_guidance"] = guidance
    result["structuredContent"] = structured
    result.setdefault("content", []).append({"type": "text", "text": json.dumps({"computer_use_guidance": guidance}, ensure_ascii=False)})
    return result


def observation_guidance(result, name, args, mode):
    """A bounded tree is not proof that a missing control does not exist."""
    if name != "get_window_state" or mode != "uia" or result.get("isError"):
        return result
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return result
    evidence_incomplete = (structured.get("elements_complete") is False or
                           structured.get("truncated") is True or structured.get("is_truncated") is True)
    total, returned = structured.get("total_element_count"), structured.get("returned_element_count")
    if type(total) is int and type(returned) is int and returned < total:
        evidence_incomplete = True
    structured["observation_limits"] = {
        "max_depth": args["max_depth"], "max_elements": args["max_elements"],
        "query_applied": bool(args.get("query")), "incomplete_reported": evidence_incomplete,
        "next_step": "이 결과는 탐색 범위가 제한되어 있습니다. 대상을 찾지 못했으면 없다고 단정하지 말고 max_depth/max_elements를 늘려 다시 관찰하세요. query를 사용했다면 검색 조건도 확인하세요. Driver가 보고한 원본 개수와 완전성 정보는 그대로 유지됩니다."}
    result["content"] = [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}]
    return result


def add_metrics(result, started):
    """Expose only elapsed time, response size and numeric tree counts."""
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        structured = {}
    # Ignore similarly named, untrusted driver fields before computing our own.
    structured.pop("computer_use_metrics", None)
    metrics = {"response_size_bytes": len(json.dumps(result, ensure_ascii=False).encode("utf-8"))}
    for field in ("element_count", "returned_element_count", "total_element_count"):
        value = structured.get(field)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            metrics[field] = value
    if "element_count" not in metrics and isinstance(structured.get("elements"), list):
        metrics["element_count"] = len(structured["elements"])
    metrics["duration_ms"] = round(max(0, time.monotonic() - started) * 1000, 3)
    structured["computer_use_metrics"] = metrics
    result["structuredContent"] = structured
    return metrics


def redact_images(value):
    """Keep evidence metadata, never raw base64 images in the action journal."""
    if isinstance(value, list):
        return [redact_images(v) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "image", "mimeType": value.get("mimeType"), "data": "[image omitted]"}
        return {k: ("[image omitted]" if k.lower() in {"base64", "image_base64", "screenshot_base64", "png_base64"}
                    else redact_images(v)) for k, v in value.items()}
    if isinstance(value, str):
        if value.startswith("data:image/") or (len(value) > 4096 and value.startswith(("iVBOR", "/9j/"))):
            return "[image omitted]"
        if value.startswith(("{", "[")):
            try:
                return json.dumps(redact_images(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
    return value


def _strip_modality(value, mode):
    if isinstance(value, list):
        return [_strip_modality(v, mode) for v in value
                if not (mode == "uia" and isinstance(v, dict) and v.get("type") == "image")]
    if isinstance(value, dict):
        banned = ({"elements", "tree_markdown", "accessibility_tree", "element_index", "element_token", "snapshot_id",
                   "total_element_count", "returned_element_count"} if mode == "visual" else
                  {"screenshot", "screenshot_base64", "image_base64", "screenshot_file_path", "screenshot_path"})
        return {k: _strip_modality(v, mode) for k, v in value.items() if k not in banned}
    return value


def filter_result(name, result, policy, process_resolver=windows_process_exe):
    if not isinstance(result, dict):
        raise GuardError("Driver returned malformed tool result.")
    result = copy.deepcopy(result)
    mode = policy["mode"]
    if result.get("isError"):
        # A refusal has no discovery rows. Rebuilding it as an empty list hides
        # the actual permissions/configuration error and encourages blind retries.
        diagnostic = [{"type": "text", "text": str(redact_images(c.get("text", "")))[:4000]}
                      for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        return {"isError": True, "content": diagnostic or [{"type": "text", "text": "Driver refused this request."}],
                "structuredContent": redact_images(_strip_modality(result.get("structuredContent", {}), mode))}
    structured = result.get("structuredContent")
    if name in {"list_apps", "list_windows"}:
        if not isinstance(structured, dict):
            raise GuardError("Driver discovery has no structured result; refusing unfiltered text.")
        filtered = {}
        for key in ("apps", "processes") if name == "list_apps" else ("windows", "_legacy_windows"):
            items = []
            for item in structured.get(key, []):
                if not isinstance(item, dict):
                    continue
                try:
                    _allowed_pid(item.get("pid"), policy, process_resolver)
                except (GuardError, OSError):
                    continue
                item = copy.deepcopy(item)
                if isinstance(item.get("windows"), list):
                    item["windows"] = [w for w in item["windows"] if isinstance(w, dict) and w.get("pid", item["pid"]) == item["pid"]]
                items.append(item)
            filtered[key] = items
        result = {"structuredContent": filtered, "content": [{"type": "text", "text": json.dumps(filtered, ensure_ascii=False)}],
                  "isError": bool(result.get("isError", False))}
    else:
        result = _strip_modality(result, mode)
        # Window observation has legacy text containing the full UIA tree. Never
        # relay it in visual mode, even if the structured tree has been removed.
        if name == "get_window_state":
            clean = result.get("structuredContent", {})
            if not clean and result.get("isError"):
                return {"isError": True, "content": [{"type": "text", "text": "Window observation failed; inspect the local driver log."}]}
            images = [c for c in result.get("content", []) if c.get("type") == "image"] if mode == "visual" else []
            result["content"] = [{"type": "text", "text": json.dumps(clean, ensure_ascii=False)}] + images
        else:
            # Some tool text is serialized JSON; sanitize that copy too.
            for content in result.get("content", []):
                if content.get("type") == "text":
                    try:
                        content["text"] = json.dumps(_strip_modality(json.loads(content["text"]), mode), ensure_ascii=False)
                    except (ValueError, TypeError, KeyError):
                        pass
    result.pop("_meta", None)
    return result


def atomic_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class DriverTransport:
    """Serial JSON-lines MCP child transport; fake Popen-compatible child injectable."""
    def __init__(self, policy, child=None, process_factory=subprocess.Popen):
        self.policy = policy
        self.run_dir = Path(policy["run_dir"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.stop_path = self.run_dir / "stop.flag"
        self.closed = threading.Event()
        self.responses = queue.Queue()
        self.serial = 0
        self.lock = threading.Lock()
        if self.stop_path.exists():
            raise GuardError("Run has already been stopped.")
        if child is None:
            env = dict(os.environ)
            env.update(policy.get("driver_env", {}))
            env.update({"CUA_DRIVER_RS_TELEMETRY_ENABLED": "false", "CUA_DRIVER_RS_UPDATE_CHECK": "false",
                        "DO_NOT_TRACK": "1", "CUA_TELEMETRY": "0"})
            # This direct child owns its runtime. Its full-desktop transparent
            # cursor overlay otherwise contaminates native picking/recording
            # occlusion checks. Disable only this child's overlay, keeping all
            # ordinary window-occlusion checks and tool permissions unchanged.
            child = process_factory([policy["driver"], "mcp", "--direct", "--no-overlay"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.child = child
        threading.Thread(target=self._read_stdout, daemon=True).start()
        if self.child.stderr is not None:
            threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._watch_stop, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.child.stdout:
                try:
                    self.responses.put(json.loads(line))
                except (ValueError, TypeError):
                    self.responses.put(GuardError("Driver emitted malformed JSON."))
                    break
        finally:
            self.responses.put(GuardError("Driver process ended."))

    def _read_stderr(self):
        with (self.run_dir / "driver-stderr.log").open("a", encoding="utf-8") as out:
            for line in self.child.stderr:
                # Diagnostics are not MCP evidence and may contain app content.
                # Suppress lines resembling encoded image payloads.
                if self.policy.get("log_detail", "metadata") != "content":
                    out.write(json.dumps({"time": utc_now(), "event": "driver_stderr", "characters": len(line),
                                          "content_omitted": True}) + "\n")
                    out.flush()
                elif len(line) < 16384 and "data:image/" not in line and '"type":"image"' not in line:
                    out.write(line)
                    out.flush()

    def _watch_stop(self):
        while not self.closed.wait(0.05):
            if self.stop_path.exists():
                self.close()
                return

    def check_running(self):
        if self.stop_path.exists() or self.closed.is_set():
            self.close()
            raise GuardError("Run stopped; no further requests will be forwarded.")
        if self.child.poll() is not None:
            self.close()
            raise GuardError("Driver process is not running.")

    def driver_request(self, method, params=None, timeout=None):
        with self.lock:
            self.check_running()
            self.serial += 1
            request_id = self.serial
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self.child.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.child.stdin.flush()
            deadline = time.monotonic() + (timeout or self.policy.get("request_timeout_seconds", 90))
            while time.monotonic() < deadline:
                self.check_running()
                try:
                    message = self.responses.get(timeout=min(0.05, max(0.001, deadline-time.monotonic())))
                except queue.Empty:
                    continue
                if isinstance(message, Exception):
                    self.close()
                    raise message
                if not isinstance(message, dict):
                    self.close()
                    raise GuardError("Malformed driver message.")
                if message.get("id") == request_id:
                    if "error" in message:
                        raise GuardError("Driver RPC error: " + str(message["error"]))
                    if "result" not in message:
                        raise GuardError("Driver response has no result.")
                    return message["result"]
                if "method" in message and "id" in message:
                    # The child cannot request sampling, elicitation or host I/O.
                    self.child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32601, "message": "Server requests are disabled."}}) + "\n")
                    self.child.stdin.flush()
            self.close()
            raise GuardError("Driver request timed out; runtime was terminated.")

    def notify(self, method):
        with self.lock:
            self.check_running()
            self.child.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
            self.child.stdin.flush()

    def close(self):
        self.closed.set()
        if self.child.poll() is None:
            try:
                self.child.kill()
            except OSError:
                pass
        try:
            self.child.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


class Guard:
    def __init__(self, policy, transport=None, process_resolver=windows_process_exe,
                 window_resolver=windows_window_pid, discovery_provider=None, checkpoint_ready_resolver=None,
                 image_geometry_resolver=None, image_focus_resolver=None):
        self.policy = validate_policy(policy)
        self.run_dir = Path(policy["run_dir"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.transport = transport or DriverTransport(policy)
        self.process_resolver = process_resolver
        self.window_resolver = window_resolver
        self.discovery_provider = discovery_provider or discover_allowed_windows
        self.checkpoint_ready_resolver = checkpoint_ready_resolver or windows_checkpoint_ready
        self.image_geometry_resolver = image_geometry_resolver or windows_image_geometry
        self.image_focus_resolver = image_focus_resolver or windows_image_focus
        self.action_count = 0
        self.observed_targets = set()

    def log(self, kind, **fields):
        if self.policy.get("log_detail", "metadata") != "content":
            fields = metadata_event(kind, fields)
        with (self.run_dir / "actions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact_images({"time": utc_now(), "event": kind, **fields}), ensure_ascii=False) + "\n")

    def check_stop(self):
        if (self.run_dir / "stop.flag").exists():
            self.transport.close()
            raise GuardError("Run stopped; no further requests will be forwarded.")
        if hasattr(self.transport, "check_running"):
            self.transport.check_running()

    def approve(self, name, args, call_id):
        self.check_stop()
        if self.policy["approval_mode"] == "run":
            return
        request_path = self.run_dir / "approval-request.json"
        response_path = self.run_dir / "approval-response.json"
        response_path.unlink(missing_ok=True)
        request_id = uuid.uuid4().hex
        atomic_json(request_path, {"id": request_id, "tool": name, "arguments": args, "created_at": utc_now()})
        deadline = time.monotonic() + self.policy.get("approval_timeout_seconds", 300)
        try:
            while time.monotonic() < deadline:
                self.check_stop()
                try:
                    answer = json.loads(response_path.read_text(encoding="utf-8-sig"))
                    if isinstance(answer, dict) and answer.get("id") == request_id:
                        if answer.get("approved") is not True:
                            raise GuardError("User declined this action.")
                        self.log("approval", request_id=call_id, tool=name, approval_id=request_id,
                                 route=journal_route(name, args, self.policy["mode"]), success=True,
                                 summary="User approved the current action.")
                        return
                except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                    pass
                time.sleep(0.05)
            raise GuardError("Action approval timed out; no action was sent.")
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

    def tools_list(self):
        self.check_stop()
        result = self.transport.driver_request("tools/list", {})
        exposed = []
        for tool in result.get("tools", []):
            name = tool.get("name")
            if name not in COMMON_TOOLS | MODE_TOOLS[self.policy["mode"]]:
                continue
            tool = copy.deepcopy(tool)
            tool.pop("outputSchema", None)
            schema = tool.get("inputSchema", {})
            properties = schema.get("properties", {})
            allowed = ALLOWED_KEYS[name].copy()
            if self.policy["mode"] == "uia":
                allowed -= PIXEL_KEYS
            else:
                allowed -= ELEMENT_KEYS | {"query"}
            properties = {k: v for k, v in properties.items() if k in allowed}
            properties.pop("target", None)  # use one unambiguous top-level target
            if "scope" in properties:
                properties["scope"] = {"type": "string", "enum": ["window"]}
            if name not in {"list_apps", "list_windows"}:
                for key in ("pid", "window_id"):
                    properties[key] = {"type": "integer", "minimum": 1, "description": "Exact approved live window target."}
            if name == "get_window_state":
                for key, value in (("include_screenshot", self.policy["mode"] == "visual"),
                                   ("include_accessibility_tree", self.policy["mode"] == "uia")):
                    properties[key] = {"type": "boolean", "const": value, "default": value}
            required = [k for k in schema.get("required", []) if k in properties]
            if name not in {"list_apps", "list_windows"}:
                required = sorted(set(required) | {"pid", "window_id"})
            tool["inputSchema"] = {"type": "object", "additionalProperties": False,
                                   "properties": properties, "required": required}
            mode_note = ("Only accessibility handles and window-scoped keys are accepted; images and pixel inputs are disabled."
                         if self.policy["mode"] == "uia" else
                         "Only screenshot-guided coordinates and window-scoped keys are accepted; accessibility handles and tree output are disabled.")
            tool["description"] = ("TRIAL GATE: Only exact approved windows are allowed. " + mode_note +
                " Every mutation needs a fresh successful get_window_state for the same pid and window_id; the observation is consumed by that action."
                " Never use desktop scope, shell, APIs or file edits. Observe after acting; tool success alone is not task success.\n" +
                ("Discovery uses local Win32 window metadata. Only approved running apps in the current user/session are listed. No UI snapshot or input is performed."
                 if name in {"list_apps", "list_windows"} else tool.get("description", "")))
            exposed.append(tool)
        return {"tools": exposed}

    def capture_checkpoint(self, target):
        """One explicit, read-only window image without changing session modality.

        The private visual guard is never exposed to callers. Its sole request
        is get_window_state; it shares this session's transport and live target
        resolvers, but grants no visual inputs or reusable UIA observation.
        """
        started, call_id = time.monotonic(), uuid.uuid4().hex
        self.observed_targets.clear()
        self.log("checkpoint_request", request_id=call_id, tool="get_window_state",
                 route="checkpoint", mode=self.policy["mode"], arguments=target)
        try:
            self.check_stop()
            if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                    or any(type(value) is not int or value <= 0 for value in target.values())):
                raise GuardError("Checkpoint needs exactly one approved pid and window_id.")
            target = copy.deepcopy(target)
            policy = copy.deepcopy(self.policy)
            policy["mode"] = "visual"
            # Checkpoints never persist pixels or screen text, including when
            # the ordinary session opted into verbose action diagnostics.
            policy["log_detail"] = "metadata"
            validate_arguments("get_window_state", target, policy, self.process_resolver, self.window_resolver)
            executable = _allowed_pid(target["pid"], self.policy, self.process_resolver)
            def require_ready():
                if self.checkpoint_ready_resolver(target["window_id"]) is not True:
                    raise CheckpointRequiresForeground("화면 확인 대상 창을 최소화하지 않은 상태로 맨 앞으로 가져오세요. "
                        "다른 창의 화면 노출을 막기 위해 캡처를 중지했습니다. 창을 표시한 뒤 승인 ID 없이 이어가기로 다시 확인하세요.")
            require_ready()
            visual = Guard(policy, transport=self.transport, process_resolver=self.process_resolver,
                           window_resolver=self.window_resolver, discovery_provider=self.discovery_provider,
                           checkpoint_ready_resolver=self.checkpoint_ready_resolver)
            answer = visual.call("get_window_state", target)
            self.check_stop()
            require_ready()
            # A reused HWND/PID, even for another allowed program, invalidates
            # the image. Do not forward pixels until ownership is rechecked.
            if (self.window_resolver(target["window_id"]) != target["pid"]
                    or _allowed_pid(target["pid"], self.policy, self.process_resolver) != executable
                    or self.window_resolver(target["window_id"]) != target["pid"]):
                raise GuardError("Checkpoint window changed or is no longer the approved target.")
            if not isinstance(answer, dict) or answer.get("isError"):
                raise GuardError("Checkpoint screenshot failed; no image or input was forwarded.")
            payload = answer.get("structuredContent", {})
            if not isinstance(payload, dict) or any(key in payload and payload[key] != value for key, value in target.items()):
                raise GuardError("Checkpoint screenshot returned a different window target.")
            images = [item for item in answer.get("content", []) if isinstance(item, dict) and item.get("type") == "image"]
            if (not 1 <= len(images) <= 2 or any(not isinstance(item.get("data"), str) or not item["data"]
                    or len(item["data"]) > 16*1024*1024 or item.get("mimeType") not in {"image/png", "image/jpeg", "image/webp"}
                    for item in images)):
                raise GuardError("Checkpoint did not return a supported window image.")
            result = {"structuredContent": {**target, "capture_scope": "window", "read_only": True,
                        "image_verified": False, "task_verified": False, "input_dispatched": False},
                      "content": [{key: copy.deepcopy(item[key]) for key in ("type", "data", "mimeType")} for item in images]}
            self.check_stop()
            require_ready()
            metrics = add_metrics(result, started)
            self.log("checkpoint_result", request_id=call_id, tool="get_window_state", route="checkpoint",
                     success=True, action_count=self.action_count, arguments=target,
                     summary={"image_count": len(images), "task_verified": False}, **metrics)
            return result
        except (GuardError, OSError, ValueError) as error:
            result = {"isError": True, "structuredContent": {"task_verified": False, "input_dispatched": False},
                      "content": [{"type": "text", "text": str(error)}]}
            if isinstance(error, CheckpointRequiresForeground):
                result["structuredContent"]["error_code"] = "checkpoint_requires_foreground"
            metrics = add_metrics(result, started)
            self.log("checkpoint_denied", request_id=call_id, tool="get_window_state", route="checkpoint",
                     success=False, summary=str(error), **metrics)
            return result
        finally:
            self.observed_targets.clear()

    def image_action(self, step, target, matcher, check_active):
        """Private saved-image entry; never grants arbitrary coordinates to UIA callers.

        The matcher receives only the current Driver PNG. A match is used once
        against the identical live window geometry; no old location is retained.
        """
        from image_steps import validate_image_step
        from image_targets import png_dimensions
        from operations import OperationError
        step = validate_image_step(step)
        before = self.action_count
        visual = None
        expected_focus = None
        self.observed_targets.clear()
        def result(code, message, *, verified=False, deferred=False):
            return {"operation": step["operation"], "status": "verified" if verified else "needs_review",
                    "task_verified": verified, "input_dispatched": self.action_count > before,
                    "verification_deferred": deferred,
                    "diagnostic": {"code": code, "message": message, "automatic_replay": False}}
        try:
            check_active()
            if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                    or any(type(v) is not int or v < 1 for v in target.values())):
                raise OperationError("이미지를 찾을 정확한 현재 창이 필요합니다.", "invalid_target")
            policy = copy.deepcopy(self.policy)
            policy.update(mode="visual", log_detail="metadata")
            validate_arguments("get_window_state", target, policy, self.process_resolver, self.window_resolver)
            executable = _allowed_pid(target["pid"], self.policy, self.process_resolver)
            geometry = self.image_geometry_resolver(target["window_id"])
            if not isinstance(geometry, tuple) or len(geometry) != 5 or geometry[4] not in {1, 2}:
                raise OperationError("이 프로그램의 DPI 비인식 화면은 Driver 캡처와 클릭 좌표가 어긋날 수 있어 이미지 입력을 중지했습니다.", "image_dpi_unsupported")
            def ready():
                check_active()
                self.check_stop()
                if self.checkpoint_ready_resolver(target["window_id"]) is not True:
                    raise OperationError("이미지 작업 대상 창을 최소화하지 않은 상태로 맨 앞으로 가져오세요.", "image_requires_foreground")
                if (self.window_resolver(target["window_id"]) != target["pid"]
                        or _allowed_pid(target["pid"], self.policy, self.process_resolver) != executable
                        or self.image_geometry_resolver(target["window_id"]) != geometry):
                    raise OperationError("이미지를 읽은 뒤 창의 위치·크기·소유 프로그램이 달라졌습니다. 다시 관찰해야 합니다.", "image_target_changed")
                if expected_focus is not None and self.image_focus_resolver(target) != expected_focus:
                    raise OperationError("입력 대상의 포커스가 달라졌습니다. 후속 키와 글자는 보내지 않았습니다.", "image_focus_changed")
            ready()
            visual = Guard(policy, transport=self.transport, process_resolver=self.process_resolver,
                           window_resolver=self.window_resolver, discovery_provider=self.discovery_provider,
                           checkpoint_ready_resolver=self.checkpoint_ready_resolver,
                           image_geometry_resolver=self.image_geometry_resolver, image_focus_resolver=self.image_focus_resolver)
            visual.action_count = self.action_count
            def approve(name, args, call_id):
                self.approve(name, args, call_id)
                ready()  # Human approval may have changed the foreground/geometry.
            visual.approve = approve
            def observe_image():
                ready()
                answer = visual.call("get_window_state", target)
                ready()
                images = [c for c in answer.get("content", []) if isinstance(c, dict) and c.get("type") == "image"]
                if answer.get("isError") or len(images) != 1 or images[0].get("mimeType") != "image/png":
                    raise OperationError("현재 창의 PNG 화면을 읽지 못했습니다.", "image_capture_failed")
                png = images[0].get("data")
                width, height = png_dimensions(png)
                metadata = answer.get("structuredContent", {})
                if (any(metadata.get(k) != v for k, v in target.items())
                        or metadata.get("screenshot_width") != width or metadata.get("screenshot_height") != height):
                    raise OperationError("Driver 캡처의 대상 또는 실제 이미지 크기가 일치하지 않습니다.", "image_coordinate_mismatch")
                return png, width, height
            def locate():
                png, width, height = observe_image()
                matched = matcher(step["image_target"], png)
                ready()
                if matched.get("status") != "matched":
                    code = "image_ambiguous" if matched.get("status") == "ambiguous" else "image_not_found"
                    raise OperationError("같은 이미지가 여러 곳에 있습니다. 주변의 구별되는 내용까지 다시 선택하세요." if code == "image_ambiguous" else "현재 화면에서 저장한 이미지를 찾지 못했습니다.", code)
                if (matched.get("screenshot") != {"width": width, "height": height}
                        or any(type(matched.get(k)) is not int for k in ("x", "y"))
                        or not 0 <= matched["x"] < width or not 0 <= matched["y"] < height):
                    raise OperationError("이미지 비교 위치가 현재 캡처 범위를 벗어났습니다.", "image_coordinate_mismatch")
                return {"x": matched["x"], "y": matched["y"]}
            point = locate()
            if step["operation"] == "wait_for_image":
                return result("image_appeared", "현재 창에서 저장한 이미지를 하나로 확인했습니다.", verified=True)
            operation = step["operation"][6:]
            def dispatch(name, args, *, require_same_window_after=True):
                ready()
                try:
                    answer = visual.call(name, {**target, "delivery_mode": "foreground", **args})
                finally:
                    self.action_count = visual.action_count
                if answer.get("isError"):
                    raise OperationError("이미지 대상의 입력 전달을 확인하지 못했습니다. 같은 동작을 반복하지 않습니다.", "image_input_unconfirmed")
                if require_same_window_after:
                    ready()
            if operation in {"type_text", "press_key", "hotkey"}:
                dispatch("click", point)
                expected_focus = self.image_focus_resolver(target)
                if type(expected_focus) is not int or expected_focus < 1:
                    raise OperationError("입력칸의 포커스를 확인하지 못했습니다.", "image_focus_changed")
                # Focus can legitimately change the selection highlight/caret
                # and therefore the template pixels. Refresh the image while
                # retaining the exact focused child and unchanged target. The
                # pre-click match already established the input location; no
                # second match or click is allowed to undo that focus/selection.
                observe_image()
            # Driver x/y forms perform their own focus click. Once the explicit
            # click above established focus, omit x/y for key/text dispatches so
            # a later Ctrl+A selection is not accidentally cleared by a re-click.
            args = {} if operation in {"type_text", "press_key", "hotkey"} else dict(point)
            if operation == "type_text":
                if step.get("replace_all", False):
                    dispatch("hotkey", {"keys": ["CTRL", "A"]})
                    # Refresh the consumed observation, retaining the exact
                    # focused native child. Selection highlight can legitimately
                    # change pixels; no re-click or re-match follows Ctrl+A.
                    observe_image()
                    if not step["value"]:
                        operation = "press_key"
                        args["key"] = "DELETE"
                    else:
                        args["text"] = step["value"]
                else:
                    args["text"] = step["value"]
            elif operation == "press_key":
                args["key"] = step["key"]
            elif operation == "hotkey":
                args["keys"] = step["keys"]
            elif operation == "scroll":
                args.update(direction=step["direction"], amount=step["amount"])
            # A successfully delivered final input can legitimately open a new
            # window, move/close the target, or change the foreground. Those are
            # possible effects, not a reason to discard the delivery record and
            # strand this step as an uncertain input. The mandatory following
            # checkpoint still requires a freshly bound, visible approved window.
            # The intermediate focus click above retains its post-input ready(),
            # exact focused-child check and fresh image before text/key dispatch.
            dispatch(operation, args, require_same_window_after=False)
            return result("image_input_awaiting_review", "이미지 위치로 입력을 한 번 전달했습니다. 다음 화면 확인 단계에서 실제 결과를 확인해야 합니다.", deferred=True)
        except (GuardError, OperationError, OSError, ValueError) as error:
            return result(getattr(error, "code", "image_guard_failed"), str(error))
        finally:
            if visual is not None:
                self.action_count = visual.action_count
            self.observed_targets.clear()

    def call(self, name, arguments):
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        safe_arguments = arguments if isinstance(arguments, dict) else {}
        route = journal_route(name, safe_arguments, self.policy["mode"])
        targeting = route_for(name, safe_arguments, self.policy["mode"])
        self.log("request", request_id=call_id, tool=name, arguments=arguments, route=route,
                 targeting=targeting, mode=self.policy["mode"])
        try:
            self.check_stop()
            args = validate_arguments(name, arguments, self.policy, self.process_resolver, self.window_resolver)
            target_key = (args.get("pid"), args.get("window_id"))
            if background_shortcut(name, args):
                # Some Windows applications receive only the bare character
                # from a background hotkey. A refusal is safer than changing
                # document contents. Do not retry or switch delivery modes.
                self.observed_targets.discard(target_key)
                raise BackgroundShortcutUnavailable(
                    "background_unavailable: 배경 전달에서는 수정키가 누락되어 일반 문자가 입력될 수 있어 요청을 전달하지 않았습니다. "
                    "입력은 전송되지 않았습니다. get_window_state로 창을 새로 관찰한 뒤 화면 메뉴를 사용하거나 "
                    "delivery_mode='foreground'를 명시하여 필요한 조합을 다시 요청하세요. 자동 전환이나 재입력은 하지 않습니다.")
            if name not in OBSERVATIONS:
                if target_key not in self.observed_targets:
                    raise GuardError("Reobserve this exact window with get_window_state(pid, window_id) before the next action.")
                if self.action_count >= self.policy["max_actions"]:
                    raise GuardError("Maximum action count reached.")
                self.approve(name, args, call_id)
                # Approval may have taken minutes: resolve PID/HWND again.
                args = validate_arguments(name, args, self.policy, self.process_resolver, self.window_resolver)
            self.check_stop()
            target_evidence = {}
            if name not in {"list_apps", "list_windows"}:
                target_evidence = {"pid": args["pid"],
                                   "exe": _allowed_pid(args["pid"], self.policy, self.process_resolver)}
            if name in {"list_apps", "list_windows"}:
                rows = self.discovery_provider(self.policy, args, self.process_resolver, self.window_resolver)
                result = local_discovery_result(name, rows)
                target_evidence["discovery_source"] = "guard_win32_allowed_window_metadata"
            else:
                if name == "get_window_state":
                    self.observed_targets.discard(target_key)
                if name not in OBSERVATIONS:
                    self.action_count += 1
                try:
                    driver_name, driver_args = name, args
                    if name == "press_key" and args.get("modifiers"):
                        # Only explicit foreground modifier requests reach this
                        # branch. Normalize them to one hotkey dispatch; both
                        # background paths were refused above. Never replay.
                        driver_name = "hotkey"
                        driver_args = {k: v for k, v in args.items() if k not in {"key", "modifiers"}}
                        driver_args["keys"] = [*args["modifiers"], args["key"]]
                        driver_args = validate_arguments(driver_name, driver_args, self.policy, self.process_resolver, self.window_resolver)
                        target_evidence["driver_tool"] = driver_name
                    request = {"name": driver_name, "arguments": driver_args}
                    if name in OBSERVATIONS and isinstance(self.transport, DriverTransport):
                        raw = self.transport.driver_request("tools/call", request,
                            timeout=self.policy.get("observation_timeout_seconds", 20))
                    else:
                        raw = self.transport.driver_request("tools/call", request)
                finally:
                    if name not in OBSERVATIONS:
                        # Once dispatched, even an error may have changed the UI.
                        self.observed_targets.discard(target_key)
                result = action_guidance(filter_result(name, raw, self.policy, self.process_resolver), name)
                result = observation_guidance(result, name, args, self.policy["mode"])
                if name == "get_window_state" and not result.get("isError"):
                    self.observed_targets.add(target_key)
            metrics = add_metrics(result, started)
            self.log("result", request_id=call_id, tool=name, route=route, targeting=targeting,
                      action_count=self.action_count, success=not bool(result.get("isError")),
                      summary=result_summary(result), **target_evidence, **metrics)
            return result
        except (GuardError, OSError, ValueError) as error:
            refused = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            if isinstance(error, BackgroundShortcutUnavailable):
                refused["structuredContent"] = {"error_code": "background_unavailable", "input_sent": False,
                                                "effect": "not_applied", "automatic_retry": False}
            refused = action_guidance(refused, name)
            metrics = add_metrics(refused, started)
            self.log("denied" if isinstance(error, GuardError) else "error", request_id=call_id,
                     tool=name, route=route, success=False, summary=str(error), **metrics)
            return refused

    def handle(self, message):
        method = message.get("method")
        self.check_stop()
        if method == "initialize":
            result = self.transport.driver_request(method, message.get("params", {}))
            result["capabilities"] = {"tools": {}}
            result["serverInfo"] = {"name": "cua-rpa-trial-guard", "version": "1.0.0"}
            result["instructions"] = ("Use only listed tools and approved live windows. Observe before and after actions. "
                "Do not use API, shell, file editing, clipboard injection, developer consoles or external tools as screen automation. "
                "A coordinate-targeted request may internally use UIA; route labels describe target selection, not guaranteed input backend.")
            return result
        if method == "notifications/initialized":
            self.transport.notify(method)
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return self.tools_list()
        if method == "tools/call":
            params = message.get("params", {})
            return self.call(params.get("name", ""), params.get("arguments", {}))
        if method.startswith("notifications/"):
            return None
        raise GuardError("MCP method is unavailable: " + str(method))


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    options = parser.parse_args(argv)
    guard = None
    try:
        policy = json.loads(Path(options.policy).read_text(encoding="utf-8-sig"))
        guard = Guard(policy)
        for line in sys.stdin:
            message = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or not isinstance(message.get("method"), str):
                    raise GuardError("Invalid MCP message.")
                result = guard.handle(message)
                if "id" not in message:
                    continue
                response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            except Exception as error:
                if isinstance(message, dict) and "id" not in message:
                    continue
                response = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                            "error": {"code": -32603, "message": str(error)}}
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception as error:
        print("CUA trial gate failed: " + str(error), file=sys.stderr)
        return 1
    finally:
        if guard is not None:
            guard.transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
