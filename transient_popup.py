"""Narrow, run-scoped fallback for a proven HWND omitted by the Driver.

No public coordinate API. Guard owns policy, approvals, budgets and journaling.
The only mutation is a single image-verified left click after exact non-delivery.
"""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import copy
import ctypes
from ctypes import wintypes as W
import json
import re
import threading

from operations import OperationError

_scope = ContextVar("recorded_popup_scope", default=None)
_render_lock = threading.Lock()


@contextmanager
def recorded_window_scope(task):
    classes = set()
    def collect(steps):
        for step in steps:
            if step.get("operation") == "wait_for_window":
                classes.add(step["class_name"])
            if isinstance(step.get("steps"), list):
                collect(step["steps"])
    collect(task.get("steps", []))
    token = _scope.set({"classes": classes, "bindings": {}, "failures": {}})
    try:
        yield
    finally:
        _scope.reset(token)


def _allowed_class(value, declarations):
    from recording_windows import popup_class_matches
    return (value == "ComboLBox"
            or re.fullmatch(r"WindowsForms10\.Window\.(?:808|20808)\.app\.0\.[0-9a-fA-F]+_r\d+_ad\d+", value) is not None
            or any(popup_class_matches(name, value) for name in declarations))

def proven_popup(target):
    """Read-only query; declarations alone never establish window identity."""
    try:
        _binding(target)
        return True
    except (OperationError, OSError, RuntimeError):
        return False


def exact_missing(answer, hwnd):
    if answer.get("isError") is not True:
        return False
    candidates = [answer.get("structuredContent", {})]
    texts = []
    for item in answer.get("content", []):
        if item.get("type") == "text":
            try: candidates.append(json.loads(item["text"]))
            except (ValueError, TypeError):
                if isinstance(item.get("text"), str): texts.append(item["text"])
    for data in list(candidates):
        if isinstance(data, dict):
            candidates.extend(data.get(key) for key in ("error", "diagnostic") if isinstance(data.get(key), dict))
    for data in candidates:
        if not isinstance(data, dict): continue
        # A missing-HWND sentence cannot override contradictory delivery or
        # refusal evidence. Explicit unknown delivery is not non-delivery.
        delivery = data.get("delivery", {})
        if (any(key in data and data[key] is not False for key in ("input_sent", "input_dispatched"))
                or data.get("dispatch") in ("sent", "unknown")
                or data.get("status") == "unknown"
                or any(data.get(key) not in (None, "target_unavailable") for key in ("diagnostic_code", "error_code"))
                or data.get("code") not in (None, "tool_invocation_failed", "target_unavailable")
                or isinstance(delivery, dict) and (delivery.get("state") in ("sent", "unknown")
                    or type(delivery.get("delivered_count")) is int and delivery["delivered_count"] > 0)):
            return False
    pattern = (r"No window with window_id " + str(hwnd) +
               r" exists\.(?: Call `?list_windows\(\{\"pid\":\s*\d+\}\)`? for candidates\.)?")
    for data in candidates:
        if not isinstance(data, dict): continue
        messages = [data["message"]] if "message" in data else texts
        if (data.get("code") == "tool_invocation_failed"
                and any(isinstance(message,str) and re.fullmatch(pattern,message) for message in messages)):
            return True
    return False


