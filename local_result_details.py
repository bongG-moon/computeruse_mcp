"""Bounded scalar recognition evidence for the local form, never MCP delivery.

This is a projection, not a recursive redactor. Pixels, application content,
paths, target coordinates and arbitrary error messages are never copied.
"""
from __future__ import annotations

import math


_REASONS = frozenset(("accepted", "shape_mismatch", "colored_ink_changed", "missing_ink",
                     "insufficient_ink", "selection_context_missing", "ink_metrics_changed"))
_SCOPES = frozenset(("whole_template", "clicked_band"))
_PIXELS = 4096 * 4096
_MENU_SOURCE_OUTCOMES = frozenset(("source_incomplete", "invalid_completeness", "invalid_elements",
    "invalid_identity", "path_not_found", "ambiguous", "hidden_or_visibility_unknown", "disabled",
    "enabled_unknown", "bounds_unavailable", "invalid_capabilities", "parent_capability_unknown",
    "parent_capability_unavailable", "command_is_parent", "leaf_capability_unknown", "evidence_unavailable", "matched"))
_MENU_OUTCOMES = frozenset(("matched", "invalid_menu_target", "menu_observation_invalid",
    "menu_observation_incomplete", "menu_item_not_found", "menu_item_ambiguous", "menu_item_hidden",
    "menu_item_disabled", "menu_item_bounds_unavailable", "menu_parent_capability_unavailable",
    "menu_leaf_capability_unavailable", "menu_command_not_leaf", "menu_evidence_unavailable"))
_GRID_REASONS = frozenset(("invalid_grid_intent", "grid_observation_incomplete", "grid_target_unavailable",
    "grid_state_unavailable", "grid_cell_state_unavailable", "grid_not_ready", "grid_active_sheet_unavailable",
    "grid_active_sheet_mismatch", "grid_view_state_unavailable", "grid_selection_unavailable",
    "grid_selection_ambiguous", "grid_selection_scope_mismatch", "grid_empty_forbidden", "grid_empty_no_data_cell",
    "grid_data_cell_unavailable", "grid_query_freshness_unverified", "grid_context_unavailable",
    "grid_context_changed", "grid_selection_changed", "grid_driver_scope_mismatch",
    "grid_driver_cell_unavailable", "grid_input_busy", "grid_observation_timeout", "grid_window_changed",
    "grid_observation_changed", "grid_cell_changed", "grid_run_context_required", "grid_context_save_failed",
    "grid_menu_missing", "grid_menu_ready", "grid_export_ambiguous", "grid_input_changed",
    "grid_menu_already_open", "grid_menu_baseline_unavailable", "grid_menu_episode_changed",
    "grid_menu_episode_save_failed", "grid_menu_identity_unavailable", "grid_export_contract_unsupported",
    "grid_source_continuity_unverified"))


def _number(value, lower, upper):
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


def project_menu_recognition(value):
    """Fixed technical vocabulary, no menu labels or native evidence aliases."""
    if not isinstance(value, dict) or value.get("method") != "menu_semantic":
        return {}
    clean = {"method": "menu_semantic"}
    enums = {"purpose": {"presence", "parent", "command"},
             "selected_source": {"uia_raw", "direct_msaa"}, "outcome": _MENU_OUTCOMES,
             "fallback_reason": _MENU_SOURCE_OUTCOMES,
             "image_fallback": {"not_attempted"}, "local_ocr": {"not_implemented"}}
    for key, allowed in enums.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            clean[key] = value[key]
    sources = value.get("sources")
    if isinstance(sources, dict):
        collected = {}
        for name in ("uia_raw", "direct_msaa"):
            source = sources.get(name)
            if not isinstance(source, dict):
                continue
            fields = {}
            for key, allowed in (("status", {"ok", "empty", "partial", "error", "unavailable", "timeout", "unknown"}),
                                 ("outcome", _MENU_SOURCE_OUTCOMES)):
                if isinstance(source.get(key), str) and source[key] in allowed:
                    fields[key] = source[key]
            for key in ("complete", "truncated"):
                if type(source.get(key)) is bool:
                    fields[key] = source[key]
            for key in ("menu_items", "path_candidates", "visible_candidates"):
                if type(source.get(key)) is int and 0 <= source[key] <= 500:
                    fields[key] = source[key]
            if fields:
                collected[name] = fields
        if collected:
            clean["sources"] = collected
    return clean


