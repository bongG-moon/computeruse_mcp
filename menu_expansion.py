"""One proven parent expansion followed by bounded, read-only child discovery.

The child command is deliberately a separate action. Menu visibility is never
reported as export completion. A resume only rechecks the child; it cannot
repeat an uncertain parent input or authorize a leaf input from old handles.
"""
from __future__ import annotations

import copy
import time

from operations import OperationError, _Execution, _descendant, _elements, _find, _limited, _name, _unique, validate_selector


FIELDS = {"operation", "selector", "image_target", "child_selector", "child_window",
          "child_image_target", "expansion_method", "expansion_key", "menu_depth", "menu_path",
          "verification_timeout_ms", "poll_interval_ms", "step_timeout_ms"}


def validate_menu_expansion(step):
    if not isinstance(step, dict) or step.get("operation") != "expand_menu" or set(step) - FIELDS:
        raise OperationError("메뉴 전개 단계의 형식이 올바르지 않습니다.", "invalid_menu_expansion")
    paired_hover = ("selector" in step and "image_target" in step
                    and step.get("expansion_method") == "hover"
                    and "child_selector" in step and "child_window" not in step)
    if ("selector" in step) == ("image_target" in step) and not paired_hover:
        raise OperationError("펼칠 부모 메뉴의 selector 또는 image_target 하나가 필요합니다.", "invalid_menu_expansion")
    if "selector" in step:
        validate_selector(step["selector"])
        if step["selector"].get("role") != "MenuItem":
            raise OperationError("부모 대상은 실제 MenuItem이어야 합니다.", "invalid_menu_expansion")
    if "child_selector" in step:
        validate_selector(step["child_selector"])
        if step["child_selector"].get("role") != "MenuItem":
            raise OperationError("자식 대상은 실제 MenuItem이어야 합니다.", "invalid_menu_expansion")
    from image_targets import validate_image_target
    for key in ("image_target", "child_image_target"):
        if key in step:
            validate_image_target(step[key])
    if "child_window" in step:
        child = step["child_window"]
        if (not isinstance(child, dict) or set(child) != {"title", "class_name"}
                or not isinstance(child["title"], str) or len(child["title"]) > 1000
                or not isinstance(child["class_name"], str) or not 1 <= len(child["class_name"]) <= 256
                or not ("child_selector" in step or "child_image_target" in step)):
            raise OperationError("별도 자식 팝업에는 제목/종류와 실제 자식 요소 또는 이미지가 필요합니다.", "invalid_menu_expansion")
    elif "child_selector" not in step or "selector" not in step or "child_image_target" in step:
        raise OperationError("같은 창의 논리 자식은 부모와 자식 selector 및 실제 계층으로 확인해야 합니다.", "invalid_menu_expansion")
    method = step.get("expansion_method")
    path = step.get("menu_path", [])
    if not isinstance(path, list) or len(path) > 16 or any(not isinstance(label, str) or not 1 <= len(label.strip()) <= 1000 for label in path):
        raise OperationError("메뉴 진단 경로는 관찰한 이름 목록이어야 합니다.", "invalid_menu_expansion")
    if method not in {"hover", "click", "keyboard", "accessibility"}:
        raise OperationError("관찰된 메뉴 전개 방식을 지정하세요.", "invalid_menu_expansion")
    if method == "keyboard":
        if step.get("expansion_key") not in {"RIGHT", "LEFT", "ENTER", "SPACE", "DOWN"}:
            raise OperationError("녹화 또는 관찰한 메뉴 전개 키를 지정하세요.", "invalid_menu_expansion")
    elif "expansion_key" in step:
        raise OperationError("전개 키는 keyboard 방식에서만 사용합니다.", "invalid_menu_expansion")
    for key, default, low, high in (("verification_timeout_ms", 1500, 0, 10000),
                                    ("poll_interval_ms", 100, 100, 1000),
                                    ("step_timeout_ms", 30000, 1, 120000),
                                    ("menu_depth", 1, 1, 16)):
        if type(step.get(key, default)) is not int or not low <= step.get(key, default) <= high:
            raise OperationError("메뉴 대기 시간과 깊이를 확인하세요.", "invalid_menu_expansion")
    return copy.deepcopy(step)


