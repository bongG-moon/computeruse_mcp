"""Developer acceptance of human-authored processes through the real MCP.

--run opens only fresh synthetic WinForms windows. It operates the native
editor/picker, authors a process, runs it with the real Driver, and verifies
screenshot checkpoints and persistence. Checkpoint acknowledgements are
synthetic test input, never evidence of human review or model image recognition.
Do not run concurrently with another foreground GUI acceptance.
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

from generic_validation import prepare
from live_validation import Client, DRIVER, decoded, safe
from native_fixture import DATA
from picker_validation import Native

HERE = Path(__file__).resolve().parent
EDITOR = "Computer Use MCP 프로세스 만들기.exe"
PICKER = "Computer Use MCP 요소 선택.exe"


def wait(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("Synthetic UI condition did not complete within its deadline")


def run_process(manifest, bundle=None):
    folder = Path(manifest["folder"])
    root = bundle or HERE
    native = Native()
    app = manifest["apps"][0]
    helpers, processes, records = [], [], []
    client = None
    run_dir = None
    report = {"passed": False, "developer_only": True, "synthetic_fixture": True,
              "human_authoring_tested": False, "automated_editor_actions": True,
              "automated_checkpoint_acknowledgement": True, "human_image_review_tested": False,
              "external_llm_used": False, "private_application_used": False,
              "administrator_gui_tested": False, "packaged": bundle is not None, "scenarios": {}}
    from build_portable import source_files
    paths = [root / name for name in source_files() if name.endswith(".py")]
    paths += [root / EDITOR, root / PICKER]
    paths += [root / name for name in ("ProcessEditor.cs", "ElementPicker.cs", "BUILD-MANIFEST.json") if (root / name).is_file()]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    report["tested_build"] = {"root": str(root), "sha256": hashes,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(Path(app["exe"]).read_bytes()).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(manifest["driver"]).read_bytes()).hexdigest()}

    def save():
        (folder / "process-steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "process-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def call(name, args=None, *, raw=False, allow_error=False):
        started = time.monotonic()
        answer = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        records.append({"tool": name, "seconds": round(time.monotonic()-started, 3), "result": safe(answer)})
        save()
        if answer.get("isError") and not allow_error:
            raise AssertionError(name + " failed: " + json.dumps(safe(decoded(answer)), ensure_ascii=False)[:3000])
        return answer if raw else decoded(answer)

    def connect():
        if bundle:
            from build_portable import runtime_environment
            return Client(folder / "config.json", root / "server.py", root / "runtime/python.exe", runtime_environment(root / "runtime"))
        return Client(folder / "config.json", HERE / "server.py")

    def spawn(suffix):
        title = app["title"] + " " + suffix
        child = subprocess.Popen([app["exe"], title, app["receipt"]], cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(child)
        return child, title

    def begin():
        nonlocal run_dir
        result = call("computer_begin", {"program_ids": [app["id"]], "task_description":
            "새로 연 합성 시험 프로그램에서 사람이 작성하는 프로세스 편집, 저장, 실제 실행과 화면 확인 중단을 검증합니다."})
        run_dir = Path(result["run_dir"]).resolve()
        run_dir.relative_to((folder / "state/runs").resolve())

    def target_for(child, title):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            values = call("list_windows", {"pid": child.pid, "on_screen_only": True}).get("windows", [])
            found = [item for item in values if item.get("title") == title]
            if len(found) == 1:
                return {key: found[0][key] for key in ("pid", "window_id")}
            time.sleep(.1)
        raise AssertionError("Owned synthetic fixture was not observed")

    def receipt():
        for _ in range(40):
            try:
                value = json.loads(Path(app["receipt"]).read_text(encoding="utf-8"))
                assert value["synthetic_fixture"] is True
                return value
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.05)
        raise AssertionError("Synthetic receipt missing")

    def controls(helper):
        return native.windows(helper["pid"], helper["hwnd"])

    def choose(helper, label):
        text = ctypes.create_unicode_buffer(label)
        for row in controls(helper):
            if "COMBOBOX" not in row["class"].upper():
                continue
            index = native.user.SendMessageW(row["hwnd"], 0x0158, -1, ctypes.addressof(text))  # CB_FINDSTRINGEXACT
            if index >= 0:
                hwnd = row["hwnd"]
                native.user.SendMessageW(hwnd, 0x014E, index, 0)
                parent = native.user.GetParent(hwnd)
                assert native.pid(parent) == helper["pid"]
                native.user.SendMessageW(parent, 0x0111, (1 << 16) | (native.user.GetDlgCtrlID(hwnd) & 65535), hwnd)
                time.sleep(.1)
                return
        raise AssertionError("Editor option missing: " + label)

    def set_text(helper, hwnd, text):
        assert native.pid(hwnd) == helper["pid"]
        value = ctypes.create_unicode_buffer(text)
        native.user.SendMessageW(hwnd, 0x000C, 0, ctypes.addressof(value))

    def left_edit(helper):
        rows = [row for row in controls(helper) if "EDIT" in row["class"].upper() and row["visible"]]
        # The left field is identified relative to other owned child controls,
        # avoiding outer-window virtualization differences after Hide/Show.
        first_x = min(row["bounds"][0] for row in rows)
        rows = [row for row in rows if row["bounds"][0] == first_x]
        assert len(rows) == 1, rows
        return rows[0]["hwnd"]

    def process_state(editor_id, expected_count=None, terminal=False):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            answer = call("computer_process_status", {"editor_id": editor_id, "wait_ms": 200}, allow_error=True)
            if answer.get("status") in {"failed", "cancelled"}:
                raise AssertionError("Process editor failed: " + json.dumps(answer, ensure_ascii=False))
            if terminal and answer.get("status") == "saved" and not answer.get("cleanup_pending") and answer.get("pending") is False:
                return answer
            if expected_count is not None and len(answer.get("steps", [])) == expected_count:
                return answer
            time.sleep(.1)
        raise AssertionError("Process editor did not acknowledge the expected change")

    def add_step(helper, editor_id, label, count, text=None):
        choose(helper, label)
        if text is not None:
            set_text(helper, left_edit(helper), text)
        native.click_helper(helper, "이 동작을 다음 단계로 추가")
        return process_state(editor_id, expected_count=count)

    def close(target, child):
        answer = call("computer_close", {**target, "scope": "process", "delivery_mode": "foreground",
            "close_action": {"operation": "hotkey", "keys": ["alt", "f4"]}, "timeout_ms": 5000})
        assert answer["task_verified"] is True and child.wait(timeout=5) == 0, answer

    def run_saved(task_id, target, prefix):
        args = {"task_id": task_id, "targets": [{"program_id": app["id"], "window_ref": "main", **target}], "delivery_mode": "foreground"}
        call("get_window_state", {**target, "include_accessibility_tree": True, "include_screenshot": False})
        call("bring_to_front", target)
        first = call("computer_run_task", args, raw=True)
        value = decoded(first)
        assert value["status"] == "needs_review" and value["task_verified"] is False, value
        assert value["completed_steps"] == 3 and value["pending_step"] == 3 and value["total_steps"] == 5, value
        assert value["checkpoint"]["capture_available"] is True and value["checkpoint"]["image_verified"] is False, value
        images = [item for item in first.get("content", []) if item.get("type") == "image"]
        assert images, "Actual screenshot content is required at the checkpoint"
        pixels = base64.b64decode(images[0]["data"], validate=True)
        assert len(pixels) > 1000
        (folder / (prefix + "-checkpoint.png")).write_bytes(pixels)
        assert receipt()["input"] == "프로세스 검증 문구"
        paused = call("computer_task_progress", {"run_id": value["run_id"]})
        assert paused["completed_steps"] == 3 and paused["pending_step"] == 3, paused
        before_rejected_ack = receipt()
        rejected = call("computer_run_task", {**args, "resume_run_id": value["run_id"],
            "acknowledge_checkpoint": "0" * 32}, raw=True, allow_error=True)
        assert rejected.get("isError") is True, rejected
        assert receipt() == before_rejected_ack, "A wrong checkpoint ID must never dispatch input"
        paused_again = call("computer_task_progress", {"run_id": value["run_id"]})
        assert paused_again["completed_steps"] == 3 and paused_again["checkpoint"]["id"] == value["checkpoint"]["id"]
        # Synthetic test ACK only: not a claim that a human/model reviewed pixels.
        completed = call("computer_run_task", {**args, "resume_run_id": value["run_id"],
            "acknowledge_checkpoint": value["checkpoint"]["id"]})
        assert completed["status"] == "verified" and completed["task_verified"] is True and completed["completed_steps"] == 5, completed
        assert completed.get("checkpoint_images_verified") is False
        return {"passed": True, "paused_before_last_step": True, "actual_checkpoint_image_bytes": len(pixels),
                "wrong_checkpoint_id_rejected": True, "explicit_synthetic_acknowledgement": True,
                "run_id": value["run_id"], "completed_steps": completed["completed_steps"]}

    try:
        child, title = spawn("first")
        client = connect()
        report["version"] = call("computer_status")["version"]
        begin()
        target = target_for(child, title)
        before = receipt()
        started = call("computer_process_editor", {"targets": [{"program_id": app["id"], "window_ref": "main", **target}],
            "name": "합성 프로세스 전체 검증", "timeout_seconds": 300})
        assert started["status"] == "editing" and started["editor_visible"] is True, started
        editor_id = started["editor_id"]
        editor = native.ready_helper(started, root / EDITOR); helpers.append(editor)
        native.capture_helper(editor, folder / "process-editor-open.png")
        duplicate = call("computer_process_editor", {"targets": [{"program_id": app["id"], "window_ref": "main", **target}],
            "name": "합성 프로세스 전체 검증", "timeout_seconds": 300})
        assert duplicate["editor_id"] == editor_id and duplicate.get("reused") is True, duplicate
        conflicting = call("computer_process_editor", {"targets": [{"program_id": app["id"], "window_ref": "main", **target}],
            "name": "중복 방지", "timeout_seconds": 300})
        assert conflicting["editor_id"] == editor_id and conflicting.get("busy") is True, conflicting
        report["scenarios"]["visible_editor_and_duplicate_request"] = {"passed": True, "reused_same_helper": True}
        known_ready = set((run_dir / "learning").glob("*.ready.json")) if (run_dir / "learning").exists() else set()
        native.click_helper(editor, "요소 직접 선택")
        def new_picker_ready():
            for path in (run_dir / "learning").glob("*.ready.json"):
                if path not in known_ready:
                    try:
                        return json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        pass
            return None
        picker = native.ready_helper(wait(new_picker_ready), root / PICKER); helpers.append(picker)
        assert not native.user.IsWindowVisible(editor["hwnd"]), "Editor should expose the target while selecting"
        edits = [row for row in native.windows(target["pid"], target["window_id"]) if row["visible"] and "EDIT" in row["class"].upper()]
        field = sorted(edits, key=lambda row: row["bounds"][1])[0]  # Own fixed synthetic fixture's first input only.
        # Use the real countdown UI: no keyboard event is sent to the app and
        # no foreground activation is required. Raise only our exact fixture
        # without activating it so the chosen point is not covered by the editor.
        assert native.pid(target["window_id"]) == child.pid
        native.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        native.user.SetWindowPos.restype = wintypes.BOOL
        assert native.user.SetWindowPos(target["window_id"], -1, 0, 0, 0, 0, 0x0053)
        native.click_helper(picker, "3초 후 위치 선택")
        x1, y1, x2, y2 = field["bounds"]
        point = wintypes.POINT((x1+x2)//2, (y1+y2)//2)
        native.user.SetCursorPos(point.x, point.y)
        native.user.WindowFromPoint.argtypes = [wintypes.POINT]
        native.user.WindowFromPoint.restype = wintypes.HWND
        hit = native.user.WindowFromPoint(point)
        hit_root = native.user.GetAncestor(hit, 2)
        assert hit_root == target["window_id"] and native.pid(hit_root) == child.pid, {
            "expected_target": target, "hit_window": int(hit_root or 0), "hit_pid": native.pid(hit_root), "point": [point.x, point.y]}
        native.wait_review(picker)
        native.capture_helper(picker, folder / "process-element-review.png")
        assert receipt() == before
        native.click_helper(picker, "이 요소로 선택")
        assert native.wait_exited(picker)
        wait(lambda: native.user.IsWindowVisible(editor["hwnd"]))
        wait(lambda: any("작업 문구" in row["text"] for row in controls(editor)))
        report["scenarios"]["nested_picker_human_confirmation_flow"] = {"passed": True, "fixture_unchanged": receipt() == before}
        add_step(editor, editor_id, "글자 입력", 1, "프로세스 검증 문구")
        add_step(editor, editor_id, "요소가 나타날 때까지 대기", 2)
        choose(editor, "정해진 시간 대기")
        # NumericUpDown's editor is the only left-side text control in delay mode.
        set_text(editor, left_edit(editor), "0")
        native.click_helper(editor, "이 동작을 다음 단계로 추가")
        process_state(editor_id, expected_count=3)
        add_step(editor, editor_id, "화면 캡처 후 확인할 때까지 일시정지", 4, "합성 입력 결과를 확인하는 시험 지점")
        add_step(editor, editor_id, "요소가 나타날 때까지 대기", 5)
        native.capture_helper(editor, folder / "process-editor-five-steps.png")
        assert receipt() == before, "Authoring must never mutate the synthetic business program"
        report["scenarios"]["five_step_authoring_without_app_input"] = {"passed": True, "step_count": 5}
        native.click_helper(editor, "프로세스 저장")
        saved = process_state(editor_id, terminal=True)
        assert native.wait_exited(editor)
        task_id = saved["saved_task"]["id"]
        task = call("computer_get_task", {"id": task_id})
        # Public get_task wraps the task in some older compatible hosts.
        task = task.get("task", task)
        assert len(task["steps"]) == 5
        assert task["steps"][2]["duration_ms"] == 0
        assert [step["operation"] for step in task["steps"]] == ["set_value", "wait_for_element", "delay", "checkpoint", "wait_for_element"]
        assert all("pid" not in step and "window_id" not in step and "element_index" not in step for step in task["steps"])
        report["scenarios"]["saved_declarative_process"] = {"passed": True, "task_id": task_id}
        report["scenarios"]["real_driver_checkpoint_and_resume"] = run_saved(task_id, target, "first")
        close(target, child)
        call("computer_end")
        client.close(); client = None
        child, title = spawn("reopened")
        client = connect(); begin(); target = target_for(child, title)
        reloaded = call("computer_get_task", {"id": task_id}); reloaded = reloaded.get("task", reloaded)
        assert reloaded["steps"] == task["steps"]
        report["scenarios"]["reconnect_reopen_and_reuse"] = run_saved(task_id, target, "reopened")
        before_cancel = receipt()
        cancelling_editor = call("computer_process_editor", {"targets": [{"program_id": app["id"], "window_ref": "main", **target}],
            "name": "저장하지 않을 합성 취소 시험", "timeout_seconds": 90})
        cancel_editor = native.ready_helper(cancelling_editor, root / EDITOR); helpers.append(cancel_editor)
        known_ready = set((run_dir / "learning").glob("*.ready.json")) if (run_dir / "learning").exists() else set()
        native.click_helper(cancel_editor, "요소 직접 선택")
        cancel_picker = native.ready_helper(wait(new_picker_ready), root / PICKER); helpers.append(cancel_picker)
        cancelled = call("computer_process_status", {"editor_id": cancelling_editor["editor_id"], "cancel": True, "wait_ms": 5000}, allow_error=True)
        for _ in range(4):
            if cancelled.get("status") == "cancelled" and not cancelled.get("cleanup_pending"):
                break
            cancelled = call("computer_process_status", {"editor_id": cancelling_editor["editor_id"], "wait_ms": 5000}, allow_error=True)
        assert cancelled.get("status") == "cancelled" and not cancelled.get("cleanup_pending"), cancelled
        assert native.wait_exited(cancel_picker) and native.wait_exited(cancel_editor)
        assert receipt() == before_cancel, "Cancelling authoring must never change the business fixture"
        preserved = call("computer_get_task", {"id": task_id}); preserved = preserved.get("task", preserved)
        assert preserved == reloaded, "Cancelling a new process must preserve the previously saved task"
        report["scenarios"]["nested_picker_cancellation_cleanup"] = {"passed": True,
            "editor_exited": True, "nested_picker_exited": True, "fixture_unchanged": True, "saved_task_preserved": True}
        close(target, child); call("computer_end")
        report["passed"] = all(value["passed"] for value in report["scenarios"].values())
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:5000]}
        evidence = []
        for index, helper in enumerate(helpers):
            if not native.wait_exited(helper, 0):
                try:
                    evidence.append({"pid": helper["pid"], "hwnd": helper["hwnd"], "controls": controls(helper)})
                    if native.user.IsWindowVisible(helper["hwnd"]):
                        native.capture_helper(helper, folder / ("failed-owned-helper-" + str(index) + ".png"))
                except Exception as capture_error:
                    evidence.append({"capture_error": type(capture_error).__name__})
        (folder / "failure-owned-ui.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if client is not None:
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = type(error).__name__
                report["passed"] = False
                if client.child.poll() is None:
                    client.child.kill(); client.child.wait(timeout=5)
        for helper in helpers:
            if not native.wait_exited(helper, 5):
                native.kernel.TerminateProcess(helper["handle"], 1)
                native.wait_exited(helper, 5)
                report["forced_helper_failure_cleanup_used"] = True; report["passed"] = False
            native.kernel.CloseHandle(helper["handle"])
        for child in processes:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)
                report["fixture_failure_cleanup_used"] = True; report["passed"] = False
        report["owned_helpers_exited"] = bool(helpers) and all(item["closed"] for item in helpers)
        report["owned_fixtures_exited"] = bool(processes) and all(child.poll() is not None for child in processes)
        report["tested_build"]["unchanged_through_validation"] = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
        if not report["tested_build"]["unchanged_through_validation"]:
            report["passed"] = False
        save()
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=DATA / "process-validation" / uuid.uuid4().hex[:10])
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    if (options.folder / "process-result.json").exists() or (options.folder / "state").exists():
        raise ValueError("Use a fresh isolated folder; existing validation evidence is retained")
    manifest = prepare(options.folder, options.driver)
    report = run_process(manifest, options.bundle.resolve() if options.bundle else None) if options.run else manifest
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not options.run or report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
