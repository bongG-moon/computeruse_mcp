"""Small, verified UIA operations built exclusively from guarded runtime calls.

Selectors and assertions are data, never code. A delivery acknowledgement is not
task success, and uncertain mutations are never replayed by this module.
"""
from __future__ import annotations

import copy
import json
import math
import time
from accessibility_tree import normalize_snapshot
from window_transitions import WINDOW_TRANSITION_SCHEMA, WindowTransition, TransitionError, validate_transition


class OperationError(ValueError):
    def __init__(self, message, code="invalid_operation"):
        super().__init__(message)
        self.code = code


ANCESTOR_SELECTOR_SCHEMA = {
    "type": "object", "additionalProperties": False, "minProperties": 1,
    "properties": {key: {"type": "string", "minLength": 1, "maxLength": 1000}
                   for key in ("name", "role", "automation_id")},
}
SELECTOR_SCHEMA = copy.deepcopy(ANCESTOR_SELECTOR_SCHEMA)
SELECTOR_SCHEMA["properties"]["within"] = dict(copy.deepcopy(ANCESTOR_SELECTOR_SCHEMA),
    description="One exact unique ancestor identified in the same UIA snapshot. Restricts matches to its descendants; nested within is not allowed.")
ASSERTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"selector": SELECTOR_SCHEMA,
                   "property": {"enum": ["value", "name", "selected", "enabled"]},
                   "equals": {"type": ["string", "boolean", "integer", "number", "null"]},
                   "require_change": {"type": "boolean", "description": "Require an observed change from the pre-action baseline before equality can pass. Read-only resume assertions cannot recreate a lost baseline."}},
    "required": ["selector", "property", "equals"],
}
OPERATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "operation": {"enum": ["set_value", "select_option", "select_item", "set_checked",
                                 "click", "double_click", "right_click", "press_key", "hotkey", "assert"]},
        "selector": SELECTOR_SCHEMA,
        "value": {"type": "string", "maxLength": 16000},
        "checked": {"type": "boolean", "description": "Desired state for set_checked. Requires an observed boolean selected state and toggle capability."},
        "key": {"type": "string", "minLength": 1, "maxLength": 80},
        "key_target": {"enum": ["element", "window"], "description": "press_key/hotkey only. Default element requires selector. Explicit window omits selector and targets the currently focused control in the freshly observed approved window. Requires expect; never used as an automatic fallback."},
        "modifiers": {"type": "array", "maxItems": 4, "uniqueItems": True,
                      "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        "keys": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        "option_order": {"type": "array", "minItems": 1, "maxItems": 30, "uniqueItems": True,
                         "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                         "description": "select_option only. Exact option labels in observed or user-confirmed order; never infer or guess this order. Uses at most 12 verified Up/Down transitions without opening a native popup."},
        "strategy": {"enum": ["edit_commit"], "description": "Explicit editable ComboBox strategy; never an automatic fallback."},
        "edit_selector": SELECTOR_SCHEMA,
        "commit_key": {"enum": ["ENTER", "TAB"]},
        "expect": {"type": "array", "minItems": 1, "maxItems": 20, "items": ASSERTION_SCHEMA},
        "completion_mode": {"enum": ["human"], "description": "Explicit recorded click/double_click/right_click only: dispatch once and defer completion to a separate human screenshot checkpoint. Never returns task_verified. Cannot combine with expect or window_transition."},
        "window_transition": WINDOW_TRANSITION_SCHEMA,
        "verification_timeout_ms": {"type": "integer", "minimum": 0, "maximum": 60000,
            "description": "Completion observation/polling deadline, default 10000ms. A running observation keeps its normal read budget within step_timeout_ms; late results do not pass. Zero means one normal bounded read."},
        "poll_interval_ms": {"type": "integer", "minimum": 100, "maximum": 2000},
        "step_timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
    },
    "required": ["operation"],
}


def validate_selector(selector, allow_within=True):
    permitted = {"name", "role", "automation_id"} | ({"within"} if allow_within else set())
    if not isinstance(selector, dict) or not selector or set(selector) - permitted:
        raise OperationError("selector에는 정확한 name, role, automation_id와 단일 부모 범위 within만 사용할 수 있습니다.")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for k, v in selector.items() if k != "within"):
        raise OperationError("선택 기준은 1~1000자의 비어 있지 않은 문자열이어야 합니다.")
    if not ({"name", "automation_id"} & set(selector)):
        raise OperationError("role만으로는 대상을 선택할 수 없습니다. name 또는 automation_id를 함께 지정하세요.")
    if "within" in selector:
        validate_selector(selector["within"], allow_within=False)
    return copy.deepcopy(selector)


def validate_assertions(assertions):
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 20:
        raise OperationError("완료 조건 expect는 1~20개여야 합니다.")
    for item in assertions:
        if (not isinstance(item, dict) or not {"selector", "property", "equals"} <= set(item)
                or set(item) - {"selector", "property", "equals", "require_change"}):
            raise OperationError("완료 조건에는 selector, property, equals와 선택적인 require_change만 사용할 수 있습니다.")
        validate_selector(item["selector"])
        if item["property"] not in ("value", "name", "selected", "enabled"):
            raise OperationError("지원하지 않는 완료 조건 속성입니다.")
        value = item["equals"]
        if type(value) not in (str, bool, int, float, type(None)) or (isinstance(value, float) and not math.isfinite(value)):
            raise OperationError("equals는 유한한 JSON 기본 값이어야 합니다.")
        if isinstance(value, str) and len(value) > 16000:
            raise OperationError("완료 조건 문자열이 너무 깁니다.")
        if item["property"] in ("selected", "enabled") and type(value) is not bool:
            raise OperationError("selected와 enabled의 equals는 true 또는 false여야 합니다.")
        if "require_change" in item and type(item["require_change"]) is not bool:
            raise OperationError("require_change는 true 또는 false여야 합니다.")
    return copy.deepcopy(assertions)


