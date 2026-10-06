"""Read the current Windows token; no elevation, policy, or profile changes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


def execution_privileges() -> dict:
    result = {"administrator": None, "elevated": None, "integrity": "unknown",
              "session_id": None, "checked": False,
              "message": "실행 권한을 확인하지 못했습니다."}
    if os.name != "nt":
        return result
    token = wintypes.HANDLE()
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return result
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            return result
        required = wintypes.DWORD()
        elevation = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(required)):
            return result
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(required))
        if not 0 < required.value < 65536:
            return result
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(token, 25, buffer, required, ctypes.byref(required)):
            return result
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        count = advapi.GetSidSubAuthorityCount(sid).contents.value
        if not count:
            return result
        rid = advapi.GetSidSubAuthority(sid, count - 1).contents.value
        # Membership alone can be true for a disabled administrative group.
        # A genuine elevated token and high IL are required by this status flag.
        elevated = bool(elevation.value)
        integrity = "system" if rid >= 0x4000 else "high" if rid >= 0x3000 else "medium" if rid >= 0x2000 else "low"
        administrator = elevated and rid >= 0x3000
        return {"administrator": administrator, "elevated": elevated, "integrity": integrity,
                "session_id": session.value, "checked": True,
                "message": "MCP와 새로 실행하는 Driver는 관리자 권한으로 동작합니다." if administrator else
                           "현재 MCP는 일반 권한입니다. 관리자 프로그램을 조작하려면 관리자 연결 프로그램으로 다시 연결하세요."}
    except (OSError, ValueError):
        return result
    finally:
        if token:
            kernel.CloseHandle(token)
