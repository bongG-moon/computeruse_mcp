"""Native owned-fixture acceptance for image input plus changed UIA proof.

Exercises the actual product SessionRuntime, image matcher, Driver and workflow.
No user program or corporate service is touched; run with exclusive GUI ownership.
"""
from __future__ import annotations
import argparse
import base64
import copy
import ctypes
from ctypes import wintypes
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image
def run(folder, driver=None, bundle=None, execute=False):
    root = Path(bundle).resolve() if bundle else Path(__file__).resolve().parent
    if bundle:
        if not (root / "BUILD-MANIFEST.json").is_file(): raise ValueError("Select a built bundle folder")
        sys.path.insert(0, str(root))
    from live_validation import DRIVER
    from picker_validation import Native
    from process_validation import wait
    from session_runtime import SessionRuntime
    from settings import save_config
    from workflows import WorkflowRunner
    import image_steps, operations, session_runtime, workflows, visual_fixture
    if driver is None: driver = DRIVER
    if bundle and any(Path(module.__file__).resolve().parent != root for module in (image_steps, operations, session_runtime, workflows)):
        raise RuntimeError("Bundle validation must run in a fresh Python process with exact bundle modules")
    folder = Path(folder).resolve()
    if folder.exists(): raise ValueError("Use a fresh owned fixture folder")
    folder.mkdir(parents=True)
    original_source = visual_fixture.SOURCE
    try:
        visual_fixture.SOURCE = original_source.replace('for(int i=0;i<3;i++) {', 'for(int i=0;i<1;i++) {').replace('DoubleBuffered=true;', 'DoubleBuffered=true; TopMost=true;').replace('Shown += delegate { Save(); };', 'Shown += delegate { Activate(); Save(); };').replace('result.SetBounds(30,420,650,45);',
            'result.Name="completion_status"; result.SetBounds(30,420,650,45);').replace(
            'result.Text="RESULT "+(char)(\'A\'+i)+" / "+clicks[i];', 'result.Text="DONE";')
        exe = visual_fixture.compile_fixture(folder / "fixture")
    finally:
        visual_fixture.SOURCE = original_source
    program = {"id": "painted", "name": "Synthetic completion fixture", "exe": str(exe), "enabled": True, "control_exes": [], "hints": "Owned fixture"}
    config = {"version": 1, "driver": str(Path(driver).resolve()), "state_dir": str(folder / "state"),
        "programs": [program], "mode": "uia", "approval": "client", "log_detail": "metadata",
        "max_minutes": 10, "max_actions": 20, "approval_timeout_seconds": 300, "observation_timeout_seconds": 20}
    save_config(folder / "config.json", config)
    if not execute:
        prepared = {"prepared": True, "gui_launched": False, "folder": str(folder), "config": str(folder / "config.json")}
        (folder / "prepared.json").write_text(json.dumps(prepared, indent=2), encoding="utf-8")
        return prepared
    report = {"passed": False, "synthetic_fixture": True, "corporate_application_tested": False,
        "external_llm_used": False, "packaged": bundle is not None, "scenarios": {}, "owned_processes_closed": False,
        "tested_modules": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (image_steps, operations, session_runtime, workflows)}}
    runtime = None; children = []; native = Native()
    native.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    def write():
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    def spawn(suffix):
        receipt = folder / (suffix + ".json")
        child = subprocess.Popen([str(exe), "Synthetic image completion " + suffix, str(receipt)], cwd=folder,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        children.append(child)
        rows = wait(lambda: [row for row in native.windows(child.pid) if row["visible"]])
        assert len(rows) == 1, rows
        target = {"pid": child.pid, "window_id": rows[0]["hwnd"]}
        wait(lambda: receipt.exists())
        return target, receipt
    def image_step(target):
        runtime.call("bring_to_front", target)
        hwnd = target["window_id"]
        assert native.pid(hwnd) == target["pid"]
        native.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)
        foreground = native.user.GetForegroundWindow(); thread = native.kernel.GetCurrentThreadId()
        other = native.user.GetWindowThreadProcessId(foreground, None)
        attached = other != thread and bool(native.user.AttachThreadInput(thread, other, True))
        try: native.user.SetForegroundWindow(hwnd)
        finally:
            if attached: native.user.AttachThreadInput(thread, other, False)
        if native.user.GetForegroundWindow() != hwnd:
            # Test setup only: the fixture has no business action in its title bar.
            # Verify hit-test ownership immediately before real activation input.
            bounds = wintypes.RECT(); assert native.user.GetWindowRect(hwnd, ctypes.byref(bounds))
            caption = wintypes.POINT(bounds.left + 45, bounds.top + 12)
            assert native.pid(hwnd) == target["pid"] and native.user.GetAncestor(native.user.WindowFromPoint(caption), 2) == hwnd
            assert native.user.SetCursorPos(caption.x, caption.y)
            native.user.mouse_event(2, 0, 0, 0, 0); native.user.mouse_event(4, 0, 0, 0, 0)
        wait(lambda: native.user.GetForegroundWindow() == hwnd)
        answer = runtime.capture_checkpoint(target)
        if not any(item.get("type") == "image" for item in answer.get("content", [])):
            raise AssertionError("Screenshot response: " + repr(answer)[:2400])
        image = next(item for item in answer["content"] if item.get("type") == "image")
        with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as bitmap:
            bounds = next(row["bounds"] for row in native.windows(target["pid"]) if row["hwnd"] == target["window_id"])
            point = wintypes.POINT(0, 0); assert native.user.ClientToScreen(target["window_id"], ctypes.byref(point))
            x, y = point.x - bounds[0], point.y - bounds[1]
            crop = bitmap.crop((x + 150, y + 30, x + 660, y + 105)).convert("RGB")
            buffer = io.BytesIO(); crop.save(buffer, format="PNG")
            image_target = {"format": "computer-image-target/v1", "template_png": base64.b64encode(buffer.getvalue()).decode(),
                "width": 510, "height": 75, "anchor": {"x": (610-150)/510, "y": (67-30)/75},
                "capture_window": {"width": bitmap.width, "height": bitmap.height}, "min_score": .94, "ambiguity_margin": .03}
        return {"program_id": "painted", "operation": "image_click", "image_target": image_target,
            "expect": [{"selector": {"automation_id": "completion_status"}, "property": "name", "equals": "DONE", "require_change": True}],
            "verification_timeout_ms": 0}
    def recipe(step, name):
        return {"id": name, "revision": 1, "program_ids": ["painted"], "variables": {}, "steps": [step]}
    try:
        runtime = SessionRuntime(config, [program], "uia", {"instructions": "Native synthetic image completion", "expected": "Changed status and exactly one click"})
        runtime.start()
        runner = WorkflowRunner(config["state_dir"])
        target, receipt = spawn("changed-and-stale")
        step = image_step(target); task = recipe(step, "changed-and-stale")
        first = runner.run(runtime, task, {}, [{"program_id": "painted", **target}])
        counters = json.loads(receipt.read_text(encoding="utf-8"))
        report["scenarios"]["changed_condition"] = {"result": first, "receipt": counters}
        write()
        assert first["task_verified"] and counters["clicks"] == [1, 0, 0], first
        stale = runner.run(runtime, task, {}, [{"program_id": "painted", **target}])
        counters = json.loads(receipt.read_text(encoding="utf-8"))
        report["scenarios"]["stale_condition_rejected"] = {"result": stale, "receipt": counters}
        write()
        assert not stale["task_verified"] and counters["clicks"] == [2, 0, 0], stale
        children[-1].terminate(); children[-1].wait(timeout=10)
        target, receipt = spawn("resume")
        step = image_step(target); task = recipe(step, "resume")
        action = runtime.image_action
        def interrupted(*args):
            delivered = action(*args)
            if delivered.get("input_dispatched") is not True: return delivered
            raise RuntimeError("Synthetic acknowledgement failure after input")
        runtime.image_action = interrupted
        pending = runner.run(runtime, task, {}, [{"program_id": "painted", **target}])
        runtime.image_action = action
        counters = json.loads(receipt.read_text(encoding="utf-8"))
        assert not pending["task_verified"] and counters["clicks"] == [1, 0, 0], pending
        resumed = runner.run(runtime, task, {}, [{"program_id": "painted", **target}], resume_run_id=pending["run_id"])
        counters = json.loads(receipt.read_text(encoding="utf-8"))
        report["scenarios"]["resume_without_replay"] = {"pending": pending, "result": resumed, "receipt": counters}
        write()
        assert resumed["task_verified"] and counters["clicks"] == [1, 0, 0], resumed
        report["passed"] = True
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)[:4000]
        raise
    finally:
        if runtime is not None:
            runtime.stop("Owned synthetic acceptance complete"); runtime.wait_stopped(10)
        for child in children:
            if child.poll() is None: child.terminate()
            child.wait(timeout=10)
        report["owned_processes_closed"] = all(child.poll() is not None for child in children)
        write()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True); parser.add_argument("--driver")
    parser.add_argument("--bundle"); parser.add_argument("--run", action="store_true", help="Open only the owned synthetic fixture and exercise the actual product")
    args = parser.parse_args()
    result = run(args.folder, args.driver, args.bundle, args.run)
    print(json.dumps({key: list(result[key]) if key == "scenarios" else result[key] for key in
        ("prepared", "gui_launched", "passed", "scenarios", "owned_processes_closed") if key in result}))
