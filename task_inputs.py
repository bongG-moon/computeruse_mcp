"""Typed declarative inputs and bounded expansion. No expression evaluation."""
from __future__ import annotations

import copy
from datetime import date
import math
import re

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")
STEP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]{0,39})\}")
MAX_EXPANDED_STEPS = 300
MAX_ITERATIONS = 100


class InputError(ValueError):
    pass


def validate_value(value, spec, name):
    kind = spec.get("type", "text")
    good = False
    if kind in {"text", "date", "enum"}:
        good = isinstance(value, str) and len(value) <= 4000 and "\x00" not in value
        if good:
            try: value.encode("utf-8")
            except UnicodeError: good = False
        if good and kind == "date":
            try: good = date.fromisoformat(value).isoformat() == value
            except ValueError: good = False
        if good and kind == "enum": good = value in spec["values"]
    elif kind == "integer": good = type(value) is int and abs(value) <= 1e15
    elif kind == "number": good = type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e15
    elif kind == "boolean": good = type(value) is bool
    elif kind == "list":
        good = isinstance(value, list) and 1 <= len(value) <= spec.get("max_items", MAX_ITERATIONS)
        if good:
            for item in value: validate_value(item, spec["items"], name)
    if good and type(value) in (int, float):
        good = value >= spec.get("minimum", -1e15) and value <= spec.get("maximum", 1e15)
    if not good: raise InputError("입력값의 형식 또는 범위를 확인하세요: " + name)
    return copy.deepcopy(value)


def validate_spec(spec, name, *, item=False):
    if not isinstance(spec, dict): raise InputError("입력 형식을 확인하세요: " + name)
    kind = spec.get("type", "text")
    allowed = {"description", "default", "type"}
    if kind == "enum": allowed |= {"values"}
    elif kind == "list": allowed |= {"items", "max_items"}
    elif kind in {"number", "integer"}: allowed |= {"minimum", "maximum"}
    if (kind not in {"text", "date", "enum", "number", "integer", "boolean", "list"}
            or item and kind == "list" or set(spec) - allowed
            or not isinstance(spec.get("description", ""), str) or len(spec.get("description", "")) > 4000):
        raise InputError("지원하는 입력 형식과 속성을 사용하세요: " + name)
    if kind == "enum":
        values = spec.get("values")
        if (not isinstance(values, list) or not 1 <= len(values) <= 100
                or any(not isinstance(v, str) or not v or len(v) > 4000 for v in values)
                or len(values) != len(set(values))):
            raise InputError("선택 입력에는 중복 없는 values 목록이 필요합니다: " + name)
    if kind == "list":
        if type(spec.get("max_items", MAX_ITERATIONS)) is not int or not 1 <= spec.get("max_items", MAX_ITERATIONS) <= MAX_ITERATIONS:
            raise InputError("목록 반복은 최대 100개입니다: " + name)
        validate_spec(spec.get("items"), name, item=True)
    for field in ("minimum", "maximum"):
        if field in spec and (type(spec[field]) not in (int, float) or not math.isfinite(spec[field]) or abs(spec[field]) > 1e15):
            raise InputError("숫자 입력 범위를 확인하세요: " + name)
        if field in spec and kind == "integer" and type(spec[field]) is not int:
            raise InputError("정수 입력의 범위도 정수로 지정하세요: " + name)
    if spec.get("minimum", -1e15) > spec.get("maximum", 1e15): raise InputError("숫자 입력 범위가 반대입니다: " + name)
    if "default" in spec: validate_value(spec["default"], spec, name)
    return copy.deepcopy(spec)


def validate_specs(specs):
    if not isinstance(specs, dict) or len(specs) > 30: raise InputError("입력 변수는 최대 30개입니다.")
    for name, spec in specs.items():
        if not isinstance(name, str) or not NAME.fullmatch(name): raise InputError("입력 변수 이름을 확인하세요.")
        validate_spec(spec, name)
    return copy.deepcopy(specs)


def example(spec):
    if "default" in spec: return copy.deepcopy(spec["default"])
    kind = spec.get("type", "text")
    if kind == "list": return [example(spec["items"])]
    if kind == "enum": return spec["values"][0]
    if kind == "date": return "2000-01-01"
    if kind == "boolean": return False
    if kind in {"integer", "number"}: return spec.get("minimum", min(0, spec.get("maximum", 0)))
    return "input"


