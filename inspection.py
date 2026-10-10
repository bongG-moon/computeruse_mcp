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
                   search="", within=None, offset=0, actionable_only=False, observation="auto", requested_goal=""):
    if (not isinstance(target, dict) or set(target) != {"pid", "window_id"}
            or any(type(v) is not int or v < 1 for v in target.values())):
        raise OperationError("관찰할 현재 창의 pid/window_id를 지정하세요.")
    for name, value, ceiling in (("max_controls", max_controls, 200), ("max_depth", max_depth, 32), ("max_elements", max_elements, 5000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise OperationError(f"{name}는 1~{ceiling} 범위의 정수여야 합니다.")
    if not isinstance(search, str) or len(search) > 200:
        raise OperationError("search는 200자 이하 검색어여야 합니다.")
    if not isinstance(requested_goal, str) or len(requested_goal) > 2000:
        raise OperationError("작업 목표는 2000자 이하 문자열이어야 합니다.")
    if type(offset) is not int or not 0 <= offset <= 5000 or type(actionable_only) is not bool:
        raise OperationError("목록 시작 위치와 조작 요소 필터를 확인하세요.")
    if within is not None:
        within = validate_selector(within)
    if observation not in ("auto", "uia", "visual", "both"):
        raise OperationError("observation은 auto, uia, visual, both 중 하나여야 합니다.")
    if runtime.mode != "uia" and observation in ("uia", "both"):
        raise OperationError("이 세션은 이미지 관찰 방식입니다. UIA 관찰은 uia 세션에서 요청하세요.", "unsupported_observation")
    runtime.check_active()
    uia = runtime.mode == "uia" and observation != "visual"
    if not uia and (search or within is not None or offset or actionable_only):
        raise OperationError("요소 검색·영역·페이지 필터는 UIA 방식에서만 사용할 수 있습니다.")
    scoped = False
    scope_fallback = None
    answer = None
    report = getattr(runtime, "report_progress", None)
    if callable(report):
        report("searching" if within is not None else "observing")
    # Native scoped inspection resolves one unique parent first, then walks only
    # its subtree. It supplies no Driver handles and cannot authorize input.
    inspector = getattr(runtime, "inspect_controls", None)
    if uia and within is not None and "within" not in within and callable(inspector):
        try:
            answer = inspector(dict(target), within=copy.deepcopy(within), max_depth=max_depth,
                               max_elements=max_elements, timeout_ms=6000)
            scoped = True
        except NotImplementedError:
            scope_fallback = "scoped_helper_unavailable"
        # Timeout, ambiguous scope, permission and stale-target errors are final:
        # never turn them into an unbounded full-window walk or screenshot.
    if answer is None:
        if runtime.mode == "uia" and not uia:
            answer = {"structuredContent": dict(target), "content": []}
        else:
            args = {**target, "include_accessibility_tree": uia, "include_screenshot": not uia}
            if uia:
                args.update(max_depth=max_depth, max_elements=max_elements)
            answer = runtime.call("get_window_state", args)
    if answer.get("isError"):
        return answer
    snapshot = _payload(answer)
    if any(k in snapshot and snapshot[k] != v for k, v in target.items()):
        raise OperationError("관찰된 창이 요청한 창과 다릅니다.", "target_mismatch")
    if scoped and (snapshot.get("scoped_inspection") is not True or snapshot.get("read_only") is not True
                   or snapshot.get("scope_complete") is not True or snapshot.get("within") != within):
        raise OperationError("선택 영역의 읽기 전용 관찰 범위를 확인하지 못했습니다.", "scoped_response_invalid")
    inspection = {"mode": runtime.mode, "control_count": 0, "controls": [], "controls_omitted": 0,
                  "scope": "one_current_window", "capability_scope": "observed_controls_only",
                  "execution_verified": False,
                  "task_verified": False, "input_dispatched": False,
                  "requested_observation": observation, "input_mode_unchanged": True,
                  "requested_goal": requested_goal,
                  "image_delivery": {"format": "mcp_image_content", "client_rendering_verified": False,
                                     "model_image_understanding_verified": False}}
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
        parents = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int} if scope is not None else None
        query = search.casefold().strip()
        for element in elements:
            actions = element.get("observed_patterns" if scoped else "actions", [])
            if not isinstance(actions, list):
                actions = []
            # Keep app text/status for completion checks, not just actionable widgets.
            if not (_base_selector(element) or actions):
                continue
            all_candidates += 1
            if scope is not None and not _descendant(element, scope, elements, parents):
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
            if scoped:
                # Partial subtree uniqueness is not proof of an exact selector.
                item["selector"] = None
                if not snapshot.get("truncated"):
                    for candidate in _selector_candidates(element):
                        candidate = {**candidate, "within": copy.deepcopy(within)}
                        if _is_unique(snapshot, candidate, element):
                            item["selector"] = candidate
                            break
                item["verification_only"] = True
                item["input_requires_fresh_observation"] = True
                item.pop("element_index", None)
                item.pop("parent_index", None)
            else:
                item["selector"] = control_selector(snapshot, element)
            item["selector_unique"] = item["selector"] is not None
            item["suggested_operations"] = suggested_operations({**element, "actions": actions}) if item["selector_unique"] else []
            item["capability_evidence"] = "native_read_only_uia_properties_and_patterns" if scoped else "observed_uia_properties_and_patterns"
            item["execution_verified"] = False
            for key in ("name", "value", "automation_id"):
                if isinstance(item.get(key), str) and len(item[key]) > 16000:
                    item[key + "_truncated"] = True
                    item[key] = item[key][:16000]
            controls.append(item)
        inspection.update(status="uia_observation" if controls else "no_accessible_controls",
                          observation_scope={"kind": "native_selected_subtree" if scoped else "driver_window_tree", "max_depth": max_depth,
                                             "max_elements": max_elements},
                          filter_scope="selected_subtree_before_property_read" if scoped else "returned_controls_after_window_observation",
                          filters_reduce_uia_traversal=False,
                          filters_reduce_uia_property_reads=scoped,
                          scope_lookup="whole_window_identity_metadata" if scoped else "driver_window_tree",
                          scope_lookup_control_count=snapshot.get("lookup_metadata_count") if scoped else None,
                          control_count=candidate_count, controls=controls,
                          observed_control_count=all_candidates, offset=offset,
                          next_offset=offset+len(controls) if offset+len(controls) < candidate_count else None,
                          controls_omitted=max(0, candidate_count-len(controls)),
                          filters={"search": search, "within": within, "actionable_only": actionable_only},
                          traversal_may_be_limited=bool(len(elements) >= max_elements or snapshot.get("truncated") or snapshot.get("max_depth_reached") or snapshot.get("max_elements_reached")
                                                       or any(type(e.get("depth")) is int and e["depth"] >= max_depth for e in elements)
                                                       or type(snapshot.get("total_element_count")) is int and snapshot["total_element_count"] > len(elements)),
                          next_step="저장한 요소는 computer_elements로 찾고 computer_use_element로 검증하며 사용하세요. 새 요소는 사용자가 직접 computer_teach_element로 가르칠 수 있습니다. 검색·within·offset으로 목록을 좁힐 수 있으며 번호는 현재 관찰에만 유효합니다. 일반 작업은 관찰된 selector와 완료 조건으로 computer_perform을 사용하세요." if controls else
                                    "이 창에서 UIA 조작 대상을 확인하지 못했습니다. 함께 반환된 화면 이미지나 observation=visual로 현재 창을 읽고, UIA가 제공하지 않는 요소는 computer_process_editor에서 이미지로 선택해 저장할 수 있습니다. 입력 방식과 권한은 바뀌지 않습니다.")
    if scope_fallback:
        inspection["scope_fallback"] = scope_fallback
    images = [copy.deepcopy(c) for c in answer.get("content", [])
              if isinstance(c, dict) and c.get("type") == "image"] if not uia else []
    weak = False
    if uia:
        chrome = [e for e in elements if e.get("role") == "TitleBar"]
        ancestry = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int}
        actionable = [e for e in elements if _base_selector(e)
                      and not any(_descendant(e, bar, elements, ancestry) for bar in chrome)
                      and e.get("role") in {"Button", "Edit", "Document", "TextBox", "ComboBox", "CheckBox",
                                           "ToggleButton", "ListItem", "TabItem", "TreeItem", "RadioButton"}
                      and e.get("observed_patterns" if scoped else "actions")]
        weak = not actionable
        inspection["uia_evidence"] = ("actionable_controls_observed" if actionable else
                                      "structure_only" if elements else "empty_accessibility")
        inspection["input_requires_fresh_observation"] = scoped
    # A usable toolbar is not evidence that the requested row/icon is exposed.
    # Goal-aware callers receive visual evidence even when unrelated UIA controls
    # exist. This never claims that text-only clients understand those pixels.
    goal_needs_image = bool(requested_goal.strip())
    inspection["goal_target_verified"] = False
    inspection["missing_target_evidence"] = bool(goal_needs_image)
    needs_image = runtime.mode == "uia" and (observation in ("visual", "both") or observation == "auto" and (weak or goal_needs_image))
    capture_failed = False
    if needs_image:
        capture = getattr(runtime, "capture_checkpoint", None)
        reason = ("explicit_request" if observation in ("visual", "both") else
                  "goal_target_not_verified" if goal_needs_image else "weak_accessibility")
        image_state = {"requested": True, "reason": reason, "read_only": True, "status": "unavailable"}
        if callable(capture):
            if callable(report):
                report("observing")
            captured = capture(dict(target))
            runtime.check_active()
            if captured.get("isError"):
                details = _payload(captured)
                code = details.get("error_code", "capture_unavailable")
                image_state.update(diagnostic_code=code, next_step=(
                    "대상 창을 최소화하지 않고 맨 앞으로 가져온 뒤 observation=both로 다시 읽으세요. 자동으로 창을 전환하지 않았습니다."
                    if code == "checkpoint_requires_foreground" else
                    "이미지를 받지 못했습니다. 대상 창과 캡처 진단을 확인하세요. 입력이나 다른 창 캡처는 하지 않았습니다."))
                capture_failed = True
            else:
                visual_snapshot = _payload(captured)
                if any(visual_snapshot.get(k) != v for k, v in target.items()):
                    raise OperationError("캡처 결과의 창 식별자가 요청한 창과 다릅니다.", "target_mismatch")
                images = [copy.deepcopy(c) for c in captured.get("content", [])
                          if isinstance(c, dict) and c.get("type") == "image"]
                image_state.update(status="available" if images else "missing_image", image_count=len(images))
                capture_failed = not images
                for key in ("screenshot_width", "screenshot_height", "window_bounds", "capture_coverage"):
                    if key in visual_snapshot:
                        snapshot[key] = visual_snapshot[key]
        else:
            image_state.update(diagnostic_code="capture_not_supported", next_step="이 연결은 읽기 전용 이미지 보완을 지원하지 않습니다. MCP 버전을 확인하세요.")
            capture_failed = True
        image_state["capture_scope"] = "approved_window"
        image_state["uia_and_image_atomic"] = False
        inspection["image_capture"] = image_state
        inspection["image_count"] = len(images)
        # A checkpoint intentionally invalidates prior Driver input handles.
        # Do not present the earlier UIA indices/snapshot as immediately usable.
        inspection["input_requires_fresh_observation"] = True
        for control in inspection["controls"]:
            control["input_requires_fresh_observation"] = True
            control.pop("element_index", None)
            control.pop("parent_index", None)
        snapshot.pop("snapshot_id", None)
        if images:
            inspection["status"] = "combined_observation" if uia else "visual_observation"
            inspection["next_step"] = ("같은 승인 창의 화면 이미지가 함께 제공됐습니다. 이미지 이해를 지원하는 연결 모델이 화면을 읽을 수 있습니다. "
                                       "UIA selector는 새 관찰로 확인하고, UIA가 노출하지 않는 대상은 computer_process_editor에서 이미지로 선택해 저장하세요. "
                                       "이미지 자체는 작업 성공이나 좌표 입력 권한을 의미하지 않습니다.")
        elif not uia:
            inspection["status"] = "missing_image"
        if capture_failed:
            inspection["next_step"] = image_state.get("next_step", "이 창의 화면 이미지가 반환되지 않았습니다. 입력 전에 화면을 다시 확인하세요.")
    inspection["observed_modalities"] = (["uia"] if uia else []) + (["visual"] if images else [])
    data = {**target, "inspection": inspection}
    for key in ("window_title", "snapshot_id", "computer_use_metrics", "screenshot_width", "screenshot_height", "window_bounds", "capture_coverage", "accessibility_normalization"):
        if key in snapshot:
            data[key] = snapshot[key]
    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    content.extend(images)
    return {"isError": bool(capture_failed and not uia), "structuredContent": data, "content": content}
