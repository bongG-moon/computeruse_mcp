"""Read-only current-grid export contracts, separate from input authorization.

The native reader supplies actual Grid/GridItem/Selection property values. No
cell contents, old coordinates, runtime IDs or REA implementation field names
are used as durable target identities. Missing provider state is unavailable,
not an empty selection or a successfully completed query.
"""
from __future__ import annotations

import copy
import math

from operations import (OperationError, _descendant, _elements, _unique,
                        validate_assertions, validate_selector)


FORMAT = "computer-grid-intent/v1"
GRID_ROLES = {"DataGrid", "Table", "Custom", "Pane"}
CELL_ROLES = {"Cell", "DataItem", "Custom", "Text", "Edit"}


def _fail(message, code):
    raise OperationError(message, code)


def validate_grid_intent(intent):
    fields = {"format", "grid_selector", "export_scope", "active_sheet",
              "ready_when", "view_conditions", "allow_empty"}
    if not isinstance(intent, dict) or set(intent) != fields or intent.get("format") != FORMAT:
        _fail("표 내보내기의 대상, 활성 시트, 출력 범위와 확인 조건을 명시하세요.", "invalid_grid_intent")
    selector = validate_selector(intent["grid_selector"])
    if (selector.get("role") not in GRID_ROLES or "row" in selector or "label" in selector
            or not (selector.get("automation_id") or selector.get("name"))):
        _fail("내보내기 대상에는 실제 표의 안정적인 식별 기준과 표 역할이 필요합니다.", "invalid_grid_intent")
    if (not isinstance(intent["export_scope"], str)
            or intent["export_scope"] not in {"whole_active_sheet", "selected_ranges"}
            or type(intent["allow_empty"]) is not bool):
        _fail("활성 시트 전체와 선택 범위 내보내기를 구분하세요.", "invalid_grid_intent")
    validate_assertions([intent["active_sheet"]])
    sheet = intent["active_sheet"]
    if (sheet.get("require_change") or sheet["property"] not in {"selected", "name"}
            or sheet["selector"].get("role") != "TabItem"
            or sheet["property"] == "selected" and sheet["equals"] is not True
            or sheet["property"] == "name" and (not isinstance(sheet["equals"], str) or not sheet["equals"].strip())
            or "row" in sheet["selector"]):
        _fail("현재 표에 속한 실제 선택된 시트 탭을 지정하세요. 일반 입력값을 활성 시트로 사용하지 않습니다.", "invalid_grid_intent")
    validate_assertions(intent["ready_when"])
    conditions = intent["view_conditions"]
    if not isinstance(conditions, list) or len(conditions) > 20:
        _fail("숨김 및 필터 상태 확인 조건은 0~20개여야 합니다.", "invalid_grid_intent")
    if conditions:
        validate_assertions(conditions)
    if any(item.get("require_change") for item in [*intent["ready_when"], *conditions]):
        _fail("현재 표 조건에는 정적인 비교를 사용하고 새 조회 전이는 실행 기록으로 확인하세요.", "invalid_grid_intent")
    return copy.deepcopy(intent)


def _real(element):
    return (not element.get("synthetic_ancestor") and element.get("source") != "driver.tree_markdown"
            and element.get("role") != "Password"
            and not any(element.get(key) is True for key in ("protected", "is_password", "is_protected", "password")))


def _bounds(element):
    if element.get("coordinate_space", "desktop_physical") != "desktop_physical":
        return None
    raw = element.get("bounds")
    if raw is None and isinstance(element.get("frame"), dict):
        frame = element["frame"]
        raw = {name: frame.get(key) for name, key in zip(("x", "y", "width", "height"), ("x", "y", "w", "h"))}
    if not isinstance(raw, dict) or set(raw) != {"x", "y", "width", "height"}:
        return None
    if not all(type(v) in (int, float) and math.isfinite(v) for v in raw.values()):
        return None
    if raw["width"] <= 0 or raw["height"] <= 0:
        return None
    return dict(raw)


def _inside(inner, outer):
    return (inner["x"] >= outer["x"] and inner["y"] >= outer["y"]
            and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
            and inner["y"] + inner["height"] <= outer["y"] + outer["height"])


def _condition(snapshot, condition, code):
    try:
        element = _unique(snapshot, condition["selector"])
    except OperationError as error:
        _fail("업무 의미 상태의 조건을 유일하게 읽지 못했습니다: " + error.code, code)
    if not _real(element) or condition["property"] not in element:
        _fail("업무 의미 상태의 실제 속성을 읽지 못했습니다.", code)
    value = element[condition["property"]]
    expected = condition["equals"]
    if type(value) is not type(expected) or value != expected:
        _fail("현재 업무 의미 상태가 지정한 조건과 다릅니다.", code)
    return {"selector": copy.deepcopy(condition["selector"]), "property": condition["property"],
            "value": copy.deepcopy(value)}


