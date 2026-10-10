"""Bounded declarative UI workflows; no scripts, credentials or raw handles saved."""
from __future__ import annotations

import copy
from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from vendor.guard import atomic_json, check_app, utc_now
from process_steps import PROCESS_OPERATIONS, execute_process_step, validate_process_step
from image_steps import IMAGE_OPERATIONS, IMAGE_MUTATIONS, execute_image_step, validate_image_step, validate_image_verification
from recording_windows import validate_window_wait, dynamic_window_keys, execute_window_wait


class WorkflowError(ValueError):
    pass


VARIABLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]{0,39})\}")
MAX_STEPS = 30
WINDOW_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class BindingError(WorkflowError):
    pass


def operation_step(step):
    return {k: v for k, v in step.items() if k not in {"program_id", "window_ref"}}


def validate_workflow_step(step):
    from operations import validate_step
    operation = operation_step(step)
    kind = operation.get("operation")
    if kind == "wait_for_window":
        return validate_window_wait(step)
    if isinstance(kind, str) and kind in IMAGE_OPERATIONS:
        return validate_image_step(operation)
    return validate_process_step(operation) if isinstance(kind, str) and kind in PROCESS_OPERATIONS else validate_step(operation)


def target_key(step):
    return step["program_id"], step.get("window_ref", "main")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def deferred_input(step):
    """Delivery is checked by the immediately following explicit checkpoint."""
    return (step.get("operation") in IMAGE_MUTATIONS and not step.get("expect")) or (
        step.get("operation") in {"click", "double_click", "right_click"} and step.get("completion_mode") == "human")


def validate_recipe(steps, variables, program_ids):
    from operations import validate_step
    if not isinstance(variables, dict) or len(variables) > 30:
        raise WorkflowError("입력 변수는 최대 30개까지 지정할 수 있습니다.")
    for key, spec in variables.items():
        if not isinstance(key, str) or not VARIABLE.fullmatch(key) or not isinstance(spec, dict):
            raise WorkflowError("입력 변수의 이름과 형식을 확인하세요.")
        if set(spec) - {"description", "default"} or not isinstance(spec.get("description", ""), str):
            raise WorkflowError("입력 변수에는 설명과 기본값만 사용할 수 있습니다.")
        if any(not isinstance(v, str) or len(v) > 4000 for v in spec.values()):
            raise WorkflowError("입력 변수의 설명과 기본값은 4,000자 이하 문자열이어야 합니다.")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise WorkflowError("반복 작업은 1~30단계로 지정하세요.")
    popup_owners = {target_key(step): step.get("owner_ref") for step in steps
        if isinstance(step, dict) and step.get("operation") == "wait_for_window" and "program_id" in step}
    def linked_image_checkpoint(image, checkpoint):
        if not isinstance(checkpoint, dict) or checkpoint.get("operation") != "checkpoint": return False
        if "opened_from" in checkpoint: return False
        if "return_from" not in checkpoint: return target_key(checkpoint) == target_key(image)
        return (checkpoint.get("program_id") == image["program_id"]
            and checkpoint["return_from"] == image.get("window_ref", "main")
            and popup_owners.get(target_key(image)) == checkpoint.get("window_ref", "main"))
    def linked_popup_checkpoint(image, wait, checkpoint):
        if not all(isinstance(item, dict) for item in (image, wait, checkpoint)): return False
        return (deferred_input(image)
            and wait.get("operation") == "wait_for_window" and checkpoint.get("operation") == "checkpoint"
            and wait.get("program_id") == image.get("program_id") == checkpoint.get("program_id")
            and wait.get("owner_ref") == image.get("window_ref", "main") == checkpoint.get("opened_from")
            and wait.get("window_ref") == checkpoint.get("window_ref", "main")
            and "return_from" not in checkpoint)
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("program_id") not in program_ids:
            raise WorkflowError("각 단계에는 작업에 등록된 program_id가 필요합니다.")
        ref = step.get("window_ref", "main")
        if not isinstance(ref, str) or not WINDOW_REF.fullmatch(ref):
            raise WorkflowError("window_ref는 영문자로 시작하는 1~64자 창 이름이어야 합니다.")
        validate_workflow_step(step)
        if deferred_input(step):
            direct = index+1 < len(steps) and linked_image_checkpoint(step, steps[index+1])
            opened = index+2 < len(steps) and linked_popup_checkpoint(step, steps[index+1], steps[index+2])
            if not direct and not opened:
                raise WorkflowError("직접 확인할 입력 뒤에는 같은 창의 화면 확인 또는 소유 팝업 연결과 해당 팝업의 화면 확인이 필요합니다.")
        if step.get("return_from") is not None and (index == 0 or not deferred_input(steps[index-1])
                or not linked_image_checkpoint(steps[index-1], step)):
            raise WorkflowError("돌아온 창 확인은 해당 소유 팝업의 이미지 동작 바로 다음에만 사용할 수 있습니다.")
        if "opened_from" in step and (index < 2 or not linked_popup_checkpoint(steps[index-2], steps[index-1], step)):
            raise WorkflowError("새 팝업 화면 확인은 원래 창의 이미지 동작과 해당 소유 팝업 연결 바로 뒤에만 사용할 수 있습니다.")
        # Templates may only substitute strings, never keys or the tool/program identity.
        if "${" in step["program_id"] or "${" in step.get("operation", "") or "${" in ref:
            raise WorkflowError("프로그램이나 동작 이름은 입력 변수로 바꿀 수 없습니다.")
        refs = set(PLACEHOLDER.findall(json.dumps(step, ensure_ascii=False)))
        if refs - set(variables):
            raise WorkflowError("정의되지 않은 입력 변수가 있습니다: " + ", ".join(sorted(refs - set(variables))))
    dynamic_window_keys(steps)
    return copy.deepcopy(steps), copy.deepcopy(variables)


