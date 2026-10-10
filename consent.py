"""Native, human-controlled consent and desktop ownership for the local MCP.

No MCP or model value can substitute for a dialog response. This module writes
no protocol output to stdout. The only process it terminates is its own dialog.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import uuid


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _timeout(config: dict) -> float:
    value = config.get("approval_timeout_seconds", config.get("consent_timeout_seconds", 300))
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            return 300.0
        return min(seconds, 3600.0)
    except (TypeError, ValueError, OverflowError):
        return 300.0


def _response_approved(path: Path, nonce: str) -> bool:
    try:
        if path.stat().st_size > 16_384:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(value, dict) and value.get("nonce") == nonce
                and type(value.get("approved")) is bool and value["approved"])
    except (OSError, ValueError, TypeError):
        return False


def _dialog_python() -> str:
    current = Path(sys.executable)
    # The distribution includes its own Python; use that same environment.
    windowed = current.with_name("pythonw.exe")
    if os.name == "nt" and current.name.lower().startswith("python") and windowed.is_file():
        return str(windowed)
    return str(current)


class ConsentBroker:
    """Serialize native prompts; fail closed on stop, malformed responses or exit."""

    def __init__(self, config: dict, run_dir: Path):
        self.config = dict(config)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._closed = threading.Event()
        self._confirm_lock = threading.Lock()
        self._children_lock = threading.Lock()
        self._children: dict[int, subprocess.Popen] = {}
        self._pending_exchanges: dict[int, tuple[Path, Path]] = {}
        self.last_error = ""

    def _stopped(self, stop_event: threading.Event) -> bool:
        return self._closed.is_set() or stop_event.is_set() or (self.run_dir / "stop.flag").exists()

    def _spawn_dialog(self, request: Path, response: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [_dialog_python(), str(Path(__file__).resolve()), "--dialog", str(request), str(response)],
            cwd=str(Path(__file__).resolve().parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            close_fds=True,
        )

    @staticmethod
    def _end_child(child: subprocess.Popen) -> bool:
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)
            return child.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return False

    def confirm(self, kind: str, title: str, details: str, stop_event: threading.Event, *, request_deadline=None) -> bool:
        """Show a real dialog, then erase the sensitive exchange after child exit."""
        if request_deadline is not None and (type(request_deadline) not in (int, float) or not math.isfinite(request_deadline)):
            self.last_error = "승인 요청의 제한 시간을 확인할 수 없습니다."
            return False
        def expired():
            return request_deadline is not None and time.monotonic() >= request_deadline
        if expired():
            self.last_error = "단계 시간이 지나 승인을 시작하지 않았습니다."
            return False
        while not self._confirm_lock.acquire(timeout=min(.05, max(0, request_deadline-time.monotonic())) if request_deadline is not None else .05):
            if self._stopped(stop_event) or expired():
                if expired(): self.last_error = "단계 시간이 지나 승인 대기를 취소했습니다."
                return False
        child = None
        request = response = None
        accepted = False
        outcome = "stopped"
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        try:
            if not self._stopped(stop_event) and not expired():
                self.last_error = ""
                duration = _timeout(self.config)
                deadline = time.monotonic() + duration
                if request_deadline is not None:
                    deadline = min(deadline, request_deadline)
                    duration = max(0, deadline-time.monotonic())
                request = self.run_dir / "consent" / (nonce + ".request.json")
                response = request.with_name(nonce + ".response.json")
                _atomic_json(request, {"nonce": nonce, "kind": str(kind), "title": str(title),
                                       "details": str(details), "timeout_seconds": duration})
                with self._children_lock:
                    if not self._stopped(stop_event) and time.monotonic() < deadline:
                        child = self._spawn_dialog(request, response)
                        self._children[id(child)] = child
                        self._pending_exchanges[id(child)] = (request, response)
                while child is not None and not self._stopped(stop_event):
                    if time.monotonic() >= deadline:
                        outcome = "timeout"
                        self.last_error = "승인 대기 시간이 지나 취소했습니다."
                        break
                    if response.exists():
                        accepted = _response_approved(response, nonce) and not self._stopped(stop_event)
                        outcome = "approved" if accepted else "denied"
                        break
                    if child.poll() is not None:
                        outcome = "closed"
                        self.last_error = "승인 창이 응답 없이 닫혀 취소했습니다."
                        break
                    stop_event.wait(min(0.05, max(0.001, deadline - time.monotonic())))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            outcome = "error"
            self.last_error = "승인 창을 열거나 응답을 확인하지 못했습니다: " + str(exc)
        finally:
            ended = child is None or self._end_child(child)
            if ended and child is not None:
                with self._children_lock:
                    self._children.pop(id(child), None)
                    self._pending_exchanges.pop(id(child), None)
            if not ended:
                accepted = False
                outcome = "close_failed"
                self.last_error = "승인 창 종료를 확인하지 못했습니다. 중지를 다시 시도해 주세요."
            if self._stopped(stop_event):
                accepted = False
                if ended:
                    outcome = "stopped"
            if request is not None:
                removed = ended
                if ended:
                    for path in (request, response):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            removed = False
                    if not removed:
                        self.last_error = "승인 상세 기록 일부를 지우지 못했습니다. 기록 폴더에서 확인해 주세요."
                try:
                    _atomic_json(request.with_name(nonce + ".audit.json"), {
                        "version": 1, "request_id": nonce,
                        "kind": kind if kind in {"scope", "session", "action", "launch"} else "other",
                        "outcome": outcome, "approved": accepted,
                        "resolved_at": time.time(), "exchange_removed": removed})
                except OSError:
                    self.last_error = "승인 결과 기록을 저장하지 못했습니다."
            self._confirm_lock.release()
        return accepted and not self._stopped(stop_event) and not expired()

    def close(self) -> None:
        self._closed.set()
        with self._children_lock:
            children = list(self._children.values())
        failed = False
        for child in children:
            if self._end_child(child):
                with self._children_lock:
                    self._children.pop(id(child), None)
                    exchange = self._pending_exchanges.pop(id(child), None)
                if exchange:
                    removed = True
                    for path in exchange:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            removed = False
                    audit = exchange[0].with_name(exchange[0].name.replace(".request.json", ".audit.json"))
                    try:
                        if audit.is_file():
                            record = json.loads(audit.read_text(encoding="utf-8"))
                            record.update(exchange_removed=removed, approved=False, outcome="stopped")
                            _atomic_json(audit, record)
                    except (OSError, ValueError):
                        pass
            else:
                failed = True
        if failed:
            raise OSError("승인 창 종료를 확인하지 못했습니다.")
        # The runtime may mark the run stopped immediately after close returns.
        # Wait for sensitive-file cleanup so maintenance cannot race that writer.
        if not self._confirm_lock.acquire(timeout=3):
            raise OSError("승인 처리 종료를 확인하지 못했습니다.")
        self._confirm_lock.release()


def _windows_user_id() -> str:
    """Read the actual process token SID, independent of user-supplied config."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "현재 Windows 사용자 정보를 확인할 수 없습니다.")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "Windows 사용자 토큰을 읽을 수 없습니다.")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "Windows 사용자 토큰을 읽을 수 없습니다.")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        sid = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid)):
            raise OSError(ctypes.get_last_error(), "Windows 사용자 식별자를 읽을 수 없습니다.")
        try:
            return sid.value
        finally:
            kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _desktop_identity() -> tuple[str, int]:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise OSError(ctypes.get_last_error(), "Windows 로그인 세션을 확인할 수 없습니다.")
        if session.value == 0:
            raise RuntimeError("화면 자동화는 사용자가 로그인한 바탕화면에서 실행해야 합니다. Windows 서비스 세션에서는 사용할 수 없습니다.")
        return _windows_user_id(), session.value
    return str(os.getuid()), os.getsid(0)