def resolve_values(specs, inputs):
    validate_specs(specs)
    if not isinstance(inputs, dict) or set(inputs) - set(specs): raise InputError("정의된 입력 변수만 지정하세요.")
    return {name: validate_value(inputs.get(name, spec.get("default")), spec, name) for name, spec in specs.items()}


def _allowed(path):
    # Program identity, saved image bytes, operation names, keys and timing are
    # never substituted. Values/selectors are plain data, not code or paths.
    return (path in {("value",), ("checked",), ("message",)}
        or len(path) >= 2 and path[0] in {"selector", "edit_selector"} and path[-1] in {"name", "automation_id"}
        or len(path) >= 3 and path[0] == "expect" and (path[-1] == "equals" or path[-1] in {"name", "automation_id"} and "selector" in path))


def substitute_step(step, values, specs):
    def visit(value, path=()):
        ref = value.get("input_ref") if isinstance(value, dict) else None
        matches = PLACEHOLDER.findall(value) if isinstance(value, str) else []
        if ref is not None or matches:
            if not _allowed(path): raise InputError("이 필드는 입력값으로 바꿀 수 없습니다: " + ".".join(map(str, path)))
            refs = [ref] if ref is not None else matches
            if (ref is not None and set(value) != {"input_ref"}) or any(not isinstance(r, str) or r not in specs for r in refs):
                raise InputError("정의되지 않은 입력 변수가 있습니다.")
            if any(specs[r].get("type") == "list" for r in refs): raise InputError("목록 입력은 foreach에서만 사용하세요.")
            resolved = values[ref] if ref is not None else PLACEHOLDER.sub(lambda m: str(values[m[1]]), value)
            if ref is not None and (path[-1] == "value" or path[-1] in {"name", "automation_id", "message"}):
                resolved = str(resolved)
            if ref is not None and path[-1] == "equals" and step.get("expect", [])[path[1]].get("property") in {"value", "name"}:
                resolved = str(resolved)
            return copy.deepcopy(resolved)
        if isinstance(value, dict): return {k: visit(v, path+(k,)) for k, v in value.items()}
        if isinstance(value, list): return [visit(v, path+(i,)) for i, v in enumerate(value)]
        return value
    return visit(step)


def expand(steps, specs, values):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30: raise InputError("작업은 1~30개의 단계 또는 반복 그룹으로 지정하세요.")
    seen, result = set(), []
    def check_id(step):
        ident = step.get("step_id")
        if ident is not None:
            if not isinstance(ident, str) or not STEP_ID.fullmatch(ident) or ident in seen: raise InputError("단계 ID가 잘못되었거나 중복됩니다.")
            seen.add(ident)
    for index, step in enumerate(steps):
        if not isinstance(step, dict): raise InputError("작업 단계의 형식을 확인하세요.")
        check_id(step)
        if step.get("operation") != "foreach":
            result.append(substitute_step(step, values, specs))
            continue
        if set(step)-{"operation", "step_id", "input", "item", "steps"}:
            raise InputError("foreach에는 input, item, steps만 지정하세요.")
        name, item, body = step.get("input"), step.get("item"), step.get("steps")
        if (not isinstance(name, str) or name not in specs or specs[name].get("type") != "list"
                or not isinstance(item, str) or not NAME.fullmatch(item) or item in specs
                or not isinstance(body, list) or not 1 <= len(body) <= 30):
            raise InputError("foreach에는 목록 입력과 고유한 item 이름, 1~30단계가 필요합니다.")
        for child in body:
            if not isinstance(child, dict) or child.get("operation") in {"foreach", "wait_for_window"}:
                raise InputError("반복 안에는 중첩 반복이나 팝업 재연결을 사용할 수 없습니다.")
            check_id(child)
        child_specs = {**specs, item: specs[name]["items"]}
        for iteration, value in enumerate(values[name]):
            for child in body:
                rendered = substitute_step(child, {**values, item: value}, child_specs)
                rendered["iteration_id"] = (step.get("step_id") or "group-"+str(index+1)) + ":" + str(iteration+1)
                result.append(rendered)
    if len(result) > MAX_EXPANDED_STEPS: raise InputError("목록을 펼친 전체 동작은 최대 300단계입니다.")
    return result
