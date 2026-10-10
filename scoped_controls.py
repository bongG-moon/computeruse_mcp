"""Persistent, read-only, cancellable native UIA property queries for verification.

This helper never supplies Driver handles, marks a window observed for input,
or caches current values between requests. Every call resolves current targets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from operations import OperationError, validate_selector
from vendor.guard import validate_arguments

HELPER_NAME = "Computer Use MCP 빠른 확인.exe"


class ScopedControls:
    def __init__(self, runtime, *, factory=subprocess.Popen):
        self.runtime, self.factory = runtime, factory
        self.process = None
        self.responses = queue.Queue(maxsize=4)
        self.lock = threading.RLock()
        self.close_lock = threading.Lock()
        self.reader_thread = None

    def _start(self):
        path = Path(__file__).resolve().parent / HELPER_NAME
        if os.name != "nt" or not path.is_file():
            raise NotImplementedError("scoped_helper_unavailable")
        self.process = self.factory([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            cwd=str(path.parent))
        self.responses = queue.Queue(maxsize=4)
        process, responses = self.process, self.responses
        def reader():
            try:
                while True:
                    line = process.stdout.readline(2097153)
                    if not line or len(line) > 2097152:
                        break
                    responses.put_nowait(line)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try: responses.put_nowait(None)
                except queue.Full: pass
        self.reader_thread = threading.Thread(target=reader, daemon=True, name="scoped-controls-reader")
        self.reader_thread.start()

    def close(self):
        # Can be called by emergency stop without waiting for query's lock.
        if not self.close_lock.acquire(blocking=False):
            raise OperationError("빠른 확인 도우미를 정리 중입니다.", "scoped_cleanup_pending")
        try:
            process = self.process
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try: process.kill(); process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired): pass
            if process.poll() is None:
                raise OperationError("빠른 확인 도우미의 종료를 확인하지 못했습니다.", "scoped_cleanup_pending")
            if self.reader_thread is not None:
                self.reader_thread.join(timeout=1)
                if self.reader_thread.is_alive():
                    raise OperationError("빠른 확인 응답 읽기의 종료를 기다립니다.", "scoped_cleanup_pending")
            for stream in (process.stdin, process.stdout):
                try: stream.close()
                except (OSError, ValueError, AttributeError): pass
            self.process = None
        finally:
            self.close_lock.release()

    def observe(self, target, selectors, *, timeout_ms=3000):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120000:
            raise OperationError("빠른 확인의 제한 시간을 확인하세요.")
        if not isinstance(selectors, (list, tuple)) or not 1 <= len(selectors) <= 40:
            raise OperationError("확인할 요소는 1~40개로 지정하세요.")
        selectors = [validate_selector(s) for s in selectors]
        if not isinstance(target, dict) or set(target) != {"pid", "window_id"}:
            raise OperationError("현재 프로그램과 창을 지정하세요.")
        return self._query(target, {"selectors": selectors}, timeout_ms=timeout_ms, max_elements=80)

    def inspect(self, target, *, within, max_depth=12, max_elements=600, timeout_ms=3000):
        """Resolve one exact region, then read only its bounded subtree."""
        within = validate_selector(within, allow_within=False)
        for name, value, ceiling in (("max_depth", max_depth, 32), ("max_elements", max_elements, 5000)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise OperationError(f"{name}는 1~{ceiling} 범위의 정수여야 합니다.")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120000:
            raise OperationError("영역 관찰 제한 시간을 확인하세요.")
        if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                or any(type(v) is not int or v < 1 for v in target.values())):
            raise OperationError("현재 프로그램과 창을 지정하세요.")
        return self._query(target, {"operation": "inspect", "within": within,
                                   "max_depth": max_depth, "max_elements": max_elements},
                           timeout_ms=timeout_ms, max_elements=max_elements, inspection=True)

    def _query(self, target, payload, *, timeout_ms, max_elements, inspection=False):
        runtime = self.runtime
        guard = runtime.guard
        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        def check():
            runtime.check_active()
            validate_arguments("get_window_state", target, guard.policy, guard.process_resolver, guard.window_resolver)
            hosted = getattr(guard, "hosted_target", None)
            if callable(hosted) and hosted(target) is not None:
                # Native helper deliberately enforces one UIA process. A UWP
                # frame and its registered child have different PIDs; let the
                # caller use the guarded Driver tree for this exact frame.
                raise NotImplementedError("hosted_scoped_helper_unavailable")
        with self.lock:
            check()
            if self.process is None:
                self._start()
            process = self.process
            request_id = uuid.uuid4().hex
            try:
                process.stdin.write(json.dumps({"id": request_id, **target, **payload}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                while True:
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OperationError("선택한 요소의 빠른 확인이 시간 안에 끝나지 않았습니다.", "scoped_observation_timeout")
                    try: line = self.responses.get(timeout=min(.05, remaining))
                    except queue.Empty: continue
                    if line is None:
                        raise OperationError("빠른 확인 도우미가 종료됐습니다.", "scoped_helper_exited")
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get("id") != request_id:
                        raise OperationError("빠른 확인 응답의 식별자가 다릅니다.", "scoped_response_invalid")
                    if value.get("ok") is not True:
                        code = str(value.get("code", "scoped_read_failed"))
                        message = {"ambiguous_selector": "같은 선택 기준에 맞는 요소가 여러 개입니다. 이름 또는 automation_id로 범위를 더 구체적으로 지정하세요.",
                                   "selector_not_found": "현재 창에서 지정한 영역을 찾지 못했습니다. 새 화면에서 영역 이름을 확인하세요.",
                                   "scope_identity_changed": "읽는 동안 선택한 영역이 바뀌었습니다. 입력하지 말고 현재 화면을 다시 읽으세요.",
                                   "target_mismatch": "현재 창의 프로그램 또는 식별자가 달라 관찰을 중단했습니다.",
                                   "scope_lookup_limit": "창의 식별 요소가 관찰 한도를 초과했습니다. 복잡한 영역을 접거나 별도 창으로 열어 확인하세요."}.get(code,
                                   "현재 요소 속성을 확인하지 못했습니다. 같은 입력을 반복하지 말고 관찰 진단을 확인하세요.")
                        raise OperationError(message, code)
                    data = value.get("data")
                    if (not isinstance(data, dict) or any(data.get(k) != v for k, v in target.items())
                            or data.get("scope_complete") is not True or data.get("read_only") is not True
                            or data.get("scoped_observation") is not True or not isinstance(data.get("elements"), list)
                            or len(data["elements"]) > max_elements):
                        raise OperationError("빠른 확인 결과의 범위를 검증하지 못했습니다.", "scoped_response_invalid")
                    if inspection and (data.get("scoped_inspection") is not True
                            or data.get("within") != payload["within"] or type(data.get("truncated")) is not bool):
                        raise OperationError("선택 영역의 관찰 범위를 확인하지 못했습니다.", "scoped_response_invalid")
                    for element in data["elements"]:
                        if (not isinstance(element, dict) or element.get("verification_only") is not True
                                or any(k in element for k in ("element_token", "snapshot_id", "actions"))):
                            raise OperationError("빠른 확인 결과에 입력 권한이 포함되었습니다.", "scoped_response_invalid")
                    check()
                    data["metrics"] = {"observation_ms": round((time.monotonic()-started)*1000, 2),
                                       "source": "native_selected_subtree" if inspection else "native_scoped_properties",
                                       **({"visited_controls": len(data["elements"]), "property_reads_subtree_only": True,
                                           "lookup_scope": "whole_window_identity_metadata"}
                                          if inspection else {"queried_controls": len(payload["selectors"])})}
                    return {"structuredContent": data, "content": []}
            except BaseException:
                self.close()
                raise