def validate_step(step):
    if not isinstance(step, dict) or set(step) - set(OPERATION_SCHEMA["properties"]):
        raise OperationError("작업 단계의 형식 또는 인자가 올바르지 않습니다.")
    operation = step.get("operation")
    if operation not in OPERATION_SCHEMA["properties"]["operation"]["enum"]:
        raise OperationError("지원하지 않는 작업 종류입니다.")
    result = copy.deepcopy(step)
    human_completion = step.get("completion_mode") == "human"
    if "completion_mode" in step and (not human_completion or operation not in ("click", "double_click", "right_click")
            or "expect" in step or "window_transition" in step):
        raise OperationError("직접 화면 확인은 클릭 동작에만 지정하며 expect 또는 window_transition과 함께 사용할 수 없습니다.")
    if "window_transition" in step:
        try:
            result["window_transition"] = validate_transition(step["window_transition"])
        except TransitionError as error:
            raise OperationError(str(error), error.code) from error
        if operation == "assert":
            raise OperationError("assert는 현재 창을 다시 확인합니다. 창 전환 조건은 입력 동작에 지정하세요.")
    key_target = step.get("key_target", "element")
    if "key_target" in step and (operation not in ("press_key", "hotkey") or key_target not in ("element", "window")):
        raise OperationError("key_target은 press_key/hotkey에서 element 또는 window로만 지정하세요.")
    window_key = operation in ("press_key", "hotkey") and key_target == "window"
    if window_key and "selector" in step:
        raise OperationError("key_target: window는 특정 요소가 아닌 현재 창의 키 입력입니다. selector를 함께 지정할 수 없습니다.")
    if not window_key and (operation != "assert" or "selector" in step):
        result["selector"] = validate_selector(step.get("selector"))
    if operation in ("set_value", "select_option"):
        if not isinstance(step.get("value"), str) or len(step["value"]) > 16000:
            raise OperationError("입력하거나 선택할 value는 16000자 이하 문자열이어야 합니다.")
        if operation == "select_option" and not step["value"].strip():
            raise OperationError("선택할 항목의 이름이 필요합니다.")
    elif "value" in step:
        raise OperationError("이 작업에는 value를 사용할 수 없습니다.")
    if operation == "set_checked":
        if type(step.get("checked")) is not bool:
            raise OperationError("set_checked에는 원하는 상태 checked: true 또는 false가 필요합니다.")
    elif "checked" in step:
        raise OperationError("checked는 set_checked에만 사용할 수 있습니다.")
    if operation == "press_key":
        if not isinstance(step.get("key"), str) or not step["key"].strip() or len(step["key"]) > 80:
            raise OperationError("press_key에는 1~80자의 key 이름이 필요합니다.")
        if "modifiers" in step:
            modifiers = step["modifiers"]
            if (not isinstance(modifiers, list) or len(modifiers) > 4
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 80 for v in modifiers)
                    or len(set(modifiers)) != len(modifiers)):
                raise OperationError("modifiers에는 중복 없는 최대 4개의 수정키 이름을 지정하세요.")
    elif "key" in step or "modifiers" in step:
        raise OperationError("key와 modifiers는 press_key에만 사용할 수 있습니다.")
    if operation == "hotkey":
        keys = step.get("keys")
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 8
                or any(not isinstance(v, str) or not v.strip() or len(v) > 80 for v in keys)
                or len(set(keys)) != len(keys)):
            raise OperationError("hotkey에는 1~8개 키 이름 목록 keys가 필요합니다.")
    elif "keys" in step:
        raise OperationError("keys는 hotkey에만 사용할 수 있습니다.")
    if "option_order" in step:
        order = step["option_order"]
        if (operation != "select_option" or not isinstance(order, list) or not 1 <= len(order) <= 30
                or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in order)
                or len(set(order)) != len(order)):
            raise OperationError("option_order는 select_option에만 사용하는, 직접 관찰하거나 사용자가 확인한 중복 없는 1~30개 항목 순서입니다.")
        if step["value"] not in order:
            raise OperationError("선택할 value가 확인된 option_order에 없습니다.")
    if any(key in step for key in ("strategy", "edit_selector", "commit_key")):
        if (operation != "select_option" or step.get("strategy") != "edit_commit"
                or step.get("commit_key") not in ("ENTER", "TAB") or "option_order" in step):
            raise OperationError("edit_commit은 select_option에서 edit_selector와 ENTER/TAB 확정키를 명시해야 하며 option_order와 함께 사용할 수 없습니다.")
        result["edit_selector"] = validate_selector(step.get("edit_selector"))
    if "expect" in step:
        result["expect"] = validate_assertions(step["expect"])
    if operation in ("click", "double_click", "right_click", "press_key", "hotkey", "assert") and not step.get("expect") and not human_completion:
        raise OperationError("클릭, 키 입력, assert에는 실제 완료를 확인할 expect 조건이 필요합니다.")
    timeout = step.get("verification_timeout_ms", 10000)
    if type(timeout) is not int or not 0 <= timeout <= 60000:
        raise OperationError("verification_timeout_ms는 0~60000 사이 정수여야 합니다.")
    if type(step.get("poll_interval_ms", 150)) is not int or not 100 <= step.get("poll_interval_ms", 150) <= 2000:
        raise OperationError("poll_interval_ms는 100~2000 사이 정수여야 합니다.")
    if type(step.get("step_timeout_ms", 30000)) is not int or not 1 <= step.get("step_timeout_ms", 30000) <= 120000:
        raise OperationError("step_timeout_ms는 1~120000 사이 정수여야 합니다.")
    return result


def verification_step(step):
    """Return a read-only recheck; useful when resuming without replaying input."""
    step = validate_step(step)
    if step.get("completion_mode") == "human":
        raise OperationError("직접 확인 방식의 입력은 화면 확인 지점에서 결과를 확인하세요. 입력을 자동 반복하지 않습니다.", "human_review_required")
    checks = copy.deepcopy(step.get("expect", []))
    if step["operation"] in ("set_value", "select_option"):
        checks.insert(0, {"selector": step["selector"], "property": "value", "equals": step["value"]})
    elif step["operation"] in ("set_checked", "select_item"):
        checks.insert(0, {"selector": step["selector"], "property": "selected",
                          "equals": step["checked"] if step["operation"] == "set_checked" else True})
    return {"operation": "assert", "expect": checks,
            "verification_timeout_ms": step.get("verification_timeout_ms", 10000),
            **{key: step[key] for key in ("poll_interval_ms", "step_timeout_ms") if key in step}}


def _payload(answer):
    if not isinstance(answer, dict):
        raise OperationError("화면 도구 응답 형식이 올바르지 않습니다.", "invalid_response")
    structured = answer.get("structuredContent")
    if isinstance(structured, dict) and "elements" in structured:
        return normalize_snapshot(structured)
    for item in answer.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and "elements" in parsed:
                # Runtime timing/recovery metadata can create structuredContent
                # even when an older Driver returned its tree only as JSON text.
                # Do not let that metadata hide the actual observed elements.
                if isinstance(structured, dict):
                    parsed.update(structured)
                return normalize_snapshot(parsed)
    return structured if isinstance(structured, dict) else answer


def _name(element):
    return element.get("name", element.get("label"))


def _matches(element, selector):
    for key, value in selector.items():
        if key == "within":
            continue
        actual = _name(element) if key == "name" else element.get(key)
        if actual != value:
            return False
    return True


def _elements(snapshot):
    elements = snapshot.get("elements")
    if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
        raise OperationError("구조화된 UIA 요소를 읽지 못했습니다.", "missing_accessibility")
    return elements


