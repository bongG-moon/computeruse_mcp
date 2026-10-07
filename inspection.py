"""Program-independent, read-only control discovery through the guarded Driver."""
from __future__ import annotations

import copy
import json

from operations import OperationError, _descendant, _elements, _name, _payload, _unique, validate_selector


def _selector_candidates(element):
    candidates = []
    identity = {}
    if isinstance(element.get("automation_id"), str) and element["automation_id"].strip() and len(element["automation_id"]) <= 1000:
        identity = {"automation_id": element["automation_id"]}
        candidates.append(identity)
    name = _name(element)
    if isinstance(name, str) and name.strip() and len(name) <= 1000:
        named = {"name": name, **({"role": element["role"]} if isinstance(element.get("role"), str) and 0 < len(element["role"]) <= 1000 else {})}
        candidates.append(named)
        if identity:
            candidates.append({**identity, **named})
    return candidates


def _base_selector(element):
    candidates = _selector_candidates(element)
    return candidates[0] if candidates else None


def _is_unique(snapshot, selector, element):
    try:
        return _unique(snapshot, selector) is element
    except OperationError:
        return False


def control_selector(snapshot, element):
    candidates = _selector_candidates(element)
    if not candidates:
        return None
    for selector in candidates:
        if _is_unique(snapshot, selector, element):
            return selector
    by_index = {e["element_index"]: e for e in _elements(snapshot) if type(e.get("element_index")) is int}
    seen = set()
    parent = element.get("parent_index")
    for _ in range(20):
        if type(parent) is not int or parent in seen or parent not in by_index:
            break
        seen.add(parent)
        ancestor = by_index[parent]
        for scope in _selector_candidates(ancestor):
            if _is_unique(snapshot, scope, ancestor):
                for selector in candidates:
                    scoped = {**selector, "within": scope}
                    if _is_unique(snapshot, scoped, element):
                        return scoped
        parent = ancestor.get("parent_index")
    return None


def suggested_operations(element):
    actions = element.get("actions", [])
    actions = set(a for a in actions if isinstance(a, str)) if isinstance(actions, list) else set()
    role = element.get("role")
    supported = ["assert"]
    if role in {"Edit", "Document", "TextBox"} and "set_value" in actions and element.get("read_only") is not True and element.get("is_read_only") is not True:
        supported.append("set_value")
    if role == "ComboBox" and actions & {"expand", "select", "set_value"}:
        supported.append("select_option")
    if role in {"CheckBox", "ToggleButton", "Button"} and "toggle" in actions and type(element.get("selected")) is bool:
        supported.append("set_checked")
    if role in {"ListItem", "TabItem", "TreeItem", "RadioButton"} and "select" in actions and type(element.get("selected")) is bool:
        supported.append("select_item")
    if actions & {"invoke", "click", "toggle", "select", "expand"}:
        supported.append("click")
    return supported


