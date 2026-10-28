"""Content-free MCP delivery for local authoring. Not an OS network sandbox.

The public boundary is a whitelist, never a recursive best-effort redactor.
No raw diagnostics, program names, paths, input values or nested tool results
are copied. Local helper IPC and the existing protected task store stay local.
"""
import json
import re

_ID = re.compile(r"(?:(?:task|run|editor|review|window|preview|skill-preview|local-run)-)?[a-f0-9]{24,80}\Z")
_TASK_ID = re.compile(r"(?:task-)?[a-f0-9]{32}\Z")
_STATUSES = frozenset(("ok", "connected", "editing", "starting", "saved", "cancelled", "failed", "blocked", "verified", "needs_review", "interrupted", "running", "pending", "awaiting_input", "completed", "choose_window", "window_unavailable", "stopped", "active", "not_required"))
_COUNTS = ("revision", "completed_steps", "total_steps", "step_count", "pending_step", "enabled_programs", "count", "offset", "limit", "total")
_FLAGS = ("task_verified", "input_dispatched", "focus_dispatched", "pending", "editor_visible", "runnable", "review_required", "cancelled", "unsaved_changes", "cleanup_pending", "result_visible", "has_more")
_IDS = ("task_id", "run_id", "editor_id", "review_id", "window_ref", "preview_id", "local_run_id", "diagnostic_id")


def enabled(config):
    # Missing/unknown policy must never release business content. An existing
    # connected deployment can explicitly retain its reviewed client boundary.
    return config.get("data_policy", "local") != "connected"


def _teaching_receipt(support):
    """Installation metadata only: fixed helper names and presence booleans."""
    from teaching_support import TEACHING_HELPER_FILES
    if not isinstance(support, dict):
        return {}
    version = support.get('version')
    if (support.get('format') != 'computer-teaching-capabilities/v1'
            or not isinstance(version, str)
            or not re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', version)):
        return {}
    def helper(value):
        if (isinstance(value, dict) and value.get('filename') in TEACHING_HELPER_FILES
                and type(value.get('file_present')) is bool):
            return {key: value[key] for key in ('filename', 'file_present')}
        return {}
    direct = support.get('direct_picker', {})
    image = support.get('image_process', {})
    items = image.get('helpers', []) if isinstance(image, dict) else []
    return {'format': 'computer-teaching-capabilities/v1', 'version': version,
            'direct_picker': {'helper': helper(direct.get('helper')) if isinstance(direct, dict) else {}},
            'image_process': {'helpers': [helper(item) for item in items[:3]] if isinstance(items, list) else []}}


def receipt(value, task_alias=None):
    out = {"data_policy": "local", "details_location": "local_editor"}
    if not isinstance(value, dict):
        return {**out, "status": "blocked", "diagnostic": {"code": "local_operation_failed"}}
    out["status"] = value.get("status") if value.get("status") in _STATUSES else "blocked" if 'status' in value or 'diagnostic' in value else "ok"
    version = value.get('version')
    if isinstance(version, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', version):
        out['version'] = version
    if value.get('tool_profile') in ('simple', 'legacy'):
        out['tool_profile'] = value['tool_profile']
    if 'teaching_support' in value:
        out['teaching_support'] = _teaching_receipt(value['teaching_support'])
    # Omission is unknown, not healthy. Preserve these through repeated filtering
    # without ever forwarding session details or the Driver's raw error text.
    for key in ('screen_session_present', 'driver_schema_error_present'):
        if type(value.get(key)) is bool:
            out[key] = value[key]
    if 'session' in value:
        out['screen_session_present'] = value['session'] is not None
    if 'driver_schema_error' in value:
        error = value['driver_schema_error']
        out['driver_schema_error_present'] = not isinstance(error, str) or bool(error)
    for key in _COUNTS:
        if type(value.get(key)) is int and 0 <= value[key] <= 10**9:
            out[key] = value[key]
    for key in _FLAGS:
        if type(value.get(key)) is bool: out[key] = value[key]
    for key in _IDS:
        if key != 'task_id' and isinstance(value.get(key), str) and _ID.fullmatch(value[key]): out[key] = value[key]
    task_id = value.get('task_id', value.get('id'))
    if isinstance(task_id, str):
        public_id = task_alias(task_id) if task_alias is not None else task_id
        if isinstance(public_id, str) and _TASK_ID.fullmatch(public_id): out['task_id'] = public_id
    for key in ("saved_task", "last_test"):
        if isinstance(value.get(key), dict):
            nested = value[key]
            if key == 'last_test':
                # Trial recipes have private, transient draft IDs, not saved
                # TaskStore identities. Run receipts remain resumable by run ID.
                nested = {k: v for k, v in nested.items() if k not in {'task_id', 'id'}}
            out[key] = receipt(nested, task_alias)
    for key in ("tasks", "items"):
        if isinstance(value.get(key), list):
            out[key] = [receipt(v, task_alias) for v in value[key][:100] if isinstance(v, dict)]
    if "steps" in value and isinstance(value["steps"], list): out["step_count"] = len(value["steps"])
    # Arbitrary error strings/codes may contain source data. Only our own fixed
    # policy codes are safe to return; detailed errors remain in the local UI.
    code = value.get("diagnostic", {}).get("code") if isinstance(value.get("diagnostic"), dict) else None
    known = {"local_visual_dependency", "local_use_editor", "local_vision_disabled", "local_operation_failed", "local_input_cancelled", "local_run_busy"}
    if code or out["status"] in {"failed", "blocked"}:
        out["diagnostic"] = {"code": code if code in known else "local_operation_failed"}
        if out['status'] == 'ok': out['status'] = 'blocked'
    if out.get('task_verified') and out['status'] != 'verified': out['task_verified'] = False
    return out


def filter_response(response, task_alias=None):
    try:
        value = receipt(response.get("structuredContent"), task_alias)
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value, "isError": bool(response.get("isError"))}
    except Exception:
        value = {"status": "blocked", "data_policy": "local", "diagnostic": {"code": "local_operation_failed"}}
        return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": True}


def require_local_task(task):
    """Reject external review dependencies once, before the first mutation."""
    from operations import OperationError
    def visit(steps):
        for step in steps:
            if step.get("review_mode") in {"visual", "deferred_visual"}:
                raise OperationError("이 작업에는 모델 화면 판정이 있습니다. 로컬 편집창에서 자동 완료 조건 또는 직접 확인으로 새 revision을 저장하세요. 기존 작업은 바꾸지 않았습니다.", "local_visual_dependency")
            yield from visit(step.get("steps", []))
            yield step
    list(visit(task.get("steps", [])))