def _runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "CompanyComputerUse" / "runtime"


def _desktop_key() -> str:
    user, session = _desktop_identity()
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:16] + "-session-" + str(session)


def _active_registry_dir() -> Path:
    return _runtime_root() / "active" / _desktop_key()


def _local_path(value: Path | str) -> Path:
    raw = str(value)
    if not Path(raw).is_absolute() or raw.startswith(("\\\\", "//")) or "\x00" in raw:
        raise ValueError("작업 기록은 이 PC의 전체 경로여야 합니다.")
    return Path(os.path.normcase(os.path.abspath(raw)))


def _plain_chain(path: Path, *, allow_missing=False) -> bool:
    """Do not follow symlinks or Windows junctions in registry/run paths."""
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _trusted_run(run_dir: Path) -> Path:
    path = _local_path(run_dir)
    if path.parent.name != "runs" or not re.fullmatch(r"[a-fA-F0-9]{32}", path.name):
        raise ValueError("현재 패키지가 만든 작업 기록 폴더만 등록할 수 있습니다.")
    if not _plain_chain(path) or not path.is_dir():
        raise ValueError("작업 기록 폴더가 없거나 바로가기 경로입니다.")
    return path


def _process_birth(pid: int) -> str | None:
    """PID plus creation time avoids treating a reused process ID as its owner."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return "windows:" + str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # Linux fallback allows the same file/ownership tests on development hosts.
        text = Path(f"/proc/{pid}/stat").read_text()
        return "proc:" + text[text.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def _registry_entry(root: Path, run: Path) -> Path:
    return root / (hashlib.sha256(str(run).encode("utf-8")).hexdigest() + ".json")


def _read_small_object(path: Path) -> dict:
    if not _plain_chain(path) or not path.is_file() or path.stat().st_size > 16_384:
        raise ValueError("작업 소유 기록이 올바르지 않습니다.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("작업 소유 기록이 올바르지 않습니다.")
    return value


def register_active_run(run_dir: Path) -> None:
    """Register a generated run before consent, across arbitrary config files."""
    run = _trusted_run(run_dir)
    root = _local_path(_active_registry_dir())
    if not _plain_chain(root, allow_missing=True):
        raise ValueError("공통 작업 목록 폴더에 바로가기 경로가 포함되어 있습니다.")
    root.mkdir(parents=True, exist_ok=True)
    birth = _process_birth(os.getpid())
    if birth is None:
        raise RuntimeError("현재 자동화 프로세스의 소유 정보를 확인할 수 없습니다.")
    record = {"version": 1, "run_dir": str(run), "pid": os.getpid(), "birth": birth,
              "token": uuid.uuid4().hex + uuid.uuid4().hex}
    # Matching ownership copies prevent a stale/edited path from redirecting writes.
    _atomic_json(run / ".computer-use-active.json", record)
    _atomic_json(_registry_entry(root, run), record)


def unregister_active_run(run_dir: Path) -> None:
    """Remove only this process's entry; retain the run marker as an audit record."""
    try:
        run = _local_path(run_dir)
        root = _local_path(_active_registry_dir())
        entry = _registry_entry(root, run)
        record = _read_small_object(entry)
        if record.get("run_dir") == str(run) and record.get("pid") == os.getpid() and record.get("birth") == _process_birth(os.getpid()):
            entry.unlink(missing_ok=True)
    except (OSError, ValueError):
        return


