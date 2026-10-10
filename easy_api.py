"""Small public desktop contract; legacy tools remain an opt-in compatibility surface."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import time

from operations import OperationError, _payload
from session_runtime import SessionError
from task_revision import TaskRevisions, TaskRevisionError

INSTRUCTIONS = """Two workflows: (1) computer_open -> computer_observe -> computer_act for conversational work; (2) computer_process_editor for ALL human picking, recording and editing, then computer_run_task. Use computer_programs and computer_register_program for apps; never search the user's disk or edit configuration. computer_open manages bounded sessions and returns logical window_ref; do not guess OS handles. Request image_delivery=vision only when this host/model supports images and screen use is authorized; otherwise text works with UIA and local saved images. Images are sent only to the connected host, no external model API is called. If UIA is sparse, inspect the returned image; do not repeat tree dumps. Use an observed target_ref or image_region with observation_id. For identical buttons include their distinguishing row in scope_region. Input dispatched is not success. If needs_observation_review, observe the current result and submit computer_act action=review with exact action/frame/observation IDs, a verdict and visible evidence; this is visual_assessed, not deterministic verification. Never repeat uncertain input. User teaching occurs ONLY in computer_process_editor. Poll computer_status(editor_id) while authoring. Recording performs real actions, returns an editable draft and does not save or replay automatically. Saved tasks have typed inputs and stable step IDs: use computer_tasks action=get/history, computer_update_task with expected_revision, and computer_export_skill. Run inputs can differ without editing the task; resume retains its original inputs/revision and never repeats uncertain steps. Saved image tasks may return a visual_review request; observe the delivered frame and pass observation_review on resume. Human checkpoints require the actual human's confirmation. computer_stop stops automation, not business apps. Use computer_act action=close with the user's save/discard policy, then verify closure; never force kill. Report final tool results promptly; computer_status exposes progress and phase timings."""

INSTRUCTIONS += " If a saved image task returns needs_target_review, compare the saved target reference (its anchor identifies the intended point) with the delivered current frame. Resume the SAME run with target_review containing the exact challenge IDs, target_region whose CENTER is the intended click, and visible evidence; include distinguishing context in scope_region for repeated icons. This is one-run model targeting, not human teaching or a saved-task edit. Never use this path for an already-dispatched or uncertain action."