def project_grid_recognition(value):
    """Grid identity/selection/query proofs without cells, labels or ranges."""
    if not isinstance(value, dict) or value.get("recognition") != "grid_semantic":
        return {}
    clean = {"recognition": "grid_semantic"}
    for key in ("grid_context_verified", "query_verified", "data_values_compared", "business_result_verified"):
        if type(value.get(key)) is bool:
            clean[key] = value[key]
    context = value.get("context") if isinstance(value.get("context"), dict) else value.get("grid_context")
    context = context if isinstance(context, dict) else {}
    scope = value.get("export_scope", context.get("export_scope"))
    if scope in ("whole_active_sheet", "selected_ranges"):
        clean["export_scope"] = scope
    reason = value.get("fallback_reason")
    if isinstance(reason, str) and reason in _GRID_REASONS:
        clean["fallback_reason"] = reason
    diagnostic = value.get("diagnostic")
    if isinstance(diagnostic, dict) and isinstance(diagnostic.get("code"), str) and diagnostic["code"] in _GRID_REASONS:
        clean["outcome"] = diagnostic["code"]
    return clean


def _project_recognitions(raw):
    # Only known result wrappers, bounded independently from image projection.
    # Input/store/context/element trees are never traversed.
    output, seen = {}, set()
    def visit(value, depth=0):
        if not isinstance(value, dict) or depth > 8 or len(seen) >= 48 or id(value) in seen:
            return
        seen.add(id(value))
        visit(value.get("last_result"), depth+1)
        if "menu_recognition" not in output:
            menu = project_menu_recognition(value.get("menu_recognition"))
            if menu:
                output["menu_recognition"] = menu
        if "grid_recognition" not in output:
            grid = project_grid_recognition(value)
            if grid:
                output["grid_recognition"] = grid
        if len(output) == 2:
            return
        for key in ("diagnostic", "native_delivery", "menu_expansion", "observation", "cause"):
            visit(value.get(key), depth+1)
    visit(raw)
    return output


def local_recognition_summary(projected):
    """Render the fixed projection into the existing local result text field."""
    lines = []
    menu = project_menu_recognition(projected.get("menu_recognition"))
    grid = project_grid_recognition(projected.get("grid_recognition"))
    if menu:
        labels = {"uia_raw": "UIA", "direct_msaa": "MSAA"}
        lines.append("메뉴 인식: 현재 이름과 부모 관계 / 출처 " + labels.get(menu.get("selected_source"), "미확인"))
        if menu.get("outcome"):
            lines.append("메뉴 확인: " + menu["outcome"])
        if menu.get("fallback_reason"):
            lines.append("UIA 대신 다른 출처를 확인한 이유: " + menu["fallback_reason"])
        for name, item in menu.get("sources", {}).items():
            state = item.get("outcome", item.get("status", "unknown"))
            lines.append(labels[name] + ": " + state + " / 경로 후보 " + str(item.get("path_candidates", "미확인")))
        if menu.get("local_ocr") == "not_implemented" and menu.get("outcome") != "matched":
            lines.append("접근성 정보가 부족해도 OCR로 추측하지 않습니다. 이 버전에는 메뉴 OCR 경로가 없습니다.")
    if grid:
        scope = {"whole_active_sheet": "활성 시트 전체", "selected_ranges": "선택 범위"}.get(grid.get("export_scope"), "미확인")
        lines.append("표 인식: 현재 표와 업무 의미 상태 / 내보내기 범위 " + scope)
        for key, label in (("query_verified", "이번 조회 완료"), ("grid_context_verified", "표와 선택 상태")):
            if key in grid:
                lines.append(label + ": " + ("확인됨" if grid[key] else "미확인"))
        if grid.get("data_values_compared") is False:
            lines.append("결과 셀 값과 녹화 화면의 동일성은 비교하지 않았습니다.")
        if grid.get("fallback_reason") or grid.get("outcome"):
            lines.append("표 확인 사유: " + grid.get("fallback_reason", grid.get("outcome", "")))
        lines.append("표 또는 메뉴 확인은 출력 파일 생성 완료를 뜻하지 않습니다.")
    return "\n".join(lines)[:1800]


