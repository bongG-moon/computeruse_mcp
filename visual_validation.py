"""Synthetic native editor/image/recording acceptance through the actual MCP.

--run opens only freshly compiled owned fixtures and helpers. Injected input is
test automation, never evidence of human recording or actual workplace support.
Run serially with every other foreground GUI acceptance. No LLM or downloads.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from live_validation import Client, DRIVER, decoded, safe
from picker_validation import Native
from process_validation import wait
from settings import save_config
from visual_fixture import compile_fixture

HERE = Path(__file__).resolve().parent
EDITOR = "Computer Use MCP 프로세스 만들기.exe"
VISUAL = "Computer Use MCP 이미지 도구.exe"


def run(folder, driver, bundle=None):
    folder = folder.resolve(); root = bundle.resolve() if bundle else HERE
    if folder.exists(): raise ValueError("Use a fresh validation folder")
    folder.mkdir(parents=True)
    exe = compile_fixture(folder / "fixture")
    receipt_path = folder / "receipt.json"
    save_config(folder / "config.json", {"version": 1, "driver": str(driver.resolve()), "state_dir": str(folder / "state"),
        "programs": [{"id": "painted", "name": "합성 이미지 시험", "exe": str(exe), "control_exes": [], "hints": "Owned fixture only", "enabled": True}],
        "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 15, "max_actions": 120,
        "approval_timeout_seconds": 300, "observation_timeout_seconds": 20})
    report = {"passed": False, "synthetic_fixture": True, "external_llm_used": False, "human_recording_tested": False,
        "automated_helper_selection": True, "automated_checkpoint_acknowledgement": True,
        "private_application_used": False, "administrator_gui_tested": False, "packaged": bundle is not None, "scenarios": {}}
    from build_portable import source_files
    paths = [root / name for name in source_files() if name.endswith((".py", ".cs"))]
    paths += [root / EDITOR, root / VISUAL]
    paths += [root / name for name in ("ProcessEditor.cs", "VisualTools.cs", "BUILD-MANIFEST.json") if (root / name).is_file()]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    report["tested_build"] = {"sha256": hashes, "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "driver_sha256": hashlib.sha256(driver.read_bytes()).hexdigest(), "fixture_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
    if (root / "BUILD-MANIFEST.json").is_file():
        (folder / "tested-BUILD-MANIFEST.json").write_bytes((root / "BUILD-MANIFEST.json").read_bytes())
    native = Native(); helpers = []; processes = []; records = []; client = None; run_dir = None
    native.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    native.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    native.user.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]

    def write():
        (folder / "visual-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "visual-steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def call(name, args=None, raw=False, allow_error=False):
        answer = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        records.append({"tool": name, "result": safe(answer)}); write()
        if answer.get("isError") and not allow_error:
            raise AssertionError(name + ": " + json.dumps(safe(decoded(answer)), ensure_ascii=False)[:3000])
        return answer if raw else decoded(answer)

    def connect():
        if bundle:
            from build_portable import runtime_environment
            return Client(folder / "config.json", root / "server.py", root / "runtime/python.exe", runtime_environment(root / "runtime"))
        return Client(folder / "config.json", root / "server.py")

    def spawn(suffix):
        title = "Synthetic visual acceptance " + folder.name + " " + suffix
        child = subprocess.Popen([str(exe), title, str(receipt_path)], cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(child)
        rows = wait(lambda: [r for r in native.windows(child.pid) if r["text"] == title and r["visible"]])
        assert len(rows) == 1
        return child, {"pid": child.pid, "window_id": rows[0]["hwnd"]}

    def begin():
        nonlocal run_dir
        answer = call("computer_begin", {"program_ids": ["painted"], "task_description": "Synthetic image authoring and recording acceptance"})
        run_dir = Path(answer["run_dir"]); run_dir.resolve().relative_to(folder / "state/runs")

    def receipt():
        def read():
            try: return json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError): return None
        result = wait(read); assert result["synthetic_fixture"] is True; return result

    def activate(target):
        hwnd = target["window_id"]; assert native.pid(hwnd) == target["pid"]
        if native.user.GetForegroundWindow() == hwnd:
            return
        native.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)
        fg = native.user.GetForegroundWindow(); thread = native.kernel.GetCurrentThreadId()
        other = native.user.GetWindowThreadProcessId(fg, None)
        attached = other != thread and bool(native.user.AttachThreadInput(thread, other, True))
        try: native.user.SetForegroundWindow(hwnd)
        finally:
            if attached: native.user.AttachThreadInput(thread, other, False)
        wait(lambda: native.user.GetForegroundWindow() == hwnd)

    def client_point(target, x, y):
        assert native.pid(target["window_id"]) == target["pid"]
        point = wintypes.POINT(x, y); assert native.user.ClientToScreen(target["window_id"], ctypes.byref(point))
        return point

    def owned_click(target, x, y):
        activate(target); time.sleep(.35)  # Let the recorder refresh its pre-action frame after activation.
        point = client_point(target, x, y)
        assert native.user.SetCursorPos(point.x, point.y)
        assert native.user.GetAncestor(native.user.WindowFromPoint(point), 2) == target["window_id"]
        assert native.user.GetForegroundWindow() == target["window_id"]
        native.user.mouse_event(2, 0, 0, 0, 0); native.user.mouse_event(4, 0, 0, 0, 0)

    def controls(helper):
        if native.pid(helper["hwnd"]) != helper["pid"]:
            rows = [row for row in native.windows(helper["pid"]) if row["visible"]]
            if not rows and not native.wait_exited(helper, 0):
                return []  # Owned picker is briefly hidden during capture/DPI recreation.
            if len(rows) != 1:
                raise AssertionError({"owned_helper_window_unavailable": helper["pid"], "windows": rows})
            # WinForms may recreate its HWND after its first per-monitor DPI
            # transition. The retained process handle still pins ownership.
            helper["hwnd"] = rows[0]["hwnd"]
            report.setdefault("helper_hwnd_recreated", []).append(helper["pid"])
        return native.windows(helper["pid"], helper["hwnd"])

    def status(editor_id, count=None, saved=False):
        def check():
            state = call("computer_process_status", {"editor_id": editor_id, "wait_ms": 100}, allow_error=True)
            if state.get("status") in {"failed", "cancelled"}: raise AssertionError(state)
            return state if (saved and state["status"] == "saved" and state.get("pending") is False) or (count is not None and len(state["steps"]) == count) else None
        return wait(check, 40)

    def recording_status(editor_id, expected):
        def check():
            state = call("computer_process_status", {"editor_id": editor_id, "wait_ms": 100}, allow_error=True)
            progress = state.get("recording", {})
            if state.get("status") in {"failed", "cancelled"} or progress.get("state") == "failed":
                raise AssertionError(state)
            return progress if progress.get("state") == expected else None
        progress = wait(check, 20)
        assert progress.get("task_verified") is False and progress.get("hover_is_action") is False, progress
        report.setdefault("recording_progress", []).append(progress)
        return progress

    def acknowledge_recording_warnings(edit, editor_id):
        state = call("computer_process_status", {"editor_id": editor_id, "wait_ms": 100})
        review = state.get("recording_review", {})
        if not review.get("partial"):
            return {"present": False, "synthetic_ack_only": False}
        assert review.get("warning_codes") and review.get("acknowledged") is False, review
        save_button = next(r for r in controls(edit) if r["text"] == "프로세스 저장")
        assert not save_button["enabled"], "Unacknowledged partial recording must not be saveable"
        native.click_helper(edit, "누락 경고 확인 · 저장 전 필수")
        dialog = wait(lambda: next((r for r in native.windows(edit["pid"]) if r["visible"] and r["text"] == "부분 기록과 누락 내용 확인"), None))
        dialog_helper = {**edit, "hwnd": dialog["hwnd"]}
        native.capture_helper(dialog_helper, folder / "partial-recording-warning-review.png")
        native.click_helper(dialog_helper, "누락 내용을 확인했습니다")
        def acknowledged():
            current = call("computer_process_status", {"editor_id": editor_id, "wait_ms": 100}).get("recording_review", {})
            return current if current.get("acknowledged") is True else None
        accepted = wait(acknowledged)
        assert accepted["partial"] is True and accepted["warning_codes"] == review["warning_codes"]
        return {"present": True, "synthetic_ack_only": True, "warning_codes": accepted["warning_codes"], "save_disabled_before_ack": True}

    def editor(target, name):
        started = call("computer_process_editor", {"targets": [{"program_id": "painted", **target}], "name": name, "timeout_seconds": 600})
        helper = native.ready_helper(started, root / EDITOR); helpers.append(helper)
        return helper, started["editor_id"]

    def nested(editor_helper, label):
        before = set((run_dir / "visual").glob("*.ready.json"))
        native.click_helper(editor_helper, label)
        def ready():
            for path in (run_dir / "visual").glob("*.ready.json"):
                if path not in before:
                    try: return json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError): pass
        helper = native.ready_helper(wait(ready), root / VISUAL); helpers.append(helper)
        return helper

    def select_row(helper, index, double=False):
        view = next(r for r in controls(helper) if "SYSLISTVIEW32" in r["class"].upper())
        header = next(r for r in native.windows(helper["pid"], view["hwnd"]) if "HEADER" in r["class"].upper())
        y = header["bounds"][3] - view["bounds"][1] + 12
        activate({"pid": helper["pid"], "window_id": helper["hwnd"]})
        point = wintypes.POINT(view["bounds"][0]+25, view["bounds"][1]+y)
        assert native.user.SetCursorPos(point.x, point.y)
        assert native.user.GetAncestor(native.user.WindowFromPoint(point), 2) == helper["hwnd"]
        for _ in range(2 if double and index == 0 else 1):
            native.user.mouse_event(2, 0, 0, 0, 0); native.user.mouse_event(4, 0, 0, 0, 0); time.sleep(.07)
        if not double or index:
            # Real key events establish ListView keyboard focus, unlike direct
            # WM_KEYDOWN which may leave WinForms' focused item unchanged.
            for key in [0x24] + [0x28] * index:
                assert native.user.GetForegroundWindow() == helper["hwnd"]
                native.user.keybd_event(key, 0, 0, 0); native.user.keybd_event(key, 0, 2, 0); time.sleep(.08)
            wait(lambda: native.user.SendMessageW(view["hwnd"], 0x100C, -1, 2) == index)
            if double:
                # Enter opens the selected row preview in the editor itself.
                assert native.user.GetForegroundWindow() == helper["hwnd"]
                native.user.keybd_event(0x0D, 0, 0, 0); native.user.keybd_event(0x0D, 0, 2, 0)

    def crop(picker, target, area=(30, 130, 680, 205), click_at=(610, 167)):
        native.click_helper(picker, "대상 창을 가져와 캡처")
        wait(lambda: native.user.IsWindowVisible(picker["hwnd"]))
        wait(lambda: any("찾을 부분을" in row["text"] for row in controls(picker)))
        def ready_boxes():
            boxes = [r for r in controls(picker) if "WINDOW" in r["class"].upper() and r["visible"] and not r["text"] and r["bounds"][3]-r["bounds"][1] > 200]
            boxes = sorted(boxes, key=lambda r: r["bounds"][3]-r["bounds"][1])[:2]
            boxes = sorted(boxes, key=lambda r: r["bounds"][0])
            return boxes if len(boxes) == 2 and boxes[0]["bounds"][1] == boxes[1]["bounds"][1] else None
        left, right = wait(ready_boxes)
        window = next(r for r in native.windows(target["pid"]) if r["hwnd"] == target["window_id"])["bounds"]
        origin = client_point(target, 0, 0); ox, oy = origin.x-window[0], origin.y-window[1]
        ww, wh = window[2]-window[0], window[3]-window[1]
        rect = wintypes.RECT(); native.user.GetClientRect(left["hwnd"], ctypes.byref(rect)); pw, ph = rect.right, rect.bottom
        scale = min(pw/ww, ph/wh); dx, dy = (pw-int(ww*scale))//2, (ph-int(wh*scale))//2
        a = (dx+round((ox+area[0])*scale), dy+round((oy+area[1])*scale))
        b = (dx+round((ox+area[2])*scale), dy+round((oy+area[3])*scale))
        def mouse(hwnd, message, xy, flags=0):
            assert native.pid(hwnd) == picker["pid"]
            native.user.SendMessageW(hwnd, message, flags, (xy[1] << 16) | xy[0])
        mouse(left["hwnd"], 0x201, a, 1); mouse(left["hwnd"], 0x200, b, 1); mouse(left["hwnd"], 0x202, b)
        # The context crop includes the row label; anchor specifically targets
        # its identical-looking button, not the center of the whole row.
        native.user.GetClientRect(right["hwnd"], ctypes.byref(rect)); rw, rh = rect.right, rect.bottom
        cw, ch = area[2]-area[0], area[3]-area[1]
        factor = min(rw/cw, rh/ch); anchor = (round((rw-cw*factor)/2+(click_at[0]-area[0])*factor), round((rh-ch*factor)/2+(click_at[1]-area[1])*factor))
        mouse(right["hwnd"], 0x201, anchor, 1); mouse(right["hwnd"], 0x202, anchor)
        native.capture_helper(picker, folder / "image-picker-context-and-anchor.png")
        native.click_helper(picker, "이 이미지로 선택"); assert native.wait_exited(picker)

    def run_task(task_id, target, prefix):
        activate(target)
        args = {"task_id": task_id, "targets": [{"program_id": "painted", **target}], "delivery_mode": "foreground"}
        before = receipt()["clicks"]
        answer = call("computer_run_task", args, raw=True); value = decoded(answer)
        assert value["status"] == "needs_review" and value["checkpoint"]["capture_available"] and not value["task_verified"], value
        image = next(c for c in answer["content"] if c.get("type") == "image")
        (folder / (prefix + "-checkpoint.png")).write_bytes(base64.b64decode(image["data"], validate=True))
        after = receipt()["clicks"]; assert after == [before[0], before[1]+1, before[2]], after
        wrong = call("computer_run_task", {**args, "resume_run_id": value["run_id"], "acknowledge_checkpoint": "0"*32}, allow_error=True)
        assert receipt()["clicks"] == after
        done = call("computer_run_task", {**args, "resume_run_id": value["run_id"], "acknowledge_checkpoint": value["checkpoint"]["id"]})
        assert done["status"] == "verified" and done["completed_steps"] == 2, done
        return {"passed": True, "exact_second_row_clicked_once": True, "paused_for_human_review": True, "synthetic_ack_only": True}

    def replay_recording(task_id, target, field):
        assert native.pid(field["hwnd"]) == target["pid"]
        original = ctypes.create_unicode_buffer("OLD")
        native.user.SendMessageW(field["hwnd"], 0x000C, 0, ctypes.addressof(original))
        assert receipt()["input"] == "OLD"
        activate(target)
        args = {"task_id": task_id, "targets": [{"program_id": "painted", **target}], "delivery_mode": "foreground"}
        initial_clicks = receipt()["clicks"]
        response = call("computer_run_task", args, raw=True)
        for ordinal, expected in enumerate(("OLD", "reviewed input"), 1):
            state = decoded(response)
            assert state["status"] == "needs_review" and state["checkpoint"]["capture_available"], state
            assert not state["task_verified"] and receipt()["input"] == expected, state
            frame = next(item for item in response["content"] if item.get("type") == "image")
            (folder / ("recording-replay-checkpoint-" + str(ordinal) + ".png")).write_bytes(base64.b64decode(frame["data"], validate=True))
            response = call("computer_run_task", {**args, "resume_run_id": state["run_id"],
                "acknowledge_checkpoint": state["checkpoint"]["id"]}, raw=True)
        done = decoded(response)
        assert done["status"] == "verified" and done["completed_steps"] == 4, done
        assert receipt()["input"] == "reviewed input" and receipt()["clicks"] == initial_clicks
        return {"passed": True, "existing_value_replaced_exactly": True, "checkpoint_count": 2,
            "synthetic_ack_only": True, "completed_steps": 4}

    try:
        child, target = spawn("first"); client = connect(); begin()
        edit, editor_id = editor(target, "합성 이미지 동작")
        before = receipt(); pick = nested(edit, "이미지로 선택"); crop(pick, target)
        wait(lambda: native.user.IsWindowVisible(edit["hwnd"]))
        wait(lambda: any("이미지 인식" in r["text"] for r in controls(edit)))
        native.click_helper(edit, "이 동작을 다음 단계로 추가"); draft = status(editor_id, 2)
        assert [s["action"] for s in draft["steps"]] == ["image_click", "checkpoint"]
        assert receipt() == before
        native.capture_helper(edit, folder / "image-editor-review.png")
        select_row(edit, 0, double=True)
        preview = wait(lambda: next((r for r in native.windows(edit["pid"]) if r["visible"] and r["text"] == "기록한 동작과 대상 확인"), None))
        preview_helper = {**edit, "hwnd": preview["hwnd"]}
        native.capture_helper(preview_helper, folder / "recorded-target-preview.png")
        replacement = nested(preview_helper, "이미지 대상 다시 선택"); crop(replacement, target)
        def replaced():
            state = status(editor_id, 2)
            return state if state["steps"][0]["label"] == "다시 선택한 이미지" else None
        updated = wait(replaced)
        assert [s["action"] for s in updated["steps"]] == ["image_click", "checkpoint"] and receipt() == before
        report["scenarios"]["retarget_preserves_action_and_review"] = {"passed": True, "native_thumbnail_review": True, "step_count": 2}
        native.click_helper(edit, "프로세스 저장"); saved = status(editor_id, saved=True); assert native.wait_exited(edit)
        task_id = saved["saved_task"]["id"]
        stored = call("computer_get_task", {"id": task_id}); assert "template_png" not in json.dumps(stored)
        report["scenarios"]["visible_image_authoring_and_save"] = {"passed": True, "app_unchanged_while_authoring": True, "image_bytes_excluded_from_task_text": True}
        report["scenarios"]["real_driver_image_click_and_review"] = run_task(task_id, target, "first")
        call("computer_end"); client.close(); client = None
        child.terminate(); child.wait(timeout=5)
        child, target = spawn("reopened"); client = connect(); begin()
        report["scenarios"]["reconnect_reopen_and_reuse"] = run_task(task_id, target, "reopened")
        # Native input is explicitly injected into this exact owned test HWND.
        # This scenario covers fast-click image/manual fallback. Deliberately
        # hover over the painted (non-UIA) area first, then click the Edit.
        # Dedicated recording_validation.py tests the new hovered UIA semantic
        # Edit/checkbox path and final-value postconditions independently.
        field = next(r for r in native.windows(child.pid, target["window_id"]) if "EDIT" in r["class"].upper() and r["bounds"][0] == min(q["bounds"][0] for q in native.windows(child.pid, target["window_id"]) if "EDIT" in q["class"].upper()))
        text = ctypes.create_unicode_buffer("OLD")
        native.user.SendMessageW(field["hwnd"], 0x000C, 0, ctypes.addressof(text))
        edit, editor_id = editor(target, "합성 동작 녹화")
        record = nested(edit, "● 동작 녹화")
        # Keep the visible recording toolbar beside, never over, the fixture.
        bounds = next(r for r in native.windows(child.pid) if r["hwnd"] == target["window_id"])["bounds"]
        width = bounds[2] - bounds[0]
        native.user.SetWindowPos(record["hwnd"], -1, 20, 20, 0, 0, 0x0011)
        native.user.SetWindowPos(target["window_id"], 0, native.user.GetSystemMetrics(0)-width-30, 30, 0, 0, 0x0011)
        preflight = recording_status(editor_id, "ready")
        assert preflight["probe"]["hooks"] == "available", preflight
        native.click_helper(record, "기록 시작"); activate(target)
        recording_status(editor_id, "recording")
        point = client_point(target, 150, 260)
        assert native.user.SetCursorPos(point.x, point.y); time.sleep(.7)
        owned_click(target, 150, 385); time.sleep(.75)
        assert native.user.GetForegroundWindow() == target["window_id"]
        native.user.keybd_event(0x51, 0, 0, 0); native.user.keybd_event(0x51, 0, 2, 0); time.sleep(.85)
        native.capture_helper(record, folder / "recorder-before-review.png")
        native.click_helper(record, "기록 마치고 검토"); assert native.wait_exited(record)
        draft = status(editor_id, 4)
        assert draft["steps"][2]["action"] == "manual_entry" and draft["steps"][2]["editable_input"], draft
        assert len(call("computer_tasks")["tasks"]) == 1
        # Select the unresolved row with focus asserted on this exact owned
        # ListView, then use the actual native text-resolution dialog.
        select_row(edit, 2)
        native.click_helper(edit, "입력 내용 지정")
        dialog = wait(lambda: next((r for r in native.windows(edit["pid"]) if r["visible"] and r["text"] == "녹화한 입력 내용 지정"), None))
        dialog_helper = {**edit, "hwnd": dialog["hwnd"]}
        input_control = next(r for r in native.windows(edit["pid"], dialog["hwnd"]) if "EDIT" in r["class"].upper())
        text = ctypes.create_unicode_buffer("reviewed input")
        native.user.SendMessageW(input_control["hwnd"], 0x000C, 0, ctypes.addressof(text))
        native.click_helper(dialog_helper, "내용 반영")
        wait(lambda: not native.user.IsWindowVisible(dialog["hwnd"]))
        def resolved():
            value = status(editor_id, 4)
            return value if value["steps"][2]["action"] == "image_type_text" else None
        draft = wait(resolved)
        # The automatic click crop can contain changing input glyphs. Exercise
        # the intended human draft-review repair: choose stable label/field
        # context without those glyphs, preserving the recorded text action.
        select_row(edit, 2, double=True)
        preview = wait(lambda: next((r for r in native.windows(edit["pid"]) if r["visible"] and r["text"] == "기록한 동작과 대상 확인"), None))
        replacement = nested({**edit, "hwnd": preview["hwnd"]}, "이미지 대상 다시 선택")
        crop(replacement, target, (102, 335, 195, 400), (150, 382))
        wait(lambda: status(editor_id, 4)["steps"][2]["label"] == "다시 선택한 이미지")
        native.capture_helper(edit, folder / "recorded-draft-resolved.png")
        warning_review = acknowledge_recording_warnings(edit, editor_id)
        native.click_helper(edit, "프로세스 저장"); saved = status(editor_id, saved=True); assert native.wait_exited(edit)
        assert len(call("computer_tasks")["tasks"]) == 2
        if warning_review["present"]:
            stored_review = call("computer_get_task", {"id": saved["saved_task"]["id"]})["recording_review"]
            assert stored_review["partial"] is True and stored_review["acknowledged"] is True
            assert stored_review["warning_codes"] == warning_review["warning_codes"]
        report["scenarios"]["scoped_recording_review_and_manual_input"] = {"passed": True, "injected_test_input": True,
            "not_saved_until_review": True, "manual_input_resolved_in_native_dialog": True,
            "input_image_reselected_in_native_dialog": True, "recorded_steps": 4,
            "startup_preflight_observed": True, "warning_review": warning_review}
        report["recording_limitations"] = ["Automatic input crops can fail after focus or text changes; this validation reselects stable input context in the native review dialog before replay."]
        report["scenarios"]["saved_recording_real_driver_replay"] = replay_recording(saved["saved_task"]["id"], target, field)
        report["passed"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:5000]}
        for index, helper in enumerate(helpers):
            if not native.wait_exited(helper, 0) and native.user.IsWindowVisible(helper["hwnd"]):
                native.capture_helper(helper, folder / ("failure-helper-" + str(index) + ".png"))
                (folder / ("failure-controls-" + str(index) + ".json")).write_text(json.dumps(controls(helper), ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if client:
            try: call("computer_end", allow_error=True); client.close()
            except Exception:
                report["passed"] = False
                if client.child.poll() is None: client.child.kill(); client.child.wait(timeout=5)
        for helper in helpers:
            if not native.wait_exited(helper, 5):
                native.kernel.TerminateProcess(helper["handle"], 1); native.wait_exited(helper, 5); report["passed"] = False
            native.kernel.CloseHandle(helper["handle"])
        for child in processes:
            if child.poll() is None: child.terminate(); child.wait(timeout=5)
        report["owned_helpers_exited"] = all(h["closed"] for h in helpers)
        report["owned_fixtures_exited"] = all(p.poll() is not None for p in processes)
        report["tested_build"]["unchanged_through_validation"] = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items())
        if not report["tested_build"]["unchanged_through_validation"]: report["passed"] = False
        write()
    return report


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True); parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path); parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run: raise SystemExit("Pass --run to launch isolated synthetic GUI acceptance")
    answer = run(args.folder, args.driver, args.bundle)
    print(json.dumps(answer, ensure_ascii=False, indent=2)); raise SystemExit(0 if answer["passed"] else 1)
