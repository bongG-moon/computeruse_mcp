"""Program-independent, read-only control discovery through the guarded Driver."""
from __future__ import annotations

import copy
import json
import math
import time

from operations import OperationError, _descendant, _elements, _name, _payload, _unique, validate_selector, supports_set_value


def _protected(element):
    return (element.get("role") == "Password" or any(element.get(key) is True
            for key in ("protected", "is_protected", "is_password", "password")))


def _selector_candidates(element):
    if _protected(element):
        return []
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
    if _protected(element):
        return []
    actions = element.get("actions", [])
    actions = set(a for a in actions if isinstance(a, str)) if isinstance(actions, list) else set()
    role = element.get("role")
    supported = ["assert"]
    if "set_value" in actions and supports_set_value(element):
        supported.append("set_value")
    if role == "ComboBox" and actions & {"expand", "select", "set_value"}:
        supported.append("select_option")
    if role in {"CheckBox", "ToggleButton", "Button"} and "toggle" in actions and type(element.get("selected")) is bool:
        supported.append("set_checked")
    if role in {"ListItem", "TabItem", "TreeItem", "RadioButton", "DataItem", "Row"} and "select" in actions and type(element.get("selected")) is bool:
        supported.append("select_item")
    if actions & {"invoke", "click", "toggle", "select", "expand"}:
        supported.append("click")
    return supported


def _observed_rectangle(element):
    # Same documented Driver contract as learning_picker._driver_rectangle:
    # frame{x,y,w,h} and the legacy bounds{x,y,width,height} are desktop physical
    # rectangles. They are NOT image coordinates; never infer a scale/offset.
    if element.get("coordinate_space", "desktop_physical") != "desktop_physical":
        return None
    field = "frame" if "frame" in element else "bounds" if "bounds" in element else None
    keys = ("x", "y", "w", "h") if field == "frame" else ("x", "y", "width", "height")
    value = element.get(field)
    if not isinstance(value, dict) or any(key not in value for key in keys):
        return None
    values = [value[key] for key in keys]
    try:
        valid = (all(type(number) in (int, float) and math.isfinite(number) for number in values)
                 and values[2] > 0 and values[3] > 0
                 and math.isfinite(values[0] + values[2]) and math.isfinite(values[1] + values[3]))
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        return None
    return {"bounds": dict(zip(("x", "y", "width", "height"), values)),
            "coordinate_space": "desktop_physical", "source": "driver." + field,
            "image_mapping": "not_inferred", "read_only": True}


def _context_name(element):
    name = _name(element)
    if (_protected(element) or not isinstance(name, str) or not name.strip() or len(name) > 200
            or element.get("role") in {"Window", "Document", "Edit", "TextBox", "Password"}
            or isinstance(element.get("value"), str) and element.get("value") and name == element["value"]):
        return None
    return name


def _context_index(elements):
    by_index, duplicates, labels = {}, set(), {}
    for element in elements:
        index = element.get("element_index")
        if type(index) is int:
            if index in by_index:
                duplicates.add(index)
            by_index[index] = element
    for index in duplicates:
        by_index.pop(index)
    for element in elements:
        parent = element.get("parent_index")
        if (type(parent) is int and parent in by_index and not _protected(by_index[parent])
                and element.get("role") in {"Text", "Label", "StaticText"} and _context_name(element)):
            labels.setdefault(parent, []).append(element)
    return by_index, labels