def project_appearance_diagnostic(value):
    """Validate a fixed schema without retaining unknown fields or text."""
    if not isinstance(value, dict) or type(value.get("accepted")) is not bool:
        return {}
    if (not isinstance(value.get("reason"), str) or value["reason"] not in _REASONS
            or not isinstance(value.get("scope"), str) or value["scope"] not in _SCOPES
            or value["accepted"] != (value["reason"] == "accepted")):
        return {}
    clean = {key: value[key] for key in ("accepted", "reason", "scope")}
    for key in ("changed_color_pixels", "missing_ink_pixels"):
        if type(value.get(key)) is int and 0 <= value[key] <= _PIXELS:
            clean[key] = value[key]
    for key in ("clicked_dark_selection", "clicked_light_selection"):
        if type(value.get(key)) is bool:
            clean[key] = value[key]
    for scope in ("whole_template", "clicked_band"):
        metrics = value.get(scope)
        if not isinstance(metrics, dict):
            continue
        safe = {}
        for key in ("ink_pixels", "pattern_ink_pixels", "current_ink_pixels", "minimum_ink_pixels"):
            if type(metrics.get(key)) is int and 0 <= metrics[key] <= _PIXELS:
                safe[key] = metrics[key]
        for key, low, high in (("dice", 0, 1), ("minimum_dice", 0, 1),
                ("mean_difference", 0, 255), ("maximum_mean_difference", 0, 255),
                ("correlation", -1, 1), ("contrast_difference", 0, 255),
                ("maximum_contrast_difference", 0, 255)):
            if _number(metrics.get(key), low, high):
                safe[key] = round(metrics[key], 6)
        for key in ("accepted", "selection_tolerance_used"):
            if type(metrics.get(key)) is bool:
                safe[key] = metrics[key]
        if isinstance(metrics.get("reason"), str) and metrics["reason"] in _REASONS:
            safe["reason"] = metrics["reason"]
        if safe:
            clean[scope] = safe
    return clean


def _project_match(value):
    if not isinstance(value, dict):
        return {}
    clean = {}
    enums = {
        "status": {"matched", "not_found", "ambiguous"},
        "code": {"image_not_found", "image_ambiguous", "image_template_low_detail"},
        "backend": {"native-rgb", "airtest-template-rgb", "airtest-light-background", "airtest-list-selection"},
        "appearance_rejected": {"foreground_ink_changed"},
        "search_diagnostic": {"candidate_budget_exceeded"},
        "backend_fallback": {"image_backend_unavailable", "image_backend_failed"},
    }
    for key, allowed in enums.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            clean[key] = value[key]
    for key in ("score", "second_score", "threshold", "ambiguity_margin", "score_gap", "original_score", "appearance_score"):
        if _number(value.get(key), 0, 1):
            clean[key] = round(value[key], 6)
    for key in ("candidate_count", "candidate_evaluations", "candidate_budget"):
        if type(value.get(key)) is int and 0 <= value[key] <= 100000:
            clean[key] = value[key]
    for key in ("appearance_verified", "search_complete", "search_reused", "low_detail"):
        if type(value.get(key)) is bool:
            clean[key] = value[key]
    if _number(value.get("search_ms"), 0, 3600000):
        clean["search_ms"] = round(value["search_ms"], 2)
    for key in ("capture_window", "screenshot"):
        size = value.get(key)
        if (isinstance(size, dict) and all(type(size.get(k)) is int and 1 <= size[k] <= 16384
                                        for k in ("width", "height"))):
            clean[key] = {k: size[k] for k in ("width", "height")}
    appearance = project_appearance_diagnostic(value.get("appearance_diagnostic"))
    if appearance:
        clean["appearance_diagnostic"] = appearance
    # A status by itself may be an operation result, rather than image evidence.
    if not any(key in clean for key in ("score", "threshold", "appearance_diagnostic", "appearance_rejected", "backend")):
        return {}
    return clean


def project_local_diagnostics(raw):
    """Return at most one image comparison from fixed, bounded result paths.

    Latest wait evidence and explicit image wrappers take precedence. Parent
    and child wrappers carry stage provenance without copying the menu path.
    """
    visits = 0
    seen = set()
    def visit(value, source="direct_match", stage="image_comparison", depth=0):
        nonlocal visits
        if not isinstance(value, dict) or depth > 8 or visits >= 48 or id(value) in seen:
            return None
        visits += 1
        seen.add(id(value))
        menu = value.get("menu_expansion")
        menu_stage = menu.get("stage") if isinstance(menu, dict) else None
        if menu_stage == "parent_observation":
            stage = "parent_precondition"
        elif menu_stage in ("child_observation", "child_visible"):
            stage = "child_observation"
        wrappers = (("child_image_match", "parent_image_match") if stage == "child_observation"
                    else ("parent_image_match", "child_image_match"))
        for key in ("last_match", "image_match", *wrappers,
                    "diagnostic", "last_result", "menu_expansion", "observation", "cause"):
            child_source = key if key in {"last_match", "image_match", "parent_image_match", "child_image_match"} else source
            # An inner image_match is the payload of a parent/child wrapper.
            if source in {"parent_image_match", "child_image_match"}:
                child_source = source
            child_stage = ("parent_precondition" if key == "parent_image_match" else
                           "child_observation" if key == "child_image_match" else stage)
            found = visit(value.get(key), child_source, child_stage, depth+1)
            if found:
                return found
        projected = _project_match(value)
        return {"source": source, "stage": stage, **projected} if projected else None
    comparison = visit(raw)
    return {**({"image_comparison": comparison} if comparison else {}), **_project_recognitions(raw)}