def stop_active_runs() -> int:
    """Request stop for this user's live runs in this Windows session only."""
    root = _local_path(_active_registry_dir())
    if not _plain_chain(root, allow_missing=True):
        raise ValueError("공통 작업 목록 폴더를 안전하게 읽을 수 없습니다.")
    try:
        root.lstat()
    except FileNotFoundError:
        return 0
    if not _plain_chain(root) or not root.is_dir():
        raise ValueError("공통 작업 목록 폴더를 안전하게 읽을 수 없습니다.")
    count = 0
    failures = 0
    for entry in root.glob("*.json"):
        try:
            record = _read_small_object(entry)
            run = _trusted_run(Path(record.get("run_dir", "")))
            if entry.name != _registry_entry(root, run).name or record.get("version") != 1:
                raise ValueError("잘못된 작업 참조")
            token = record.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
                raise ValueError("잘못된 작업 소유 정보")
            if not record.get("birth") or _process_birth(record.get("pid")) != record["birth"]:
                raise ValueError("종료된 작업")
            if _read_small_object(run / ".computer-use-active.json") != record:
                raise ValueError("작업 소유 기록 불일치")
            state_file = run / "session.json"
            try:
                state_file.lstat()
            except FileNotFoundError:
                pass
            else:
                if _read_small_object(state_file).get("state") == "stopped":
                    raise ValueError("종료된 작업")
        except (OSError, ValueError, TypeError):
            # Unlinking an entry symlink removes the link itself, never its target.
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        try:
            flag = run / "stop.flag"
            try:
                flag.lstat()
            except FileNotFoundError:
                # Exclusive creation never follows a pre-existing stop-file link.
                try:
                    descriptor = os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    if not _plain_chain(flag) or not flag.is_file():
                        raise OSError("중지 표시가 일반 파일이 아닙니다.")
                else:
                    os.close(descriptor)
            else:
                if not _plain_chain(flag) or not flag.is_file():
                    raise OSError("중지 표시가 일반 파일이 아닙니다.")
            count += 1
        except OSError:
            failures += 1
    if failures:
        raise RuntimeError(f"{count}개 작업에 중지를 요청했지만 {failures}개 작업에는 기록 권한을 확인해야 합니다.")
    return count