def _grid_cells(elements, grid, counts):
    cells = {}
    for element in elements:
        item = element.get("grid_item")
        if (not isinstance(item, dict) or item.get("status") != "supported"
                or item.get("containing_grid") != grid["element_index"]):
            continue
        if (element.get("role") not in CELL_ROLES or not _real(element)
                or not _descendant(element, grid, elements)):
            continue
        coordinates = [item.get(key) for key in ("row", "column", "row_span", "column_span")]
        if (any(type(value) is not int for value in coordinates) or min(coordinates[:2]) < 0
                or min(coordinates[2:]) < 1 or coordinates[0] + coordinates[2] > counts[0]
                or coordinates[1] + coordinates[3] > counts[1]):
            _fail("표 셀의 실제 행과 열 범위가 올바르지 않습니다.", "grid_cell_state_unavailable")
        cells[element["element_index"]] = (element, tuple(coordinates))
    return cells


def inspect_grid_intent(snapshot, intent):
    """Validate current evidence for authoring; this never proves a new query."""
    intent = validate_grid_intent(intent)
    if (not isinstance(snapshot, dict) or any(snapshot.get(key) is True for key in
            ("truncated", "max_depth_reached", "max_elements_reached"))):
        _fail("표 관찰이 불완전하여 출력 범위를 확인할 수 없습니다.", "grid_observation_incomplete")
    elements = _elements(snapshot)
    indices = [element.get("element_index") for element in elements]
    if any(type(index) is not int for index in indices) or len(indices) != len(set(indices)):
        _fail("표 관찰의 요소 관계가 유일하지 않습니다.", "grid_observation_incomplete")
    grid = _unique(snapshot, intent["grid_selector"])
    if (grid.get("role") not in GRID_ROLES or not _real(grid) or grid.get("enabled") is not True
            or grid.get("offscreen") is not False or _bounds(grid) is None):
        _fail("현재 표시되고 사용 가능한 실제 표를 확인하지 못했습니다.", "grid_target_unavailable")
    patterns = grid.get("patterns")
    pattern = patterns.get("Grid") if isinstance(patterns, dict) else None
    if (not isinstance(pattern, dict) or pattern.get("status") != "supported"
            or any(type(pattern.get(key)) is not int or pattern[key] < 0 for key in ("row_count", "column_count"))):
        _fail("표의 실제 행과 열 수를 읽을 수 없습니다.", "grid_state_unavailable")
    counts = (pattern["row_count"], pattern["column_count"])
    ready = [_condition(snapshot, item, "grid_not_ready") for item in intent["ready_when"]]
    sheet = _condition(snapshot, intent["active_sheet"], "grid_active_sheet_unavailable")
    # An ordinary selector cannot attach a sheet from another grid simply
    # because its label matches. The observed provider tree must connect it.
    sheet_element = _unique(snapshot, intent["active_sheet"]["selector"])
    if sheet_element.get("role") != "TabItem" or sheet_element.get("selected") is not True:
        _fail("고정 라벨이나 선택된 데이터 셀은 활성 시트 증거가 아닙니다. 실제 선택된 시트 탭을 지정하세요.",
              "grid_active_sheet_unavailable")
    if sheet_element is not grid and not _descendant(sheet_element, grid, elements):
        _fail("활성 시트 상태가 선택한 표의 계층에 연결되지 않았습니다.", "grid_active_sheet_mismatch")
    view = [_condition(snapshot, item, "grid_view_state_unavailable") for item in intent["view_conditions"]]
    selection = snapshot.get("grid_selection")
    if (not isinstance(selection, dict) or selection.get("status") != "supported"
            or selection.get("complete") is not True or selection.get("grid_element_index") != grid["element_index"]
            or not isinstance(selection.get("selected_items"), list)
            or len(selection["selected_items"]) > 5000
            or any(type(index) is not int for index in selection["selected_items"])
            or len(selection["selected_items"]) != len(set(selection["selected_items"]))):
        _fail("표의 전체 선택 상태를 읽지 못했습니다. 빈 선택으로 가정하지 않습니다.", "grid_selection_unavailable")
    cells = _grid_cells(elements, grid, counts)
    selected = selection["selected_items"]
    if any(index not in cells for index in selected):
        _fail("선택된 항목의 실제 셀 범위가 현재 관찰에 없습니다.", "grid_selection_unavailable")
    ranges = [cells[index][1] for index in selected]
    if len(ranges) != len(set(ranges)):
        _fail("같은 선택 셀을 여러 요소로 관찰하여 범위가 모호합니다.", "grid_selection_ambiguous")
    whole = intent["export_scope"] == "whole_active_sheet"
    if whole and (len(ranges) > 1 or ranges and ranges[0][2:] != (1, 1)):
        _fail("활성 시트 전체 내보내기 전에 다중 선택 또는 넓은 선택 범위를 확인하세요.", "grid_selection_scope_mismatch")
    if not whole and not ranges:
        _fail("선택 범위 내보내기인데 실제 선택 범위가 없습니다.", "grid_selection_scope_mismatch")
    if not whole and len(ranges) == 1 and ranges[0][2:] == (1, 1):
        _fail("Active sheet 명령은 단일 셀 선택을 전체 시트로 처리합니다. 한 셀만 내보내는 선택 범위로 사용할 수 없습니다.", "grid_selection_scope_mismatch")
    if not all(counts):
        if not intent["allow_empty"]:
            _fail("조회 결과가 비어 있고 이 작업은 빈 결과 내보내기를 허용하지 않습니다.", "grid_empty_forbidden")
        _fail("빈 결과는 허용되지만 문맥 메뉴를 열 실제 데이터 셀이 없습니다. 헤더나 표 중심을 대신 누르지 않습니다.", "grid_empty_no_data_cell")
    candidates = [value for index, value in cells.items() if (not selected or index in selected)
                  and value[0].get("enabled") is True and value[0].get("offscreen") is False
                  and _bounds(value[0]) is not None and _inside(_bounds(value[0]), _bounds(grid))]
    if not candidates:
        _fail("출력 범위를 유지하며 문맥 메뉴를 열 현재 데이터 셀이 없습니다.", "grid_data_cell_unavailable")
    # For this explicit export intent these current cells are interchangeable.
    # Prefer the provider's focused cell, otherwise a visible actual GridItem.
    # This is not a saved row-index selector or a rule for row edit/delete.
    candidates.sort(key=lambda item: (item[0].get("focused") is not True, item[1]))
    cell = candidates[0][0]
    context = {"format": "computer-grid-context/v1", "grid_selector": copy.deepcopy(intent["grid_selector"]),
        "export_scope": intent["export_scope"], "active_sheet": sheet,
        "selection_ranges": [dict(zip(("row", "column", "row_span", "column_span"), item)) for item in sorted(ranges)],
        "view_states": view, "ready_states": ready, "complete": True,
        "scope": "selection_and_declared_view_conditions"}
    return {"grid": copy.deepcopy(grid), "cell": copy.deepcopy(cell), "context": context,
            "recognition": "grid_semantic", "read_only": True, "authorizes_input": False,
            "query_verified": False, "data_values_compared": False}