def inspect_window(runtime, target, *, max_controls=80, max_depth=12, max_elements=600,
                   search="", within=None, offset=0, actionable_only=False):
    if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
            or any(type(v) is not int or v < 1 for v in target.values())):
        raise OperationError("관찰할 현재 창의 pid/window_id를 지정하세요.")
    for name, value, ceiling in (("max_controls", max_controls, 200), ("max_depth", max_depth, 32), ("max_elements", max_elements, 5000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise OperationError(f"{name}는 1~{ceiling} 범위의 정수여야 합니다.")
    if not isinstance(search, str) or len(search) > 200:
        raise OperationError("search는 200자 이하 검색어여야 합니다.")
    if type(offset) is not int or not 0 <= offset <= 5000 or type(actionable_only) is not bool:
        raise OperationError("목록 시작 위치와 조작 요소 필터를 확인하세요.")
    if within is not None:
        within = validate_selector(within)
    runtime.check_active()
    uia = runtime.mode == "uia"
    if not uia and (search or within is not None or offset or actionable_only):
        raise OperationError("요소 검색·영역·페이지 필터는 UIA 방식에서만 사용할 수 있습니다.")
    args = {**target, "include_accessibility_tree": uia, "include_screenshot": not uia}
    if uia:
        args.update(max_depth=max_depth, max_elements=max_elements)
    answer = runtime.call("get_window_state", args)
    if answer.get("isError"):
        return answer
    snapshot = _payload(answer)
    if any(k in snapshot and snapshot[k] != v for k, v in target.items()):
        raise OperationError("관찰된 창이 요청한 창과 다릅니다.", "target_mismatch")
    inspection = {"mode": runtime.mode, "control_count": 0, "controls": [], "controls_omitted": 0,
                  "scope": "one_current_window", "capability_scope": "observed_controls_only",
                  "execution_verified": False,
                  "task_verified": False, "input_dispatched": False}
    if not uia:
        image_count = sum(c.get("type") == "image" for c in answer.get("content", []))
        inspection.update(status="visual_observation" if image_count else "missing_image", image_count=image_count,
                          next_step="이미지를 이해하는 연결 모델이 화면을 읽고, 이 관찰의 창 안에서 좌표를 지정해야 합니다. 좌표는 저장 작업에 재사용하지 않습니다." if image_count else "이 창의 화면 이미지가 반환되지 않았습니다. 입력하지 말고 관찰 상태를 확인하세요.")
    else:
        try:
            elements = _elements(snapshot)
        except OperationError:
            elements = []
        controls = []
        candidate_count = 0
        all_candidates = 0
        scope = _unique(snapshot, within) if within is not None else None
        query = search.casefold().strip()
        for element in elements:
            actions = element.get("actions", [])
            if not isinstance(actions, list):
                actions = []
            # Keep app text/status for completion checks, not just actionable widgets.
            if not (_base_selector(element) or actions):
                continue
            all_candidates += 1
            if scope is not None and not _descendant(element, scope, elements):
                continue
            if actionable_only and not actions:
                continue
            searchable = " ".join(str(value) for value in (_name(element) or "", element.get("role", ""), element.get("automation_id", "")))
            if query and query not in searchable.casefold():
                continue
            candidate_count += 1
            if candidate_count <= offset:
                continue
            if len(controls) >= max_controls:
                continue
            item = {k: copy.deepcopy(element[k]) for k in ("element_index", "parent_index", "role", "value", "selected", "enabled", "read_only", "automation_id") if k in element}
            if _name(element) is not None:
                item["name"] = _name(element)
            item["actions"] = [a for a in actions if isinstance(a, str)]
            item["selector"] = control_selector(snapshot, element)
            item["selector_unique"] = item["selector"] is not None
            item["suggested_operations"] = suggested_operations(element) if item["selector_unique"] else []
            item["capability_evidence"] = "observed_uia_properties_and_patterns"
            item["execution_verified"] = False
            for key in ("name", "value", "automation_id"):
                if isinstance(item.get(key), str) and len(item[key]) > 16000:
                    item[key + "_truncated"] = True
                    item[key] = item[key][:16000]
            controls.append(item)
        inspection.update(status="uia_observation" if controls else "no_accessible_controls",
                          observation_scope={"kind": "driver_window_tree", "max_depth": max_depth,
                                             "max_elements": max_elements},
                          filter_scope="returned_controls_after_window_observation",
                          filters_reduce_uia_traversal=False,
                          control_count=candidate_count, controls=controls,
                          observed_control_count=all_candidates, offset=offset,
                          next_offset=offset+len(controls) if offset+len(controls) < candidate_count else None,
                          controls_omitted=max(0, candidate_count-len(controls)),
                          filters={"search": search, "within": within, "actionable_only": actionable_only},
                          traversal_may_be_limited=bool(len(elements) >= max_elements or snapshot.get("truncated") or snapshot.get("max_depth_reached") or snapshot.get("max_elements_reached")
                                                       or any(type(e.get("depth")) is int and e["depth"] >= max_depth for e in elements)
                                                       or type(snapshot.get("total_element_count")) is int and snapshot["total_element_count"] > len(elements)),
                          next_step="저장한 요소는 computer_elements로 찾고 computer_use_element로 검증하며 사용하세요. 새 요소는 사용자가 직접 computer_teach_element로 가르칠 수 있습니다. 검색·within·offset으로 목록을 좁힐 수 있으며 번호는 현재 관찰에만 유효합니다. 일반 작업은 관찰된 selector와 완료 조건으로 computer_perform을 사용하세요." if controls else
                                    "이 창에서 UIA 조작 대상을 확인하지 못했습니다. 창 준비 상태를 확인하거나 현재 세션을 끝내고 명시적으로 visual 방식으로 관찰하세요. 자동 입력·방식 전환은 하지 않았습니다.")
    data = {**target, "inspection": inspection}
    for key in ("window_title", "snapshot_id", "computer_use_metrics", "screenshot_width", "screenshot_height", "window_bounds", "capture_coverage", "accessibility_normalization"):
        if key in snapshot:
            data[key] = snapshot[key]
    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    if not uia:
        content.extend(copy.deepcopy(c) for c in answer.get("content", []) if c.get("type") == "image")
    return {"isError": False, "structuredContent": data, "content": content}