class DesktopLease:
    """One automation owner for the current user/session, across all configs."""

    def __init__(self, lock_dir: Path | None = None):
        user, session = _desktop_identity()
        user_key = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
        base = Path(lock_dir) if lock_dir is not None else _runtime_root()
        self.path = base / f"desktop-{user_key}-session-{session}.lock"
        self._file = None
        self._guard = threading.Lock()

    def acquire(self) -> bool:
        with self._guard:
            if self._file is not None:
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.open("a+b", buffering=0)
            except OSError as exc:
                raise RuntimeError("화면 자동화 잠금 폴더에 접근할 수 없습니다: " + str(self.path.parent)) from exc
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {32, 33, 36}:
                    return False
                raise RuntimeError("다른 자동화와의 충돌 여부를 확인할 수 없습니다: " + str(exc)) from exc
            self._file = handle
            return True

    def release(self) -> None:
        with self._guard:
            handle, self._file = self._file, None
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            # Leave the inode in place: deleting it would allow a second lock file.

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("이 바탕화면에서 다른 자동화가 실행 중입니다. 먼저 종료한 뒤 다시 시작해주세요.")
        return self

    def __exit__(self, *_):
        self.release()


class EmergencyStop:
    """Register only Ctrl+Alt+F12; never capture or replay normal typing."""

    HOTKEY_ID = 0xC0A

    def __init__(self, run_dir: Path, stop_callback):
        self.run_dir = Path(run_dir)
        self.stop_callback = stop_callback
        self.available = False
        self.error = ""
        self._thread = None
        self._started = False
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._triggered = threading.Event()
        self._lifecycle = threading.Lock()

    def start(self) -> None:
        with self._lifecycle:
            if self._started or self._closing.is_set():
                return
            self._started = True
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._ready.clear()
            self._triggered.clear()
            self.available = False
            self.error = ""
            self._thread = threading.Thread(target=self._message_loop, name="computer-use-stop-key", daemon=True)
            self._thread.start()
        self._ready.wait(timeout=2)

    def _register_hotkey(self) -> bool:
        if os.name != "nt":
            self.error = "전역 중지 단축키는 Windows에서 사용할 수 있습니다."
            return False
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user.RegisterHotKey.restype = wintypes.BOOL
        self._user32 = user
        okay = bool(user.RegisterHotKey(None, self.HOTKEY_ID, 0x0002 | 0x0001 | 0x4000, 0x7B))
        if not okay:
            self.error = "Ctrl + Alt + F12를 등록하지 못했습니다. 다른 프로그램이 사용 중일 수 있습니다. ‘모든 자동화 중지’ 버튼을 사용하세요."
        return okay

    def _unregister_hotkey(self) -> None:
        self._user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def _poll_hotkey(self) -> bool:
        message = wintypes.MSG()
        matched = False
        while self._user32.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
            if message.wParam == self.HOTKEY_ID:
                matched = True
        return matched

    def _trigger(self) -> None:
        if self._triggered.is_set():
            return
        self._triggered.set()
        try:
            flag = self.run_dir / "stop.flag"
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        except OSError as exc:
            self.error = "중지 표시 파일을 기록하지 못했습니다: " + str(exc)
        try:
            self.stop_callback()
        except Exception as exc:
            self.error = "중지 요청을 처리하는 중 문제가 발생했습니다: " + str(exc)

    def _message_loop(self) -> None:
        registered = False
        try:
            # Run directories are unique. An existing flag is a real stop request,
            # including one written by setup while this thread was starting.
            if (self.run_dir / "stop.flag").exists():
                self.error = "이미 중지가 요청된 작업입니다."
                self._ready.set()
                self._trigger()
                return
            registered = self._register_hotkey()
            self.available = registered and not self._closing.is_set()
            self._ready.set()
            if not registered:
                return
            while not self._closing.wait(0.03):
                if (self.run_dir / "stop.flag").exists() or self._poll_hotkey():
                    self._trigger()
        except Exception as exc:
            self.error = "중지 단축키를 사용할 수 없습니다: " + str(exc)
        finally:
            self.available = False
            self._ready.set()
            if registered:
                self._unregister_hotkey()

    def close(self) -> None:
        with self._lifecycle:
            self._closing.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self.available = False


