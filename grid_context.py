"""Explicit grid-export intent execution. Provider evidence is never an input handle.

No screenshot matching or persisted cell value is used here. The only mutation
is one Guard-authorized right click with a newly observed Driver cell handle.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from contextlib import contextmanager

from operations import OperationError, _Execution, _descendant, _elements, _unique, _name
from grid_intent import (validate_grid_intent, resolve_grid_intent,
                         inspect_grid_intent, compare_grid_context, _bounds)


def validate_grid_step(step):
    from semantic_menus import validate_menu_target
    from task_inputs import STEP_ID
    allowed = {"operation", "grid_intent", "menu_target", "query_step_id",
               "verification_timeout_ms", "step_timeout_ms"}
    if (not isinstance(step, dict) or step.get("operation") != "open_grid_menu"
            or set(step) - allowed or not isinstance(step.get("query_step_id"), str)
            or not STEP_ID.fullmatch(step["query_step_id"])):
        raise OperationError("조회 완료 단계와 표 내보내기 의도를 지정하세요.", "invalid_grid_step")
    validate_grid_intent(step.get("grid_intent"))
    validate_menu_target(step.get("menu_target"))
    for key, default, low, high in (("verification_timeout_ms", 3000, 1, 10000),
                                    ("step_timeout_ms", 15000, 1, 60000)):
        value = step.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise OperationError("표 메뉴 관찰 제한 시간을 확인하세요.", "invalid_grid_step")
    return copy.deepcopy(step)


def grid_chain(steps, index):
    """Bind this explicit intent to its recorded query, parent and leaf only."""
    opening = steps[index]
    key = lambda row: (row.get("program_id"), row.get("window_ref", "main"))
    queries = [i for i, row in enumerate(steps[:index])
               if row.get("step_id") == opening["query_step_id"]
               and row.get("iteration_id") == opening.get("iteration_id")]
    if len(queries) != 1:
        raise OperationError("이번 반복의 앞선 조회 단계를 유일하게 연결하세요.", "grid_query_step_required")
    query = steps[queries[0]]
    if (key(query) != key(opening) or query.get("operation") not in
            {"click", "image_click", "press_key", "image_press_key", "hotkey", "image_hotkey"}
            or not any(check.get("require_change") is True for check in query.get("expect", []))):
        raise OperationError("현재 업무 창의 조회 완료 변화 조건이 필요합니다.", "grid_query_step_required")
    completed = [{k: v for k, v in check.items() if k != "require_change"}
                 for check in query.get("expect", [])]
    if not all(check in completed for check in opening["grid_intent"]["ready_when"]):
        raise OperationError("조회 완료 검증에는 내보낼 표의 모든 준비 조건이 포함되어야 합니다. 별도 완료 세대 변화도 함께 사용할 수 있습니다.", "grid_query_readiness_mismatch")
    cursor, scope = index+1, key(opening)
    def advance(position, source):
        wait = None
        if position < len(steps) and steps[position].get("operation") == "wait_for_window":
            wait = steps[position]
            if wait.get("program_id") != source[0] or wait.get("owner_ref") not in {source[1], opening.get("window_ref", "main")}:
                raise OperationError("표에서 열린 메뉴의 소유 관계가 다릅니다.", "grid_menu_relation_unverified")
            source = key(wait)
            position += 1
        if position < len(steps) and steps[position].get("operation") == "checkpoint":
            if key(steps[position]) != source:
                raise OperationError("표 메뉴 확인의 창 관계가 다릅니다.", "grid_menu_relation_unverified")
            position += 1
        return position, source, wait
    cursor, scope, wait = advance(cursor, scope)
    parent = steps[cursor] if cursor < len(steps) else {}
    if (parent.get("operation") != "expand_menu" or key(parent) != scope
            or parent.get("menu_target") != opening["menu_target"] or not parent.get("child_menu_target")):
        raise OperationError("표 메뉴의 부모와 자식을 먼저 이름과 계층으로 지정하세요.", "grid_menu_semantics_required")
    parent_index = cursor
    cursor, scope, child_wait = advance(cursor+1, scope)
    command = steps[cursor] if cursor < len(steps) else {}
    if (command.get("operation") != "menu_command" or key(command) != scope
            or command.get("menu_target") != parent["child_menu_target"]
            or ("child_window" in parent) != (child_wait is not None)
            or child_wait is not None and any(child_wait.get(k) != parent["child_window"].get(k) for k in ("title", "class_name"))):
        raise OperationError("내보내기 최종 명령을 관찰된 부모의 자식 경로로 연결하세요.", "grid_menu_semantics_required")
    if ([item["name"] for item in opening["menu_target"]["path"]] != ["Export to excel"]
            or command["menu_target"]["path"][-1]["name"] != "Active sheet"):
        # The single-cell vs ranges rule is a reviewed Active-sheet contract,
        # not permission to choose an arbitrary cell for Edit/Delete/All sheet.
        raise OperationError("이 표 범위 규칙은 확인된 Export to excel → Active sheet 경로에만 적용합니다.", "grid_export_contract_unsupported")
    return {"query_index": queries[0], "parent_index": parent_index, "command_index": cursor, "menu_wait": wait}


def ensure_menu_closed(runtime, target, menu_target, menu_wait=None):
    """Prove absence through complete sources and retain their scope.

    One unavailable provider is not an empty provider. A complete independent
    provider may prove absence, but only that source may subsequently prove
    the new menu's appearance. Partial positive evidence still vetoes input.
    """
    from semantic_menus import observe_menu, resolve_menu, validate_menu_target, _path
    from transient_popup import _allowed_class
    from recording_windows import owned_by
    factory = getattr(runtime, "create_transition_probe", None)
    if callable(factory):
        probe = factory(dict(target))
        try:
            capture = (getattr(probe, "transition_snapshot", None) or getattr(probe, "snapshot", None)
                       or getattr(probe, "capture", None))
            if not callable(capture):
                raise OperationError("메뉴 전이의 관찰 경로가 없습니다.", "grid_menu_baseline_unavailable")
            state = capture()
            rows = state.get("windows", [])
            declarations = {menu_wait["class_name"]} if menu_wait else set()
            for row in rows:
                if (row.get("window_id") != target["window_id"] and row.get("pid") == target["pid"]
                        and row.get("visible") is True
                        and (row.get("class_name") == "#32768" or _allowed_class(row.get("class_name", ""), declarations))
                        and (owned_by(row, target["window_id"], rows) or callable(getattr(probe, "ownership_path", None))
                             and probe.ownership_path(row["window_id"], target["window_id"]))):
                    raise OperationError("이미 열린 관련 메뉴를 닫은 시작 상태에서 실행하세요. 우클릭하지 않았습니다.", "grid_menu_already_open")
        finally:
            probe.close()
    elif menu_wait is not None:
        raise OperationError("기존 팝업의 부재를 관찰할 수 없습니다.", "grid_menu_baseline_unavailable")
    observation = observe_menu(runtime, target)
    sources = observation.get("sources", {})
    if not isinstance(sources, dict):
        raise OperationError("메뉴 관찰의 출처 형식이 올바르지 않습니다.", "grid_menu_baseline_unavailable")
    names, details = [], {}
    expected_path = validate_menu_target(menu_target)["path"]
    expected_name = expected_path[-1]["name"]
    for name in ("uia_raw", "direct_msaa"):
        source = sources.get(name, {})
        if not isinstance(source, dict):
            raise OperationError("메뉴 관찰의 출처 형식이 올바르지 않습니다.", "grid_menu_baseline_unavailable")
        status = source.get("status", "unknown")
        details[name] = status if status in {"ok", "empty", "partial", "unavailable", "error", "timeout"} else "unknown"
        rows = source.get("elements", [])
        if not isinstance(rows, list):
            raise OperationError("메뉴 관찰의 요소 형식이 올바르지 않습니다.", "grid_menu_baseline_unavailable")
        # Even a partial tree can positively expose an existing parent. It
        # cannot prove absence or authorize input, but its positive evidence
        # must not be discarded in favour of an empty peer provider.
        for row in rows:
            if (not isinstance(row, dict) or row.get("role") != "MenuItem"
                    or row.get("name") != expected_name or row.get("visible") is not True):
                continue
            by_index = {item.get("index"): item for item in rows if isinstance(item, dict)}
            try:
                same_path = _path(row, by_index) == expected_path
            except OperationError:
                # A visible expected name with missing ancestors is not proof
                # of another menu. Keep the partial positive evidence blocking.
                same_path = True
            if same_path:
                raise OperationError("관찰된 접근성 경로에 예상 부모 메뉴가 이미 표시됩니다. 우클릭하지 않았습니다.",
                                     "grid_menu_already_open")
        if (status in {"ok", "empty"} and source.get("complete") is True
                and source.get("truncated") is False and not source.get("provider_errors")):
            names.append(name)
    if not names:
        raise OperationError("기존 메뉴의 부재를 확인할 완전한 접근성 관찰이 없습니다. 미지원과 시간 초과를 빈 메뉴로 취급하지 않습니다.",
                             "grid_menu_baseline_unavailable")
    try:
        resolve_menu(observation, menu_target, purpose="parent")
    except OperationError as error:
        if error.code == "menu_item_not_found":
            return {"target": dict(target), "complete_sources": names, "source_statuses": details,
                    "scope": "current_window_menu_path", "read_only": True}
        raise
    raise OperationError("이미 열린 메뉴가 어느 표에서 왔는지 확인하지 못해 우클릭하지 않았습니다.", "grid_menu_already_open")


def driver_cell(snapshot, resolved, intent):
    """Cross-check a real GridItem against this fresh Driver tree only."""
    grid = _unique(snapshot, intent["grid_selector"])
    native_grid, native = resolved["grid"], resolved["cell"]
    if grid.get("synthetic_ancestor") or _bounds(grid) != _bounds(native_grid):
        raise OperationError("Provider와 Driver의 현재 표 경계가 다릅니다.", "grid_driver_scope_mismatch")
    candidates = []
    for item in _elements(snapshot):
        if (item.get("synthetic_ancestor") or item.get("enabled") is not True
                or item.get("offscreen", item.get("is_offscreen", False)) is True
                or not _descendant(item, grid, _elements(snapshot))
                or _bounds(item) != _bounds(native) or item.get("role") != native.get("role")):
            continue
        # These are CURRENT provider properties, never a saved cell identity.
        if (native.get("name") and _name(item) != native["name"]
                or native.get("automation_id") and item.get("automation_id") != native["automation_id"]):
            continue
        driver_grid_item = item.get("grid_item")
        if isinstance(driver_grid_item, dict) and any(
                driver_grid_item.get(k) != native.get("grid_item", {}).get(k)
                for k in ("row", "column", "row_span", "column_span")):
            continue
        candidates.append(item)
    if len(candidates) != 1:
        raise OperationError("현재 데이터 셀을 Driver 입력 대상으로 유일하게 연결하지 못했습니다.",
                             "grid_driver_cell_unavailable")
    return candidates[0]


def _cell_signature(cell):
    return {key: copy.deepcopy(cell.get(key)) for key in
            ("role", "name", "automation_id", "bounds", "frame", "grid_item", "runtime_id")}


def context_hash(context):
    stable = copy.deepcopy(context)
    if stable.get("export_scope") == "whole_active_sheet":
        # Zero or one current 1x1 cell was validated separately. Its location
        # must not turn a whole-sheet workflow into a recorded-row workflow.
        stable.pop("selection_ranges", None)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def menu_identity_hash(selected, window_identity):
    """Current provider identity, NOT a Windows visibility-episode counter.

    Hover, expansion state and position are rechecked separately. MSAA without
    IAccIdentity cannot grant a continuation capability merely from its path.
    """
    path = selected.get("item", {}).get("evidence", {}).get("path", [])
    source = selected.get("source")
    if not path or source not in {"uia_raw", "direct_msaa"}:
        return None
    identities = []
    for node in path:
        identity = node.get("identity")
        kind = node.get("identity_kind", "uia_runtime_id" if source == "uia_raw" else None)
        if not identity or kind != ("uia_runtime_id" if source == "uia_raw" else "iacc_identity"):
            return None
        identities.append({"identity": identity, "kind": kind})
    return context_hash({"window": window_identity, "source": source, "identities": identities})


@contextmanager
def guarded_grid_input(runtime, target, callback):
    """Private single-input capability; the Guard runs it AFTER approval."""
    guard = runtime.guard
    if getattr(guard, "_grid_input_contract", None) is not None:
        raise OperationError("다른 표 입력이 진행 중입니다.", "grid_input_busy")
    guard._grid_input_contract = (dict(target), callback)
    try:
        yield
    finally:
        guard._grid_input_contract = None


def execute_grid_menu(runtime, step, target, *, query_evidence=None, run_id=None,
                      menu_wait=None, verify_only=False, prior_context_hash=None,
                      save_context=None, prior_menu_episode=None, save_menu_episode=None):
    from image_steps import _identity
    from semantic_menus import verify_menu_precondition
    from recording_windows import execute_window_wait
    step = validate_grid_step(step)
    started = time.monotonic()
    deadline = started + step.get("step_timeout_ms", 15000) / 1000
    sent = False
    context = None
    recognition = {}
    execution = _Execution(runtime, {"operation": "right_click", "completion_mode": "human",
        "step_timeout_ms": step.get("step_timeout_ms", 15000)}, dict(target), "background")
    baseline = None
    menu_baseline = None
    def delivery():
        if not execution.dispatched:
            return False
        facts = execution.action_evidence
        if facts and type(facts[-1].get("input_sent")) is bool:
            return facts[-1]["input_sent"]
        return None
    def current():
        runtime.check_active()
        remaining = int((deadline-time.monotonic())*1000)
        if remaining < 1:
            raise OperationError("표 의미 상태 확인 시간이 지났습니다.", "grid_observation_timeout")
        if _identity(runtime, target) != baseline:
            raise OperationError("조회한 업무 창이 교체됐습니다.", "grid_window_changed")
        snapshot = runtime.observe_grid_context(target, step["grid_intent"], timeout_ms=min(2000, remaining))
        result = resolve_grid_intent(snapshot, step["grid_intent"], query_evidence=query_evidence, run_id=run_id)
        if time.monotonic() >= deadline or _identity(runtime, target) != baseline:
            raise OperationError("표를 읽는 동안 창 또는 확인 시간이 달라졌습니다.", "grid_observation_changed")
        return result
    def answer(verified, code, message):
        return {"operation": "open_grid_menu", "status": "verified" if verified else "needs_review",
            "task_verified": verified, "business_result_verified": False, "input_dispatched": sent,
            "target": dict(target), "recognition": "grid_semantic", "data_values_compared": False,
            "query_verified": context is not None, "grid_context_verified": verified,
            "verification_scope": "current_grid_selection_and_related_menu_only",
            "diagnostic": {"code": code, "message": message, "automatic_replay": False},
            "menu_recognition": recognition,
            "metrics": {**execution.metrics, "elapsed_ms": round((time.monotonic()-started)*1000, 2),
                        "image_comparisons": 0},
            **({"grid_context": copy.deepcopy(context)} if context is not None else {})}
    try:
        baseline = _identity(runtime, target)
        resolved = current()
        context = resolved["context"]
        if verify_only and (not prior_context_hash or context_hash(context) != prior_context_hash):
            raise OperationError("우클릭 전 저장한 출력 범위 증거와 현재 상태가 다릅니다.", "grid_context_changed")
        if not verify_only:
            menu_baseline = ensure_menu_closed(runtime, target, step["menu_target"], menu_wait)
            if not callable(save_context):
                raise OperationError("입력 전 표 의미 상태를 저장할 실행 기록이 없습니다.", "grid_run_context_required")
            save_context(context_hash(context))
            snapshot = execution._observe([step["grid_intent"]["grid_selector"]])
            cell = driver_cell(snapshot, resolved, step["grid_intent"])
            signature = _cell_signature(resolved["cell"])
            def revalidate():
                nonlocal menu_baseline
                menu_baseline = ensure_menu_closed(runtime, target, step["menu_target"], menu_wait)
                fresh = current()
                compare_grid_context(context, fresh["context"])
                if _cell_signature(fresh["cell"]) != signature:
                    raise OperationError("승인 중 데이터 셀이 바뀌어 우클릭하지 않았습니다.", "grid_cell_changed")
            with guarded_grid_input(runtime, target, revalidate):
                execution._mutate("right_click", cell, snapshot)
            sent = delivery()
        # A sent/unknown resume is read-only, never a second right click.
        menu_target = dict(target)
        if menu_wait is not None:
            wait = copy.deepcopy(menu_wait)
            wait["timeout_ms"] = min(step.get("verification_timeout_ms", 3000),
                                      max(0, int((deadline-time.monotonic())*1000)))
            found = execute_window_wait(runtime, wait,
                {(wait["program_id"], wait["owner_ref"]): dict(target)}, menu_hint=step["menu_target"])
            if found.get("task_verified") is not True:
                return answer(False, found.get("diagnostic", {}).get("code", "grid_menu_missing"),
                              "표 우클릭 후 관련 메뉴를 확인하지 못했습니다. 입력은 반복하지 않습니다.")
            menu_target = found["target"]
        result = verify_menu_precondition(runtime, {"menu_target": step["menu_target"]}, menu_target)
        recognition = result.get("menu_recognition", {})
        if result.get("task_verified") is not True:
            return answer(False, result.get("diagnostic", {}).get("code", "grid_menu_missing"),
                          "현재 표에서 열린 메뉴의 부모 항목을 확인하지 못했습니다.")
        from semantic_menus import observe_menu, resolve_menu
        selected = resolve_menu(observe_menu(runtime, menu_target), step["menu_target"], purpose="parent")
        if not verify_only:
            recognition.update(baseline_sources=list(menu_baseline["complete_sources"]),
                               baseline_source_statuses=dict(menu_baseline["source_statuses"]))
            if selected["source"] not in menu_baseline["complete_sources"]:
                raise OperationError("메뉴가 나타난 접근성 경로는 입력 전 부재를 확인한 경로와 다릅니다. 우클릭을 반복하지 않습니다.",
                                     "grid_menu_source_changed")
        episode = menu_identity_hash(selected, _identity(runtime, menu_target))
        if verify_only and (not prior_menu_episode or episode != prior_menu_episode):
            raise OperationError("이 실행에서 연 메뉴의 관찰 근거가 달라졌습니다. 입력을 반복하지 않습니다.", "grid_menu_episode_changed")
        if not verify_only and episode is not None and callable(save_menu_episode):
            save_menu_episode(episode)
        compare_grid_context(context, current()["context"])
        return answer(True, "grid_menu_ready", "현재 표의 출력 범위와 관련 메뉴를 확인했습니다. 출력 명령은 아직 보내지 않았습니다.")
    except (OperationError, OSError, ValueError, RuntimeError, NotImplementedError) as error:
        sent = delivery() if not verify_only else False
        recognition = getattr(error, "menu_recognition", recognition)
        return answer(False, getattr(error, "code", "grid_context_unavailable"), str(error))


def verify_export_context(runtime, target, intent, expected_hash):
    """Read-only immediately before final command; never changes selection."""
    snapshot = runtime.observe_grid_context(target, intent, timeout_ms=2000)
    fresh = inspect_grid_intent(snapshot, intent)
    if context_hash(fresh["context"]) != expected_hash:
        raise OperationError("내보내기 전 활성 시트 또는 출력 범위가 바뀌었습니다.", "grid_context_changed")
    return True


@contextmanager
def guarded_export_input(runtime, callback):
    guard = runtime.guard
    if getattr(guard, "_grid_export_revalidate", None) is not None:
        raise OperationError("다른 내보내기 검증이 진행 중입니다.", "grid_input_busy")
    guard._grid_export_revalidate = callback
    try:
        callback()
        yield
    finally:
        guard._grid_export_revalidate = None