def _read_only_context(element, by_index, labels, window_bounds=None):
    if _protected(element):
        return None
    result = {"read_only": True, "authorizes_input": False}
    geometry = _observed_rectangle(element)
    window_geometry = _observed_rectangle({"bounds": window_bounds}) if isinstance(window_bounds, dict) else None
    if geometry and window_geometry:
        bounds, window = geometry["bounds"], window_geometry["bounds"]
        if (min(bounds["x"] + bounds["width"], window["x"] + window["width"]) <= max(bounds["x"], window["x"])
                or min(bounds["y"] + bounds["height"], window["y"] + window["height"]) <= max(bounds["y"], window["y"])):
            # Some native providers return a child-local (3,3) frame despite
            # the Driver's desktop-frame contract. Do not guess an offset.
            geometry = None
            result["geometry_unavailable_reason"] = "outside_observed_window"
    if geometry:
        result["geometry"] = geometry
    ancestors, seen, parent = [], set(), element.get("parent_index")
    for depth in range(1, 21):
        if type(parent) is not int or parent in seen or parent not in by_index:
            break
        seen.add(parent)
        ancestor = by_index[parent]
        if _protected(ancestor):
            break
        name = _context_name(ancestor)
        if name and len(ancestors) < 4:
            ancestors.append({"name": name, "role": ancestor.get("role"), "depth": depth})
        parent = ancestor.get("parent_index")
    if ancestors:
        result["named_ancestors"] = ancestors
    if element.get("rendered_parent_gap") is True:
        result["rendered_parent_path_incomplete"] = True
    # Only explicit references to a label in THIS observation are reported as
    # provider relationships. Text similarity or an arbitrary descriptor is not
    # a LabeledBy relationship. Unknown provider formats remain unreported.
    associated, associated_indices = [], set()
    for key in ("labeled_by", "labelled_by"):
        reference = element.get(key)
        index = reference.get("element_index") if isinstance(reference, dict) else reference
        label = by_index.get(index) if type(index) is int else None
        if (label is not None and index not in associated_indices and label is not element and _context_name(label)
                and label.get("role") in {"Text", "Label", "StaticText"}
                and label.get("source") != "driver.tree_markdown"):
            associated_indices.add(index)
            associated.append({"name": _context_name(label), "role": label["role"],
                               "source": "driver." + key, "inferred": False})
    if associated:
        result["label_associations"] = associated
        result["label_association_ambiguous"] = len(associated) > 1
    rendered = [label for label in labels.get(element.get("parent_index"), [])
                if label.get("source") == "driver.tree_markdown"]
    if rendered:
        result.update(rendered_sibling_labels=[{"name": _context_name(label), "inferred": True,
                          "evidence": "same_parent_in_rendered_tree_only"} for label in rendered[:8]],
                      rendered_sibling_label_count=len(rendered), rendered_sibling_labels_ambiguous=len(rendered) > 1,
                      rendered_sibling_labels_truncated=len(rendered) > 8)
        preceding = [label for label in rendered if label.get("following_element_index") == element.get("element_index")]
        if preceding:
            result.update(preceding_text_candidates=[{"name": _context_name(label), "inferred": True,
                              "evidence": "preceding_text_in_rendered_tree_only"} for label in preceding[:8]],
                          preceding_text_candidate_count=len(preceding), preceding_text_ambiguous=len(preceding) > 1)
    # Nearby siblings are hints for visual cross-checking, not new selectors.
    # Keep multiple plausible labels rather than selecting a nearest winner.
    nearby = []
    parent = element.get("parent_index")
    if geometry and type(parent) is int and parent in by_index:
        target = geometry["bounds"]
        for label in labels.get(parent, []):
            rectangle = _observed_rectangle(label)
            if rectangle is None:
                continue
            bounds = rectangle["bounds"]
            vertical = min(bounds["y"] + bounds["height"], target["y"] + target["height"]) - max(bounds["y"], target["y"])
            horizontal = min(bounds["x"] + bounds["width"], target["x"] + target["width"]) - max(bounds["x"], target["x"])
            dx, dy = target["x"] - bounds["x"] - bounds["width"], target["y"] - bounds["y"] - bounds["height"]
            position = ("left" if 0 <= dx <= max(target["width"], bounds["width"])
                        and vertical >= min(target["height"], bounds["height"]) / 2 else
                        "above" if 0 <= dy <= 2 * max(target["height"], bounds["height"])
                        and horizontal >= min(target["width"], bounds["width"]) / 2 else None)
            if position:
                nearby.append({"name": _context_name(label), "role": label["role"], "bounds": bounds,
                               "relative_position": position, "inferred": True,
                               "evidence": "same_parent_desktop_geometry"})
    if nearby:
        result.update(nearby_labels=nearby[:8], nearby_label_candidate_count=len(nearby),
                      nearby_labels_ambiguous=len(nearby) > 1, nearby_labels_truncated=len(nearby) > 8)
    return result if len(result) > 2 else None