def _dialog_size(screen_width: int, screen_height: int, line_height: int) -> tuple[int, int, int, int]:
    """Size in Tk's coordinate space, using the actual font rather than assumed DPI."""
    scale = max(0.85, min(3.0, line_height / 20.0))
    available_width = max(280, screen_width - round(48 * scale))
    available_height = max(280, screen_height - round(80 * scale))
    width = min(round(620 * scale), available_width)
    height = min(round(600 * scale), available_height)
    return width, height, min(round(500 * scale), width), min(round(440 * scale), height)


def _countdown_text(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"자동 취소까지 {minutes:02d}:{remainder:02d}"


def _build_consent_dialog(root, request: dict, answer) -> dict:
    """Build only native widgets. Keep the complete request readable and copyable."""
    import tkinter as tk
    from tkinter import font as tkfont, ttk

    background, card, ink = "#F3F6FB", "#FFFFFF", "#172B4D"
    muted, border, accent = "#58687E", "#DCE4EE", "#2563EB"
    family = "맑은 고딕" if os.name == "nt" else "TkDefaultFont"
    root.title("화면 자동화 승인")
    root.configure(background=background)
    root.option_add("*Font", (family, 10))
    body_font = tkfont.Font(root=root, family=family, size=10)
    line_height = body_font.metrics("linespace")
    scale = max(0.85, min(3.0, line_height / 20.0))
    px = lambda value: max(1, round(value * scale))
    width, height, minimum_width, minimum_height = _dialog_size(
        root.winfo_screenwidth(), root.winfo_screenheight(), line_height)
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 2 - px(20))
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.minsize(minimum_width, minimum_height)

    style = ttk.Style(root)
    # Clam supplies consistent colors without downloading a theme or using a web view.
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("Consent.Primary.TButton", font=(family, 10, "bold"),
                    padding=(px(20), px(11)), background=accent, foreground=card,
                    borderwidth=0, focusthickness=2, focuscolor="#93B4FD")
    style.map("Consent.Primary.TButton", background=[("pressed", "#1D4ED8"), ("active", "#1E55CF")],
              foreground=[("disabled", "#CBD5E1")])
    style.configure("Consent.Secondary.TButton", font=(family, 10),
                    padding=(px(20), px(11)), background=card, foreground=ink,
                    borderwidth=1, bordercolor=border, lightcolor=card, darkcolor=card,
                    focusthickness=2, focuscolor="#93B4FD")
    style.map("Consent.Secondary.TButton", background=[("pressed", "#E7EDF7"), ("active", "#EDF2FA")])
    style.configure("Consent.Vertical.TScrollbar", background="#C4CFDF", troughcolor=card,
                    bordercolor=card, arrowcolor=muted, lightcolor=card, darkcolor=card,
                    borderwidth=0, arrowsize=px(13))

    body = tk.Frame(root, background=background, padx=px(24), pady=px(22))
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(3, weight=1)
    header = tk.Frame(body, background=background)
    header.grid(row=0, column=0, sticky="ew")
    tk.Label(header, text="COMPUTER USE", font=(family, 9, "bold"),
             background=background, foreground=accent).pack(side="left")
    kind_label = {"action": "개별 조작 확인", "launch": "프로그램 실행 확인"}.get(
        request.get("kind"), "작업 범위 확인")
    tk.Label(header, text=kind_label, font=(family, 9), background="#E5EDFB", foreground="#35588A",
             padx=px(9), pady=px(4)).pack(side="right")
    heading = "이 조작을 허용할까요?" if request.get("kind") == "action" else "화면 작업을 허용할까요?"
    tk.Label(body, text=heading, font=(family, 18, "bold"), background=background,
             foreground=ink, anchor="w").grid(row=1, column=0, sticky="ew", pady=(px(16), px(7)))
    intro = tk.Label(body, text="요청한 프로그램과 작업 내용을 확인해 주세요.",
                     background=background, foreground=muted, justify="left", anchor="w")
    intro.grid(row=2, column=0, sticky="ew", pady=(0, px(18)))

    panel = tk.Frame(body, background=card, highlightbackground=border, highlightthickness=1)
    panel.grid(row=3, column=0, sticky="nsew")
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(2, weight=1)
    tk.Label(panel, text="요청 내용", font=(family, 10, "bold"), foreground=ink,
             background=card, anchor="w", padx=px(15), pady=px(12)).grid(row=0, column=0, sticky="ew")
    tk.Frame(panel, background=border, height=1).grid(row=1, column=0, columnspan=2, sticky="ew")
    details = tk.Text(panel, wrap="word", height=8, width=1, font=body_font,
                      background=card, foreground=ink, selectbackground="#DCE9FF", selectforeground=ink,
                      relief="flat", borderwidth=0, highlightthickness=0,
                      padx=px(15), pady=px(12), spacing1=px(2), spacing3=px(5), takefocus=True)
    scrollbar = ttk.Scrollbar(panel, orient="vertical", command=details.yview,
                              style="Consent.Vertical.TScrollbar")
    details.configure(yscrollcommand=scrollbar.set)
    details.grid(row=2, column=0, sticky="nsew")
    scrollbar.grid(row=2, column=1, sticky="ns", pady=px(5))
    # Never shorten a title or payload: long paths and action text remain available
    # in this scrollable area, even on a small screen.
    details.insert("1.0", str(request.get("title", "요청한 작업의 범위를 확인하세요")) + "\n\n")
    details.insert("end", str(request.get("details", "")))
    details.configure(state="disabled")
    details.bind("<Tab>", lambda event: (event.widget.tk_focusNext().focus_set(), "break")[1])
    details.bind("<Shift-Tab>", lambda event: (event.widget.tk_focusPrev().focus_set(), "break")[1])

    remaining = tk.StringVar(root)
    status = tk.Frame(body, background=background)
    status.grid(row=4, column=0, sticky="ew", pady=(px(14), px(15)))
    tk.Label(status, textvariable=remaining, background=background, foreground=muted,
             font=(family, 9)).pack(anchor="w")
    tk.Label(status, text="Esc 키를 누르거나 창을 닫으면 취소됩니다.", background=background,
             foreground=muted, font=(family, 9)).pack(anchor="w", pady=(px(3), 0))
    actions = tk.Frame(body, background=background)
    actions.grid(row=5, column=0, sticky="ew")
    allow_text = "이 조작 허용" if request.get("kind") == "action" else "이번 작업 허용"
    allow = ttk.Button(actions, text=allow_text, style="Consent.Primary.TButton", command=lambda: answer(True))
    allow.pack(side="right")
    cancel = ttk.Button(actions, text="취소", style="Consent.Secondary.TButton", command=lambda: answer(False))
    cancel.pack(side="right", padx=(0, px(10)))
    root.protocol("WM_DELETE_WINDOW", lambda: answer(False))
    root.bind("<Escape>", lambda _: answer(False))
    # Enter is never an approval shortcut. Explicitly focused buttons retain the
    # standard Space-key interaction for keyboard accessibility.
    root.bind("<Return>", lambda _: "break")
    root.bind("<KP_Enter>", lambda _: "break")
    # Widget bindings run before ttk class bindings. Keep this safeguard even if
    # a platform theme gives focused buttons a Return-key activation binding.
    allow.bind("<Return>", lambda _: "break")
    allow.bind("<KP_Enter>", lambda _: "break")
    root.after_idle(cancel.focus_set)
    body.bind("<Configure>", lambda event: intro.configure(wraplength=max(120, event.width - px(48))))
    return {"details": details, "remaining": remaining, "allow": allow, "cancel": cancel,
            "body_font": body_font}