def _limited(snapshot, depth, count):
    # This Driver also marks elements_complete=false when non-actionable tree
    # nodes are omitted from the structured projection. That flag alone is not
    # proof of a truncated walk (normal 27-element captures contain it).
    elements = _elements(snapshot)
    return (snapshot.get("truncated") is True or snapshot.get("max_elements_reached") is True
            or snapshot.get("max_depth_reached") is True
            or len(elements) >= count
            or any(type(e.get("depth")) is int and e["depth"] >= depth for e in elements)
            or (type(snapshot.get("total_element_count")) is int
                and snapshot["total_element_count"] > len(elements)))


def _find(snapshot, selector):
    found = [e for e in _elements(snapshot) if _matches(e, selector)]
    if "within" in selector:
        ancestors = [e for e in _elements(snapshot) if _matches(e, selector["within"])]
        if len(ancestors) > 1:
            raise OperationError("within에 해당하는 부모 영역이 여러 개입니다. 부모 선택 기준을 더 구체적으로 지정하세요.", "ambiguous_scope")
        if not ancestors:
            return []
        elements = _elements(snapshot)
        parents = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int}
        found = [e for e in found if _descendant(e, ancestors[0], elements, parents)]
    return found


def _unique(snapshot, selector):
    found = _find(snapshot, selector)
    if len(found) != 1:
        raise OperationError("일치하는 화면 요소가 없습니다." if not found else "같은 조건의 화면 요소가 여러 개입니다. 선택 기준을 더 구체적으로 지정하세요.",
                             "selector_not_found" if not found else "ambiguous_selector")
    return found[0]


def _descendant(element, ancestor, elements, parents=None):
    ancestor_index = ancestor.get("element_index")
    if type(ancestor_index) is not int:
        return False
    if parents is None:
        parents = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int}
    seen = set()
    parent = element.get("parent_index")
    while type(parent) is int and parent not in seen:
        if parent == ancestor_index:
            return True
        seen.add(parent)
        parent = parents.get(parent, {}).get("parent_index")
    return False


class Operations:
    def __init__(self, runtime):
        self.runtime = runtime
        self._verified_observation = None

    def execute(self, step, target, delivery_mode="background", reuse_verified=False):
        # Reuse is exclusively for adjacent steps in one caller-locked recipe.
        # Standalone calls and resume assertions always observe again.
        previous = self._verified_observation
        self._verified_observation = None
        step = validate_step(step)
        if not isinstance(target, dict) or set(target) != {"pid", "window_id"} or any(type(v) is not int or v < 1 for v in target.values()):
            raise OperationError("pid와 window_id로 정확한 창을 지정하세요.")
        if delivery_mode not in ("background", "foreground"):
            raise OperationError("delivery_mode는 background 또는 foreground여야 합니다.")
        if self.runtime.mode != "uia":
            raise OperationError("검증 작업은 uia 세션에서만 사용할 수 있습니다.", "unsupported_mode")
        initial = None
        guard = getattr(self.runtime, "guard", None)
        observed_targets = getattr(guard, "observed_targets", set())
        if (reuse_verified is True and step["operation"] != "assert" and previous is not None
                and not any(check.get("require_change") for check in step.get("expect", []))
                and previous["target"] == target and time.monotonic() - previous["observed_at"] < 0.5
                and (target["pid"], target["window_id"]) in observed_targets):
            initial = previous
        execution = _Execution(self.runtime, step, dict(target), delivery_mode, initial)
        result = execution.run()
        if (result["status"] == "verified" and step["operation"] != "assert" and execution.snapshot is not None
                and execution.snapshot_target == target):
            self._verified_observation = {"snapshot": execution.snapshot, "target": dict(target),
                                          "observed_at": execution.observed_at, "limits": execution.observation_limits}
        return result

    def wait_for_state(self, assertions, target, *, timeout_ms, poll_interval_ms=250):
        """Read-only property wait; changes are relative to this wait's start."""
        if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
                or any(type(value) is not int or value < 1 for value in target.values())):
            raise OperationError("현재 승인한 창의 pid/window_id가 필요합니다.", "invalid_target")
        if self.runtime.mode != "uia":
            raise OperationError("상태 대기는 UIA 세션에서 실행하세요.", "unsupported_mode")
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 60000:
            raise OperationError("상태 대기 제한 시간 timeout_ms는 0~60000ms로 지정하세요.")
        self._verified_observation = None
        step = validate_step({"operation": "assert", "expect": assertions,
            "verification_timeout_ms": timeout_ms, "poll_interval_ms": poll_interval_ms,
            "step_timeout_ms": max(30000, timeout_ms)})
        execution = _Execution(self.runtime, step, dict(target), "background", baseline_at_start=True)
        result = execution.run()
        result["operation"] = "wait_for_state"
        if result["status"] == "failed" and result["diagnostic"]["code"] == "verification_failed":
            result["diagnostic"]["code"] = "state_wait_timeout"
        return result


