"""Read-only connection diagnosis: metadata and MCP schema/status, never UI tasks."""
from __future__ import annotations

import base64
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

from register import make_server_entry
from settings import save_config, validate_config, VERSION
from vendor.windows import OwnedProcess, hidden_flags, native_claude


def classify_signature(code: int) -> dict:
    code &= 0xFFFFFFFF
    if code == 0:
        return {"status": "ok", "detail": "Windows의 로컬 신뢰 정보로 서명을 확인했습니다. 최신 인증서 취소 상태는 조회하지 않았습니다.", "code": "0x00000000"}
    damaged = {0x80096010, 0x800B010C, 0x800B0111}
    if code in damaged:
        return {"status": "error", "detail": "서명 무결성 또는 신뢰 거절 문제가 있어 파일을 실행하지 않았습니다. 제공처에 파일을 확인해주세요.", "code": f"0x{code:08X}"}
    if code == 0x800B0100:
        return {"status": "error", "detail": "실행파일에서 서명을 찾지 못했습니다. 직접 받은 공식 Driver 파일인지 확인해주세요.", "code": f"0x{code:08X}"}
    if code in {0x800B0001, 0x800B0003}:
        return {"status": "unsupported", "detail": "이 Windows 환경에서 해당 서명 형식을 확인할 수 없습니다. 손상 여부를 판정하지 않았습니다.", "code": f"0x{code:08X}"}
    return {"status": "warning", "detail": "오프라인 신뢰 정보만으로 서명 확인을 끝내지 못했습니다. 파일 손상으로 판정한 결과는 아닙니다.", "code": f"0x{code:08X}"}


def signature_status(path: Path) -> dict:
    if os.name != "nt":
        return {"status": "unsupported", "detail": "Windows에서만 실행파일 서명을 확인할 수 있습니다.", "code": ""}

    class GUID(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]

    class FILE_INFO(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("path", wintypes.LPCWSTR), ("file", wintypes.HANDLE), ("subject", ctypes.c_void_p)]

    class TRUST_DATA(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("policy", ctypes.c_void_p), ("sip", ctypes.c_void_p),
                    ("ui", wintypes.DWORD), ("revocation", wintypes.DWORD), ("choice", wintypes.DWORD),
                    ("file", ctypes.POINTER(FILE_INFO)), ("action", wintypes.DWORD), ("state", wintypes.HANDLE),
                    ("url", wintypes.LPWSTR), ("flags", wintypes.DWORD), ("context", wintypes.DWORD), ("signature", ctypes.c_void_p)]

    action = GUID.from_buffer_copy(uuid.UUID("00aac56b-cd44-11d0-8cc2-00c04fc295ee").bytes_le)
    info = FILE_INFO(ctypes.sizeof(FILE_INFO), str(path), None, None)
    data = TRUST_DATA()
    data.size, data.ui, data.choice, data.action = ctypes.sizeof(data), 2, 1, 1
    data.file = ctypes.pointer(info)
    # Microsoft WINTRUST_DATA: WTD_CACHE_ONLY_URL_RETRIEVAL + WTD_REVOCATION_CHECK_NONE.
    # https://learn.microsoft.com/windows/win32/api/wintrust/ns-wintrust-wintrust_data
    data.flags = 0x1000 | 0x10
    library = ctypes.WinDLL("wintrust", use_last_error=True)
    library.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(TRUST_DATA)]
    library.WinVerifyTrust.restype = ctypes.c_long
    try:
        return classify_signature(library.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)))
    finally:
        data.action = 2
        library.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))


def file_metadata(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {"path": str(path), "sha256": digest.hexdigest(), "size": path.stat().st_size,
              "signature": signature_status(path), "file_version": "", "product_version": "", "signer": ""}
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if os.name == "nt" and powershell.is_file():
        script = """$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$f=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($env:CUA_DIAGNOSTIC_EXE)
$signer=''
try { $cert=[System.Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($env:CUA_DIAGNOSTIC_EXE); $signer=$cert.Subject; $cert.Dispose() } catch {}
@{file_version=$f.FileVersion;product_version=$f.ProductVersion;signer=$signer} | ConvertTo-Json -Compress
"""
        env = dict(os.environ, CUA_DIAGNOSTIC_EXE=str(path))
        try:
            checked = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                                     capture_output=True, env=env, timeout=10, creationflags=hidden_flags())
            if checked.returncode == 0:
                details = json.loads(checked.stdout.decode("utf-8-sig"))
                result.update({key: str(details.get(key) or "") for key in ("file_version", "product_version", "signer")})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return result


