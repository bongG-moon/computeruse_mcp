"""Bounded scalar image evidence for the local result form, never MCP delivery.

This is a projection, not a recursive redactor. Pixels, application content,
paths, target coordinates and arbitrary error messages are never copied.
"""
from __future__ import annotations

import math


_REASONS = frozenset(("accepted", "shape_mismatch", "colored_ink_changed", "missing_ink",
                     "insufficient_ink", "selection_context_missing", "ink_metrics_changed"))
_SCOPES = frozenset(("whole_template", "clicked_band"))
_PIXELS = 4096 * 4096


def _number(value, lower, upper):
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


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
    return {"image_comparison": comparison} if comparison else {}