def control_display_label(snapshot, element, default):
    """Describe an unnamed/duplicate input without changing its identity.

    This consumes only the existing observation. Inferred text is marked as
    such and must never be copied into a selector or a completion condition.
    """
    if _protected(element) or element.get("role") not in {"Edit", "TextBox", "ComboBox"}:
        return default
    elements = _elements(snapshot)
    if not any(item is element for item in elements):
        return default
    name = _name(element)
    if name and sum(_name(item) == name for item in elements) == 1:
        return default
    rendered = snapshot.get("read_only_labels", [])
    if (snapshot.get("accessibility_normalization", {}).get("status") not in {"augmented", "verified"}
            or not isinstance(rendered, list)):
        rendered = []
    rendered = [row for row in rendered if isinstance(row, dict) and row.get("read_only") is True
                and row.get("source") == "driver.tree_markdown"]
    by_index, labels = _context_index(elements + rendered)
    current, seen = element, set()
    for _ in range(21):
        if _protected(current):
            return default
        parent = current.get("parent_index")
        if parent is None:
            break
        if type(parent) is not int or parent in seen or parent not in by_index:
            return default
        seen.add(parent)
        current = by_index[parent]
    else:
        return default
    context = _read_only_context(element, by_index, labels, snapshot.get("window_bounds")) or {}
    channels = [("label_associations", "label_association_ambiguous"),
                ("preceding_text_candidates", "preceding_text_ambiguous"),
                ("nearby_labels", "nearby_labels_ambiguous")]
    candidates = []
    for key, ambiguous in channels:
        rows = context.get(key, [])
        if context.get(ambiguous) or len(rows) > 1:
            return default
        candidates.extend(rows)
    names = {row["name"] for row in candidates if isinstance(row.get("name"), str)}
    if len(names) != 1:
        return default
    label = names.pop()
    if label == element.get("value"):
        return default
    ancestors = context.get("named_ancestors", [])
    # Keep the field visible in narrow editor columns. Provider-inherited Pane
    # or Window names often contain a whole screen description, not a group.
    group = next((row["name"] for row in ancestors if row.get("role") == "Group"
                  and isinstance(row.get("name"), str) and 0 < len(row["name"]) <= 40
                  and not any(character in row["name"] for character in "\r\n\t")), None)
    source = "주변 글자 기준" if any(row.get("inferred") is True for row in candidates) else "연결된 라벨"
    return label + " (" + source + ")" + (" · " + group if group else "")


_DIAGNOSTIC_PATTERNS = ("Invoke", "Value", "RangeValue", "Text", "Text2", "Toggle", "Selection",
    "SelectionItem", "ExpandCollapse", "Grid", "GridItem", "Table", "TableItem", "ItemContainer",
    "VirtualizedItem", "Scroll", "ScrollItem", "LegacyIAccessible", "Window", "Transform")
_DIAGNOSTIC_METADATA = {
    "framework_id": ("framework_id", "frameworkId", "FrameworkId"),
    "class_name": ("class_name", "className", "ClassName"),
    "provider_description": ("provider_description", "providerDescription", "ProviderDescription"),
    "native_window_handle": ("native_window_handle", "nativeWindowHandle", "NativeWindowHandle"),
    "runtime_id": ("runtime_id", "runtimeId", "RuntimeId"),
}


def _pattern_diagnostics(element):
    """Unknown is distinct from unsupported; Driver actions are not UIA patterns."""
    result = {name: {"status": "unknown", "source": "not_reported"} for name in _DIAGNOSTIC_PATTERNS}
    def canonical(name):
        if not isinstance(name, str):
            return None
        compact = name.removesuffix("Pattern").replace("_", "").casefold()
        return next((item for item in result if item.casefold() == compact), None)
    for field in ("supported_patterns", "unsupported_patterns"):
        rows = element.get(field)
        if not isinstance(rows, list):
            continue
        for name in rows:
            key = canonical(name)
            if key is not None:
                result[key] = {"status": "supported" if field == "supported_patterns" else "unsupported",
                               "source": "driver." + field}
    for field in ("patterns", "pattern_support"):
        rows = element.get(field)
        if not isinstance(rows, dict):
            continue
        for name, value in rows.items():
            key = canonical(name)
            if key is None:
                continue
            status = ("supported" if value is True else "unsupported" if value is False else
                      value if isinstance(value, str) and value in {"supported", "unsupported", "unknown"} else
                      value.get("status") if isinstance(value, dict) else None)
            if not isinstance(status, str) or status not in {"supported", "unsupported", "unknown"}:
                status = "unknown"
            entry = {"status": status, "source": "driver." + field}
            if isinstance(value, dict):
                for error_field in ("hresult", "timeout", "error_code"):
                    error = value.get(error_field)
                    if type(error) in (int, bool) or isinstance(error, str) and len(error) <= 160:
                        entry[error_field] = error
            result[key] = entry
    return result