class ReadOnlyMCP:
    """Only the three diagnostic requests below are accepted by this client."""
    ALLOWED = {"initialize", "tools/list", "tools/call"}

    def __init__(self, entry: dict, *, owner_factory=None):
        self.events = queue.Queue()
        self.sequence = 0
        self.stderr_messages = []
        self.stderr_lock = threading.Lock()
        self.stderr_done = threading.Event()
        self.cleanup = {"verified": False, "graceful": False}
        self.process = subprocess.Popen([entry["command"], *entry["args"]], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, **entry.get("env", {})), creationflags=hidden_flags(), bufsize=1)
        try:
            self.owner = (owner_factory or OwnedProcess)(self.process)
        except Exception:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            raise
        self.process_management = {"mode": "job_object" if self.owner.job is not None else "stdio_lifecycle",
                                   "job_error": getattr(self.owner, "job_error", None)}
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read_stderr(self):
        try:
            while True:
                line = self.process.stderr.readline(2049)
                if not line:
                    break
                # Do not expose arbitrary logs/tracebacks or unbounded screen data.
                if line.startswith(("computer-use-admin:", "computer-use-mcp:")):
                    text = " ".join(line[:1024].split())
                    with self.stderr_lock:
                        self.stderr_messages.append(text)
                        self.stderr_messages[:] = self.stderr_messages[-4:]
        finally:
            self.stderr_done.set()

    def _exit_error(self):
        self.stderr_done.wait(.25)
        code = self.process.poll()
        if code is None:
            try:
                code = self.process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
        with self.stderr_lock:
            detail = self.stderr_messages[-1] if self.stderr_messages else ""
        if code == 1223 or "관리자 권한 요청이 취소" in detail:
            return RuntimeError("Windows 관리자 승인(UAC)이 취소되어 MCP 연결이 시작되지 않았습니다. 같은 로그인 계정으로 승인한 뒤 다시 연결하세요.")
        message = "MCP 진단 프로세스가 응답 전에 종료되었습니다."
        if code is not None:
            message += f" 종료 코드: {code}."
        if detail:
            message += " 실제 실행 오류: " + detail
        else:
            message += " 종료 원인을 확인하지 못했습니다. 회사 보안 정책 차단으로 단정할 수 없습니다."
        return RuntimeError(message)

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(2_000_001)
                if not line:
                    break
                if len(line) > 2_000_000:
                    self.events.put(RuntimeError("MCP 진단 응답이 너무 큽니다."))
                    break
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put(RuntimeError("MCP가 올바른 응답 형식으로 실행되지 않았습니다."))
                    break
        finally:
            self.events.put(self._exit_error())

    def request(self, method: str, params=None, timeout=120) -> dict:
        params = params or {}
        if method not in self.ALLOWED or (method == "tools/call" and params != {"name": "computer_status", "arguments": {}}):
            raise ValueError("연결 확인에서는 상태 조회만 허용합니다.")
        self.sequence += 1
        request_id = self.sequence
        try:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise self._exit_error() from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError("연결 확인 응답을 기다리는 시간이 지났습니다.") from exc
            if isinstance(value, Exception):
                raise value
            if not isinstance(value, dict) or value.get("id") != request_id:
                continue
            if "error" in value:
                raise RuntimeError("MCP 연결 확인 오류: " + str(value["error"].get("message", "응답 오류")))
            if not isinstance(value.get("result"), dict):
                raise RuntimeError("MCP 응답에서 결과를 읽을 수 없습니다.")
            return value["result"]
        raise TimeoutError("연결 확인 응답을 기다리는 시간이 지났습니다.")

    def initialized(self):
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def close(self):
        failure = None
        try:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
                self.cleanup.update(verified=True, graceful=self.process.returncode == 0, exit_code=self.process.returncode)
                if self.process.returncode != 0:
                    failure = self._exit_error()
            except subprocess.TimeoutExpired:
                if self.owner.job is not None:
                    self.owner.close()
                else:
                    # Exact Popen handle only. Pipe loss makes the authenticated
                    # bridge close its own MCP; never use a process-name/tree scan.
                    self.process.kill()
                self.process.wait(timeout=5)
                self.cleanup.update(verified=True, graceful=False, exit_code=self.process.returncode)
                failure = RuntimeError("MCP 진단의 정상 종료를 확인하지 못해 이번 연결만 중지했습니다. 연결 성공으로 처리하지 않았습니다.")
        finally:
            if self.owner.job is not None:
                self.owner.close()
            self.reader.join(timeout=1)
            self.stderr_reader.join(timeout=1)
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()
        if failure:
            raise failure


