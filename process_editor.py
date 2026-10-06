"""Human-authored declarative processes, using the same guarded picker and task store."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from inspection import suggested_operations
from learning import ElementLibrary, stable_selector, _text
from learning_picker import _run_helper, _helper_visible, _end_helper, match_picked_element
from operations import OperationError, _name, _unique
from teaching_sessions import _Cancellation
from workflows import validate_recipe, WINDOW_REF


HELPER_NAME = "Computer Use MCP 프로세스 만들기.exe"
ACTION_LABELS = {"click": "클릭", "double_click": "두 번 클릭", "right_click": "오른쪽 클릭",
    "set_value": "글자 입력", "press_key": "키 누르기", "hotkey": "단축키", "select_option": "목록 선택",
    "set_checked": "체크 변경", "wait_for_element": "요소가 나타날 때까지 대기", "delay": "고정 대기",
    "checkpoint": "화면 이미지 확인"}


class ProcessDraft:
    """No executable code or transient window/element handles can enter a recipe."""
    def __init__(self, programs, task=None):
        self.programs = copy.deepcopy(programs)
        self.targets = {(p["program_id"], p.get("window_ref", "main")): p for p in programs}
        self.selections = {}
        self.steps = copy.deepcopy((task or {}).get("steps", []))
        self.variables = copy.deepcopy((task or {}).get("variables", {}))
        self.labels = [step.get("selector", {}).get("name", "저장된 요소") for step in self.steps]
        if self.steps:
            validate_recipe(self.steps, self.variables, list({p["program_id"] for p in programs}))
            if any((s["program_id"], s.get("window_ref", "main")) not in self.targets for s in self.steps):
                raise OperationError("기존 프로세스의 프로그램과 창을 모두 연결하세요.", "missing_target")

    def target(self, payload):
        key = (payload.get("program_id"), payload.get("window_ref", "main"))
        target = self.targets.get(key)
        if target is None or any(payload.get(k) != target[k] for k in ("pid", "window_id")):
            raise OperationError("편집 창에 연결한 정확한 프로그램과 창을 선택하세요.", "target_mismatch")
        return target

    def selection(self, selection_id, target):
        item = self.selections.get(selection_id) if isinstance(selection_id, str) else None
        if item is None or item["target"] != (target["program_id"], target.get("window_ref", "main")):
            raise OperationError("먼저 이 프로그램의 요소를 선택하세요.", "selection_required")
        return item

    def remember(self, target, snapshot, native):
        if native.get("human_confirmed") is not True:
            raise OperationError("선택 창에서 요소를 확정해야 합니다.", "picker_confirmation_required")
        matched = match_picked_element(snapshot, native, {k: target[k] for k in ("pid", "window_id")})
        element = _unique(snapshot, matched["expected_selector"])
        selector = stable_selector(snapshot, element)
        identity = uuid.uuid4().hex
        view = {"selection_id": identity, "label": _name(element) or selector.get("automation_id", "선택 요소"),
                "role": element["role"], "actions": suggested_operations(element)}
        self.selections[identity] = {**view, "selector": selector,
                                    "target": (target["program_id"], target.get("window_ref", "main"))}
        return copy.deepcopy(view)

    def add(self, payload):
        if set(payload) - {"action", "program_id", "pid", "window_id", "window_ref", "selection_id", "value", "key", "keys",
                           "seconds", "timeout_seconds", "option_order", "expect", "checked"}:
            raise OperationError("지원하지 않는 단계 설정입니다.")
        target = self.target(payload)
        action = payload.get("action")
        if action not in ACTION_LABELS:
            raise OperationError("동작 종류를 선택하세요.")
        step = {"operation": action, "program_id": target["program_id"], "window_ref": target.get("window_ref", "main")}
        label = "현재 창"
        if action == "delay":
            step["duration_ms"] = self.milliseconds(payload.get("seconds", 1))
        elif action == "checkpoint":
            step["message"] = payload.get("value", "현재 화면을 확인하세요.")
        else:
            selected = self.selection(payload.get("selection_id"), target)
            step["selector"], label = copy.deepcopy(selected["selector"]), selected["label"]
            if action == "wait_for_element":
                step["timeout_ms"] = self.milliseconds(payload.get("timeout_seconds", 20))
            elif action in {"set_value", "select_option"}:
                step["value"] = payload.get("value", "")
                if action == "select_option" and payload.get("option_order"):
                    step["option_order"] = copy.deepcopy(payload["option_order"])
            elif action == "set_checked":
                step["checked"] = payload.get("checked")
            elif action == "press_key":
                step["key"] = payload.get("key")
            elif action == "hotkey":
                step["keys"] = copy.deepcopy(payload.get("keys"))
        if "expect" in payload:
            expected = payload["expect"]
            if not isinstance(expected, dict) or set(expected) != {"selection_id", "property", "equals"}:
                raise OperationError("확인할 요소와 예상 결과를 지정하세요.")
            selected = self.selection(expected["selection_id"], target)
            step["expect"] = [{"selector": copy.deepcopy(selected["selector"]),
                               "property": expected["property"], "equals": expected["equals"]}]
        proposed = [*self.steps, step]
        validate_recipe(proposed, self.variables, [p["program_id"] for p in self.programs])
        self.steps, self.labels = proposed, [*self.labels, label]

    @staticmethod
    def milliseconds(seconds):
        if type(seconds) not in {int, float} or not 0 <= seconds <= 60:
            raise OperationError("대기 시간은 0~60초로 지정하세요.")
        return round(seconds * 1000)

    def change(self, action, payload):
        allowed = {"index", "direction"} if action == "move_step" else {"index"}
        index = payload.get("index")
        if set(payload) != allowed or type(index) is not int or not 0 <= index < len(self.steps):
            raise OperationError("목록에서 변경할 단계를 선택하세요.")
        if action == "remove_step":
            self.steps.pop(index)
            self.labels.pop(index)
        else:
            direction = payload["direction"]
            if type(direction) is not int or direction not in {-1, 1} or not 0 <= index + direction < len(self.steps):
                raise OperationError("이 방향으로 단계를 이동할 수 없습니다.")
            other = index + direction
            for values in (self.steps, self.labels):
                values[index], values[other] = values[other], values[index]

    def summaries(self):
        rows = []
        for index, (step, label) in enumerate(zip(self.steps, self.labels)):
            action = step["operation"]
            target = self.targets[(step["program_id"], step.get("window_ref", "main"))]
            detail = (str(step.get("duration_ms", 0)/1000) + "초" if action == "delay" else
                      str(step.get("timeout_ms", 0)/1000) + "초 이내" if action == "wait_for_element" else
                      str(step.get("message", step.get("value", step.get("key", "+".join(step.get("keys", [])))))))
            if step.get("expect"):
                check = step["expect"][0]
                detail += " → 확인: " + str(check["selector"].get("name", check["selector"].get("automation_id", "요소"))) + " = " + str(check["equals"])
            action_label = ACTION_LABELS.get(action, action)
            rows.append({"index": index, "program": target["label"], "label": label, "action": action,
                         "detail": detail, "summary": f"{index+1}. {action_label} · {label} · {detail}"})
        return rows


class ProcessEditors:
    def __init__(self, library, tasks):
        self.library, self.tasks = library, tasks
        self.lock = threading.RLock()
        self.jobs = {}

    def pending(self, runtime=None):
        with self.lock:
            return next((j for j in self.jobs.values() if (runtime is None or j["runtime"] is runtime)
                         and (not j["done"].is_set() or j["result"].get("cleanup_pending"))), None)

    def _view(self, job, **extra):
        with self.lock:
            return copy.deepcopy({**job["result"], "editor_id": job["id"], "input_dispatched": False,
                                  "automatic_retry": False, "steps": job["summaries"], **extra})

    def _check(self, job):
        if job["cancel"].is_set() or job["runtime"].stop_event.is_set():
            raise OperationError("프로세스 만들기를 취소했습니다.", "editor_cancelled")
        job["runtime"].check_active()

    def start(self, runtime, args, cancel_event=None):
        runtime.check_active()
        if runtime.mode != "uia":
            raise OperationError("프로세스 만들기는 UIA 세션에서 시작하세요.", "unsupported_mode")
        programs = []
        for target in args["targets"]:
            if (not isinstance(target, dict) or set(target) - {"program_id", "pid", "window_id", "window_ref"}
                    or not {"program_id", "pid", "window_id"} <= set(target)
                    or not isinstance(target["program_id"], str)
                    or any(type(target[k]) is not int or target[k] < 1 for k in ("pid", "window_id"))
                    or not isinstance(target.get("window_ref", "main"), str)
                    or not WINDOW_REF.fullmatch(target.get("window_ref", "main"))):
                raise OperationError("프로그램과 현재 창을 정확히 지정하세요.")
            self.library._program(runtime, target["program_id"], {k: target[k] for k in ("pid", "window_id")})
            label = next(p["name"] for p in runtime.programs if p["id"] == target["program_id"])
            programs.append({**target, "label": label + " · " + target.get("window_ref", "main")})
        if not 1 <= len(programs) <= 10 or len({(p["program_id"], p.get("window_ref", "main")) for p in programs}) != len(programs):
            raise OperationError("중복 없는 프로그램/창을 1~10개 연결하세요.")
        task = self.tasks.get(args["task_id"]) if args.get("task_id") else None
        draft = ProcessDraft(programs, task)
        with self.lock:
            active = self.pending()
            if active:
                return self._view(active, reused=active["args"] == args, busy=active["args"] != args,
                                  next_tool="computer_process_status")
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "runtime": runtime, "draft": draft, "cancel": _Cancellation(cancel_event),
                   "started": threading.Event(), "done": threading.Event(), "summaries": draft.summaries(),
                   "args": copy.deepcopy(args), "name": args.get("name", (task or {}).get("name", "새 프로세스")),
                   "description": (task or {}).get("instructions", ""), "result": {"status": "starting", "editor_visible": False}}
            self.jobs[job_id] = job
            for key in [k for k, j in self.jobs.items() if j["done"].is_set() and not j["result"].get("cleanup_pending")][:-19]:
                del self.jobs[key]
            job["worker"] = threading.Thread(target=self._work, args=(job,), daemon=True, name="process-editor-" + job_id[:8])
            job["worker"].start()
        if not job["started"].wait(9):
            job["cancel"].set()
            return self._view(job, status="cancelling", message="편집 창 표시를 확인하지 못해 취소하고 있습니다.")
        return self._view(job)

    def _command(self, job, command):
        self._check(job)
        action, payload = command.get("action"), command.get("payload")
        if not isinstance(payload, dict):
            raise OperationError("편집 요청 형식이 올바르지 않습니다.")
        draft, runtime = job["draft"], job["runtime"]
        extra = {}
        if action == "pick_element":
            if set(payload) - {"program_id", "pid", "window_id", "window_ref", "purpose"} or payload.get("purpose") not in {"action", "expect"}:
                raise OperationError("요소 선택의 대상과 용도를 확인하세요.")
            target = draft.target(payload)
            exact = {k: target[k] for k in ("pid", "window_id")}
            self.library._program(runtime, target["program_id"], exact)
            selected = _run_helper(runtime, exact, "동작할 요소" if payload["purpose"] == "action" else "결과를 확인할 요소", 120,
                                   on_ready=lambda _info: self._check(job), cancel_event=job["cancel"])
            with runtime.execution_lock:
                self._check(job)
                snapshot, _binding = self.library._observe(runtime, exact, target["program_id"])
                extra = {"purpose": payload["purpose"], "selection": draft.remember(target, snapshot, selected)}
        elif action == "add_step":
            draft.add(payload)
        elif action in {"remove_step", "move_step"}:
            draft.change(action, payload)
        elif action == "save":
            if set(payload) != {"name", "description"}:
                raise OperationError("프로세스 이름과 설명을 입력하세요.")
            name = _text(payload["name"], "프로세스 이름", 100).strip()
            description = _text(payload["description"], "설명", 4000, empty=True)
            validate_recipe(draft.steps, draft.variables, [p["program_id"] for p in draft.programs])
            # Linearize acceptance against cancellation without holding the
            # editor lock during a potentially blocked task-store write. Stop
            # remains responsive; a save already accepted reports its result.
            with self.lock:
                self._check(job)
                job["save_started"] = True
            saved = self.tasks.save({"name": name, "instructions": description or "저장한 단계 순서대로 실행합니다.",
                "expected": "각 단계의 확인 조건을 통과하고 화면 확인 단계는 사용자가 확인합니다.",
                "program_ids": list(dict.fromkeys(s["program_id"] for s in draft.steps)),
                "steps": draft.steps, "variables": draft.variables})
            extra["saved_task"] = {"id": saved["id"], "name": saved["name"], "step_count": len(saved["steps"])}
            with self.lock:
                job["result"] = {"status": "saved", "editor_visible": True, **extra,
                                 "message": "프로세스를 저장했습니다. 아직 실행하지 않았습니다."}
        else:
            raise OperationError("지원하지 않는 편집 요청입니다.")
        with self.lock:
            job["summaries"] = draft.summaries()
        return {"status": "ok", "message": "저장했습니다." if action == "save" else "반영했습니다.", "steps": draft.summaries(), **extra}

    @staticmethod
    def _check_path(path):
        if not path.is_absolute():
            raise OperationError("편집 교환 경로는 전체 경로여야 합니다.", "editor_unsafe_path")
        for parent in reversed(path.parents):
            ElementLibrary._reject_link(parent)
        ElementLibrary._reject_link(path, file=True)

    @classmethod
    def _write(cls, path, data):
        cls._check_path(path)
        # Each random exchange path has exactly one Python writer. A fixed,
        # exclusive temporary suffix avoids exceeding legacy Windows MAX_PATH.
        temporary = path.with_name(path.name + ".tmp")
        cls._check_path(temporary)
        created = False
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                created = True
                json.dump(data, stream, ensure_ascii=False)
            deadline = time.monotonic() + .5
            while True:
                try:
                    cls._check_path(path)
                    cls._check_path(temporary)
                    os.replace(temporary, path)
                    break
                except OSError as exc:
                    # Windows readers can briefly deny an atomic replacement.
                    # Retry only this same file, never the accepted command.
                    if not cls._sharing_error(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _sharing_error(exc):
        return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {32, 33}

    @classmethod
    def _read(cls, path, nonce):
        deadline = time.monotonic() + .5
        while True:
            try:
                cls._check_path(path)
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise OperationError("편집 응답이 너무 큽니다.", "editor_invalid_response")
                raw = path.read_text(encoding="utf-8-sig")
                break
            except OSError as exc:
                # ReplaceFile can also briefly leave the name unresolved;
                # absence is retried for reads only, within the same bound.
                if not (cls._sharing_error(exc) or isinstance(exc, FileNotFoundError)) or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("nonce") != nonce:
            raise OperationError("편집 창의 실행 정보를 확인하지 못했습니다.", "editor_invalid_response")
        return data

    def _work(self, job):
        child = None
        runtime = job["runtime"]
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        folder = runtime.run_dir / "process-editor"
        # Keep the full nonce inside messages; shorter random filenames leave
        # room for atomic-write suffixes on legacy Windows MAX_PATH runtimes.
        request = folder / (nonce[:16] + ".request.json")
        response = folder / (nonce[:16] + ".response.json")
        ready, command, event = [Path(str(response) + suffix) for suffix in (".ready.json", ".command.json", ".event.json")]
        try:
            self._check(job)
            helper = Path(__file__).resolve().with_name(HELPER_NAME)
            if not helper.is_file():
                raise OperationError("프로세스 편집 실행파일이 없습니다. 새 배포 ZIP 전체를 풀어 실행하세요.", "editor_missing")
            self._check_path(request)
            folder.mkdir(parents=True, exist_ok=True)
            timeout = job["args"].get("timeout_seconds", 600)
            self._write(request, {"nonce": nonce, "timeout_seconds": timeout, "programs": job["draft"].programs,
                                 "draft": {"name": job["name"], "description": job["description"], "steps": job["summaries"]}})
            child = subprocess.Popen([str(helper), "--edit", str(request), str(response)], cwd=str(helper.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            started, sequence = time.monotonic(), 0
            visible = False
            while True:
                self._check(job)
                if not visible and ready.exists():
                    info = self._read(ready, nonce)
                    if (info.get("status") != "ready" or info.get("helper_pid") != child.pid
                            or type(info.get("helper_window_id")) is not int
                            or not _helper_visible(child.pid, info["helper_window_id"])):
                        raise OperationError("프로세스 편집 창 표시를 확인하지 못했습니다.", "editor_not_visible")
                    visible = True
                    with self.lock:
                        job["result"] = {"status": "editing", "editor_visible": True,
                            "helper_pid": child.pid, "helper_window_id": info["helper_window_id"],
                            "next_tool": "computer_process_status", "message": "편집 창에서 요소와 동작을 단계로 추가한 뒤 저장하세요. 작성 중에는 프로그램에 입력을 보내지 않습니다."}
                    job["started"].set()
                if command.exists():
                    data = self._read(command, nonce)
                    seq = data.get("seq")
                    if type(seq) is not int or seq < 1 or seq > sequence + 1:
                        raise OperationError("편집 요청 순서를 확인하지 못했습니다.", "editor_invalid_sequence")
                    if seq == sequence + 1:
                        if not visible or job["result"].get("status") == "saved":
                            raise OperationError("편집 가능한 상태가 아닙니다.", "editor_invalid_state")
                        try:
                            answer = self._command(job, data)
                        except Exception as exc:
                            # A failed nested-picker cleanup is terminal. Do
                            # not turn it into a recoverable form error that
                            # could launch a second picker over the orphan.
                            if getattr(exc, "helper_cleanup_pending", False):
                                raise
                            with self.lock:
                                if job["result"].get("status") != "saved":
                                    job["save_started"] = False
                            self._check(job)
                            answer = {"status": "error", "message": str(exc), "steps": job["draft"].summaries(),
                                      "code": getattr(exc, "code", "editor_command_failed")}
                        self._write(event, {"nonce": nonce, "seq": seq, **answer})
                        sequence = seq
                if response.exists():
                    final = self._read(response, nonce)
                    if job["result"].get("status") != "saved":
                        if final.get("status") not in {"cancelled", "timeout"}:
                            raise OperationError("프로세스 저장 확인 없이 편집 창이 종료되었습니다.", "editor_unsaved")
                        raise OperationError("저장하지 않고 프로세스 만들기를 종료했습니다.", "editor_cancelled")
                    break
                if child.poll() is not None:
                    if job["result"].get("status") == "saved":
                        break
                    raise OperationError("프로세스 편집 창이 응답 없이 종료되었습니다.", "editor_closed")
                if not visible and time.monotonic() - started > 8:
                    raise OperationError("8초 안에 프로세스 편집 창을 표시하지 못했습니다.", "editor_start_timeout")
                if time.monotonic() - started > timeout + 3:
                    raise OperationError("프로세스 작성 시간이 지나 종료했습니다.", "editor_timeout")
                job["cancel"].event.wait(.05)
        except Exception as exc:
            with self.lock:
                if job["result"].get("status") != "saved":
                    job["result"] = {"status": "cancelled" if getattr(exc, "code", "") == "editor_cancelled" else "failed",
                        "message": str(exc), "diagnostic": {"code": getattr(exc, "code", "editor_failed")}, "editor_visible": False}
                    if getattr(exc, "helper_cleanup_pending", False):
                        job["result"].update(cleanup_pending=True, picker_cleanup_pending=True)
        finally:
            try:
                _end_helper(child)
            except Exception:
                with self.lock:
                    job["result"]["cleanup_pending"] = True
                    job["result"]["editor_visible"] = None
            else:
                with self.lock:
                    job["result"]["editor_visible"] = False
            for path in (request, response, ready, command, event):
                try:
                    self._check_path(path)
                    path.unlink(missing_ok=True)
                except (OSError, OperationError):
                    pass
            job["done"].set()
            job["started"].set()

    def status(self, editor_id, *, wait_ms=0, cancel=False):
        if type(wait_ms) is not int or not 0 <= wait_ms <= 5000 or type(cancel) is not bool:
            raise OperationError("상태 대기 시간은 0~5000ms로 지정하세요.")
        with self.lock:
            job = self.jobs.get(editor_id)
            if job is None:
                raise OperationError("이 연결의 프로세스 편집 ID를 찾지 못했습니다.", "editor_not_found")
            if cancel:
                job["cancel"].set()
        job["done"].wait(wait_ms/1000)
        return self._view(job, **({"status": "cancelling", "save_already_started": bool(job.get("save_started")),
            "message": "저장이 이미 시작되어 결과를 확인한 뒤 창을 닫습니다." if job.get("save_started") else "저장하지 않고 편집 창을 닫고 있습니다."}
            if cancel and not job["done"].is_set() else {}))

    def stop(self, runtime=None):
        with self.lock:
            for job in self.jobs.values():
                if runtime is None or job["runtime"] is runtime:
                    job["cancel"].set()

    def close(self, timeout=3):
        self.stop()
        deadline = time.monotonic() + timeout
        with self.lock:
            workers = [job["worker"] for job in self.jobs.values()]
        for worker in workers:
            worker.join(max(0, deadline-time.monotonic()))
        return self.pending() is None