def _diagnostic_node(element, ordinal, reason):
    protected = _protected(element)
    node = {"observation_ordinal": ordinal, "source": "driver.elements",
            "projection": reason, "read_only": True, "authorizes_input": False,
            "identifiers_lifetime": "this_observation_only", "protected": protected}
    if element.get("synthetic_ancestor") is True:
        node["source"] = "driver.tree_markdown_synthetic_ancestor"
    for key in ("element_index", "parent_index", "depth", "role", "enabled", "offscreen", "is_offscreen"):
        value = element.get(key)
        if type(value) in (int, bool) or isinstance(value, str) and len(value) <= 256:
            node[key] = value
    # Password metadata can itself contain user text; do not serialize a whole
    # protected node or raw provider exception simply because this is diagnostic.
    if protected:
        node["metadata_status"] = "redacted_protected"
        node["patterns"] = {name: {"status": "unknown", "source": "redacted_protected"}
                            for name in _DIAGNOSTIC_PATTERNS}
        return node
    for key, value in (("name", _name(element)), ("value", element.get("value")),
                       ("automation_id", element.get("automation_id"))):
        if type(value) in (bool, int, float) and (type(value) is not float or math.isfinite(value)):
            node[key] = value
        elif isinstance(value, str):
            node[key] = value[:2000]
            if len(value) > 2000:
                node[key + "_truncated"] = True
    metadata, missing = {}, []
    for name, aliases in _DIAGNOSTIC_METADATA.items():
        field = next((key for key in aliases if key in element), None)
        value = element.get(field) if field is not None else None
        valid = (isinstance(value, str) and len(value) <= 2000 or type(value) is int
                 or name == "runtime_id" and isinstance(value, list) and len(value) <= 64
                 and all(type(part) is int for part in value))
        if valid:
            metadata[name] = {"value": copy.deepcopy(value), "source": "driver." + field}
        else:
            missing.append(name)
    node.update(metadata=metadata, metadata_unreported=missing, patterns=_pattern_diagnostics(element))
    node["actions"] = [action for action in element.get("actions", [])
                       if isinstance(action, str) and len(action) <= 80] if isinstance(element.get("actions"), list) else []
    node["actions_are_pattern_support"] = False
    return node