def resolve_grid_intent(snapshot, intent, *, query_evidence=None, run_id=None):
    """Require this run's dispatched query and proven new-result completion.

    The workflow runner supplies the proof after checking the actual preceding
    query receipt, recipe, target lifetime and this run. It is not a public user
    approval field. Native selection evidence never grants input authorization.
    """
    proof = query_evidence
    if (not isinstance(snapshot, dict) or not isinstance(proof, dict)
            or not isinstance(run_id, str) or not run_id
            or proof.get("run_id") != run_id or not isinstance(proof.get("query_step_id"), str)
            or not proof["query_step_id"] or proof.get("target") != snapshot.get("target")
            or not isinstance(snapshot.get("target"), dict)
            or set(snapshot["target"]) != {"pid", "window_id"}
            or any(type(value) is not int or value < 1 for value in snapshot["target"].values())
            or any(proof.get(key) is not True for key in ("input_dispatched", "freshness_verified"))
            or proof.get("failed") is not False):
        _fail("현재 실행의 조회 입력과 준비 완료 전이를 확인하지 못했습니다. 이전 표를 내보내지 않습니다.", "grid_query_freshness_unverified")
    method = proof.get("verification_method")
    if not ((method == "busy_to_ready" and proof.get("busy_observed") is True and proof.get("ready_observed") is True)
            or (method == "require_change" and proof.get("completion_verified") is True
                and proof.get("require_change_verified") is True)):
        _fail("이번 조회의 준비 완료 전이 증거가 없습니다.", "grid_query_freshness_unverified")
    result = inspect_grid_intent(snapshot, intent)
    result["query_verified"] = True
    return result


def compare_grid_context(before, after):
    """Read-only continuity check; no grid values, row counts or coordinates."""
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or any(context.get("format") != "computer-grid-context/v1" or context.get("complete") is not True
                   or context.get("export_scope") not in ("whole_active_sheet", "selected_ranges")
                   or not all(field in context for field in ("grid_selector", "active_sheet", "view_states", "ready_states", "selection_ranges"))
                   for context in (before, after))):
        _fail("표 의미 상태의 비교 근거가 없습니다.", "grid_context_unavailable")
    for field in ("grid_selector", "export_scope", "active_sheet", "view_states", "ready_states"):
        if before.get(field) != after.get(field):
            _fail("표, 활성 시트 또는 출력에 영향을 주는 조건이 바뀌었습니다: " + field, "grid_context_changed")
    if before["export_scope"] == "selected_ranges" and before.get("selection_ranges") != after.get("selection_ranges"):
        _fail("내보낼 선택 범위가 바뀌었습니다. 명령을 보내지 않습니다.", "grid_selection_changed")
    # inspect_grid_intent has already established zero/single 1x1 selection for
    # whole-sheet contracts on both sides. Different data cells are permitted.
    if before["export_scope"] == "whole_active_sheet":
        for context in (before, after):
            ranges = context.get("selection_ranges")
            if (not isinstance(ranges, list) or len(ranges) > 1
                    or any(not isinstance(item, dict) or item.get("row_span") != 1 or item.get("column_span") != 1 for item in ranges)):
                _fail("현재 선택 상태가 활성 시트 전체 내보내기 조건과 다릅니다.", "grid_selection_scope_mismatch")
    return True