def _structured(response: dict) -> dict:
    if response.get("isError"):
        raise RuntimeError("MCP 상태 확인이 오류를 반환했습니다.")
    if isinstance(response.get("structuredContent"), dict):
        return response["structuredContent"]
    for block in response.get("content", []):
        if block.get("type") == "text":
            value = json.loads(block["text"])
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP 상태 정보를 읽을 수 없습니다.")


def connection_issues(initialized: dict, names: set, status: dict) -> list[str]:
    from teaching_support import TEACHING_HELPER_FILES
    required = {"computer_status", "computer_begin", "computer_stop", "get_window_state",
                "computer_teach_element", "computer_teach_status", "computer_process_editor", "computer_process_status"}
    issues = []
    server = initialized.get("serverInfo", {})
    if server.get("name") != "company-computer-use" or server.get("version") != VERSION or status.get("version") != VERSION:
        issues.append(f"연결된 MCP 버전이 현재 배포 {VERSION}과 다릅니다. 새 배포 폴더로 연결을 갱신하고 클라이언트를 다시 연결하세요.")
    if required - names:
        issues.append("필수 학습 도구가 없습니다: " + ", ".join(sorted(required - names)))
    support = status.get("teaching_support", {})
    if not isinstance(support, dict):
        support = {}
    if support.get("format") != "computer-teaching-capabilities/v1" or support.get("version") != VERSION:
        issues.append("학습 도우미 진단이 없는 이전 연결입니다. UIA 미지원으로 판단하지 마세요.")
        return issues
    direct, image = support.get("direct_picker", {}), support.get("image_process", {})
    helpers = [direct.get("helper", {}), *image.get("helpers", [])]
    present = {item.get("filename") for item in helpers if isinstance(item, dict) and item.get("file_present") is True}
    if set(TEACHING_HELPER_FILES) - present:
        issues.append("학습 도우미 파일을 확인하지 못했습니다: " + ", ".join(sorted(set(TEACHING_HELPER_FILES) - present)) + ". ZIP 전체를 다시 압축 해제하세요.")
    return issues


def probe_connection(config: dict) -> dict:
    copied = copy.deepcopy(config)
    with tempfile.TemporaryDirectory(prefix="computer-use-connection-") as directory:
        copied["state_dir"] = directory
        path = Path(directory) / "diagnostic-config.json"
        save_config(path, copied)
        client = ReadOnlyMCP(make_server_entry(path))
        primary_error = None
        try:
            initialized = client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "computer-use-connection-check", "version": VERSION}})
            client.initialized()
            tools = client.request("tools/list").get("tools", [])
            status = _structured(client.request("tools/call", {"name": "computer_status", "arguments": {}}))
            if status.get("session") is not None:
                raise RuntimeError("연결 확인 중 예상하지 못한 화면 세션이 감지되었습니다.")
            names = {item.get("name") for item in tools if isinstance(item, dict)}
            issues = connection_issues(initialized, names, status)
            result = {"ok": not issues and not status.get("driver_schema_error"), "tool_count": len(names),
                    "server": initialized.get("serverInfo", {}), "status": status,
                    "compatibility_issues": issues,
                    "schema_error": status.get("driver_schema_error", ""), "screen_session_started": False}
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                client.close()
            except Exception:
                if primary_error is None:
                    raise
        result["process_management"] = getattr(client, "process_management", {"mode": "unreported"})
        result["cleanup"] = getattr(client, "cleanup", {"verified": None, "graceful": None})
        return result


