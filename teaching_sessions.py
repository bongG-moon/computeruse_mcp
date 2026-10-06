"""Bounded, cancellable native teaching without holding an MCP request open."""
from __future__ import annotations

import copy
import threading
import time
import uuid

from learning_picker import _run_helper, HELPER_START_SECONDS
from operations import OperationError


class _Cancellation:
    def __init__(self, request_event=None):
        self.event = threading.Event()
        self.request_event = request_event

    def is_set(self):
        return self.event.is_set() or (self.request_event is not None and self.request_event.is_set())

    def set(self):
        self.event.set()


class TeachingSessions:
    """One owned picker per session; only the worker can commit its selection."""
    def __init__(self, library):
        self.library = library
        self.lock = threading.RLock()
        self.jobs = {}

    def pending(self, runtime=None):
        with self.lock:
            return next((job for job in self.jobs.values()
                         if (runtime is None or job["runtime"] is runtime) and
                         (not job["done"].is_set() or job["result"].get("cleanup_pending"))), None)

    def _view(self, job, **extra):
        with self.lock:
            identity = {key: job["args"][key] for key in ("program_id", "pid", "window_id", "label")}
            return copy.deepcopy({**identity, **job["result"], "teaching_id": job["id"],
                                  "automatic_retry": False, "input_dispatched": False,
                                  "model_trained": False, **extra})

    def start(self, runtime, arguments, cancel_event=None):
        args = copy.deepcopy(arguments)
        target = {key: args[key] for key in ("pid", "window_id")}
        # This resolves the executable through the guard, which enforces same
        # user/session, and verifies exact HWND ownership. No UIA tree is read.
        self.library._program(runtime, args["program_id"], target)
        with self.lock:
            active = self.pending()
            if active is not None:
                return self._view(active, reused=active["args"] == args, busy=active["args"] != args,
                                  next_tool="computer_teach_status")
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "runtime": runtime, "args": args, "cancel": _Cancellation(cancel_event),
                   "started": threading.Event(), "done": threading.Event(), "request_cancel": cancel_event,
                   "result": {"status": "starting", "stage": "starting_picker", "picker_visible": False}}
            self.jobs[job_id] = job
            # Keep a bounded terminal history for repeated status calls.
            for key in [key for key, item in self.jobs.items() if item["done"].is_set() and not item["result"].get("cleanup_pending")][:-49]:
                del self.jobs[key]
            worker = threading.Thread(target=self._work, args=(job,), name="teach-" + job_id[:8], daemon=True)
            job["worker"] = worker
            worker.start()
        if not job["started"].wait(HELPER_START_SECONDS + 1):
            job["cancel"].set()
            return self._view(job, status="cancelling", stage="starting_picker", picker_visible=False,
                              diagnostic={"code": "picker_start_timeout", "stage": "starting_picker"},
                              message="선택 창 표시를 확인하지 못해 시작을 취소했습니다. F8 안내를 반복하지 마세요.",
                              next_tool="computer_teach_status")
        if cancel_event is not None and cancel_event.is_set():
            job["cancel"].set()
            job["done"].wait(1)
        return self._view(job)

    def _cancelled(self, job):
        runtime = job["runtime"]
        request = job["request_cancel"]
        if job["cancel"].is_set() or (request is not None and request.is_set()) or runtime.stop_event.is_set():
            job["cancel"].set()
            raise OperationError("요소 학습을 취소하여 저장하지 않았습니다.", "picker_cancelled")
        runtime.check_active()

    def _work(self, job):
        stage = "starting_picker"
        runtime, args = job["runtime"], job["args"]
        target = {key: args[key] for key in ("pid", "window_id")}
        def ready(info):
            nonlocal stage
            self._cancelled(job)
            stage = "waiting_for_human"
            with self.lock:
                job["result"] = {"status": "awaiting_selection", "stage": stage, "picker_visible": True,
                                 **info, **target, "program_id": args["program_id"], "label": args["label"], "next_tool": "computer_teach_status",
                                 "message": "요소 선택 창을 표시했습니다. 창에서 안내하는 방식으로 요소를 가리키고, 후보를 확인한 뒤 '이 요소로 선택'을 누르세요.",
                                 "next_step": "사용자 선택 후 같은 teaching_id로 상태를 확인하세요. 같은 학습을 다시 시작하거나 F8 안내를 반복하지 마세요."}
            job["started"].set()
        try:
            self._cancelled(job)
            selected = _run_helper(runtime, target, args["label"], args.get("timeout_seconds", 120),
                                   on_ready=ready, cancel_event=job["cancel"])
            self._cancelled(job)
            stage = "verifying_selection"
            with self.lock:
                job["result"] = {"status": "verifying_selection", "stage": stage, "picker_visible": False,
                                 "next_tool": "computer_teach_status", "message": "선택한 요소를 다시 읽고 저장하고 있습니다."}
            with runtime.execution_lock:
                self._cancelled(job)
                entry = self.library.teach_picked(runtime, target, args["program_id"], selected, args["label"],
                    cancel_event=job["cancel"], **{key: args[key] for key in
                    ("screen", "instructions", "id", "expected_revision") if key in args})
            with self.lock:
                job["result"] = {**entry, "status": "learned", "stage": "saved", "picker_visible": False,
                                 "instructions_are_untrusted_data": True, "screen_is_grouping_label_only": True}
        except Exception as exc:
            cleanup_pending = bool(getattr(exc, "helper_cleanup_pending", False))
            cancelled = not cleanup_pending and (job["cancel"].is_set() or runtime.stop_event.is_set() or getattr(exc, "code", "") == "picker_cancelled")
            code = "picker_cancelled" if cancelled else getattr(exc, "code", "teaching_internal_error")
            message = str(exc) if isinstance(exc, OperationError) else "학습을 완료하지 못했습니다. 아래 단계와 진단 코드를 전달하세요."
            with self.lock:
                job["result"] = {"status": "cancelled" if cancelled else "learning_failed", "stage": stage,
                                 "picker_visible": None if cleanup_pending else False, "cleanup_pending": cleanup_pending, "message": message,
                                 "diagnostic": {"code": code, "stage": stage,
                                                "native": getattr(exc, "picker_diagnostic", {})},
                                 "next_step": "실패 원인을 그대로 전달하세요. F8 안내나 학습 시작을 자동 반복하지 마세요. 목록 조회와 작업 저장은 요소 학습을 대신하지 않습니다.",
                                 "task_verified": False}
        finally:
            job["done"].set()
            job["started"].set()

    def status(self, teaching_id, *, wait_ms=0, cancel=False):
        if (not isinstance(teaching_id, str) or type(wait_ms) is not int or not 0 <= wait_ms <= 5000
                or type(cancel) is not bool):
            raise OperationError("학습 ID와 0~5000ms 상태 대기 시간을 지정하세요.", "invalid_teaching_status")
        with self.lock:
            job = self.jobs.get(teaching_id)
            if job is None:
                raise OperationError("이 MCP 연결의 학습 ID를 찾지 못했습니다. 재연결했다면 저장 요소 목록을 먼저 확인하세요.", "teaching_not_found")
            if cancel and not job["done"].is_set():
                job["cancel"].set()
        job["done"].wait(wait_ms / 1000)
        if cancel and not job["done"].is_set():
            return self._view(job, status="cancelling", message="요소 선택 창을 닫고 저장 취소를 확인하고 있습니다.")
        return self._view(job)

    def stop(self, runtime=None):
        with self.lock:
            for job in self.jobs.values():
                if runtime is None or job["runtime"] is runtime:
                    if not job["done"].is_set():
                        job["cancel"].set()

    def close(self, timeout=3):
        self.stop()
        deadline = time.monotonic() + timeout
        with self.lock:
            workers = [job["worker"] for job in self.jobs.values()]
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in workers) and self.pending() is None