def render_steps(task, inputs):
    steps, variables = validate_recipe(task.get("steps"), task.get("variables", {}), task["program_ids"])
    if not isinstance(inputs, dict) or set(inputs) - set(variables):
        raise WorkflowError("저장된 작업에 정의된 입력 변수만 지정하세요.")
    values = {}
    for key, spec in variables.items():
        value = inputs.get(key, spec.get("default"))
        if not isinstance(value, str) or len(value) > 4000:
            raise WorkflowError("필수 입력값을 확인하세요: " + key)
        values[key] = value
    def substitute(value):
        if isinstance(value, str):
            return PLACEHOLDER.sub(lambda m: values[m[1]], value)
        if isinstance(value, list):
            return [substitute(v) for v in value]
        if isinstance(value, dict):
            return {k: substitute(v) for k, v in value.items()}
        return value
    rendered = substitute(steps)
    # Validate expanded sizes and fields without evaluating substituted text again.
    from operations import validate_step
    for step in rendered:
        validate_workflow_step(step)
    return rendered, values


class WorkflowRunner:
    FIELDS = {"format", "run_id", "task_id", "revision", "recipe_hash", "inputs_hash", "created_at", "updated_at",
              "completed_steps", "total_steps", "pending_step", "status", "task_verified", "session_id", "duration_ms"}
    OPTIONAL_FIELDS = {"checkpoint", "image_verification", "step_delivery"}
    def __init__(self, state_dir):
        self.root = Path(state_dir) / "workflows"
        from repeat_profiles import RepeatProfiles
        self.repeat_profiles = RepeatProfiles(state_dir)

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise WorkflowError("반복 작업 실행 ID가 올바르지 않습니다.")
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.root, self.root / (run_id + ".json"), self.root / (run_id + ".lock")):
            if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise WorkflowError("실행 기록에 연결 경로를 사용할 수 없습니다.")
        return self.root / (run_id + ".json")

    def progress(self, run_id):
        path = self._path(run_id)
        try:
            if path.stat().st_size > 64000:
                raise ValueError("size")
            value = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or not self.FIELDS <= set(value) or set(value)-self.FIELDS-self.OPTIONAL_FIELDS
                    or value.get("format") != "computer-workflow/v1" or value.get("run_id") != run_id
                    or type(value.get("completed_steps")) is not int
                    or type(value.get("total_steps")) is not int
                    or not 0 <= value["completed_steps"] <= value.get("total_steps", -1) <= MAX_STEPS):
                raise ValueError("format")
            if (value["status"] not in {"running", "needs_review", "needs_binding", "interrupted", "verified"}
                    or type(value["task_verified"]) is not bool
                    or type(value["revision"]) is not int or value["revision"] < 1
                    or any(not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in ("recipe_hash", "inputs_hash"))
                    or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 100 for k in ("task_id", "session_id", "created_at", "updated_at"))
                    or type(value["duration_ms"]) not in (int, float) or not 0 <= value["duration_ms"] < 1e12
                    or (value["pending_step"] is not None and (type(value["pending_step"]) is not int or value["pending_step"] != value["completed_steps"] or value["pending_step"] >= value["total_steps"]))
                    or value["task_verified"] != (value["status"] == "verified")
                    or (value["task_verified"] and (value["completed_steps"] != value["total_steps"] or value["pending_step"] is not None))):
                raise ValueError("metadata")
            if "checkpoint" in value:
                checkpoint = value["checkpoint"]
                if (not isinstance(checkpoint, dict) or not {"id", "step_index", "capture_available", "target_hash"} <= set(checkpoint)
                        or set(checkpoint)-{"id", "step_index", "capture_available", "target_hash", "source_step_index"}
                        or not isinstance(checkpoint["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", checkpoint["id"])
                        or type(checkpoint["step_index"]) is not int or checkpoint["step_index"] != value["pending_step"]
                        or type(checkpoint["capture_available"]) is not bool
                        or not isinstance(checkpoint["target_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", checkpoint["target_hash"])):
                    raise ValueError("checkpoint")
                if "source_step_index" in checkpoint and (type(checkpoint["source_step_index"]) is not int
                        or not 0 <= checkpoint["source_step_index"] < checkpoint["step_index"]):
                    raise ValueError("resume_checkpoint")
            if "image_verification" in value:
                proof = value["image_verification"]
                if (not isinstance(proof, dict) or set(proof) != {"step_index", "state"}
                        or type(proof["step_index"]) is not int or not 0 <= proof["step_index"] < value["total_steps"]):
                    raise ValueError("image_verification")
                validate_image_verification(proof["state"])
            if "step_delivery" in value:
                proof = value["step_delivery"]
                if (not isinstance(proof, dict) or set(proof) != {"step_index", "state"}
                        or type(proof["step_index"]) is not int or not 0 <= proof["step_index"] < value["total_steps"]
                        or proof["state"] not in {"not_sent", "sent", "unknown"}):
                    raise ValueError("step_delivery")
            return value
        except (OSError, ValueError, AttributeError, TypeError):
            raise WorkflowError("실행 기록을 읽지 못했습니다. 원본은 유지됩니다.") from None

    def recent(self, task_id=None):
        if task_id is not None and (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id)):
            raise WorkflowError("작업 ID가 올바르지 않습니다.")
        if not self.root.exists():
            return {"runs": [], "invalid_records": 0, "scan_limited": False}
        self._path("0" * 32)  # Reject a linked workflow folder before enumeration.
        paths = []
        scan_limited = False
        for path in self.root.glob("*.json"):
            if re.fullmatch(r"[a-f0-9]{32}", path.stem):
                paths.append(path)
            if len(paths) >= 10000:
                scan_limited = True
                break
        paths.sort(key=lambda p: p.lstat().st_mtime_ns, reverse=True)
        runs, invalid = [], 0
        for path in paths:
            try:
                item = self.progress(path.stem)
            except WorkflowError:
                invalid += 1
                continue
            if task_id is None or item["task_id"] == task_id:
                runs.append(item)
                if len(runs) == 10:
                    break
        return {"runs": runs, "invalid_records": invalid, "scan_limited": scan_limited}

    @contextmanager
    def _lock(self, path):
        with path.with_suffix(".lock").open("a+b") as stream:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise WorkflowError("이 반복 작업이 다른 요청에서 실행 중입니다.") from None
            try:
                # Empty files still support a Windows byte-range lock. Never
                # write the initialization byte before owning that range.
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def run(self, runtime, task, inputs, targets, *, resume_run_id=None, delivery_mode="background", acknowledge_checkpoint=None, execution_mode="auto"):
        # Nested low-level calls use the same reentrant runtime lock. Emergency
        # stop never acquires this lock, so a composite operation stays cancellable.
        with getattr(runtime, "execution_lock", nullcontext()):
            if execution_mode not in {"auto", "standard", "fast"}:
                raise WorkflowError("execution_mode는 auto, standard, fast 중 하나입니다.")
            signature = self.repeat_profiles.signature(task, runtime.programs, delivery_mode)
            profile = self.repeat_profiles.read(signature)
            use_fast = execution_mode != "standard" and profile is not None and resume_run_id is None
            previous_mode = getattr(runtime, "fast_verification_enabled", False)
            runtime.fast_verification_enabled = use_fast
            try:
                metrics = {"measured_steps": 0}
                answer = self._run(runtime, task, inputs, targets, resume_run_id=resume_run_id, delivery_mode=delivery_mode,
                                   acknowledge_checkpoint=acknowledge_checkpoint, metrics=metrics)
                answer["metrics"] = metrics
                answer["execution"] = {"requested_mode": execution_mode, "mode": "fast" if use_fast else "standard",
                    "prior_verified_runs": profile.get("successes", 0) if profile else 0,
                    "profile_matched": profile is not None, "checks_skipped": False,
                    "reason": "verified_recipe_reused" if use_fast else "resume_reobserves" if resume_run_id else
                              "standard_requested" if execution_mode == "standard" else "first_run_or_changed_recipe",
                    "fixed_delay_ms": sum(s.get("duration_ms", 0) for s in task.get("steps", []) if s.get("operation") == "delay")}
                try:
                    # Incomplete/uncertain work disables the optimization until
                    # a later full verified run. It never triggers a replay.
                    self.repeat_profiles.record(signature, answer)
                except (OSError, ValueError):
                    answer["execution"]["profile_saved"] = False
                else:
                    answer["execution"]["profile_saved"] = True
                return answer
            except Exception:
                # Validation/binding failures before the persisted run also
                # withdraw the optimization hint. Do not retain exception text
                # or submitted input values in this performance profile.
                try:
                    self.repeat_profiles.record(signature, {"task_verified": False, "status": "rejected"})
                except (OSError, ValueError):
                    pass
                raise
            finally:
                runtime.fast_verification_enabled = previous_mode

    def _run(self, runtime, task, inputs, targets, *, resume_run_id=None, delivery_mode="background", acknowledge_checkpoint=None, metrics=None):
        from operations import Operations, verification_step
        if runtime.mode != "uia":
            raise WorkflowError("검증형 반복 작업은 현재 UIA 방식에서만 지원합니다.")
        runtime.check_active()
        if delivery_mode not in {"background", "foreground"}:
            raise WorkflowError("입력 전달 방식을 확인하세요.")
        if acknowledge_checkpoint is not None and (not resume_run_id or not isinstance(acknowledge_checkpoint, str)
                                                   or not re.fullmatch(r"[a-f0-9]{32}", acknowledge_checkpoint)):
            raise WorkflowError("화면 확인을 승인하려면 resume_run_id와 해당 checkpoint.id를 함께 지정하세요.")
        steps, values = render_steps(task, inputs)
        apps = {app["id"]: app for app in runtime.programs}
        if not set(task["program_ids"]) <= set(apps):
            raise WorkflowError("작업에 필요한 모든 프로그램으로 computer_begin을 먼저 실행하세요.")
        if not isinstance(targets, list):
            raise WorkflowError("프로그램별 현재 창을 지정하세요.")
        bindings = {}
        for item in targets:
            if (not isinstance(item, dict) or set(item) - {"program_id", "window_ref", "pid", "window_id", "window_title"}
                    or not {"program_id", "pid"} <= set(item)
                    or item["program_id"] not in task["program_ids"]
                    or type(item["pid"]) is not int or item["pid"] < 1
                    or not isinstance(item.get("window_ref", "main"), str) or not WINDOW_REF.fullmatch(item.get("window_ref", "main"))):
                raise WorkflowError("프로그램과 창 이름별로 현재 pid 및 window_id 또는 정확한 window_title을 지정하세요.")
            if ("window_id" in item) == ("window_title" in item):
                raise WorkflowError("window_id와 window_title 중 하나만 지정하세요.")
            if ("window_id" in item and (type(item["window_id"]) is not int or item["window_id"] < 1)
                    or "window_title" in item and (not isinstance(item["window_title"], str) or not item["window_title"].strip() or len(item["window_title"]) > 1000)):
                raise WorkflowError("창 번호 또는 정확한 창 제목을 확인하세요.")
            key = target_key(item)
            if key in bindings:
                raise WorkflowError("같은 프로그램의 같은 창 이름을 중복 지정할 수 없습니다.")
            app = apps[item["program_id"]]
            allowed = {check_app(p) for p in [app["exe"], *app.get("control_exes", [])]}
            if check_app(runtime.guard.process_resolver(item["pid"])) not in allowed:
                raise WorkflowError("지정한 창의 프로그램이 작업에 저장된 프로그램과 다릅니다.")
            bindings[key] = {k: item[k] for k in ("pid", "window_id", "window_title") if k in item}
        dynamic_keys = dynamic_window_keys(steps)
        dynamic_definitions = {target_key(step): step for step in steps if step["operation"] == "wait_for_window"}
        all_keys = {target_key(step) for step in steps} | {(step["program_id"], step["owner_ref"])
            for step in steps if step["operation"] == "wait_for_window"}
        if not (all_keys - dynamic_keys) <= set(bindings) or set(bindings) - all_keys:
            raise WorkflowError("작업 단계에 사용한 모든 프로그램/창 이름에 맞춰 현재 창을 지정하세요.")

        def resolve(item):
            binding = bindings.get(target_key(item))
            dynamic_target = None
            if target_key(item) in dynamic_definitions:
                definition = dynamic_definitions[target_key(item)]
                owner_item = {"program_id": item["program_id"], "window_ref": definition["owner_ref"]}
                owner = resolve(owner_item)
                bindings[target_key(owner_item)] = owner
                answer = execute_window_wait(runtime, {**definition, "timeout_ms": 0}, bindings)
                if answer.get("task_verified") is not True:
                    raise BindingError("저장한 소유 관계와 제목·종류에 맞는 팝업을 확인하지 못했습니다. 현재 창을 다시 지정하세요.")
                dynamic_target = answer["target"]
                if binding is None:
                    binding = bindings[target_key(item)] = dynamic_target
            if binding is None:
                raise BindingError("이 팝업의 현재 창이 아직 연결되지 않았습니다. 현재 창을 다시 지정하세요.")
            app = apps[item["program_id"]]
            allowed = {check_app(p) for p in [app["exe"], *app.get("control_exes", [])]}
            try:
                current_executable = check_app(runtime.guard.process_resolver(binding["pid"]))
            except (OSError, ValueError, RuntimeError) as error:
                raise BindingError("연결한 프로그램의 실행 상태를 확인하지 못했습니다. 현재 창을 다시 지정하세요.") from error
            if current_executable not in allowed:
                raise BindingError("연결한 프로그램의 실행 상태가 달라졌습니다. 현재 창을 다시 지정하세요.")
            def checked(target):
                # Resume targets and cached bindings are only hints. They must
                # still name the unique popup described by its recorded owner.
                if dynamic_target is not None and target != dynamic_target:
                    raise BindingError("지정한 팝업이 저장한 소유 관계와 제목·종류로 확인한 창과 다릅니다. 입력하지 않았습니다.")
                resolver = getattr(runtime.guard, "window_resolver", None)
                try:
                    if not callable(resolver) or resolver(target["window_id"]) != target["pid"]:
                        raise BindingError("연결한 창이 없어졌거나 다른 프로그램의 창으로 바뀌었습니다. 현재 창을 다시 지정하세요.")
                except (OSError, RuntimeError) as error:
                    raise BindingError("현재 창의 소유 프로그램을 확인하지 못했습니다. 현재 창을 다시 지정하세요.") from error
                return target
            if "window_id" in binding:
                return checked(dict(binding))
            answer = runtime.call("list_windows", {"pid": binding["pid"], "on_screen_only": True})
            from operations import _payload
            data = _payload(answer)
            if answer.get("isError"):
                raise BindingError("현재 창 목록을 확인하지 못했습니다. 입력하지 않았습니다.")
            windows = data.get("windows")
            if not isinstance(windows, list):
                raise BindingError("현재 창 목록 형식을 확인하지 못했습니다.")
            matches = [w for w in windows if isinstance(w, dict) and w.get("pid") == binding["pid"] and w.get("title") == binding["window_title"]]
            if len(matches) != 1 or type(matches[0].get("window_id")) is not int or matches[0]["window_id"] < 1:
                raise BindingError("지정한 제목의 창이 없거나 여러 개입니다. 현재 창을 다시 관찰해 연결하세요.")
            return checked({"pid": binding["pid"], "window_id": matches[0]["window_id"]})
        run_id = resume_run_id or uuid.uuid4().hex
        path = self._path(run_id)
        with self._lock(path):
            recipe_hash = digest(task)
            input_hash = digest(values)
            if resume_run_id:
                record = self.progress(run_id)
                if record.get("recipe_hash") != recipe_hash or record.get("inputs_hash") != input_hash:
                    raise WorkflowError("작업 내용이나 입력값이 바뀌었습니다. 이전 실행을 이어갈 수 없습니다.")
                if acknowledge_checkpoint is not None:
                    checkpoint = record.get("checkpoint", {})
                    source_index = checkpoint.get("source_step_index", record["pending_step"])
                    if (checkpoint.get("id") != acknowledge_checkpoint or checkpoint.get("capture_available") is not True
                            or record["pending_step"] is None or type(source_index) is not int
                            or not 0 <= source_index < len(steps) or steps[source_index]["operation"] != "checkpoint"):
                        raise WorkflowError("현재 대기 중인 화면 확인 ID와 일치하지 않습니다. 최신 체크포인트를 확인하세요.")
                    if "source_step_index" in checkpoint and record.get("step_delivery") != {
                            "step_index": record["pending_step"], "state": "not_sent"}:
                        raise WorkflowError("입력 미전달이 확인되지 않아 이전 화면 확인으로 재실행할 수 없습니다.")
                    current_target = resolve(steps[source_index])
                    expected_target = digest({"session_id": runtime.id, **current_target})
                    window_resolver = getattr(runtime.guard, "window_resolver", None)
                    if (checkpoint["target_hash"] != expected_target or not callable(window_resolver)
                            or window_resolver(current_target["window_id"]) != current_target["pid"]):
                        raise WorkflowError("캡처 이후 세션이나 대상 창이 달라졌습니다. 승인 ID 없이 이어가기로 화면을 다시 확인하세요.")
            else:
                record = {"format": "computer-workflow/v1", "run_id": run_id, "task_id": task["id"],
                    "revision": task.get("revision", 1), "recipe_hash": recipe_hash, "inputs_hash": input_hash,
                    "created_at": utc_now(), "completed_steps": 0, "total_steps": len(steps),
                    "pending_step": None, "status": "running", "task_verified": False}
            record["session_id"] = runtime.id
            engine = Operations(runtime)
            started = time.monotonic()
            latest = None
            stage = "resume_verification"
            def measured(result):
                observed = result.get("metrics", {})
                if metrics is not None and isinstance(observed, dict) and observed:
                    metrics["measured_steps"] += 1
                    for key in ("tool_calls", "observations", "discovery_calls", "mutations", "observation_ms",
                                "discovery_ms", "action_ms", "expanded_observations", "reused_observations",
                                "focus_actions", "focus_ms", "scoped_observations", "elapsed_ms",
                                "transition_ms", "transition_probe_ms", "transition_discovery_ms", "transition_verification_ms"):
                        value = observed.get(key)
                        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                            metrics[key] = round(metrics.get(key, 0) + value, 2)
                return result
            def adopt_transition(item, result):
                transition = result.get("transition", {})
                if result.get("task_verified") is not True or transition.get("state") != "verified":
                    return
                key = target_key(item)
                previous = bindings[key]
                target = transition.get("target")
                if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                        or any(type(v) is not int or v < 1 for v in target.values())
                        or target["pid"] != previous["pid"]):
                    raise BindingError("검증한 창 전환의 연결 정보를 확인하지 못했습니다.")
                if key in dynamic_definitions:
                    # A verified close may return to the owner. Validate that
                    # result, but never rename the owner as the recorded popup:
                    # later popup steps must still resolve its own definition.
                    app = apps[item["program_id"]]
                    allowed = {check_app(p) for p in [app["exe"], *app.get("control_exes", [])]}
                    resolver = getattr(runtime.guard, "window_resolver", None)
                    try:
                        if (check_app(runtime.guard.process_resolver(target["pid"])) not in allowed
                                or not callable(resolver) or resolver(target["window_id"]) != target["pid"]):
                            raise BindingError("팝업 동작 후 확인한 창의 현재 소유 프로그램이 다릅니다.")
                    except (OSError, ValueError, RuntimeError) as error:
                        raise BindingError("팝업 동작 후 확인한 창의 현재 소유 프로그램을 확인하지 못했습니다.") from error
                    return
                bindings[key] = dict(target)
                resolve(item)  # Recheck allowed executable and current HWND owner.
            def save(status):
                record.update(status=status, updated_at=utc_now(), duration_ms=round((time.monotonic()-started)*1000, 2))
                atomic_json(path, record)
            def progress_event(phase, index=None, operation=None):
                report = getattr(runtime, "report_progress", None)
                if callable(report):
                    report(phase, run_id=run_id, step_index=(index + 1 if index is not None else record["completed_steps"] + 1),
                        step_total=len(steps), **({"operation": operation} if operation else {}))
            def save_image_state(index, state):
                record["image_verification"] = {"step_index": index, "state": state}
                save("running")
            def check_step(index):
                item = steps[index]
                if deferred_input(item) and item["operation"] not in IMAGE_MUTATIONS:
                    return {"task_verified": False, "input_dispatched": None,
                        "diagnostic": {"code": "input_action_uncertain", "automatic_replay": False,
                            "message": "중단된 입력의 적용 여부가 불명확합니다. 같은 입력을 반복하지 않았습니다."}}
                if item["operation"] in IMAGE_MUTATIONS:
                    if item.get("expect"):
                        proof = record.get("image_verification", {})
                        return measured(execute_image_step(runtime, operation_step(item), resolve(item),
                            verification_state=proof.get("state") if proof.get("step_index") == index else None,
                            save_verification=lambda state: save_image_state(index, state), verify_only=True))
                    return {"task_verified": False, "input_dispatched": False,
                            "diagnostic": {"code": "image_action_uncertain", "automatic_replay": False,
                                           "message": "중단된 이미지 입력의 적용 여부를 자동 판단할 수 없습니다. 입력을 재실행하지 않았습니다."}}
                if item["operation"] == "wait_for_window":
                    owner_item = {"program_id": item["program_id"], "window_ref": item["owner_ref"]}
                    bindings[target_key(owner_item)] = resolve(owner_item)
                    answer = measured(execute_window_wait(runtime, {**item, "timeout_ms": 0}, bindings))
                    if answer.get("task_verified") is True:
                        bindings[target_key(item)] = answer["target"]
                        resolve(item)
                    return answer
                if item["operation"] == "wait_for_image":
                    return measured(execute_image_step(runtime, {**operation_step(item), "timeout_ms": 0}, resolve(item)))
                if item["operation"] in {"delay", "checkpoint"}:
                    return {"task_verified": True, "input_dispatched": False}
                if item["operation"] == "wait_for_element":
                    return measured(execute_process_step(runtime, {**operation_step(item), "timeout_ms": 0}, resolve(item)))
                if item["operation"] == "wait_for_state":
                    # A past change baseline is not persisted; assertions with
                    # require_change deliberately stay unverified on resume.
                    return measured(engine.execute({"operation": "assert", "expect": item["expect"]}, resolve(item), delivery_mode=delivery_mode))
                return measured(engine.execute(verification_step(operation_step(item)), resolve(item), delivery_mode=delivery_mode))
            def check_prior(index):
                for candidate in range(index-1, -1, -1):
                    # A successfully delivered image mutation intentionally
                    # proceeds to its mandatory human screenshot checkpoint.
                    if deferred_input(steps[candidate]):
                        return {"task_verified": True, "input_dispatched": False, "verification_deferred": True}
                    if steps[candidate]["operation"] not in {"delay", "checkpoint"}:
                        return check_step(candidate)
                runtime.check_active()
                return {"task_verified": True, "input_dispatched": False}
            def capture_checkpoint(index, target, *, source_index=None):
                item = steps[index if source_index is None else source_index]
                checkpoint = {"id": uuid.uuid4().hex, "step_index": index, "capture_available": False,
                              "target_hash": digest({"session_id": runtime.id, **target})}
                if source_index is not None:
                    checkpoint["source_step_index"] = source_index
                record["checkpoint"] = checkpoint
                save("running")
                prepared = None
                if delivery_mode == "foreground":
                    from replay_preflight import prepare_image_foreground
                    prepared = prepare_image_foreground(runtime, target)
                captured = measured(prepared if prepared is not None else
                                    execute_process_step(runtime, operation_step(item), target))
                checkpoint["capture_available"] = captured.pop("checkpoint_ready", False) is True
                images = captured.pop("checkpoint_content", [])
                save("needs_review")
                return {**record, "last_result": captured,
                        "checkpoint": {**checkpoint, "message": item["message"], "human_review_required": True,
                                       "image_verified": False},
                        "checkpoint_content": images,
                        "next_step": ("이미지를 확인한 뒤 같은 run_id와 checkpoint.id를 acknowledge_checkpoint로 지정해 이어가세요. 승인 없이 다음 단계는 실행하지 않습니다."
                                      if checkpoint["capture_available"] else
                                      "화면 캡처를 완료하지 못했습니다. 진단 안내를 해결한 뒤 같은 run_id로 다시 이어가세요. 승인 ID를 보내지 마세요.")}
            try:
                if resume_run_id:
                    # An in-flight write may already be applied. Only inspect it, never replay it.
                    pending = record.get("pending_step")
                    if pending is not None:
                        if type(pending) is not int or pending != record["completed_steps"] or pending >= len(steps):
                            raise WorkflowError("실행 기록의 진행 지점이 올바르지 않습니다.")
                        special = steps[pending]["operation"] in PROCESS_OPERATIONS | {"wait_for_image", "wait_for_window"}
                        proof = record.get("step_delivery", {})
                        not_sent = (not special and proof.get("step_index") == pending
                                    and proof.get("state") == "not_sent")
                        # Only a durable, explicit non-delivery result permits
                        # this pending step to run. The normal loop resolves the
                        # current window and freshly observes/matches its target.
                        latest = check_prior(pending) if not_sent or special else check_step(pending)
                        if latest.get("task_verified") is not True:
                            save("needs_review")
                            return {**record, "last_result": latest, "next_step": "미확인 단계 또는 이전 확인 지점의 현재 결과를 확인하지 못했습니다. 입력을 재실행하지 않았습니다."}
                        if not_sent and latest.get("verification_deferred") is True:
                            # A prior image click has no machine-readable result.
                            # A stale acknowledgement cannot prove that its
                            # state survived the pause: show a fresh checkpoint.
                            prior_checkpoint = next((candidate for candidate in range(pending-1, -1, -1)
                                if steps[candidate]["operation"] == "checkpoint"), None)
                            checkpoint = record.get("checkpoint", {})
                            fresh_ack = (acknowledge_checkpoint is not None
                                and checkpoint.get("source_step_index") == prior_checkpoint)
                            if not fresh_ack:
                                if prior_checkpoint is None:
                                    raise WorkflowError("앞선 이미지 동작의 현재 결과를 확인할 화면 확인 단계가 없습니다.")
                                return capture_checkpoint(pending, resolve(steps[prior_checkpoint]), source_index=prior_checkpoint)
                            record.pop("checkpoint", None)
                        if not not_sent and (not special or steps[pending]["operation"] == "checkpoint" and acknowledge_checkpoint is not None):
                            record["completed_steps"] += 1
                            record["pending_step"] = None
                            record.pop("checkpoint", None)
                    elif record["completed_steps"]:
                        latest = check_prior(record["completed_steps"])
                        if latest.get("task_verified") is not True:
                            record["task_verified"] = False
                            save("needs_review")
                            return {**record, "last_result": latest, "next_step": "마지막 확인 지점의 현재 화면이 달라 이어가기를 중지했습니다."}
                for index in range(record["completed_steps"], len(steps)):
                    stage = "session_check"
                    runtime.check_active()
                    item = steps[index]
                    progress_event("running_step", index, item["operation"])
                    stage = "window_binding"
                    target = resolve(item) if item["operation"] != "wait_for_window" else None
                    record["pending_step"] = index
                    # Persist unknown before crossing the operation boundary.
                    # A process crash must never turn an attempted input into a
                    # safe retry. Only a returned explicit false can prove it.
                    record["step_delivery"] = {"step_index": index, "state": "unknown"}
                    record["task_verified"] = False
                    stage = "checkpoint_before_input"
                    save("running")
                    stage = "operation"
                    if item["operation"] == "checkpoint":
                        stage = "screenshot_checkpoint"
                        return capture_checkpoint(index, target)
                    if item["operation"] == "wait_for_window":
                        progress_event("waiting", index, item["operation"])
                        owner_item = {"program_id": item["program_id"], "window_ref": item["owner_ref"]}
                        bindings[target_key(owner_item)] = resolve(owner_item)
                        latest = execute_window_wait(runtime, item, bindings)
                        if latest.get("task_verified") is True:
                            bindings[target_key(item)] = latest["target"]
                            resolve(item)
                        engine = Operations(runtime)
                    elif item["operation"] in IMAGE_OPERATIONS:
                        latest = execute_image_step(runtime, operation_step(item), target,
                            save_verification=lambda state: save_image_state(index, state),
                            prepare_foreground=delivery_mode == "foreground")
                        engine = Operations(runtime)
                    elif item["operation"] in PROCESS_OPERATIONS:
                        latest = execute_process_step(runtime, operation_step(item), target)
                        engine = Operations(runtime)  # Never reuse a pre-wait observation for later input.
                    else:
                        latest = engine.execute(operation_step(item), target, delivery_mode=delivery_mode, reuse_verified=True)
                    measured(latest)
                    sent = latest.get("input_dispatched")
                    record["step_delivery"] = {"step_index": index,
                        "state": "sent" if sent is True else "not_sent" if sent is False else "unknown"}
                    if sent is False and index == 0:
                        diagnostic = latest.get("diagnostic", {})
                        source = latest.get("input_result", {}).get("diagnostic", diagnostic)
                        if source.get("code") in {"image_not_found", "image_ambiguous", "element_not_found"}:
                            latest = {**latest, "diagnostic": {**source, "code": "start_screen_not_ready",
                                "cause": source["code"], "automatic_replay": False,
                                "message": "녹화한 첫 동작의 대상을 현재 화면에서 확인하지 못했습니다. "
                                    "해당 업무 화면을 연 뒤 같은 실행에서 이어가세요. 아직 업무 입력을 보내지 않았으며 재녹화는 필요하지 않습니다."}}
                    adopt_transition(item, latest)
                    if latest.get("task_verified") is not True and not (deferred_input(item)
                            and latest.get("verification_deferred") is True and latest.get("input_dispatched") is True):
                        progress_event("needs_review", index, item["operation"])
                        save("needs_review")
                        return {**record, "last_result": latest,
                            "can_resume_pending_input": record["step_delivery"]["state"] == "not_sent",
                            "next_step": ("입력을 보내지 않은 단계입니다. 진단 원인을 해결한 뒤 같은 run_id로 이어가면 현재 대상을 새로 확인하고 이 단계부터 실행합니다. 완료한 앞 단계는 반복하지 않습니다."
                                if record["step_delivery"]["state"] == "not_sent" else
                                "현재 화면을 확인하세요. 같은 입력은 자동 반복하지 않습니다. 이어가기는 먼저 미확인 단계의 결과를 다시 검사합니다.")}
                    record["completed_steps"] = index + 1
                    record["pending_step"] = None
                    stage = "checkpoint_after_verification"
                    save("running")
                record["task_verified"] = True
                stage = "checkpoint_completion"
                save("verified")
                return {**record, "last_result": latest,
                        "verification_scope": "read_only_wait_conditions" if all(s["operation"] in {"wait_for_image", "delay", "wait_for_window"} for s in steps) else
                            "saved_step_postconditions_and_explicit_checkpoint_acknowledgements" if any(s["operation"] == "checkpoint" for s in steps) else "saved_step_postconditions",
                        **({"business_result_verified": False} if all(s["operation"] in {"wait_for_image", "delay", "wait_for_window"} for s in steps) else {}),
                        **({"checkpoint_images_verified": False} if any(s["operation"] == "checkpoint" for s in steps) else {})}
            except BindingError:
                record["task_verified"] = False
                save("needs_binding")
                return {**record, "diagnostic": {"code": "needs_binding", "automatic_replay": False},
                        "next_step": "현재 창을 다시 관찰하고 targets를 연결한 뒤 같은 run_id로 이어가세요. 창이 없거나 모호한 단계의 입력은 보내지 않았습니다."}
            except Exception as error:
                record["task_verified"] = False
                if stage == "checkpoint_before_input":
                    # The operation was never called. A checkpoint write failure
                    # must not mislabel this unattempted step as an uncertain write.
                    record["pending_step"] = None
                    record.pop("checkpoint", None)
                save("interrupted")
                error_numbers = {key: getattr(error, key) for key in ("errno", "winerror")
                                 if type(getattr(error, key, None)) is int}
                return {**record, "diagnostic": {"code": "interrupted", "automatic_replay": False,
                                                "stage": stage, "error_type": type(error).__name__, **error_numbers},
                        "next_step": "실행이 중단됐습니다. 이 run_id로 진행 기록을 확인하세요. 새 세션에서 현재 창을 다시 지정하면 마지막 확인 지점부터 검사할 수 있습니다. 미확인 입력은 자동 반복하지 않습니다."}