def schemas(legacy):
    from server import tool, object_schema, STRING, PROGRAM_IDS, SELECTOR_SCHEMA
    from operations import OPERATION_SCHEMA
    enum = lambda *values: {"type": "string", "enum": list(values)}
    obj = {"type": "object"}
    pos = {"type": "integer", "minimum": 1}
    region = object_schema({k: {"type": "integer", "minimum": 0 if k in {"x", "y"} else 1} for k in ("x", "y", "width", "height")}, ["x", "y", "width", "height"])
    target_review_schema = object_schema({**{key: STRING for key in
        ("recovery_id", "run_id", "step_id", "iteration_id", "observation_id", "frame_id")},
        "step_index": {"type": "integer", "minimum": 0}, "target_region": region, "scope_region": region,
        "evidence": {"type": "string", "minLength": 1, "maxLength": 4000}},
        ["recovery_id", "run_id", "step_index", "step_id", "iteration_id", "observation_id", "frame_id", "target_region", "evidence"])
    step_schema = copy.deepcopy(OPERATION_SCHEMA)
    step_schema["properties"]["operation"]["enum"].extend(["type_text", "scroll"])
    step_schema["properties"].update({"replace_all": {"type": "boolean"},
        "direction": enum("up", "down", "left", "right"), "amount": {"type": "integer", "minimum": 1, "maximum": 20}})
    return [
        tool("computer_programs", "List registered apps or currently open candidates. Use action=candidates when the path is unknown; registration needs no config editing.", object_schema({"action": enum("list", "candidates")}), True),
        copy.deepcopy(legacy["computer_register_program"]),
        tool("computer_open", "Attach to a registered app's current window, or launch it once if absent. Manages bounded session automatically. Multiple windows return choices; supply window_id from those choices. Returns window_ref used by observe/act. No file search.", object_schema({"program_id": STRING, "task_description": STRING, "window_id": pos, "title": STRING, "launch_if_missing": {"type": "boolean"}, "image_delivery": enum("text", "vision")}, ["program_id"])),
        tool("computer_observe", "Read a bound current window once. Image capture may bring that window forward. UIA controls and optional screenshot use one coordinate/frame contract. Use search/within to narrow results. Sparse UIA in a vision connection returns the image for visual reasoning.", object_schema({"window_ref": STRING, "goal": STRING, "search": STRING, "observation": enum("auto", "uia", "both"), "within": SELECTOR_SCHEMA}, ["window_ref"])),
        tool("computer_act", "Perform one foreground action and verify its result, or review the exact post-action observation. Provide step with operation/selector/value/expect; alternatively target_ref or image_region from a delivered observation. completion describes visible success when no deterministic expect is available. type_text/scroll require an image_region; set_value works with UIA or images. Never repeat input after unknown/needs_observation_review. close/verify_closed handle actual termination and remaining dialogs.", object_schema({"action": enum("execute", "review", "close", "verify_closed"), "window_ref": STRING, "step": step_schema, "target_ref": STRING, "observation_id": STRING, "image_region": region, "scope_region": region, "completion": STRING, "action_id": STRING, "frame_id": STRING, "verdict": enum("pass", "fail", "uncertain"), "evidence": STRING, "evidence_regions": {"type": "array", "items": region, "maxItems": 8}, "scope": enum("window", "process"), "close_id": STRING, "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 10000}}, ["window_ref"])),
        tool("computer_process_editor", "The ONE human teaching entry: select on screen, record real actions, edit values/conditions and trial-run, then save. App windows attach automatically. Reopening task_id edits the SAME version using conflict checks. Poll computer_status(editor_id). For a trial's visual_review use action=resume_test with editor_id and the bound observation_review; it resumes the unchanged draft without saving or replaying completed input.", object_schema({"action": enum("open", "resume_test"), "editor_id": STRING, "observation_review": obj, "acknowledge_checkpoint": STRING, "program_ids": PROGRAM_IDS, "task_id": STRING, "name": STRING, "window_refs": {"type": "object", "description": "Optional program_id to window_ref returned by computer_open."}, "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 1800}})),
        tool("computer_tasks", "List saved processes, get one with stable step IDs and typed inputs, or read revision history. Local image pixels stay private in storage.", object_schema({"action": enum("list", "get", "history"), "task_id": STRING, "revision": pos, "query": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), True),
        tool("computer_update_task", "Change a process by stable step IDs without rerecording. expected_revision prevents overwriting another edit. Changes: set_step(step_id,fields), set_input(name,spec), set_default(name,value), set_text(fields), insert_step(after_step_id,step), delete_step(step_id), move_step(step_id,after_step_id). Image targets are changed only in editor. restore_revision restores as a new revision. Does not run anything.", object_schema({"task_id": STRING, "expected_revision": pos, "changes": {"type": "array", "items": obj, "minItems": 1, "maxItems": 60}, "restore_revision": pos}, ["task_id", "expected_revision"])),
        tool("computer_run_task", "Run a saved process with current app windows and optional typed inputs; successful routes reuse scoped verification. No manual PID setup. On pause, resume_run_id reuses the immutable original inputs/revision. observation_review answers the exact returned visual_review challenge. target_review identifies the same saved image target on its delivered current frame, only before input: use exact challenge IDs, target_region CENTER and visible evidence. Neither review replays dispatched input or rewrites the saved process. Human checkpoint acknowledgement must come from actual human review.", object_schema({"task_id": STRING, "revision": pos, "inputs": obj, "window_refs": obj, "resume_run_id": STRING, "observation_review": obj, "target_review": target_review_schema, "acknowledge_checkpoint": STRING, "execution_mode": enum("auto", "standard", "fast")}, ["task_id"])),
        tool("computer_export_skill", "Export a thin local SKILL.md that calls the pinned saved process and its typed inputs. Does not embed screenshots or scripts, install into user profiles, or run the process.", object_schema({"task_id": STRING, "revision": pos}, ["task_id"])),
        tool("computer_status", "Read environment/progress or editor/run/checkpoint status while work is running. An explicit human trial checkpoint may open its local review window. editor_id + cancel closes the editor without running or saving its draft.", object_schema({"editor_id": STRING, "run_id": STRING, "review_id": STRING, "wait_ms": {"type": "integer", "minimum": 0, "maximum": 5000}, "cancel": {"type": "boolean"}})),
        copy.deepcopy(legacy["computer_stop"]),
    ]

def _validate(value, schema, path="arguments"):
    if "enum" in schema and value not in schema["enum"]: raise SessionError(path+" 값이 올바르지 않습니다.")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict): raise SessionError(path+"는 객체여야 합니다.")
        props = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value) or schema.get("additionalProperties") is False and set(value)-set(props):
            raise SessionError(path+"의 필수 항목 또는 지원하는 항목을 확인하세요.")
        for key, val in value.items():
            if key in props: _validate(val, props[key], path+"."+key)
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000): raise SessionError(path+" 목록 크기를 확인하세요.")
        for item in value: _validate(item, schema.get("items", {}), path)
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value): raise SessionError(path+"에 중복 항목이 있습니다.")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 32000): raise SessionError(path+" 문자열을 확인하세요.")
    elif kind == "integer":
        if type(value) is not int or not schema.get("minimum", -2147483648) <= value <= schema.get("maximum", 2147483647): raise SessionError(path+" 정수 범위를 확인하세요.")
    elif kind == "boolean" and type(value) is not bool: raise SessionError(path+"는 true 또는 false여야 합니다.")

class EasyApi:
    def __init__(self, manager):
        self.manager = manager
        self.engine = None
        self.runtime = None
        self.bindings = {}
        self.pending_launches = {}
        self.editor_reviews = {}

    def close(self):
        if self.engine: self.engine.close()
        self.engine = self.runtime = None
        self.bindings.clear()
        self.pending_launches.clear()
        self.editor_reviews.clear()

    def _engine(self):
        from interaction import InteractionEngine
        current = self.manager.session
        if current is None: raise SessionError("computer_open으로 프로그램을 먼저 연결하세요.")
        current.check_active()
        if self.runtime is not current:
            self.close(); self.runtime = current; self.engine = InteractionEngine(current)
        return self.engine

    def _session(self, ids, description, cancel_event):
        from vendor.guard import check_app
        def scope(program):
            return (check_app(program['exe']),
                    tuple(sorted(check_app(path) for path in program.get('control_exes', []))),
                    program.get('launch'))
        configured = {program['id']: program for program in self.manager.config['programs'] if program.get('enabled') is True}
        current = self.manager.session
        if current is not None and current.state != "stopped":
            current.check_active()
            old = [p["id"] for p in current.programs]
            current_scopes = {program['id']: scope(program) for program in current.programs}
            same_scope = all(identity in configured and current_scopes.get(identity) == scope(configured[identity])
                             for identity in old)
            if set(ids) <= set(old) and same_scope: return
            if self.manager.process_editors.pending(current): raise SessionError("현재 편집창을 닫은 뒤 다른 프로그램을 연결하세요.")
            # Renew the bounded approval scope; never mutate an existing Guard policy.
            ids = list(dict.fromkeys([identity for identity in old if identity in configured]+ids))
            self.manager.stop("등록한 프로그램의 현재 실행 경로와 허용 범위로 작업 연결을 새로 준비합니다.")
            if not current.wait_stopped(10): raise SessionError("이전 연결 정리가 진행 중입니다. 상태를 확인하세요.")
        self.close()
        self.manager.begin({"program_ids": ids, "task_description": description, "mode": "uia"}, cancel_event)

    def _windows(self, program_id):
        from vendor.guard import check_app
        runtime = self.manager.session
        app = next((p for p in runtime.programs if p["id"] == program_id), None)
        if not app: raise SessionError("등록된 프로그램을 먼저 연결하세요.")
        permitted = {check_app(p) for p in [app["exe"], *app.get("control_exes", [])]}
        response = runtime.call("list_windows", {"on_screen_only": True})
        if response.get("isError"): raise SessionError("창 목록을 읽지 못했습니다: "+str(_payload(response)))
        data = _payload(response)
        rows = data.get("windows", data.get("items", []))
        matches = []
        for row in rows:
            if not isinstance(row, dict): continue
            pid, window_id = row.get("pid"), row.get("window_id", row.get("id"))
            if type(pid) is not int or type(window_id) is not int: continue
            try:
                resolver = getattr(runtime.guard, 'target_executable', None)
                executable = resolver({'pid': pid, 'window_id': window_id}) if callable(resolver) else runtime.guard.process_resolver(pid)
                if check_app(executable) not in permitted: continue
            except (OSError, ValueError, RuntimeError): continue
            matches.append({"pid": pid, "window_id": window_id, "title": row.get("title", "")})
        return matches

    def _refresh_hosted_scope(self, cancel_event=None):
        """Freeze new exact UWP frames in a new session without relaunch/replay."""
        current = self.manager.session
        needs_refresh = getattr(current, 'hosted_scope_needs_refresh', None)
        if not callable(needs_refresh) or not needs_refresh(): return False
        current.check_active()
        if self.manager.process_editors.pending(current):
            raise SessionError('편집창을 닫은 뒤 새로 열린 앱 창을 연결하세요.')
        deadline = current.deadline
        remaining = deadline-time.monotonic()
        actions = current.max_actions-current.guard.action_count
        if remaining <= 0 or actions <= 0:
            raise SessionError('기존 작업 연결의 실행 한도가 끝났습니다. 새 작업을 시작하세요.')
        ids = [program['id'] for program in current.programs]
        description = current.task.get('instructions') or '등록한 앱의 현재 창 연결'
        pending = copy.deepcopy(self.pending_launches)
        self.manager.stop('승인한 패키지 앱의 새 창을 정확한 창 범위로 연결합니다. 입력은 반복하지 않습니다.')
        if not current.wait_stopped(10): raise SessionError('이전 창 연결을 정리하는 중입니다.')
        if time.monotonic() >= deadline: raise SessionError('기존 작업 연결의 제한 시간이 지났습니다.')
        self.manager.begin({'program_ids': ids, 'task_description': description, 'mode': current.mode,
            'max_minutes': min(self.manager.config['max_minutes'], max(1, math.ceil((deadline-time.monotonic())/60))),
            'max_actions': min(self.manager.config['max_actions'], actions)}, cancel_event)
        renewed = self.manager.session
        renewed.deadline = min(renewed.deadline, deadline)
        renewed.check_active()
        self.pending_launches.update(pending)
        return True

    def _attach(self, program_id, *, window_id=None, title=None, launch_if_missing=True, cancel_event=None):
        self._refresh_hosted_scope(cancel_event)
        rows = self._windows(program_id)
        launch = None
        if not rows and launch_if_missing:
            launch = self.pending_launches.get(program_id)
            if launch is None:
                launch = self.manager.session.launch(program_id)
                self.pending_launches[program_id] = copy.deepcopy(launch)
            self._refresh_hosted_scope(cancel_event)
            rows = self._windows(program_id)
        if rows:
            self.pending_launches.pop(program_id, None)
        if window_id is not None: rows = [r for r in rows if r["window_id"] == window_id]
        if title is not None: rows = [r for r in rows if r["title"] == title]
        if len(rows) != 1:
            return {"status": "choose_window" if rows else "window_unavailable", "program_id": program_id,
                    "candidates": rows, "launch": launch, "task_verified": False,
                    "next_step": "현재 창을 선택해 computer_open에 window_id를 전달하세요." if rows else "프로그램 창이 표시되는지 확인하세요. 실행 요청을 자동 반복하지 않았습니다."}
        row = rows[0]
        bound = self._engine().bind({k: row[k] for k in ("pid", "window_id")})
        self.bindings[bound["window_ref"]] = program_id
        return {"status": "connected", "program_id": program_id, "title": row["title"], **bound, "launch": launch}

    def _targets(self, ids, refs, cancel_event=None):
        if not isinstance(refs, dict) or set(refs)-set(ids): raise SessionError("작업에 필요한 프로그램의 창만 지정하세요.")
        targets = []
        for program in ids:
            if program in refs:
                ref = refs[program]
                if self.bindings.get(ref) != program: raise SessionError("다른 프로그램의 창 참조를 사용할 수 없습니다.")
                target = self._engine().target(ref)
            else:
                answer = self._attach(program, cancel_event=cancel_event)
                if answer["status"] != "connected": return None, answer
                target = answer["target"]
            targets.append({"program_id": program, **target})
        return targets, None

    def call(self, name, args, cancel_event=None):
        response = self._call(name, args, cancel_event)
        return self.normalize_response(name, args, response)

    def normalize_response(self, name, args, response):
        old = response.get('structuredContent')
        if not isinstance(old, dict): return response
        value = copy.deepcopy(old)
        self._navigation(value, args)
        # Only engine-owned status metadata is adapted. User task descriptions,
        # values, selectors, program names and saved history are not traversed.
        if name == 'computer_status' and not any(key in args for key in ('editor_id', 'run_id', 'review_id')):
            image = value.get('image_delivery', {})
            image.update(tool='computer_open', local_review_tool='computer_status',
                next_arguments={'image_delivery': 'vision'}, program_id_required=True)
            if 'adaptive_observation' in value: value['adaptive_observation']['tool'] = 'computer_observe'
            if 'program_registration' in value:
                value['program_registration'].update(candidates_tool='computer_programs', candidates_arguments={'action': 'candidates'})
            teaching = value.get('teaching_support', {})
            for section in ('direct_picker', 'image_process'):
                if section in teaching:
                    teaching[section].update(tool='computer_process_editor', status_tool='computer_status',
                        message='요소 선택과 이미지 선택·녹화는 모두 프로세스 편집창에서 진행합니다.')
        if name == 'computer_register_program' and value.get('next_tool') == 'computer_open':
            value['message'] = '등록한 프로그램을 computer_open으로 연결하면 사용할 수 있습니다. 작업 연결과 허용 범위는 자동으로 준비합니다.'
        if value.get('next_tool_if_path_unknown') == 'computer_program_candidates':
            value.update(next_tool_if_path_unknown='computer_programs', next_arguments_if_path_unknown={'action': 'candidates'})
        answer = {**response, 'structuredContent': value}
        content = []
        for item in response.get('content', []):
            if item.get('type') == 'text':
                try:
                    if json.loads(item.get('text', '')) == old:
                        content.append({**item, 'text': json.dumps(value, ensure_ascii=False)}); continue
                except (ValueError, TypeError): pass
            content.append(item)
        answer['content'] = content
        return answer

    def _navigation(self, value, args):
        """Adapt known response envelopes, never recursively rewrite app data."""
        from server import MANAGEMENT
        public = {tool['name']: tool['inputSchema']['properties'] for tool in schemas(MANAGEMENT)}
        next_tool = value.get('next_tool')
        arguments = copy.deepcopy(value.get('next_arguments', {}))
        if not isinstance(arguments, dict): arguments = {}
        if next_tool in {'computer_begin', 'computer_end', 'computer_open'}:
            program = value.get('program', {}).get('id') or args.get('program_id') or self.bindings.get(args.get('window_ref'))
            if program:
                value.update(next_tool='computer_open', next_arguments={'program_id': program, 'launch_if_missing': False} if value.get('next_action') == 'rebind_and_observe' else {'program_id': program})
            else: value.update(next_tool='computer_programs', next_arguments={'action': 'list'})
        elif next_tool == 'computer_program_candidates':
            value.update(next_tool='computer_programs', next_arguments={'action': 'candidates'},
                next_step='computer_programs(action=candidates)에서 현재 열린 프로그램을 선택해 등록하세요.')
        elif next_tool in {'computer_process_status', 'computer_review_checkpoint'}:
            key = 'editor_id' if next_tool == 'computer_process_status' else 'review_id'
            identity = value.get(key) or arguments.get(key) or args.get(key) or value.get('id')
            value.update(next_tool='computer_status', next_arguments={key: identity} if identity else {})
        elif next_tool == 'computer_run_task':
            review_id = value.get('review_id') or args.get('review_id')
            editor_id = self.editor_reviews.get(review_id)
            if editor_id:
                value.update(next_tool='computer_process_editor', next_arguments={'action': 'resume_test', 'editor_id': editor_id,
                    'acknowledge_checkpoint': value.get('checkpoint_id') or arguments.get('acknowledge_checkpoint')})
            else:
                value['next_arguments'] = {key: val for key, val in arguments.items() if key in public[next_tool]}
        elif next_tool == 'computer_status' and 'next_arguments' not in value:
            review = value.get('local_review', {})
            value['next_arguments'] = ({'review_id': review['review_id']} if review.get('review_id') else
                {'editor_id': value['editor_id']} if value.get('editor_id') else {})
        # These envelopes contain system navigation only. In particular steps,
        # task/history arrays and arbitrary diagnostics payloads are untouched.
        for key in ('local_review', 'last_test'):
            if isinstance(value.get(key), dict): self._navigation(value[key], args)
        if isinstance(value.get('image_delivery'), dict) and isinstance(value['image_delivery'].get('local_review'), dict):
            self._navigation(value['image_delivery']['local_review'], args)

    def _call(self, name, args, cancel_event=None):
        from server import MANAGEMENT, result
        from process_editor import task_view
        schema = next(t["inputSchema"] for t in schemas(MANAGEMENT) if t["name"] == name)
        _validate(args, schema)
        if cancel_event is not None and cancel_event.is_set(): raise SessionError("요청이 취소되었습니다.")
        m = self.manager
        try:
            if name == "computer_status":
                identities = [key for key in ("editor_id", "run_id", "review_id") if key in args]
                if len(identities) > 1: raise SessionError("한 번에 하나의 진행 상태를 조회하세요.")
                if "editor_id" in args:
                    return self._observation_response(m.process_editors.status(args["editor_id"], wait_ms=args.get("wait_ms", 0), cancel=args.get("cancel", False)))
                if "run_id" in args: return result(m.workflows.progress(args["run_id"]))
                if "review_id" in args: return result(m.checkpoint_reviews.status(args["review_id"], m.session))
                return result({**m.status(), "tool_profile": "simple", "workflows": ["conversational", "process_editor"]})
            if name == "computer_programs": return m._call("computer_program_candidates" if args.get("action") == "candidates" else "computer_programs", {}, cancel_event)
            if name in {"computer_register_program", "computer_stop"}: return m._call(name, args, cancel_event)
            revisions = TaskRevisions(m.tasks)
            if name == "computer_tasks":
                action = args.get("action", "list")
                if action == "list": return m._call(name, {k:v for k,v in args.items() if k in {"query", "offset", "limit"}}, cancel_event)
                if not args.get("task_id"): raise SessionError("task_id를 지정하세요.")
                return result(revisions.history(args["task_id"]) if action == "history" else task_view(revisions.get(args["task_id"], args.get("revision"))))
            if name == "computer_update_task":
                if ("changes" in args) == ("restore_revision" in args): raise SessionError("changes 또는 restore_revision 중 하나를 지정하세요.")
                answer = revisions.restore(args["task_id"], args["expected_revision"], args["restore_revision"]) if "restore_revision" in args else revisions.update(args["task_id"], args["expected_revision"], args["changes"])
                if "task" in answer: answer["task"] = task_view(answer["task"])
                return result(answer)
            if name == "computer_export_skill":
                answer = revisions.export_skill(args["task_id"], args.get("revision"))
                root = Path(m.config["state_dir"]) / "exports" / args["task_id"] / str(answer["manifest"]["pinned_revision"])
                for path in (root.parent.parent, root.parent, root, root/"SKILL.md"): m.tasks._reject_link(path)
                root.mkdir(parents=True, exist_ok=True)
                (root/"SKILL.md").write_text(answer["skill_markdown"], encoding="utf-8")
                return result({**answer, "path": str(root/"SKILL.md")})
            if name == "computer_open":
                self._session([args["program_id"]], args.get("task_description", "요청한 프로그램의 화면 작업"), cancel_event)
                if "image_delivery" in args: m.image_delivery.configure(args["image_delivery"])
                with m.session.execution_lock:
                    return result(self._attach(args["program_id"], cancel_event=cancel_event, **{k:args[k] for k in ("window_id", "title", "launch_if_missing") if k in args}))
            if name == "computer_process_editor" or name == "computer_run_task":
                if name == "computer_process_editor" and args.get("action") == "resume_test":
                    if not args.get("editor_id") or set(args) - {"action", "editor_id", "observation_review", "acknowledge_checkpoint"}:
                        raise SessionError("시험을 이어갈 editor_id와 확인 결과만 지정하세요. 초안이나 창을 변경하지 않습니다.")
                    if m.session is None: raise SessionError("열린 편집 창의 연결이 필요합니다.")
                    m.session.check_active()
                    with m.session.execution_lock:
                        if args.get("acknowledge_checkpoint"):
                            current = m.process_editors.status(args["editor_id"])
                            trial = current.get("last_test", {})
                            review = m.checkpoint_reviews.acknowledgement(m.session, trial.get("run_id"), args["acknowledge_checkpoint"])
                            if review.get("human_reviewed") is not True:
                                response = self._observation_response(current)
                                response["structuredContent"].update(status="needs_review", task_verified=False)
                                response["structuredContent"].setdefault("local_review", review)
                                response["content"][0]["text"] = json.dumps(response["structuredContent"], ensure_ascii=False)
                                return response
                        return self._observation_response(m.process_editors.resume_test(args["editor_id"], m.session,
                            **{key: args[key] for key in ("observation_review", "acknowledge_checkpoint") if key in args}))
                if name == "computer_process_editor" and any(key in args for key in ("editor_id", "observation_review", "acknowledge_checkpoint")):
                    raise SessionError("시험 결과 확인은 action=resume_test와 editor_id로 이어가세요.")
                task = revisions.get(args["task_id"], args.get("revision")) if args.get("task_id") else None
                if name == "computer_run_task" and args.get("resume_run_id"):
                    frozen = m.workflows.resume_snapshot(args["resume_run_id"], task["id"], args.get("inputs", {}))
                    if frozen: task = frozen[0]
                ids = args.get("program_ids") or (task["program_ids"] if task else [p["id"] for p in m.session.programs] if m.session else [])
                if not ids: raise SessionError("program_ids로 가르칠 프로그램을 지정하거나 computer_open으로 먼저 연결하세요.")
                self._session(ids, task["instructions"] if task else "사용자가 프로세스를 녹화하고 편집합니다.", cancel_event)
                with m.session.execution_lock:
                    targets, pending = self._targets(ids, args.get("window_refs", {}), cancel_event)
                    if pending: return result(pending)
                    if name == "computer_process_editor":
                        return result(m.process_editors.start(m.session, {"targets": targets, **{k:args[k] for k in ("name", "task_id", "timeout_seconds") if k in args}}, cancel_event))
                    if m.process_editors.pending(m.session): raise SessionError("편집창을 닫은 뒤 저장한 프로세스를 실행하세요. 시험 실행은 편집창 버튼을 사용하세요.")
                    if args.get("acknowledge_checkpoint"):
                        review = m.checkpoint_reviews.acknowledgement(m.session, args.get("resume_run_id"), args["acknowledge_checkpoint"])
                        if review.get("human_reviewed") is not True: return result({"status":"needs_review", "task_verified":False, "local_review":review})
                    kwargs = {k:args[k] for k in ("resume_run_id", "acknowledge_checkpoint", "execution_mode", "observation_review", "target_review") if k in args}
                    answer = m.workflows.run(m.session, task, args.get("inputs", {}), targets, delivery_mode="foreground", image_delivery_enabled=m.image_delivery.status()["delivery_mode"] == "vision", **kwargs)
                    content = answer.pop("checkpoint_content", [])
                    content.extend(answer.pop("observation_content", []))
                    # Vision delivery does not invoke filter_response's local
                    # viewer callback. Explicit saved HUMAN checkpoints still
                    # need the actual local button receipt in that profile.
                    checkpoint = answer.get("checkpoint", {})
                    if (content and checkpoint.get("capture_available") and not answer.get("visual_review")
                            and not answer.get("target_review") and answer.get("state") != "needs_target_review"
                            and m.image_delivery.status()["delivery_mode"] == "vision"):
                        answer["local_review"] = m.checkpoint_reviews.open(m.session, answer, content, args)
                        answer["next_tool"] = "computer_status"
                        answer["next_step"] = "이 PC의 확인 창에서 직접 결과를 확인한 뒤 computer_status(review_id)로 확인 상태를 읽으세요."
                    response = result(answer)
                    response["content"].extend(content)
                    return response
            if m.process_editors.pending(m.session): raise SessionError("편집 또는 녹화 중입니다. 편집창을 마친 뒤 채팅 동작을 실행하세요.")
            engine = self._engine()
            with m.session.execution_lock:
                vision = m.image_delivery.status()["delivery_mode"] == "vision"
                if name == "computer_observe": return engine.observe(**args, image_delivery_enabled=vision)
                action = args.get("action", "execute")
                ref = args["window_ref"]
                if action == "review":
                    required = {"action_id", "observation_id", "frame_id", "verdict", "evidence"}
                    if not required <= set(args): raise SessionError("검토할 동작·관찰·프레임 ID와 화면 근거가 필요합니다.")
                    return result(engine.review(ref, **{k:args[k] for k in required | {"evidence_regions"} if k in args}))
                if action == "verify_closed":
                    if not args.get("close_id"): raise SessionError("종료 요청에서 받은 close_id가 필요합니다.")
                    return result(m.session.closures.verify(args["close_id"], timeout_ms=args.get("timeout_ms", 1500)))
                if action == "close":
                    from close_actions import request_close
                    if not args.get("step"): raise SessionError("관찰한 닫기 버튼 또는 종료 키를 step으로 지정하세요.")
                    return result(request_close(m.session, engine.target(ref), args["step"], scope=args.get("scope", "window"), delivery_mode="foreground", timeout_ms=args.get("timeout_ms", 1500)))
                if not args.get("step"): raise SessionError("실행할 step을 지정하세요.")
                return result(engine.act(ref, args["step"], **{k:args[k] for k in ("target_ref", "observation_id", "image_region", "scope_region", "completion") if k in args}, image_delivery_enabled=vision))
        except (OperationError, TaskRevisionError) as exc:
            return result({"status":"blocked", "task_verified":False, "diagnostic":{"code":getattr(exc,"code","invalid_request"),"message":str(exc)}}, error=True)

    def _observation_response(self, answer):
        """Keep image blocks out of JSON; ComputerManager owns delivery policy."""
        from server import result
        answer = copy.deepcopy(answer)
        content = answer.pop("observation_content", [])
        checkpoint_content = answer.pop("checkpoint_content", [])
        trial = answer.get("last_test", {})
        if checkpoint_content and trial.get("checkpoint", {}).get("capture_available") and not trial.get("visual_review"):
            answer["local_review"] = self.manager.checkpoint_reviews.open(self.manager.session, trial, checkpoint_content, {})
            identity = answer["local_review"].get("review_id")
            if identity and answer.get("editor_id"):
                self.editor_reviews[identity] = answer["editor_id"]
                while len(self.editor_reviews) > 100: self.editor_reviews.pop(next(iter(self.editor_reviews)))
            answer["next_tool"] = "computer_status"
            answer["next_step"] = "이 PC의 확인 창에서 직접 결과를 확인하세요. 완료되면 computer_process_editor(action=resume_test)로 현재 시험만 이어갑니다."
        content.extend(checkpoint_content)
        response = result(answer)
        response["content"].extend(content)
        return response