def _visible(element):
    if (element.get("enabled") is not True or element.get("visible") is False
            or any(element.get(key) is True for key in ("is_offscreen", "offscreen", "hidden", "protected", "is_password", "is_protected"))):
        return False
    # Providers may publish a collapsed child's name and runtime handle. An
    # absent visibility property is unknown, never proof of an open submenu.
    if not (element.get("visible") is True or element.get("is_on_screen") is True
            or any(element.get(key) is False for key in ("is_offscreen", "offscreen"))):
        return False
    if "frame" in element or "bounds" in element:
        from inspection import _observed_rectangle
        return _observed_rectangle(element) is not None
    return True


class _MenuExpansion:
    def __init__(self, runtime, step, target, delivery_mode, verify_only):
        self.runtime, self.step, self.target = runtime, step, dict(target)
        self.execution = _Execution(runtime, step, target, delivery_mode)
        self.execution.no_parent_recovery = True
        self.verify_only = verify_only
        self.probe = None
        self.identity = None
        self.owner = dict(target)
        self.last = {}
        self.child_target = None
        self.child_representation = "unknown"
        self.delivery_state = "not_sent"
        self.expansion_result = None
        self.native_reads = 0
        self.native_read_ms = 0
        self.window_discovery_ms = 0
        self.child_image_ms = 0
        self.parent_image_ms = 0
        self.started = time.monotonic()

    def _identity(self):
        self.execution._remaining()
        began = time.monotonic()
        try:
            state = getattr(self.probe, "transition_snapshot", self.probe.snapshot)()
        finally:
            self.native_read_ms += (time.monotonic() - began) * 1000
        self.native_reads += 1
        from interaction import _identity
        current = _identity(state, self.target["window_id"])
        if current.get("pid") != self.target["pid"] or self.identity is not None and current != self.identity:
            raise OperationError("부모 메뉴의 프로세스 또는 창이 바뀌었습니다.", "menu_parent_changed")
        self.identity = current
        return state

    def _capture(self, target):
        self._identity()
        # Never activate the owner while observing a transient menu.
        for depth, count in ((12, 600), (32, 5000)):
            snapshot = self.execution._capture(target, depth, count)
            if not _limited(snapshot, depth, count):
                self._identity()
                return snapshot
        raise OperationError("메뉴 관찰이 잘려 대상의 유일성을 확인하지 못했습니다.", "menu_observation_incomplete")

    def _parent(self, snapshot):
        parent = _unique(snapshot, self.step["selector"])
        if parent.get("role") != "MenuItem" or not _visible(parent):
            raise OperationError("부모 메뉴가 숨겨졌거나 사용할 수 없습니다.", "menu_parent_unavailable")
        self.execution._handle(parent, snapshot)  # rejects synthetic/read-only handles
        return parent

    def _logical_child(self, snapshot):
        parent = self._parent(snapshot)
        candidates = [item for item in _find(snapshot, self.step["child_selector"])
                      if _descendant(item, parent, _elements(snapshot))]
        if len(candidates) > 1:
            raise OperationError("같은 부모 경로의 자식 메뉴가 여러 개입니다.", "menu_child_ambiguous")
        if not candidates:
            return None
        child = candidates[0]
        if child.get("role") != "MenuItem" or not _visible(child):
            # Hidden descendants may exist before expansion; disabled visible
            # commands are distinguished from a still-collapsed menu.
            if child.get("enabled") is False and child.get("visible") is not False and not child.get("is_offscreen", child.get("offscreen", False)):
                raise OperationError("자식 메뉴가 비활성화되어 있습니다.", "menu_child_disabled")
            return None
        self.execution._handle(child, snapshot)
        self.child_representation = "logical_uia_child"
        return dict(self.target)

    def _physical_child(self):
        from recording_windows import execute_window_wait
        wait = {"operation": "wait_for_window", "program_id": "menu", "owner_ref": "parent",
                "window_ref": "child", "timeout_ms": 0, **self.step["child_window"]}
        began = time.monotonic()
        try:
            answer = execute_window_wait(self.runtime, wait, {("menu", "parent"): self.owner},
                image_hint=self.step.get("child_image_target"), exclude_window_ids={self.target["window_id"]})
        finally:
            self.window_discovery_ms += (time.monotonic() - began) * 1000
        self.last = answer
        self._identity()
        if answer.get("task_verified") is not True:
            code = answer.get("diagnostic", {}).get("code", "menu_child_not_visible")
            if code in {"recording_window_missing"}:
                return None
            raise OperationError(answer.get("diagnostic", {}).get("message", "자식 메뉴를 확인하지 못했습니다."), code)
        target = answer.get("target")
        if not isinstance(target, dict) or target == self.target or answer.get("accessibility_scope") == "msaa_list":
            raise OperationError("별도 창 대신 논리 목록이 반환됐습니다. 실제 메뉴 계층을 다시 지정하세요.", "menu_child_representation_mismatch")
        child_identity = self._child_identity(target)
        self.child_representation = "owned_popup"
        # A common owner and a familiar child image do not prove which parent
        # menu opened it. This also runs on read-only resume/already-open paths.
        self._prove_physical_parent()
        if "child_selector" in self.step:
            snapshot = self._capture(target)
            child = _unique(snapshot, self.step["child_selector"])
            if child.get("role") != "MenuItem" or not _visible(child):
                raise OperationError("자식 메뉴가 숨겨졌거나 사용할 수 없습니다.", "menu_child_unavailable")
            self.execution._handle(child, snapshot)
        if "child_image_target" in self.step:
            from image_steps import execute_image_step
            began = time.monotonic()
            try:
                compared = execute_image_step(self.runtime, {"operation": "wait_for_image",
                    "image_target": self.step["child_image_target"], "timeout_ms": 0}, target)
            finally:
                self.child_image_ms += (time.monotonic() - began) * 1000
            self.last["child_image_match"] = compared
            if compared.get("task_verified") is not True:
                details = compared.get("diagnostic", {})
                reason = compared.get("image_match", details.get("image_match", {})).get("status")
                if reason == "ambiguous" or details.get("code") == "image_ambiguous":
                    raise OperationError("자식 메뉴 이미지가 중복됩니다.", "menu_child_ambiguous")
                raise OperationError("새 자식 메뉴의 저장 이미지가 일치하지 않습니다.", "menu_child_image_not_found")
        # Recheck this exact candidate's identity and relation without another
        # full image match or implicitly choosing a new replacement window.
        if self._child_identity(target) != child_identity:
            raise OperationError("확인 중 자식 메뉴가 닫히거나 바뀌었습니다.", "menu_child_changed")
        return target

    def _prove_physical_parent(self):
        if "selector" in self.step:
            self._parent(self._capture(self.target))
            return
        from image_steps import execute_image_step
        began = time.monotonic()
        try:
            observed = execute_image_step(self.runtime, {"operation": "wait_for_image",
                "image_target": self.step["image_target"], "timeout_ms": 0}, self.target)
        finally:
            self.parent_image_ms += (time.monotonic() - began) * 1000
        self.last["parent_image_match"] = observed
        self._identity()
        if observed.get("task_verified") is not True:
            diagnostic = observed.get("diagnostic", {})
            match = observed.get("image_match", diagnostic.get("image_match", {}))
            reason = "menu_parent_ambiguous" if match.get("status") == "ambiguous" or diagnostic.get("code") == "image_ambiguous" else "menu_parent_image_not_found"
            raise OperationError("현재 부모 메뉴가 저장한 대상과 일치하지 않습니다. 이미 열린 다른 메뉴를 사용하지 않았습니다.", reason)

    def _child_identity(self, target):
        from interaction import _identity
        from recording_windows import owned_by
        state = self._identity()
        identity = _identity(state, target["window_id"])
        rows = state.get("windows", [])
        row = next((r for r in rows if r.get("window_id") == target["window_id"]), {})
        related = owned_by(row, self.owner["window_id"], rows)
        if not related and callable(getattr(self.probe, "ownership_path", None)):
            related = bool(self.probe.ownership_path(target["window_id"], self.owner["window_id"]))
        if identity.get("pid") != self.target["pid"] or row.get("visible") is not True or not related:
            raise OperationError("자식 메뉴의 표시 상태 또는 소유 관계가 바뀌었습니다.", "menu_child_changed")
        return {"child": identity, "owner": _identity(state, self.owner["window_id"]),
                "geometry": {key: copy.deepcopy(row[key]) for key in ("bounds", "rect", "geometry") if key in row}}

    def _child(self):
        if "child_window" in self.step:
            return self._physical_child()
        return self._logical_child(self._capture(self.target))

    def _dispatch(self):
        method = self.step["expansion_method"]
        private_arguments = {}
        self._identity()
        if "selector" in self.step:
            snapshot = self._capture(self.target)
            parent = self._parent(snapshot)
            patterns = parent.get("patterns", {})
            expand = patterns.get("ExpandCollapse", {}) if isinstance(patterns, dict) else {}
            pattern_supported = "expand" in parent.get("actions", []) or isinstance(expand, dict) and expand.get("status") == "supported"
            capable = pattern_supported or (method != "accessibility" and any(
                e.get("role") == "MenuItem" and _descendant(e, parent, _elements(snapshot)) for e in _elements(snapshot)))
            if not capable:
                raise OperationError("이 부모 메뉴의 펼침 기능이 관찰되지 않았습니다. Invoke를 내보내기 명령으로 추정하지 않습니다.", "menu_parent_capability_unavailable")
            if method == "hover" and "image_target" in self.step:
                from inspection import _observed_rectangle
                geometry = _observed_rectangle(parent)
                if geometry is None:
                    raise OperationError("부모 MenuItem의 현재 물리 경계를 읽지 못했습니다. 이미지 좌표를 추정하지 않았습니다.", "menu_parent_geometry_unavailable")
                bounds = copy.deepcopy(geometry["bounds"])
                identity_fields = ("role", "name", "label", "automation_id", "runtime_id", "RuntimeId", "native_window_handle")
                parent_identity = {key: copy.deepcopy(parent[key]) for key in identity_fields if key in parent}
                def revalidate_parent():
                    current = self._parent(self._capture(self.target))
                    rectangle = _observed_rectangle(current)
                    if (rectangle is None or rectangle["bounds"] != bounds or
                            {key: current[key] for key in identity_fields if key in current} != parent_identity):
                        raise OperationError("승인 또는 이미지 확인 중 부모 메뉴 요소가 바뀌었습니다. 이동을 보내지 않았습니다.", "menu_parent_changed")
                    return copy.deepcopy(bounds)
                private_arguments.update(parent_bounds=bounds, parent_revalidate=revalidate_parent)
            if method in {"click", "keyboard"}:
                self._identity()
                self.delivery_state = "unknown"
                try:
                    self.execution._mutate("click" if method == "click" else "press_key", parent, snapshot,
                        **({"key": self.step["expansion_key"]} if method == "keyboard" else {}))
                except Exception:
                    if not self.execution.dispatched:
                        self.delivery_state = "not_sent"
                    raise
                self.delivery_state = "sent" if self.execution.dispatched else "not_sent"
                return
        if "image_target" in self.step and method in {"click", "keyboard"}:
            callback = getattr(self.runtime, "image_action", None)
            request = {"operation": "image_click" if method == "click" else "image_press_key",
                       "image_target": self.step["image_target"],
                       **({"key": self.step["expansion_key"]} if method == "keyboard" else {})}
            args = (request, self.target)
        else:
            callback = getattr(self.runtime, "expand_menu_once", None)
            args = (self.target, self.step)
        if not callable(callback):
            raise OperationError("현재 Guard에서 요청한 메뉴 전개 방식을 지원하지 않습니다.", "menu_expansion_backend_unavailable")
        self.delivery_state = "unknown"
        self.execution.dispatched = True
        self.execution.metrics["mutations"] += 1
        started = time.monotonic()
        try:
            answer = callback(*args, **private_arguments)
        finally:
            self.execution.metrics["action_ms"] += (time.monotonic() - started) * 1000
        self.expansion_result = answer
        data = answer.get("structuredContent", answer) if isinstance(answer, dict) else {}
        sent = data.get("input_dispatched", data.get("input_sent"))
        self.delivery_state = "sent" if sent is True else "not_sent" if sent is False else "unknown"
        self.execution.dispatched = sent is not False
        if sent is not True or not isinstance(answer, dict) or answer.get("isError") or data.get("status") in {"unknown", "failed", "blocked"}:
            diagnostic = data.get("diagnostic", {})
            raise OperationError(diagnostic.get("message", "메뉴 전개 입력의 전달을 확인하지 못했습니다. 반복하지 않았습니다."),
                                 diagnostic.get("code", "menu_expansion_input_unknown" if sent is not False else "menu_expansion_not_sent"))

    def _result(self, status, code, message):
        result = self.execution._result(status, code, message)
        result.update(child_representation=self.child_representation, business_result_verified=False,
                      verification_scope="menu_child_visibility_only", step_delivery=self.delivery_state,
                      input_dispatched=True if self.delivery_state == "sent" else False if self.delivery_state == "not_sent" else None)
        result["menu_expansion"] = {"stage": "child_visible" if self.child_target else "child_observation" if self.execution.dispatched else "parent_observation",
            "expansion_method": self.step["expansion_method"], "verify_only": self.verify_only,
            "menu_depth": self.step.get("menu_depth", 1),
            "menu_path": self.step.get("menu_path", [s["name"] for s in (self.step.get("selector", {}), self.step.get("child_selector", {})) if s.get("name")]),
            "child_representation": self.child_representation, "expansion_delivery": self.delivery_state,
            "child_command_delivery": "not_sent", "business_result_verified": False,
            "observation": self.last, "input_result": self.expansion_result}
        result["metrics"].update(menu_native_reads=self.native_reads,
            menu_native_read_ms=round(self.native_read_ms, 2),
            menu_window_discovery_ms=round(self.window_discovery_ms, 2),
            menu_parent_image_ms=round(self.parent_image_ms, 2),
            menu_child_image_ms=round(self.child_image_ms, 2))
        if self.child_target:
            result["child_target"] = dict(self.child_target)
        return result

    def run(self):
        try:
            if "image_target" in self.step and self.step["expansion_method"] == "keyboard":
                # image_press_key includes a focus click. In a menu that extra
                # click may expand the parent before ENTER invokes a leaf.
                # Only a fresh selector can use the existing key-only route.
                raise OperationError("이미지 메뉴의 키 입력은 먼저 클릭할 수 있어 전개 방식으로 지원하지 않습니다. 검증된 요소의 키 전개 또는 호버를 지정하세요.", "menu_expand_method_unsupported")
            factory = getattr(self.runtime, "create_transition_probe", None)
            if not callable(factory):
                raise OperationError("메뉴 창의 수명을 증명할 수 없습니다.", "menu_identity_unavailable")
            self.probe = factory(self.target)
            self.probe.capture()
            self._identity()
            from transient_popup import proven_popup, popup_owner
            if proven_popup(self.target):
                self.owner = popup_owner(self.target)
            self.child_target = self._child()
            if self.child_target:
                return self._result("verified", "menu_already_expanded", "현재 부모 경로의 자식 메뉴를 확인했습니다. 추가 입력은 보내지 않았습니다.")
            if not self.verify_only:
                self._dispatch()
            deadline = min(self.execution.deadline, time.monotonic() + self.step.get("verification_timeout_ms", 1500) / 1000)
            while True:
                self.execution._remaining()
                self.child_target = self._child()
                if self.child_target:
                    if self.step.get("verification_timeout_ms", 1500) and time.monotonic() >= deadline:
                        raise OperationError("메뉴 확인 결과가 제한 시간 이후 도착했습니다.", "menu_child_timeout")
                    return self._result("verified", "menu_child_visible", "펼쳐진 자식 메뉴를 새로 확인했습니다. 최종 명령은 별도 단계에서 실행합니다.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OperationError("제한 시간 안에 자식 메뉴를 확인하지 못했습니다. 부모 입력을 반복하지 않았습니다.", "menu_child_timeout")
                self.runtime.stop_event.wait(min(remaining, self.step.get("poll_interval_ms", 100) / 1000))
        except Exception as error:
            return self._result("unknown" if self.execution.dispatched else "failed",
                                getattr(error, "code", "menu_expansion_failed"), str(error)[:1000])
        finally:
            if self.probe is not None:
                self.probe.close()


def execute_menu_expansion(runtime, step, target, *, delivery_mode="background", verify_only=False):
    step = validate_menu_expansion(step)
    if not isinstance(target, dict) or set(target) != {"pid", "window_id"} or any(type(v) is not int or v < 1 for v in target.values()):
        raise OperationError("승인된 현재 메뉴 창을 지정하세요.", "invalid_target")
    if runtime.mode != "uia":
        raise OperationError("메뉴 전개는 UIA 세션에서 실행하세요.", "unsupported_mode")
    return _MenuExpansion(runtime, step, target, delivery_mode, verify_only).run()


def verify_menu_expansion(runtime, step, target):
    """Resume only the postcondition; never redispatch the recorded expansion."""
    return execute_menu_expansion(runtime, step, target, verify_only=True)


def local_menu_summary(raw):
    """Bounded local UI projection, never a public receipt or replay input."""
    if not isinstance(raw, dict):
        return {}
    last = raw.get('last_result')
    last = last if isinstance(last, dict) else {}
    source = raw if isinstance(raw.get('menu_expansion'), dict) else last
    menu = source.get('menu_expansion')
    visible = {}
    if isinstance(menu, dict):
        # Captures, complete UIA trees and nested Driver input results can be
        # huge. The form needs these discrete states, not the full observation.
        allowed = {
            'stage': {'parent_observation', 'child_observation', 'child_visible'},
            'expansion_method': {'hover', 'click', 'keyboard', 'accessibility'},
            'child_representation': {'hwnd', 'logical', 'owned_popup', 'logical_uia_child', 'unknown'},
            'expansion_delivery': {'not_sent', 'sent', 'unknown'},
            'child_command_delivery': {'not_sent', 'sent', 'unknown'},
        }
        projected = {key: menu[key] if menu.get(key) in values else 'unknown' for key, values in allowed.items()
                     if isinstance(menu.get(key), str)}
        if type(menu.get('menu_depth')) is int and 1 <= menu['menu_depth'] <= 16:
            projected['menu_depth'] = menu['menu_depth']
        path = menu.get('menu_path')
        if isinstance(path, list):
            projected['menu_path'] = [item.replace('\r', ' ').replace('\n', ' ')[:256]
                                      for item in path[:16] if isinstance(item, str)]
        for key in ('verify_only', 'business_result_verified'):
            if type(menu.get(key)) is bool:
                projected[key] = menu[key]
        visible.update(menu_expansion=projected, pending_operation='expand_menu')
        if type(source.get('input_dispatched')) is bool or source.get('input_dispatched') is None:
            visible['input_dispatched'] = source.get('input_dispatched')
        if type(source.get('business_result_verified')) is bool:
            visible['business_result_verified'] = source['business_result_verified']
    if not isinstance(menu, dict) and raw.get('status') == 'needs_review':
        checkpoint = raw.get('checkpoint')
        visual = raw.get('visual_review')
        if isinstance(checkpoint, dict) and checkpoint:
            mode = checkpoint.get('review_mode', 'human')
            visible.update(pending_operation='checkpoint', review_kind=mode if mode in {'human', 'visual', 'popup_image'} else 'unknown')
        elif isinstance(visual, dict) and visual:
            visible.update(pending_operation='checkpoint', review_kind='visual')
    return visible