class _Execution:
    def __init__(self, runtime, step, target, delivery, initial_observation=None, baseline_at_start=False):
        self.runtime, self.step, self.target, self.delivery = runtime, step, target, delivery
        self.started = time.monotonic()
        self.deadline = self.started + step.get("step_timeout_ms", 30000) / 1000
        self.observation_deadline = None
        self.baseline_at_start = baseline_at_start
        self.baselines = {}
        self.change_seen = set()
        self.metrics = {"tool_calls": 0, "observations": 0, "discovery_calls": 0, "mutations": 0,
                        "observation_ms": 0, "discovery_ms": 0, "action_ms": 0, "expanded_observations": 0,
                        "reused_observations": 0, "focus_actions": 0, "focus_ms": 0,
                        "scoped_observations": 0}
        self.dispatched = False
        self.focus_dispatched = False
        self.focus_attempted = False
        self.checks = []
        self.snapshot = None
        self.last_guidance = None
        self.no_parent_recovery = False
        self.read_failed = False
        self.strategy = None
        self.initial_observation = initial_observation
        self.snapshot_target = None
        self.observed_at = 0
        self.observation_limits = (12, 600)
        self.original_target = dict(target)
        self.transition_spec = step.get("window_transition", {"mode": "auto"})
        self.transition_tracker = None
        self.transition = None
        self.transition_row = None
        self.action_evidence = []
        self.action_acknowledgement_error = None

    def _progress(self, stage):
        report = getattr(self.runtime, "report_progress", None)
        if callable(report):
            report(stage, op=self.step["operation"])

    def _remaining(self):
        self.runtime.check_active()
        now = time.monotonic()
        if now >= self.deadline:
            raise OperationError("단계 전체 제한 시간이 지났습니다. 후속 입력을 보내지 않았습니다.", "step_timeout")
        # A short polling deadline must not abort an otherwise healthy Driver
        # request: its transport timeout closes the entire Driver connection.
        # In-flight reads retain the normal runtime budget, bounded by this
        # whole-step deadline. Verification separately rejects late answers.
        return max(1, math.floor((self.deadline - now) * 1000 + 1e-6))

    def _call(self, name, args, mutation=False):
        self._progress("acting" if mutation else "searching" if name == "list_windows" else "verifying" if self.dispatched else "observing")
        remaining = self._remaining()
        began = time.monotonic()
        self.metrics["tool_calls"] += 1
        discovery = name == "list_windows"
        focus = name == "bring_to_front"
        self.metrics["mutations" if mutation else "discovery_calls" if discovery else "observations"] += 1
        previous_dispatched = self.dispatched
        previous_focus = self.focus_dispatched
        if mutation:
            # Changing focus is an observable side effect, but it does not
            # establish that business input reached a field or button.
            if focus:
                self.focus_dispatched = True
                self.metrics["focus_actions"] += 1
            else:
                self.dispatched = True  # Unknown exceptions may follow actual dispatch.
        try:
            bounded = getattr(self.runtime, "call_with_timeout", None)
            answer = bounded(name, args, timeout_ms=remaining) if callable(bounded) else self.runtime.call(name, args)
        except Exception as error:
            if not mutation:
                self.read_failed = True
            if isinstance(error, OperationError):
                raise
            raise OperationError(str(error)[:1000], "mutation_error" if mutation else "observation_error") from error
        finally:
            duration = round((time.monotonic() - began) * 1000, 2)
            self.metrics["action_ms" if mutation else "discovery_ms" if discovery else "observation_ms"] += duration
            if focus:
                self.metrics["focus_ms"] += duration
        data = _payload(answer)
        if mutation and not focus:
            # Driver facts only: background/foreground does not imply whether
            # a physical pointer moved. A missing report stays not_reported.
            reported_mode = data.get("delivery_mode")
            backend = data.get("backend", data.get("input_backend"))
            self.action_evidence.append({"tool": name, "requested_delivery_mode": args.get("delivery_mode", "driver_default"),
                "reported_delivery_mode": reported_mode[:120] if isinstance(reported_mode, str) else "not_reported",
                "reported_backend": backend[:120] if isinstance(backend, str) else "not_reported",
                **({"input_sent": data["input_sent"]} if type(data.get("input_sent")) is bool else {})})
        if mutation and data.get("input_sent") is False:
            if focus:
                self.focus_dispatched = previous_focus
            else:
                self.dispatched = previous_dispatched
        # A guard refusal can arrive after an approval used the remaining
        # budget. Preserve its explicit no-input evidence before reporting the
        # deadline; earlier successful mutations in this step remain recorded.
        try:
            self._remaining()
        except Exception:
            if not mutation:
                self.read_failed = True
            raise
        if answer.get("isError"):
            if not mutation:
                self.read_failed = True
            guidance = data.get("computer_use_guidance", {})
            self.last_guidance = {key: guidance[key] for key in ("diagnostic_code", "next_step") if key in guidance}
            message = guidance.get("diagnostic") or next((i.get("text") for i in answer.get("content", [])
                                                          if isinstance(i, dict) and i.get("type") == "text"), "화면 도구가 실패했습니다.")
            raise OperationError(str(message)[:1000], data.get("error_code") or guidance.get("diagnostic_code") or ("mutation_error" if mutation else "observation_error"))
        return data

    def _capture(self, target, depth, count):
        snapshot = self._call("get_window_state", dict(target, include_accessibility_tree=True,
                                                      include_screenshot=False, max_depth=depth, max_elements=count))
        _elements(snapshot)
        self.snapshot = snapshot
        for key in ("pid", "window_id"):
            if key in snapshot and snapshot[key] != target[key]:
                raise OperationError("관찰 결과의 창 식별자가 요청한 창과 다릅니다.", "target_mismatch")
        return snapshot

    def _observe(self, selectors=(), option=None, target=None):
        target = self.target if target is None else target
        previous, self.initial_observation = self.initial_observation, None
        if previous is not None and previous["target"] == target and option is None:
            snapshot = previous["snapshot"]
            if (not _limited(snapshot, *previous["limits"])
                    and all(_find(snapshot, selector) for selector in selectors)):
                self.snapshot, self.snapshot_target = snapshot, dict(target)
                self.observed_at, self.observation_limits = previous["observed_at"], previous["limits"]
                self.metrics["reused_observations"] += 1
                return snapshot
        for depth, count in ((12, 600), (32, 5000)):
            if depth == 32:
                if self.observation_deadline is not None and time.monotonic() >= self.observation_deadline:
                    raise OperationError("확인 제한 시간이 지나 추가 화면 탐색을 시작하지 않았습니다.", "verification_timeout")
                self.metrics["expanded_observations"] += 1
            snapshot = self._capture(target, depth, count)
            limited = _limited(snapshot, depth, count)
            missing = any(not _find(snapshot, s) for s in selectors)
            if (missing and selectors and target == self.target and option is None and depth == 12
                    and self.delivery == "foreground" and self.step["operation"] != "assert"
                    and not self.focus_attempted and not self.read_failed and self.metrics["mutations"] == 0):
                self.focus_attempted = True
                self._call("bring_to_front", dict(target), mutation=True)
                # Some native web views expose only their blank document until
                # first activated. This explicitly requested foreground route
                # tries readiness once; it does not retry business input.
                snapshot = self._capture(target, depth, count)
                limited = _limited(snapshot, depth, count)
                missing = any(not _find(snapshot, s) for s in selectors)
            if option is not None:
                missing = missing or not self._options(snapshot, option[0], option[1])
            if depth == 12 and (limited or missing):
                continue
            if limited:
                raise OperationError("화면 요소가 잘려 정확한 대상을 확인할 수 없습니다. 더 작은 창이나 단순한 화면에서 확인하세요.", "incomplete_observation")
            self.snapshot_target, self.observed_at, self.observation_limits = dict(target), time.monotonic(), (depth, count)
            return snapshot
        raise AssertionError("observation loop exhausted")

    def _handle(self, element, snapshot):
        if snapshot.get("scoped_observation") is True or snapshot.get("read_only") is True:
            raise OperationError("속성 확인용 관찰은 입력용 Driver 핸들이 아닙니다. 새 Driver 관찰이 필요합니다.", "read_only_observation")
        if element.get("synthetic_ancestor") is True or (type(element.get("element_index")) is int and element["element_index"] < 0):
            raise OperationError("복원된 상위 영역은 선택 범위 확인용이며 직접 조작할 수 없습니다.", "read_only_ancestor")
        if element.get("enabled") is False:
            raise OperationError("비활성화된 화면 요소는 조작하지 않습니다.", "disabled_element")
        token = element.get("element_token")
        if isinstance(token, str) and token:
            return {"element_token": token}
        if type(element.get("element_index")) is int and isinstance(snapshot.get("snapshot_id"), str):
            return {"element_index": element["element_index"], "snapshot_id": snapshot["snapshot_id"]}
        raise OperationError("새 화면에서 유효한 요소 핸들을 받지 못했습니다.", "missing_handle")

    def _mutate(self, name, element, snapshot, target=None, **args):
        request = dict(self.target if target is None else target, **self._handle(element, snapshot), **args)
        if name in ("click", "double_click", "right_click", "press_key", "hotkey"):
            request["delivery_mode"] = self.delivery
        self._call(name, request, mutation=True)

    def _options(self, snapshot, selector, value):
        try:
            combo = _unique(snapshot, selector)
        except OperationError:
            return []
        return [e for e in _elements(snapshot) if _name(e) == value
                and e.get("role") in ("ListItem", "MenuItem", "Option")
                and _descendant(e, combo, _elements(snapshot))]

    def _visible_windows(self):
        data = self._call("list_windows", {"pid": self.target["pid"], "on_screen_only": True})
        windows = data.get("windows")
        if not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
            raise OperationError("선택 목록을 열기 전후의 창 목록을 확인하지 못했습니다.", "window_discovery_unavailable")
        return [w for w in windows if w.get("pid") == self.target["pid"] and type(w.get("window_id")) is int
                and w["window_id"] > 0 and w.get("is_on_screen") is True]

    def _new_owned_popup(self, before, after):
        previous = {w["window_id"] for w in before}
        candidates = [w for w in after if w["window_id"] not in previous and w["window_id"] != self.target["window_id"]
                      and self.target["window_id"] in (w.get("owner_window_id"), w.get("root_owner_window_id"))]
        if len(candidates) > 1:
            self.no_parent_recovery = True
            raise OperationError("새로 열린 연결된 창이 여러 개여서 선택 목록을 특정할 수 없습니다.", "ambiguous_popup")
        return {"pid": candidates[0]["pid"], "window_id": candidates[0]["window_id"]} if candidates else None

    def _popup_options(self, snapshot, value):
        elements = _elements(snapshot)
        containers = [e for e in elements if e.get("role") in ("List", "ListBox", "Menu")]
        return [e for e in elements if _name(e) == value and e.get("role") in ("ListItem", "MenuItem", "Option")
                and any(_descendant(e, container, elements) for container in containers)]

    def _select_known_order(self, snapshot, element):
        """Use only supplied, confirmed order; verify each single transition."""
        self.strategy = "confirmed_option_order"
        order = self.step["option_order"]
        current = element.get("value")
        if current not in order:
            raise OperationError("현재 선택값이 확인된 option_order와 다릅니다. 순서를 다시 확인하세요.", "option_order_mismatch")
        start, end = order.index(current), order.index(self.step["value"])
        if abs(end - start) > 12:
            raise OperationError("한 번에 12개를 초과하는 항목 이동은 지원하지 않습니다.", "selection_distance_exceeded")
        direction = 1 if end > start else -1
        for index in range(start + direction, end + direction, direction):
            self._mutate("press_key", element, snapshot, key="down" if direction == 1 else "up")
            snapshot = self._observe([self.step["selector"]])
            element = _unique(snapshot, self.step["selector"])
            if element.get("role") != "ComboBox" or element.get("value") != order[index]:
                self._evaluate(snapshot, verification_step(self.step)["expect"])
                return snapshot, False
        return snapshot, True

    def _editable_combo_child(self, snapshot):
        combo = _unique(snapshot, self.step["selector"])
        edit = _unique(snapshot, self.step["edit_selector"])
        if (combo.get("role") != "ComboBox" or combo.get("enabled") is False
                or not _descendant(edit, combo, _elements(snapshot))):
            raise OperationError("입력칸이 현재 선택 상자의 자식인지 확인하지 못했습니다.", "combo_edit_not_descendant")
        if (edit.get("role") not in ("Edit", "TextBox") or edit.get("enabled") is not True
                or edit.get("read_only") is True or edit.get("is_read_only") is True
                or edit.get("is_password") is True or not isinstance(edit.get("actions"), list)
                or "set_value" not in edit["actions"]):
            raise OperationError("선택 상자의 입력칸이 활성·편집 가능한 일반 입력 요소인지 확인하지 못했습니다.", "unsupported_combo_edit")
        return edit

    def _edit_commit(self, snapshot):
        self.strategy = "edit_commit"
        edit = self._editable_combo_child(snapshot)
        self._mutate("set_value", edit, snapshot, value=self.step["value"])
        # A fresh Driver snapshot is mandatory: the preceding value input
        # consumed its handles, and a native property read supplies none.
        snapshot = self._observe([self.step["selector"], self.step["edit_selector"]])
        edit = self._editable_combo_child(snapshot)
        if edit.get("value") != self.step["value"]:
            raise OperationError("선택 상자 내부 입력값을 확인하지 못해 확정키를 보내지 않았습니다.", "combo_edit_value_unconfirmed")
        self._mutate("press_key", edit, snapshot, key=self.step["commit_key"])

    def _evaluate(self, snapshot, assertions):
        checks = []
        for assertion in assertions:
            check = dict(copy.deepcopy(assertion), passed=False)
            try:
                element = _unique(snapshot, assertion["selector"])
                prop = assertion["property"]
                present = ("name" in element or "label" in element) if prop == "name" else prop in element
                actual = _name(element) if prop == "name" else element.get(prop)
                check["observed"] = actual
                # Missing false/null values cannot count as a satisfied check.
                check["passed"] = present and type(actual) is type(assertion["equals"]) and actual == assertion["equals"]
                if assertion.get("require_change"):
                    key = self._assertion_key(assertion)
                    if key not in self.baselines:
                        check.update(passed=False, reason="change_baseline_required", change_observed=False)
                    else:
                        previous = self.baselines[key]
                        if present and (type(actual) is not type(previous) or actual != previous):
                            self.change_seen.add(key)
                        check["change_observed"] = key in self.change_seen
                        check["passed"] = check["passed"] and check["change_observed"]
                        if not check["change_observed"]:
                            check["reason"] = "change_not_observed"
                if not present:
                    check["reason"] = "property_unavailable"
            except OperationError as error:
                check["reason"] = error.code
            checks.append(check)
        self.checks = checks
        return bool(checks) and all(c["passed"] for c in checks)

    @staticmethod
    def _assertion_key(assertion):
        return json.dumps({key: assertion[key] for key in ("selector", "property")}, sort_keys=True, ensure_ascii=False)

    def _baseline(self, snapshot, assertions):
        for assertion in assertions:
            if not assertion.get("require_change"):
                continue
            element = _unique(snapshot, assertion["selector"])
            prop = assertion["property"]
            present = ("name" in element or "label" in element) if prop == "name" else prop in element
            if not present:
                raise OperationError("변화 검증에 필요한 입력 전 속성을 읽지 못했습니다.", "change_baseline_unavailable")
            self.baselines[self._assertion_key(assertion)] = copy.deepcopy(_name(element) if prop == "name" else element[prop])

    def _observe_assertions(self, assertions):
        self._progress("verifying")
        selectors = [item["selector"] for item in assertions]
        scoped = getattr(self.runtime, "observe_controls", None)
        if not callable(scoped):
            return self._observe(selectors)
        started = time.monotonic()
        available = True
        try:
            answer = scoped(dict(self.target), copy.deepcopy(selectors), timeout_ms=self._remaining())
        except NotImplementedError:
            available = False
            return self._observe(selectors)
        except Exception as error:
            self.read_failed = True
            raise OperationError(str(error)[:1000], getattr(error, "code", "observation_error")) from error
        finally:
            if available:
                self.metrics["observations"] += 1
                self.metrics["scoped_observations"] += 1
                self.metrics["observation_ms"] += round((time.monotonic() - started) * 1000, 2)
        # Native property snapshots never refresh Driver handles or the
        # adjacent-step input cache, even if a provider includes token-like data.
        self.snapshot = None
        try:
            self._remaining()
            data = _payload(answer)
            if answer.get("isError"):
                raise OperationError("대상 속성 확인이 실패했습니다. 전체 창 재탐색으로 자동 전환하지 않습니다.", "scoped_observation_failed")
            if (any(data.get(key) != value for key, value in self.target.items())
                    or data.get("read_only") is not True or data.get("scoped_observation") is not True
                    or data.get("scope_complete") is not True):
                raise OperationError("속성 관찰의 대상 또는 확인 범위가 불완전합니다.", "scoped_observation_incomplete")
            _elements(data)
            return data
        except Exception:
            self.read_failed = True
            raise

    def _verify(self, assertions, initial=None):
        timeout = self.step.get("verification_timeout_ms", 10000)
        deadline = min(self.deadline, time.monotonic() + timeout / 1000)
        self.observation_deadline = deadline if timeout else None  # zero means one bounded read
        first = True
        try:
            if self.dispatched and self.transition_spec["mode"] == "new_window":
                initial = self._recover_transition(assertions, deadline if timeout else self.deadline)
            while True:
                self._remaining()
                try:
                    if (self.dispatched and self.transition is None and self.transition_tracker is not None
                            and self.transition_tracker.probe is not None and not self.transition_tracker.original_present()):
                        snapshot = self._recover_transition(assertions, deadline if timeout else self.deadline)
                    else:
                        snapshot = initial if first and initial is not None else self._observe_assertions(assertions)
                except OperationError as error:
                    if (error.code not in {"target_unavailable", "observation_error", "scoped_observation_failed"}
                            or not self.dispatched or self.transition is not None
                            or self.transition_spec["mode"] == "same_window"):
                        raise
                    if error.code != "target_unavailable" and (self.transition_tracker is None
                            or self.transition_tracker.probe is None or self.transition_tracker.original_present()):
                        raise
                    snapshot = self._recover_transition(assertions, deadline if timeout else self.deadline)
                if timeout and time.monotonic() >= deadline:
                    raise OperationError("완료 확인 제한 시간이 지난 뒤 도착한 관찰은 성공으로 판정하지 않았습니다.", "verification_timeout")
                if first and self.baseline_at_start:
                    self._baseline(snapshot, assertions)
                if self._evaluate(snapshot, assertions):
                    if self.transition is not None and self.transition.get("target"):
                        self.transition_tracker.confirm(self.target, self.transition_row)
                        self._remaining()
                        if timeout and time.monotonic() >= deadline:
                            raise OperationError("마지막 창 식별 확인이 완료 제한 시간 뒤에 끝났습니다.", "verification_timeout")
                    return True
                ambiguous = next((check["reason"] for check in self.checks
                                  if check.get("reason") in ("ambiguous_selector", "ambiguous_scope")), None)
                if ambiguous:
                    self.read_failed = True
                    raise OperationError("완료 조건의 요소가 여러 개여서 판정할 수 없습니다. 추가 관찰이나 입력을 반복하지 않았습니다.", ambiguous)
                first = False
                if not timeout or time.monotonic() >= deadline:
                    return False
                self._progress("waiting")
                self.runtime.stop_event.wait(min(self.step.get("poll_interval_ms", 150)/1000,
                                                 max(0, deadline-time.monotonic())))
                self.runtime.check_active()
                if time.monotonic() >= deadline:
                    if time.monotonic() >= self.deadline:
                        self._remaining()
                    return False
        finally:
            self.observation_deadline = None

    def _recover_transition(self, assertions, verification_deadline):
        """One bounded read-only transition attempt; never invokes mutation."""
        self._progress("transition")
        self.no_parent_recovery = True
        self.transition = {"state": "needs_target", "from_target": dict(self.original_target),
                           "automatic_replay": False, "candidates": []}
        tracker = self.transition_tracker
        if tracker is None:
            raise OperationError("전환할 창의 실행 프로세스 정보를 확보하지 못했습니다.", "transition_identity_unavailable")
        timeout = self.transition_spec.get("timeout_ms", 1500)
        deadline = min(self.deadline, verification_deadline, time.monotonic() + timeout / 1000)
        began = time.monotonic()
        try:
            while True:
                self._remaining()
                row = tracker.inspect()
                self.transition["candidates"] = copy.deepcopy(tracker.candidates)
                if row is not None:
                    if timeout and time.monotonic() >= deadline:
                        raise OperationError("새 창 검색 제한 시간이 지났습니다. 추가 화면 읽기를 시작하지 않았습니다.", "transition_timeout")
                    self.metrics["transition_discovery_ms"] = round((time.monotonic() - began) * 1000, 2)
                    target = {key: row[key] for key in ("pid", "window_id")}
                    # An independent live native ownership check surrounds the
                    # Driver read. No tokens or handles from the old UIA tree.
                    tracker.confirm(target, row)
                    read_began = time.monotonic()
                    try:
                        snapshot = self._observe([a["selector"] for a in assertions], target=target)
                        tracker.confirm(target, row)
                    finally:
                        self.metrics["transition_verification_ms"] = round((time.monotonic() - read_began) * 1000, 2)
                    self._remaining()
                    if time.monotonic() >= verification_deadline:
                        raise OperationError("창 전환 확인 시간이 지났습니다. 늦은 결과로 계속 실행하지 않았습니다.", "transition_timeout")
                    self.target = target
                    self.transition_row = row
                    self.read_failed = False
                    self.last_guidance = None
                    self.transition.update(state="observed", target=dict(target), title=row.get("title", ""))
                    return snapshot
                if not timeout or time.monotonic() >= deadline:
                    raise OperationError("관련된 새 창을 제한 시간 안에 확인하지 못했습니다. 입력을 반복하지 않았습니다.", "transition_timeout")
                self.runtime.stop_event.wait(min(0.1, max(0, deadline - time.monotonic())))
        except TransitionError as error:
            self.transition["candidates"] = copy.deepcopy(tracker.candidates)
            raise OperationError(str(error), error.code) from error
        finally:
            self.metrics["transition_ms"] = round((time.monotonic() - began) * 1000, 2)
            self.metrics.setdefault("transition_discovery_ms", self.metrics["transition_ms"])

    def _result(self, status, code, message):
        self.metrics["elapsed_ms"] = round((time.monotonic() - self.started) * 1000, 2)
        if self.transition_tracker is not None:
            self.metrics["transition_probe_ms"] = round(self.transition_tracker.elapsed_ms, 2)
        diagnostic = {"code": code, "message": message, "automatic_replay": False}
        if self.strategy:
            diagnostic["selection_strategy"] = self.strategy
        if self.last_guidance:
            diagnostic["driver_guidance"] = self.last_guidance
        if self.action_acknowledgement_error:
            diagnostic["action_acknowledgement_error"] = self.action_acknowledgement_error
        if code.startswith("transition_") and self.transition is None:
            self.transition = {"state": "needs_target", "from_target": dict(self.original_target),
                               "automatic_replay": False, "candidates": []}
        if status != "verified":
            if code.startswith("transition_") or code == "target_unavailable":
                diagnostic["next_step"] = ("클릭은 다시 보내지 마세요. 현재 프로그램의 창을 확인해 정확한 대상 창으로 완료 조건만 검사하세요. "
                    "새 프로세스로 전환됐다면 현재 창을 다시 연결하세요. 종료 목적의 동작은 computer_close로 준비·확인하세요.")
            elif code in {"background_unavailable", "background_no_effect"}:
                diagnostic["next_step"] = "현재 결과와 입력 미전달 여부를 확인한 뒤 필요한 작업만 foreground로 명시해 요청하세요. 자동 재입력하지 않습니다."
            else:
                diagnostic["next_step"] = "현재 완료 조건과 진단 원인을 확인하세요. 같은 입력을 자동 반복하지 않습니다."
        if self.transition is not None:
            self.transition["state"] = "verified" if status == "verified" else "needs_target" if "target" not in self.transition else "unverified"
        return {"status": status, "task_verified": status == "verified", "operation": self.step["operation"],
                "input_dispatched": self.dispatched, "focus_dispatched": self.focus_dispatched,
                "checks": self.checks, "metrics": self.metrics,
                "diagnostic": diagnostic, "target": dict(self.target),
                **({"transition_monitor": {"ready": self.transition_tracker.error is None,
                    "diagnostic_code": self.transition_tracker.error,
                    "detail": self.transition_tracker.error_detail}} if self.transition_tracker is not None else {}),
                "delivery": {"requested_mode": self.delivery, "actions": self.action_evidence,
                             "pointer_movement": "not_reported",
                             "acknowledgement": "error_postconditions_checked" if self.action_acknowledgement_error else "see_action_evidence"},
                "feedback": {"selected_target": {"window": dict(self.original_target),
                    **({"selector": copy.deepcopy(self.step["selector"])} if "selector" in self.step else {})},
                    "last_stage": "completion_verified" if status == "verified" else "completion_check" if self.dispatched else "before_input",
                    "phases": [{"phase": "observation", "duration_ms": round(self.metrics["observation_ms"], 2)},
                               {"phase": "input", "duration_ms": round(self.metrics["action_ms"], 2)},
                               {"phase": "window_discovery", "duration_ms": self.metrics.get("transition_discovery_ms", 0)},
                               {"phase": "transition_result_read", "duration_ms": self.metrics.get("transition_verification_ms", 0)}],
                    "phase_times_overlap": True},
                **({"transition": self.transition} if self.transition is not None else {})}

    def run(self):
        human_completion = self.step.get("completion_mode") == "human"
        assertions = [] if human_completion else verification_step(self.step)["expect"]
        try:
            self.runtime.check_active()
            if self.step["operation"] == "assert":
                if any(check.get("require_change") for check in assertions) and not self.baseline_at_start:
                    raise OperationError("이전 동작 전의 기준값이 없어 변화 여부를 재검증할 수 없습니다. 입력을 재실행하지 않았습니다.", "change_baseline_required")
                passed = self._verify(assertions)
            else:
                operation = self.step["operation"]
                window_key = operation in ("press_key", "hotkey") and self.step.get("key_target") == "window"
                selector = self.step.get("selector")
                selectors = [] if window_key else [selector]
                selectors += [check["selector"] for check in assertions if check.get("require_change")]
                if self.step.get("strategy") == "edit_commit":
                    selectors.append(self.step["edit_selector"])
                snapshot = self._observe(selectors)
                self._baseline(snapshot, assertions)
                requires_change = any(check.get("require_change") for check in assertions)
                element = None if window_key else _unique(snapshot, selector)
                if not human_completion and self.transition_spec["mode"] != "same_window":
                    self.transition_tracker = WindowTransition(self.runtime, self.target, self.transition_spec)
                    self.transition_tracker.prepare()
                    self._remaining()
                    if self.transition_spec["mode"] == "new_window" and self.transition_tracker.error:
                        raise OperationError("창 전환의 실행 프로세스 정보를 확보하지 못해 입력하지 않았습니다.", "transition_identity_unavailable")
                if operation == "set_value":
                    if element.get("role") not in ("Edit", "Document", "TextBox") or element.get("read_only") is True or element.get("is_read_only") is True:
                        raise OperationError("set_value는 편집 가능한 입력칸에만 사용할 수 있습니다. 선택 상자는 select_option을 사용하세요.", "unsupported_control")
                    if "actions" in element and "set_value" not in element["actions"]:
                        raise OperationError("이 입력칸은 값 변경 기능을 제공하지 않습니다.", "unsupported_control")
                    if element.get("value") == self.step["value"] and "value" in element and not requires_change:
                        passed = self._verify(assertions, snapshot)
                        return self._result("verified" if passed else "failed", "already_satisfied" if passed else "verification_failed", "현재 값과 완료 조건을 확인했습니다.")
                    self._mutate("set_value", element, snapshot, value=self.step["value"])
                elif operation in ("click", "double_click", "right_click"):
                    self._mutate(operation, element, snapshot)
                    if human_completion:
                        answer = self._result("needs_review" if self.dispatched else "failed",
                            "human_review_required" if self.dispatched else "input_not_dispatched",
                            "클릭을 전달했습니다. 다음 화면 확인에서 결과를 확인하세요." if self.dispatched else "클릭이 전달되지 않았습니다.")
                        answer["verification_deferred"] = self.dispatched
                        return answer
                elif operation in ("press_key", "hotkey"):
                    arguments = ({"key": self.step["key"], **({"modifiers": self.step["modifiers"]} if "modifiers" in self.step else {})}
                                 if operation == "press_key" else {"keys": self.step["keys"]})
                    # Fresh exact element handles are forwarded unchanged. The
                    # Driver focuses that element; the Guard retains all key,
                    # modifier, foreground and shortcut restrictions.
                    if window_key:
                        self.strategy = "explicit_window_keyboard"
                        self._call(operation, dict(self.target, delivery_mode=self.delivery, **arguments), mutation=True)
                    else:
                        self._mutate(operation, element, snapshot, **arguments)
                elif operation in ("set_checked", "select_item"):
                    desired = self.step["checked"] if operation == "set_checked" else True
                    roles = ("CheckBox", "ToggleButton", "Button") if operation == "set_checked" else ("ListItem", "TabItem", "TreeItem", "RadioButton")
                    action = "toggle" if operation == "set_checked" else "select"
                    actions = element.get("actions")
                    if (element.get("role") not in roles or not isinstance(actions, list) or action not in actions
                            or type(element.get("selected")) is not bool):
                        raise OperationError("이 요소는 필요한 선택 상태 또는 조작 기능을 명확히 제공하지 않습니다. 상태를 추측하여 클릭하지 않습니다.", "unsupported_control")
                    if element["selected"] is desired:
                        passed = self._verify(assertions, snapshot)
                        return self._result("verified" if passed else "failed", "already_satisfied" if passed else "verification_failed",
                                            "현재 선택 상태와 완료 조건을 확인했습니다.")
                    self._mutate("click", element, snapshot)
                else:
                    if element.get("role") != "ComboBox":
                        raise OperationError("select_option은 ComboBox 선택 상자에만 사용할 수 있습니다. 목록·탭·트리의 항목은 select_item과 해당 항목의 selector로 지정하세요.", "unsupported_control")
                    if element.get("value") == self.step["value"] and "value" in element and not requires_change:
                        passed = self._verify(assertions, snapshot)
                        return self._result("verified" if passed else "failed", "already_satisfied" if passed else "verification_failed", "현재 선택값과 완료 조건을 확인했습니다.")
                    if self.step.get("strategy") == "edit_commit":
                        self._edit_commit(snapshot)
                        passed = self._verify(assertions)
                        return self._result("verified" if passed else "failed", "verified" if passed else "verification_failed",
                                            "확정키 후 부모 선택값과 완료 조건을 확인했습니다." if passed else "입력·확정 후 부모 선택값을 확인하지 못했습니다.")
                    if "option_order" in self.step:
                        snapshot, expected_transition = self._select_known_order(snapshot, element)
                        if not expected_transition:
                            return self._result("failed", "unexpected_selection", "키 입력 후 선택값이 확인된 순서와 다릅니다. 추가 이동이나 재입력을 하지 않았습니다.")
                        passed = self._verify(assertions, snapshot)
                        return self._result("verified" if passed else "failed", "verified" if passed else "verification_failed",
                                            "확인된 순서로 선택하고 완료 조건을 확인했습니다." if passed else "선택 후 완료 조건을 충족하지 못했습니다.")
                    options = self._options(snapshot, selector, self.step["value"])
                    option_target = self.target
                    if not options:
                        before = self._visible_windows()
                        self._mutate("click", element, snapshot)
                        popup = self._new_owned_popup(before, self._visible_windows())
                        if popup is not None:
                            self.strategy = "owned_popup"
                            # Some Drivers cannot observe untitled native popups.
                            # Parent UIA can block while that popup is open, so do
                            # not fall back to a parent read after this route fails.
                            self.no_parent_recovery = True
                            snapshot = self._observe([{"name": self.step["value"]}], target=popup)
                            options = self._popup_options(snapshot, self.step["value"])
                            option_target = popup
                        else:
                            self.strategy = "combo_descendants"
                            snapshot = self._observe([selector], option=(selector, self.step["value"]))
                            options = self._options(snapshot, selector, self.step["value"])
                    if len(options) != 1:
                        raise OperationError("해당 선택 상자에 속한 항목을 유일하게 확인하지 못했습니다. 임의 키 입력이나 다른 위치의 같은 이름을 클릭하지 않습니다.", "unsupported_option_structure")
                    self._mutate("click", options[0], snapshot, target=option_target)
                    self.no_parent_recovery = False
                passed = self._verify(assertions)
            return self._result("verified" if passed else "failed", "verified" if passed else "verification_failed",
                                "모든 완료 조건을 확인했습니다." if passed else "현재 화면에서 완료 조건을 충족하지 못했습니다.")
        except Exception as error:
            code = getattr(error, "code", "session_unavailable")
            # The Driver may dispatch the click, then reject its old-HWND
            # foreground acknowledgement because that click replaced the
            # window. Only this narrow diagnostic + native disappearance may
            # proceed to read-only postconditions. Explicit input_sent:false,
            # arbitrary permission denial, and original-still-present cannot.
            acknowledgement_transition = code in {"target_unavailable", "foreground_unavailable"} or (
                code == "target_denied" and ((self.last_guidance or {}).get("diagnostic_code") == "target_unavailable"
                    or (str(error).startswith("foreground_unavailable: exact target HWND ") and "after the click" in str(error))))
            if (self.dispatched and acknowledgement_transition and not self.read_failed
                    and self.transition is None and self.transition_tracker is not None
                    and self.transition_tracker.probe is not None):
                try:
                    if not self.transition_tracker.original_present():
                        self.no_parent_recovery = True
                        self.action_acknowledgement_error = {"code": code, "message": str(error)[:1000]}
                        passed = self._verify(assertions)
                        return self._result("verified" if passed else "failed", "verified_after_window_transition" if passed else "verification_failed",
                            "입력 응답의 창 전환 오류 뒤 새 창의 완료 조건을 확인했습니다. 입력은 반복하지 않았습니다." if passed else
                            "입력 응답 오류 뒤 새 창을 확인했지만 완료 조건을 충족하지 못했습니다.")
                except Exception as recovery_error:
                    error = recovery_error
                    code = getattr(error, "code", "session_unavailable")
            # One read-only recovery attempt can document current conditions;
            # even if they match, an errored mutation stays unknown, never replayed.
            if self.dispatched and not human_completion and not self.no_parent_recovery and not self.read_failed and not code.startswith("transition_") and code not in ("observation_error", "driver_timeout", "stopped", "session_unavailable", "step_timeout", "verification_timeout"):
                try:
                    snapshot = self._observe([a["selector"] for a in assertions])
                    self._evaluate(snapshot, assertions)
                except Exception:
                    pass
            return self._result("needs_target" if code.startswith("transition_") and self.dispatched else "unknown" if self.dispatched else "failed", code, str(error)[:1000])
        finally:
            if self.transition_tracker is not None:
                self.transition_tracker.close()