def projection_diagnostics(snapshot, inspection, *, original_snapshot=None, max_nodes=5000):
    """Compare observable projection boundaries without claiming native UIA access.

    The optional native diagnostic helper is an independent observation. It can
    be joined by the caller, but node counts alone cannot prove identical state
    or attribute a missing Provider child to the Driver.
    """
    from accessibility_tree import projection_evidence
    if type(max_nodes) is not int or not 1 <= max_nodes <= 5000:
        raise OperationError("진단 요소 수는 1~5000 범위여야 합니다.")
    original = original_snapshot if isinstance(original_snapshot, dict) else snapshot
    evidence = projection_evidence(original)
    rows = snapshot.get("elements", [])
    rows = rows if isinstance(rows, list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    filters = inspection.get("filters", {})
    within, search = filters.get("within"), str(filters.get("search", "")).casefold().strip()
    scope = _unique(snapshot, within) if within is not None else None
    parents = {row.get("element_index"): row for row in rows if type(row.get("element_index")) is int}
    offset = inspection.get("offset", 0)
    page_size = len(inspection.get("controls", []))
    scoped = inspection.get("observation_scope", {}).get("kind") == "native_selected_subtree"
    candidates, records, reasons = 0, [], {}
    for ordinal, row in enumerate(rows):
        actions = row.get("observed_patterns" if scoped else "actions", [])
        actions = actions if isinstance(actions, list) else []
        if not (_base_selector(row) or actions):
            reason = "no_identity_or_actions"
        elif scope is not None and not _descendant(row, scope, rows, parents):
            reason = "outside_requested_scope"
        elif filters.get("actionable_only") and not actions:
            reason = "actionable_filter"
        else:
            searchable = str(row.get("role", "")) if _protected(row) else " ".join(
                str(value) for value in (_name(row) or "", row.get("role", ""), row.get("automation_id", "")))
            if search and search not in searchable.casefold():
                reason = "search_filter"
            else:
                candidates += 1
                reason = ("before_page_offset" if candidates <= offset else "after_page_limit"
                          if candidates > offset + page_size else "returned_control")
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(records) < max_nodes:
            records.append(_diagnostic_node(row, ordinal, reason))
    flags = {key: snapshot[key] for key in ("truncated", "max_depth_reached", "max_elements_reached")
             if type(snapshot.get(key)) is bool}
    nested_error = snapshot.get("diagnostic", snapshot.get("error", {}))
    nested_error = nested_error if isinstance(nested_error, dict) else {}
    code = snapshot.get("diagnostic_code", snapshot.get("error_code", snapshot.get("code", nested_error.get("code"))))
    code = code if isinstance(code, str) and len(code) <= 160 else None
    status = ("timeout" if code and "timeout" in code else "provider_error" if code and "provider" in code
              else "observation_error" if code else "truncated" if any(flags.values())
              else evidence["elements"]["status"] if evidence["elements"]["status"] in {"empty", "missing", "invalid"}
              else "observed")
    counts = {"provider_original": None, "driver_elements": evidence["elements"]["count"],
              "tree_markdown_rows": evidence["tree_markdown"]["count"], "normalized_elements": len(rows),
              "mcp_controls_returned": page_size, "mcp_controls_filtered": len(rows) - reasons.get("returned_control", 0)}
    report = {"format": "computer-accessibility-projection/v1", "read_only": True, "authorizes_input": False,
              "status": status, "backend": "driver_projection", "counts": counts,
              "provider_original": {"status": "not_observed", "count": None,
                                    "reason": "independent_native_probe_required"},
              "driver_elements": evidence["elements"], "tree_markdown": evidence["tree_markdown"],
              "normalization": copy.deepcopy(snapshot.get("accessibility_normalization", {"status": "not_reported"})),
              "filter_reasons": reasons, "nodes": records, "diagnostic_nodes_omitted": max(0, len(rows) - len(records)),
              "traversal_flags": flags, "completeness": "limited" if any(flags.values())
                  or inspection.get("traversal_may_be_limited") else "unknown",
              "view_membership": {"raw": "unknown", "control": "unknown", "content": "unknown"},
              "scope": copy.deepcopy(inspection.get("observation_scope", {})),
              "input_dispatched": False, "task_verified": False}
    if code is not None:
        report["diagnostic_code"] = code
    return report


def _unmodified_payload(answer):
    structured = answer.get("structuredContent")
    if isinstance(structured, dict) and "elements" in structured:
        return structured
    for item in answer.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and "elements" in parsed:
                return {**parsed, **(structured if isinstance(structured, dict) else {})}
    return structured if isinstance(structured, dict) else {}


def inspect_window(runtime, target, *, max_controls=80, max_depth=12, max_elements=600,
                   search="", within=None, offset=0, actionable_only=False, observation="auto", requested_goal="",
                   diagnostics=False, host_executable=None):
    if type(diagnostics) is not bool:
        raise OperationError("diagnostics는 true 또는 false여야 합니다.")
    diagnostic_started = time.perf_counter() if diagnostics else None
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
        if diagnostics:
            answer = copy.deepcopy(answer)
            raw = _unmodified_payload(answer)
            details = projection_diagnostics(raw, {}, original_snapshot=raw)
            details.update(observed_at=time.time(), elapsed_ms=round((time.perf_counter()-diagnostic_started)*1000, 3))
            answer["structuredContent"] = {**raw, "accessibility_diagnostics": details}
            answer["content"] = [{"type": "text", "text": json.dumps(answer["structuredContent"], ensure_ascii=False)}]
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
        # Internal identity from InteractionEngine's live binding, not a public
        # app-name option. Reuse this snapshot; no diagnostic traversal is added.
        from smartfx_profiles import host_profile, ObservedStrategy
        profile = host_profile(host_executable)
        strategy = ObservedStrategy(profile, snapshot) if profile and elements else None
        controls = []
        targetability = {"scope": "returned_controls", "actionable_controls": 0,
                         "unique_actionable_controls": 0, "ambiguous_actionable_controls": 0,
                         "unidentified_actionable_controls": 0, "incomplete_scope_controls": 0,
                         "protected_controls": 0}
        # Normalization keeps rendered Text leaves outside the actionable
        # projection. They may enrich prose context, never selectors/targets.
        rendered_labels = snapshot.get("read_only_labels", [])
        if (snapshot.get("accessibility_normalization", {}).get("status") not in {"augmented", "verified"}
                or not isinstance(rendered_labels, list)):
            rendered_labels = []
        rendered_labels = [item for item in rendered_labels if isinstance(item, dict)
                           and item.get("read_only") is True and item.get("source") == "driver.tree_markdown"]
        context_index, sibling_labels = _context_index(elements + rendered_labels)
        titlebars = [e for e in elements if e.get("role") == "TitleBar"]
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
            protected = _protected(element)
            searchable = (str(element.get("role", "")) if protected else
                          " ".join(str(value) for value in (_name(element) or "", element.get("role", ""), element.get("automation_id", ""))))
            if query and query not in searchable.casefold():
                continue
            candidate_count += 1
            if candidate_count <= offset:
                continue
            if len(controls) >= max_controls:
                continue
            fields = ("role", "enabled") if protected else ("element_index", "parent_index", "role", "value", "selected", "enabled", "read_only", "automation_id")
            item = {k: copy.deepcopy(element[k]) for k in fields if k in element}
            if not protected and _name(element) is not None:
                item["name"] = _name(element)
            item["actions"] = [] if protected else [a for a in actions if isinstance(a, str)]
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
                item["selector"] = (strategy.selector(element) if strategy else None) or control_selector(snapshot, element)
            if strategy:
                application_context = strategy.context(element)
                if application_context:
                    item["application_context"] = application_context
            item["selector_unique"] = item["selector"] is not None
            item["suggested_operations"] = suggested_operations({**element, "actions": actions}) if item["selector_unique"] else []
            item["targeting_state"] = ("protected" if protected else "scope_incomplete" if scoped and snapshot.get("truncated") else
                                       "unique" if item["selector_unique"] else "unidentified" if not _base_selector(element) else "ambiguous")
            if protected:
                item["protected"] = True
                targetability["protected_controls"] += 1
            else:
                context = _read_only_context(element, context_index, sibling_labels, snapshot.get("window_bounds"))
                if context:
                    item["context"] = context
                if (set(suggested_operations({**element, "actions": actions})) - {"assert"}
                        and element.get("enabled") is not False and element.get("offscreen") is not True
                        and element.get("is_offscreen") is not True
                        and not any(_descendant(element, bar, elements, context_index) for bar in titlebars)):
                    targetability["actionable_controls"] += 1
                    key = {"unique": "unique_actionable_controls", "ambiguous": "ambiguous_actionable_controls",
                           "unidentified": "unidentified_actionable_controls", "scope_incomplete": "incomplete_scope_controls"}[item["targeting_state"]]
                    targetability[key] += 1
            item["capability_evidence"] = "native_read_only_uia_properties_and_patterns" if scoped else "observed_uia_properties_and_patterns"
            item["execution_verified"] = False
            for key in ("name", "value", "automation_id"):
                if isinstance(item.get(key), str) and len(item[key]) > 16000:
                    item[key + "_truncated"] = True
                    item[key] = item[key][:16000]
            controls.append(item)
        unresolved = targetability["actionable_controls"] - targetability["unique_actionable_controls"]
        targetability["state"] = ("needs_disambiguation" if unresolved and not targetability["unique_actionable_controls"] else
                                  "partially_targetable" if unresolved else "uniquely_targetable" if targetability["unique_actionable_controls"] else "no_actionable_controls")
        inspection.update(status="uia_observation" if controls else "no_accessible_controls",
                          targetability=targetability,
                          context_contract={"read_only": True, "authorizes_input": False,
                                            "nearby_labels_are_inferred": True, "rendered_label_order_is_inferred": True,
                                            "image_coordinate_mapping": "not_inferred"},
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
                          next_step="computer_observe의 고유 target_ref와 완료 조건으로 computer_act를 요청하세요. 같은 이름은 고유 within 영역으로 좁히고, 직접 선택·녹화·저장은 computer_process_editor에서 진행하세요. 주변 라벨과 좌표는 읽기 전용 문맥이며 선택자나 클릭 권한이 아닙니다." if controls else
                                    "이 창에서 UIA 조작 대상을 확인하지 못했습니다. computer_observe로 현재 창의 이미지를 읽고, 직접 요소 선택·녹화·저장은 computer_process_editor에서 진행하세요. 입력 방식과 권한은 바뀌지 않습니다.")
        if unresolved:
            inspection["next_step"] = ("일부 조작 요소를 고유하게 구분하지 못했습니다. 같은 이름의 첫 항목을 임의로 선택하지 마세요. "
                "computer_observe의 현재 이미지와 읽기 전용 라벨 문맥을 함께 확인하고, computer_act에는 고유 target_ref 또는 "
                "관찰한 image_region과 구별할 라벨이 포함된 scope_region을 사용하세요. 직접 선택·녹화·저장은 computer_process_editor에서 진행하세요.")
        if profile:
            inspection["application_strategy"] = strategy.summary() if strategy else {
                **profile, "observed_regions": {}, "structure_status": "not_exposed", "extra_tree_reads": 0}
    if scope_fallback:
        inspection["scope_fallback"] = scope_fallback
    images = [copy.deepcopy(c) for c in answer.get("content", [])
              if isinstance(c, dict) and c.get("type") == "image"] if not uia else []
    weak = False
    if uia:
        chrome = [e for e in elements if e.get("role") == "TitleBar"]
        ancestry = {e.get("element_index"): e for e in elements if type(e.get("element_index")) is int}
        actionable = [e for e in elements if not _protected(e)
                      and not any(_descendant(e, bar, elements, ancestry) for bar in chrome)
                      and e.get("role") in {"Button", "Edit", "Document", "TextBox", "ComboBox", "CheckBox",
                                           "ToggleButton", "ListItem", "TabItem", "TreeItem", "RadioButton"}
                      and e.get("observed_patterns" if scoped else "actions")]
        unresolved_only = (targetability["state"] == "needs_disambiguation")
        weak = not actionable or unresolved_only
        inspection["uia_evidence"] = ("actionable_controls_need_disambiguation" if actionable and unresolved_only else
                                      "actionable_controls_observed" if actionable else
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
                                       "computer_act에는 고유 target_ref 또는 현재 이미지의 image_region과 구별할 라벨을 포함한 scope_region을 사용하세요. "
                                       "직접 선택·녹화·저장은 computer_process_editor에서 진행하세요. 요소의 desktop_physical 좌표를 이미지 좌표로 간주하지 마세요. "
                                       "추정 라벨 문맥은 선택자나 클릭 권한이 아니며, 이미지 자체는 작업 성공을 뜻하지 않습니다.")
        elif not uia:
            inspection["status"] = "missing_image"
        if capture_failed:
            inspection["next_step"] = image_state.get("next_step", "이 창의 화면 이미지가 반환되지 않았습니다. 입력 전에 화면을 다시 확인하세요.")
    inspection["observed_modalities"] = (["uia"] if uia else []) + (["visual"] if images else [])
    if diagnostics:
        details = projection_diagnostics(snapshot, inspection, original_snapshot=_unmodified_payload(answer))
        details.update(observed_at=time.time(), elapsed_ms=round((time.perf_counter()-diagnostic_started)*1000, 3),
                       target=dict(target), observation_backend="native_scoped_uia" if scoped else "driver")
        inspection["diagnostics"] = details
    data = {**target, "inspection": inspection}
    for key in ("window_title", "snapshot_id", "computer_use_metrics", "screenshot_width", "screenshot_height", "window_bounds", "capture_coverage", "accessibility_normalization"):
        if key in snapshot:
            data[key] = snapshot[key]
    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    content.extend(images)
    return {"isError": bool(capture_failed and not uia), "structuredContent": data, "content": content}