def run_diagnostics(config: dict) -> dict:
    value = validate_config(config)
    checks = []
    report = {"checks": checks, "driver": {}, "connection": {}, "cli": "", "ok": False,
              "scope": "파일 정보와 MCP 도구·상태만 확인했습니다. 화면 조작·모델 연결·외부 통신 차단은 검증하지 않았습니다."}

    def add(label, status, detail):
        checks.append({"label": label, "status": status, "detail": detail})

    driver = Path(value["driver"]) if value["driver"] else None
    if driver is None or not driver.is_file():
        add("Driver 파일", "error", "직접 받은 Driver 배포파일의 압축을 모두 푼 뒤 ‘Driver 파일 선택’에서 cua-driver.exe를 골라주세요.")
    else:
        metadata = file_metadata(driver)
        report["driver"] = metadata
        signature = metadata["signature"]
        add("Driver 서명", signature["status"], signature["detail"] + (" / " + metadata["signer"] if metadata["signer"] else ""))
        add("Driver 파일 정보", "ok", "파일 버전: " + (metadata["file_version"] or "표시 없음") + "\nSHA-256: " + metadata["sha256"])
        if signature["status"] != "error":
            try:
                env = dict(os.environ, CUA_DRIVER_RS_TELEMETRY_ENABLED="false", CUA_DRIVER_RS_UPDATE_CHECK="false", DO_NOT_TRACK="1")
                version = subprocess.run([str(driver), "--version"], capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=15, creationflags=hidden_flags())
                if version.returncode or "cua-driver" not in version.stdout.lower():
                    raise ValueError("Cua Driver의 버전 응답을 확인하지 못했습니다. 선택한 파일을 확인해주세요.")
                report["driver"]["runtime_version"] = version.stdout.strip()
                add("Driver 실행", "ok", version.stdout.strip())
                report["connection"] = probe_connection(value)
                connected = report["connection"]
                details = [f"도구 {connected['tool_count']}개 조회 / 화면 세션 시작 없음", *connected.get("compatibility_issues", [])]
                if connected["schema_error"]:
                    details.append(connected["schema_error"])
                add("MCP 실제 연결", "ok" if connected["ok"] else "error", "\n".join(details))
                management = connected.get("process_management", {})
                if management.get("mode") == "stdio_lifecycle":
                    job_error = management.get("job_error") or {}
                    detail = "Job 객체를 사용하지 못해 stdio 연결의 정상 종료를 별도로 확인했습니다. 회사 보안 정책 차단으로 판정하지 않습니다."
                    if job_error.get("winerror") is not None:
                        detail += f" Windows 오류: {job_error['winerror']} ({job_error.get('stage', 'unknown')})."
                    add("진단 프로세스 관리", "warning", detail)
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                add("MCP 실제 연결", "error", str(exc) + "\n배포 폴더를 옮겼다면 전체 압축을 다시 풀고 Driver를 선택한 뒤 재확인하세요.")
    cli = native_claude()
    report["cli"] = cli
    add("Claude Code", "ok" if cli else "warning", "기존 실행파일을 찾았습니다. 모델 연결은 기존 Claude Code에서 확인해주세요." if cli else "Claude Code를 찾지 못했습니다. 기존에 설치한 Claude Code가 실행되는지 확인하고 설정 창을 다시 여세요. 다른 MCP 클라이언트에는 연결 파일을 내보낼 수 있습니다.")
    enabled = [p for p in value["programs"] if p["enabled"]]
    add("사용할 프로그램", "ok" if enabled else "warning", f"{len(enabled)}개 사용 설정. 실제 화면을 읽고 조작할 수 있는지는 가짜 자료로 첫 시험을 진행해 확인하세요." if enabled else "‘사용할 프로그램’에서 시험할 프로그램을 한 개 이상 켜주세요.")
    report["ok"] = bool(report["connection"].get("ok")) and not any(c["status"] == "error" for c in checks)
    return report


def format_diagnostics(report: dict) -> str:
    names = {"ok": "확인", "warning": "확인 필요", "unsupported": "확인 불가", "error": "문제"}
    lines = ["연결 확인 완료" if report.get("ok") else "연결을 마치려면 아래 항목을 확인해주세요."]
    for item in report.get("checks", []):
        lines.append(f"\n[{names.get(item['status'], item['status'])}] {item['label']}\n{item['detail']}")
    lines.append("\n" + report.get("scope", ""))
    lines.append("\n다음: ‘저장하고 Claude Code에 연결’ → Claude Code 다시 열기 → /mcp에서 local-computer-use 확인 → 가짜 자료로 첫 작업 요청")
    return "\n".join(lines)