def _dialog_main(request_path: Path, response_path: Path) -> int:
    """Runs only in a child process; window close and timeout both write denial."""
    request = {}
    try:
        if request_path.stat().st_size > 1_048_576:
            return 2
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or not isinstance(request.get("nonce"), str) or len(request["nonce"]) < 32:
            return 2
        import tkinter as tk
        root = tk.Tk()
        answered = False

        def answer(approved: bool):
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                _atomic_json(response_path, {"nonce": request["nonce"], "approved": approved})
            finally:
                root.destroy()

        view = _build_consent_dialog(root, request, answer)
        deadline = time.monotonic() + _timeout({"approval_timeout_seconds": request.get("timeout_seconds", 300)})

        def countdown():
            seconds = max(0, math.ceil(deadline - time.monotonic()))
            view["remaining"].set(_countdown_text(seconds))
            if time.monotonic() >= deadline:
                answer(False)
            else:
                root.after(200, countdown)

        root.after(100, root.lift)
        root.after(0, countdown)
        root.mainloop()
        return 0
    except Exception:
        if isinstance(request, dict) and isinstance(request.get("nonce"), str):
            try:
                _atomic_json(response_path, {"nonce": request["nonce"], "approved": False})
            except OSError:
                pass
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dialog", nargs=2)
    args = parser.parse_args()
    if not args.dialog:
        return 2
    return _dialog_main(Path(args.dialog[0]), Path(args.dialog[1]))


if __name__ == "__main__":
    raise SystemExit(main())
