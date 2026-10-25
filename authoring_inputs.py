"""Local-only input editing. Never imports the recorder, Driver, or network clients.

These helpers operate on copies. The editor must still validate its complete
workflow before replacing its draft: input links do not prove app capabilities.
"""
from __future__ import annotations

import copy

from task_inputs import (InputError, NAME, PLACEHOLDER, example, expand,
                         validate_specs, validate_value)


def input_name(value):
    """Return one exact input link; interpolated text is deliberately not one."""
    if isinstance(value, dict) and set(value) == {"input_ref"}:
        name = value["input_ref"]
        return name if isinstance(name, str) and NAME.fullmatch(name) else None
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value)
        if match:
            return match[1]
    return None


def _name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise InputError("입력 이름은 영문자로 시작하는 영문·숫자·밑줄 1~40자로 지정하세요.")
    return name


def _references(steps):
    """Inventory includes foreach sources but excludes their local item names."""
    if not isinstance(steps, list):
        raise InputError("작업 단계의 형식을 확인하세요.")
    found = []

    def visit(value, index, step, path=(), local=()):
        if isinstance(value, dict) and "input_ref" in value:
            names = [value["input_ref"]]
        elif isinstance(value, str):
            names = list(dict.fromkeys(PLACEHOLDER.findall(value)))
        else:
            names = []
        if names:
            for name in names:
                if not isinstance(name, str) or not NAME.fullmatch(name):
                    raise InputError("입력 참조의 이름을 확인하세요.")
                if name not in local:
                    found.append((name, {"index": index, "path": list(path),
                        "step_id": step.get("step_id"), "operation": step.get("operation")}))
            return
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, index, step, path + (key,), local)
        elif isinstance(value, list):
            for position, item in enumerate(value):
                visit(item, index, step, path + (position,), local)

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise InputError("작업 단계의 형식을 확인하세요.")
        if step.get("operation") == "foreach":
            source = _name(step.get("input"))
            found.append((source, {"index": index, "path": ["input"],
                "step_id": step.get("step_id"), "operation": "foreach"}))
            body = step.get("steps")
            if not isinstance(body, list):
                raise InputError("반복할 단계를 확인하세요.")
            for position, child in enumerate(body):
                if not isinstance(child, dict):
                    raise InputError("반복할 단계를 확인하세요.")
                visit(child, index, child, ("steps", position), (step.get("item"),))
        else:
            visit(step, index, step)
    return found


def describe_inputs(steps, variables):
    """Return local UI rows, including defaults. Never return these over MCP."""
    specs = validate_specs(variables)
    references = _references(steps)
    rows = []
    for name, spec in specs.items():
        uses = [copy.deepcopy(use) for ref, use in references if ref == name]
        rows.append({"name": name, "spec": spec, "required": "default" not in spec,
                     "uses": uses, "use_count": len(uses)})
    return rows


def _validate_links(steps, variables):
    validate_specs(variables)
    # Expand only one example item per list. Authoring validates links without
    # doing work proportional to a user's entire runtime iteration list.
    values = {name: ([example(spec["items"])] if spec.get("type") == "list" else example(spec))
              for name, spec in variables.items()}
    if steps:
        expand(steps, variables, values)
    for name, use in _references(steps):
        if name not in variables:
            raise InputError("정의되지 않은 입력 변수가 있습니다.")
        if use["path"][-1] == "checked" and variables[name].get("type", "text") != "boolean":
            raise InputError("체크 상태에는 예/아니요 입력만 연결하세요.")


def _rewrite(value, old, new=None, *, fixed=None, replace=False, root=None, path=()):
    if isinstance(value, dict):
        if set(value) == {"input_ref"} and value["input_ref"] == old:
            if not replace:
                return {"input_ref": new}
            rendered = copy.deepcopy(fixed)
            if path and path[-1] in {"value", "name", "automation_id", "message"}:
                rendered = str(rendered)
            if (len(path) == 3 and path[0] == "expect" and path[-1] == "equals"
                    and root.get("expect", [])[path[1]].get("property") in {"value", "name"}):
                rendered = str(rendered)
            return rendered
        if value.get("operation") == "foreach":
            return {key: ([_rewrite(child, old, new, fixed=fixed, replace=replace, root=child)
                           for child in item] if key == "steps" else
                          _rewrite(item, old, new, fixed=fixed, replace=replace, root=root, path=path+(key,)))
                    for key, item in value.items()}
        return {key: _rewrite(item, old, new, fixed=fixed, replace=replace, root=root, path=path+(key,))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, old, new, fixed=fixed, replace=replace, root=root, path=path+(index,))
                for index, item in enumerate(value)]
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda match: (str(fixed) if replace else "${" + new + "}")
                               if match[1] == old else match[0], value)
    return value


def _rewrite_steps(steps, old, new=None, *, fixed=None, replace=False):
    result = [_rewrite(step, old, new, fixed=fixed, replace=replace, root=step) for step in steps]
    for before, after in zip(steps, result):
        if before.get("operation") == "foreach" and before.get("input") == old:
            if replace:
                raise InputError("반복에 사용 중인 목록 입력은 삭제할 수 없습니다. 반복 연결을 먼저 수정하세요.")
            after["input"] = new
    return result


