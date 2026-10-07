"""Bounded, content-free live execution metadata; never a second UI worker."""
from __future__ import annotations

import copy
import threading
import time
import uuid


STAGES = {
    "starting": "작업 준비 중", "observing": "화면 읽는 중", "searching": "대상 찾는 중",
    "acting": "입력 전달 중", "verifying": "결과 확인 중", "waiting": "완료 조건 대기 중",
    "transition": "다음 창 확인 중", "running_step": "저장된 단계 실행 중",
    "needs_review": "화면 확인 필요", "finished": "도구 실행 종료", "failed": "도구 오류",
}


class ExecutionProgress:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.delivery_lock = threading.RLock()
        self.current = None
        self.sink = None

    def start(self, tool, request_id=None, sink=None):
        now = self.clock()
        with self.lock:
            operation_id = uuid.uuid4().hex
            self.current = {"operation_id": operation_id, "tool": str(tool)[:100],
                "active": True, "stage": "starting", "sequence": 0,
                "started_at_monotonic": now, "updated_at_monotonic": now}
            # Request IDs can contain user text; never expose or persist them.
            self.sink = sink
        self.update(operation_id, "starting")
        return operation_id

    def update(self, operation_id, stage, **fields):
        if stage not in STAGES:
            return
        with self.lock:
            row = self.current
            if not row or row["operation_id"] != operation_id or not row["active"]:
                return
            row.update(stage=stage, updated_at_monotonic=self.clock(), sequence=row["sequence"] + 1)
            for key in ("step_index", "step_total"):
                value = fields.get(key)
                if type(value) is int and 0 <= value <= 10000:
                    row[key] = value
            run_id = fields.get("run_id")
            if isinstance(run_id, str) and len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id):
                row["run_id"] = run_id
            # Never accept arbitrary messages, selectors, input values, or screen text.
            snapshot, sink = self._snapshot_locked(), self.sink
        self._publish(operation_id, snapshot, sink)

    def _publish(self, operation_id, snapshot, sink):
        if callable(sink):
            # Serialize publication without holding the state lock. The final
            # notification cannot overtake a delayed earlier callback, and a
            # stale snapshot cannot be published after a newer operation.
            with self.delivery_lock:
                with self.lock:
                    row = self.current
                    current = bool(row and row["operation_id"] == operation_id
                                   and row["active"] == snapshot["active"]
                                   and row["sequence"] == snapshot["sequence"])
                if current:
                    try:
                        sink(snapshot)
                    except Exception:
                        # A UI notification failure must not retry a desktop action.
                        pass

    def finish(self, operation_id, error=False):
        with self.lock:
            row = self.current
            if not row or row["operation_id"] != operation_id or not row["active"]:
                return
            now = self.clock()
            row.update(active=False, ended_at_monotonic=now, updated_at_monotonic=now,
                       stage="failed" if error else "finished", sequence=row["sequence"]+1)
            snapshot, sink = self._snapshot_locked(), self.sink
            self.sink = None
        self._publish(operation_id, snapshot, sink)

    def _snapshot_locked(self):
        if self.current is None:
            return {"active": False, "status": "idle", "message": "실행 중인 작업이 없습니다.",
                    "screen_state_verified": False, "input_dispatched": False}
        row = self.current
        now = self.clock() if row["active"] else row["ended_at_monotonic"]
        result = {k: copy.deepcopy(v) for k, v in row.items() if not k.endswith("_monotonic")}
        result.update(elapsed_ms=max(0, round((now-row["started_at_monotonic"])*1000)),
                      stage_elapsed_ms=max(0, round((now-row["updated_at_monotonic"])*1000)),
                      message=STAGES[row["stage"]], screen_state_verified=False,
                      task_verified=False, poll_after_ms=500 if row["active"] else None)
        if "step_index" in row and "step_total" in row:
            result["message"] = f'{row["step_index"]}/{row["step_total"]} 단계 · {result["message"]}'
        return result

    def snapshot(self):
        with self.lock:
            return self._snapshot_locked()