def _user():
    user = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetWindowRect": ([W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
        "GetForegroundWindow": ([], W.HWND),
        "IsWindowVisible": ([W.HWND], W.BOOL),
        "IsWindowEnabled": ([W.HWND], W.BOOL),
        "GetWindowThreadProcessId": ([W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
        "GetClassNameW": ([W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
        "ScreenToClient": ([W.HWND, ctypes.POINTER(W.POINT)], W.BOOL),
        "ChildWindowFromPointEx": ([W.HWND, W.POINT, W.UINT], W.HWND),
        "SendMessageTimeoutW": ([W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                ctypes.POINTER(ctypes.c_size_t)], ctypes.c_size_t),
        "GetWindowDC": ([W.HWND], W.HDC),
        "ReleaseDC": ([W.HWND, W.HDC], ctypes.c_int),
        "PrintWindow": ([W.HWND, W.HDC, W.UINT], W.BOOL),
        "SetThreadDpiAwarenessContext": ([W.HANDLE], W.HANDLE),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(user, name); fn.argtypes = args; fn.restype = result
    return user


def _snapshot(target, owner, declarations):
    user = _user()
    previous = user.SetThreadDpiAwarenessContext(W.HANDLE(-4))
    try:
        return _physical_snapshot(target, owner, declarations)
    finally:
        if previous: user.SetThreadDpiAwarenessContext(previous)


def _physical_snapshot(target, owner, declarations):
    from closing import NativeClosureProbe
    if target["pid"] != owner["pid"] or target["window_id"] == owner["window_id"]:
        raise OperationError("팝업의 원래 창이 다릅니다.", "popup_owner_mismatch")
    probe = NativeClosureProbe(owner["pid"], owner["window_id"])
    try:
        state = probe.capture()
        path = probe.ownership_path(target["window_id"], owner["window_id"])
        row = probe._window(target["window_id"])
        user = _user()
        foreground = int(user.GetForegroundWindow() or 0)
        related_foreground = foreground in (target["window_id"], owner["window_id"])
        if not related_foreground:
            related_foreground = bool(probe.ownership_path(foreground, owner["window_id"]))
        for failed, code in ((not path, "popup_owner_mismatch"),
                (not row["visible"], "popup_not_visible"), (not row["enabled"], "popup_disabled"),
                (row["minimized"], "popup_minimized"), (not related_foreground, "popup_foreground_mismatch"),
                (not _allowed_class(row["class_name"], declarations), "popup_class_unsupported")):
            if failed:
                raise OperationError("팝업 증명을 완료하지 못했습니다: " + code, code)
        rect = W.RECT()
        if not user.GetWindowRect(target["window_id"], ctypes.byref(rect)):
            raise OperationError("팝업 크기를 확인하지 못했습니다.", "popup_geometry_unavailable")
        bounds = (rect.left, rect.top, rect.right, rect.bottom)
        if not (0 < rect.right-rect.left <= 4096 and 0 < rect.bottom-rect.top <= 4096):
            raise OperationError("팝업 캡처 범위를 벗어났습니다.", "popup_geometry_unavailable")
        return {"process": (state["creation_time"], state["executable"]), "path": path,
                "window": row, "bounds": bounds, "foreground": foreground}
    finally:
        probe.close()


def register_popup(target, owner, *, expected_proof=None):
    scope = _scope.get()
    if scope is None: return
    try:
        proof = _snapshot(target, owner, scope["classes"])
        if expected_proof is not None and any(proof.get(key) != expected_proof.get(key)
                for key in ("process", "path", "window", "bounds")):
            raise OperationError("확인하던 팝업의 수명이나 소유 경로가 바뀌었습니다.", "popup_identity_changed")
    except (OperationError, OSError, RuntimeError) as error:
        scope["failures"][(target["pid"], target["window_id"])] = {
            "code": getattr(error, "code", "popup_native_probe_failed"), "message": str(error)[:500]}
        scope["bindings"].pop((target["pid"], target["window_id"]), None)
        # Driver-supported windows still work. Missing native proof only
        # disables this optional fallback, never weakens its validation.
        return False
    scope["failures"].pop((target["pid"], target["window_id"]), None)
    scope["bindings"][(target["pid"], target["window_id"])] = {
        "target": dict(target), "owner": dict(owner), "proof": proof}
    return True


def _binding(target):
    scope = _scope.get()
    binding = scope and scope["bindings"].get((target["pid"], target["window_id"]))
    if not binding:
        raise OperationError("이번 실행에서 증명한 팝업 연결이 없습니다.", "popup_scope_unverified")
    current = _snapshot(target, binding["owner"], scope["classes"])
    # Foreground can move between proven sibling menus; identity and owner path
    # cannot. Each snapshot independently verifies the foreground relationship.
    if any(current[key] != binding["proof"][key] for key in ("process", "path", "window", "bounds")):
        raise OperationError("녹화 팝업의 수명이나 소유 경로가 바뀌었습니다.", "popup_identity_changed")
    return binding, current


def _bounded_render(function, timeout=2):
    if not _render_lock.acquire(blocking=False):
        raise OperationError("이전 팝업 렌더가 아직 끝나지 않았습니다.", "popup_render_busy")
    done, result = threading.Event(), {}
    context = copy_context()
    def work():
        try: result["value"] = context.run(function)
        except Exception as error: result["error"] = error
        finally: _render_lock.release(); done.set()
    threading.Thread(target=work, daemon=True, name="owned-popup-render").start()
    if not done.wait(timeout):
        raise OperationError("팝업 캡처 시간이 초과됐습니다.", "popup_render_timeout")
    if "error" in result: raise result["error"]
    return result["value"]


def _print_window(hwnd, bounds):
    from image_pixels import encode_png
    user = _user()
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    for name, args, result in [
        ("CreateCompatibleDC", [W.HDC], W.HDC),
        ("CreateCompatibleBitmap", [W.HDC, ctypes.c_int, ctypes.c_int], W.HBITMAP),
        ("SelectObject", [W.HDC, W.HANDLE], W.HANDLE),
        ("DeleteObject", [W.HANDLE], W.BOOL), ("DeleteDC", [W.HDC], W.BOOL),
        ("GetDIBits", [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p, W.UINT], ctypes.c_int)]:
        fn = getattr(gdi, name); fn.argtypes = args; fn.restype = result
    width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
    dc = user.GetWindowDC(hwnd); memory = bitmap = old = None
    try:
        if not dc: raise OSError("GetWindowDC failed")
        memory = gdi.CreateCompatibleDC(dc)
        bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
        if not memory or not bitmap: raise OSError("GDI allocation failed")
        old = gdi.SelectObject(memory, bitmap)
        if not user.PrintWindow(hwnd, memory, 2): raise OSError("PrintWindow failed")
        gdi.SelectObject(memory, old); old = None
        class Header(ctypes.Structure):
            _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                        ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                        ("image", W.DWORD), ("xp", W.LONG), ("yp", W.LONG),
                        ("used", W.DWORD), ("important", W.DWORD)]
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width*height*4)
        if gdi.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise OSError("GetDIBits failed")
        raw = pixels.raw
        rows = [bytes(component for index in range(y*width*4, (y+1)*width*4, 4)
                      for component in (raw[index+2], raw[index+1], raw[index])) for y in range(height)]
        # A uniform frame cannot prove a selectable image target.
        if len(set(b"".join(rows)[::3])) < 2 and len(set(b"".join(rows)[1::3])) < 2:
            raise OperationError("빈 팝업 프레임을 거부했습니다.", "popup_blank_frame")
        return encode_png(width, height, 2, rows)
    finally:
        if old: gdi.SelectObject(memory, old)
        if bitmap: gdi.DeleteObject(bitmap)
        if memory: gdi.DeleteDC(memory)
        if dc: user.ReleaseDC(hwnd, dc)


def capture(target, geometry):
    def render():
        user = _user()
        previous = user.SetThreadDpiAwarenessContext(W.HANDLE(-4))
        try:
            _, before = _binding(target)
            png = _print_window(target["window_id"], before["bounds"])
            _, after = _binding(target)
            if before != after:
                raise OperationError("렌더 중 팝업이 바뀌었습니다.", "popup_identity_changed")
            from image_pixels import crop_png
            b = before["bounds"]; g = geometry
            if not (b[0] <= g[0] < g[2] <= b[2] and b[1] <= g[1] < g[3] <= b[3]):
                raise OperationError("팝업 좌표계가 캡처와 다릅니다.", "popup_geometry_mismatch")
            if tuple(g[:4]) != b:
                png = crop_png(png, {"x":g[0]-b[0], "y":g[1]-b[1],
                                     "width":g[2]-g[0], "height":g[3]-g[1]})
            return {"structuredContent": {**target, "screenshot_width": g[2]-g[0],
                        "screenshot_height": g[3]-g[1], "backend": "owned-popup-printwindow",
                        "input_dispatched": False, "task_verified": False},
                    "content": [{"type":"image", "mimeType":"image/png", "data":png}]}
        finally:
            if previous: user.SetThreadDpiAwarenessContext(previous)
    return _bounded_render(render)


def click(target, point, geometry, check):
    """Guard has already approved and image-matched this one final click."""
    check()
    import os
    from privileges import input_integrity_preflight
    blocked = input_integrity_preflight(os.getpid(), target["pid"])
    if blocked:
        raise OperationError(blocked["message"], "elevation_required")
    _, before = _binding(target)
    user = _user()
    previous = user.SetThreadDpiAwarenessContext(W.HANDLE(-4))
    try:
        if (any(type(point.get(key)) is not int for key in ("x", "y"))
                or not 0 <= point["x"] < geometry[2]-geometry[0]
                or not 0 <= point["y"] < geometry[3]-geometry[1]):
            raise OperationError("이미지로 증명한 내부 좌표가 아닙니다.", "popup_geometry_mismatch")
        physical = W.POINT(geometry[0]+point["x"], geometry[1]+point["y"])
        parent_point = W.POINT(physical.x, physical.y)
        if not user.ScreenToClient(target["window_id"], ctypes.byref(parent_point)):
            raise OperationError("팝업 좌표를 변환하지 못했습니다.", "popup_geometry_mismatch")
        child = int(user.ChildWindowFromPointEx(target["window_id"], parent_point, 7) or target["window_id"])
        pid = W.DWORD()
        thread = user.GetWindowThreadProcessId(child, ctypes.byref(pid))
        if pid.value != target["pid"] or not thread or not user.IsWindowEnabled(child):
            raise OperationError("팝업의 입력 자식을 확인하지 못했습니다.", "popup_child_mismatch")
        from closing import NativeClosureProbe
        probe = NativeClosureProbe(target["pid"], target["window_id"])
        try:
            probe.capture()
            path = probe.ownership_path(child, target["window_id"])
            if not path: raise OperationError("팝업 입력 자식이 연결되지 않습니다.", "popup_child_mismatch")
            local = W.POINT(physical.x, physical.y)
            if not user.ScreenToClient(child, ctypes.byref(local)) or not 0 <= local.x < 32768 or not 0 <= local.y < 32768:
                raise OperationError("팝업 입력 좌표가 범위를 벗어났습니다.", "popup_geometry_mismatch")
            check()
            if _binding(target)[1] != before or probe.ownership_path(child, target["window_id"]) != path:
                raise OperationError("입력 직전에 팝업이 바뀌었습니다.", "popup_identity_changed")
            value = ctypes.c_size_t()
            packed = local.x | (local.y << 16)
            for message, flags in ((0x201, 1), (0x202, 0)):
                if not user.SendMessageTimeoutW(child, message, flags, packed, 0x22, 500, ctypes.byref(value)):
                    return {"isError":True, "structuredContent":{"backend":"owned-popup-messages",
                        "input_sent":None, "task_verified":False, "code":"popup_input_unknown",
                        "message":"메시지 전달 결과가 불명확합니다. 자동 재시도하지 않습니다."}}
            return {"structuredContent":{"backend":"owned-popup-messages", "input_sent":True,
                    "task_verified":False}, "content":[]}
        finally: probe.close()
    finally:
        if previous: user.SetThreadDpiAwarenessContext(previous)


def popup_diagnostic(target):
    """Report native proof separately from a Driver discovery error."""
    scope = _scope.get()
    key = (target["pid"], target["window_id"])
    if scope and key in scope["failures"]:
        return dict(scope["failures"][key])
    try:
        _binding(target)
        return {"code": "popup_proven"}
    except (OperationError, OSError, RuntimeError) as error:
        return {"code": getattr(error, "code", "popup_native_probe_failed"), "message": str(error)[:500]}


def popup_owner(target):
    binding, _ = _binding(target)
    return dict(binding["owner"])


def popup_proof(target):
    """Retain identity for observation review across fresh run scopes, not authority."""
    _, current = _binding(target)
    return copy.deepcopy(current)


def current_popup(owner, runtime=None):
    scope = _scope.get()
    candidates = []
    factory = getattr(runtime, "create_transition_probe", None)
    if scope is not None and callable(factory):
        probe = factory(dict(owner))
        try:
            state = probe.capture()
            # Hosted dropdowns can be child HWNDs that EnumWindows omits.
            # Capture establishes the original owner handle; the transition
            # snapshot expands discovery without granting input authority.
            snapshot = getattr(probe, "transition_snapshot", None)
            if callable(snapshot):
                initial = state
                state = snapshot()
                if (state.get("process_exited") or not state.get("target_present")
                        or any(state.get(key) != initial.get(key) for key in ("creation_time", "executable"))):
                    raise OperationError("팝업 관찰 중 원래 프로그램이나 창이 바뀌었습니다.", "popup_owner_changed")
            for row in state.get("windows", []):
                if (row.get("pid") != owner["pid"] or row.get("window_id") == owner["window_id"]
                        or row.get("visible") is not True or not _allowed_class(row.get("class_name", ""), scope["classes"])):
                    continue
                if not probe.ownership_path(row["window_id"], owner["window_id"]): continue
                candidate = {"pid":owner["pid"],"window_id":row["window_id"]}
                if not register_popup(candidate, owner):
                    cause = popup_diagnostic(candidate)
                    raise OperationError(cause.get("message", "팝업 관찰 증명이 실패했습니다."), cause["code"])
        finally:
            probe.close()
    for binding in list((scope or {}).get("bindings", {}).values()):
        if binding["owner"] == owner and proven_popup(binding["target"]):
            candidates.append(dict(binding["target"]))
    if len(candidates) > 1:
        raise OperationError("관련 팝업이 여러 개입니다. 창 참조를 지정하세요.", "popup_observation_ambiguous")
    return candidates[0] if candidates else None


def window_exists(hwnd):
    user = _user()
    user.IsWindow.argtypes = [W.HWND]
    user.IsWindow.restype = W.BOOL
    return bool(user.IsWindow(hwnd))