def _same(value, other):
    # bool and int compare equal in Python; they are different input types.
    return type(value) is type(other) and value == other


def _self_checks(step, field, before, after):
    selector = step.get("selector")
    if not isinstance(selector, dict):
        return
    prop = "selected" if field == "checked" else "value"
    old_ref = input_name(before)
    for check in step.get("expect", []):
        if (isinstance(check, dict) and check.get("selector") == selector
                and check.get("property") == prop
                and (_same(check.get("equals"), before)
                     or old_ref is not None and input_name(check.get("equals")) == old_ref)):
            check["equals"] = copy.deepcopy(after)


def _binding_step(steps, command):
    index = command.get("index")
    if type(index) is not int or not 0 <= index < len(steps):
        raise InputError("입력값을 바꿀 단계를 선택하세요.")
    step = steps[index]
    default = "checked" if step.get("operation") == "set_checked" else "value"
    field = command.get("field", default)
    allowed = (field == "value" and step.get("operation") in {"set_value", "select_option", "image_type_text"}
               or field == "checked" and step.get("operation") == "set_checked")
    if not allowed:
        raise InputError("실제 값 입력·선택·체크 동작만 실행 입력으로 바꿀 수 있습니다. 이미지 클릭은 대상을 다시 지정하세요.")
    return step, field


def edit_inputs(steps, variables, command):
    """Atomically return copied draft data for one explicit local edit.

    Actions: upsert{name,spec}, rename{name,new_name}, remove{name,value?},
    bind{index,name,field?,spec?}, unbind{index,field?,value}. A used input
    cannot be removed without an explicitly supplied, type-valid fixed value.
    No command implicitly merges equal values or changes unrelated checks.
    """
    if not isinstance(command, dict):
        raise InputError("입력값 수정 내용을 확인하세요.")
    action = command.get("action")
    fields = {"upsert": {"action", "name", "spec"},
              "rename": {"action", "name", "new_name"},
              "remove": {"action", "name", "value"},
              "bind": {"action", "index", "name", "field", "spec"},
              "unbind": {"action", "index", "field", "value"}}
    if action not in fields or set(command) - fields[action]:
        raise InputError("지원하는 입력값 수정 동작을 선택하세요.")
    proposed, specs = copy.deepcopy(steps), validate_specs(variables)
    if not isinstance(proposed, list):
        raise InputError("작업 단계의 형식을 확인하세요.")
    if action == "upsert":
        name = _name(command.get("name"))
        specs[name] = copy.deepcopy(command.get("spec"))
    elif action == "rename":
        name, new = _name(command.get("name")), _name(command.get("new_name"))
        if name not in specs:
            raise InputError("수정할 입력값을 선택하세요.")
        if new != name:
            if new in specs:
                raise InputError("이미 사용 중인 입력 이름입니다. 값을 합치지 않았습니다.")
            specs = {(new if key == name else key): spec for key, spec in specs.items()}
            proposed = _rewrite_steps(proposed, name, new)
    elif action == "remove":
        name = _name(command.get("name"))
        if name not in specs:
            raise InputError("삭제할 입력값을 선택하세요.")
        used = any(ref == name for ref, _ in _references(proposed))
        if used:
            if "value" not in command:
                raise InputError("단계에서 사용 중인 입력입니다. 연결을 해제하거나 고정값을 지정하세요.")
            fixed = validate_value(command["value"], specs[name], name)
            proposed = _rewrite_steps(proposed, name, fixed=fixed, replace=True)
        del specs[name]
    else:
        step, field = _binding_step(proposed, command)
        before = copy.deepcopy(step.get(field))
        if action == "bind":
            name = _name(command.get("name"))
            if "spec" in command:
                specs[name] = copy.deepcopy(command["spec"])
            if name not in specs:
                raise InputError("연결할 입력값을 먼저 추가하세요.")
            validate_specs(specs)
            if specs[name].get("type") == "list":
                raise InputError("목록 입력은 반복 구간에만 연결하세요.")
            if field == "checked" and specs[name].get("type", "text") != "boolean":
                raise InputError("체크 상태에는 예/아니요 입력만 연결하세요.")
            step[field] = {"input_ref": name}
        else:
            if "value" not in command:
                raise InputError("연결을 해제한 뒤 사용할 고정값을 지정하세요.")
            name = input_name(before)
            if name is None or name not in specs:
                raise InputError("단일 실행 입력으로 연결된 단계를 선택하세요.")
            fixed = validate_value(command["value"], specs[name], name)
            if field == "checked" and type(fixed) is not bool:
                raise InputError("체크 상태는 예 또는 아니요로 지정하세요.")
            step[field] = str(fixed) if field == "value" else fixed
        _self_checks(step, field, before, step[field])
    _validate_links(proposed, specs)
    return proposed, specs
