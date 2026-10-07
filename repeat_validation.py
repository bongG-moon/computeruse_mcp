"""Owned offline native fixture: real MCP first/repeat runs and independent receipts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from generic_validation import prepare
from live_validation import Client, DRIVER, HERE, decoded, safe
from build_portable import runtime_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    folder = HERE / ".data" / "repeat-validation" / uuid.uuid4().hex[:10]
    manifest = prepare(folder, DRIVER)
    app = manifest["apps"][0]
    process = subprocess.Popen([app["exe"], app["title"], app["receipt"]], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    client = None
    report = {"passed": False, "fixture": "owned_native_winforms", "llm_used": False,
              "private_application_used": False, "packaged": args.bundle is not None, "operations": [], "runs": []}
    try:
        root = args.bundle or HERE
        tested_names = ["server.py", "operations.py", "workflows.py", "session_runtime.py", "vendor/guard.py",
                        "repeat_profiles.py", "scoped_controls.py", "result_files.py", "Computer Use MCP 빠른 확인.exe"]
        report["tested_sha256"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in tested_names}
        python = root / "runtime/python.exe" if args.bundle else Path(sys.executable)
        env = runtime_environment(python.parent)
        client = Client(folder / "config.json", root / "server.py", python, env)
        def call(name, arguments=None, allow_error=False):
            started = time.monotonic()
            result = client.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=45)
            value = decoded(result)
            report["operations"].append({"tool": name, "duration_ms": round((time.monotonic()-started)*1000,2), "result": safe(value)})
            (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if result.get("isError") and not allow_error: raise AssertionError((name, value))
            return value
        tool_names = {item["name"] for item in client.request("tools/list")["tools"]}
        assert {"computer_verify_controls", "computer_prepare_result", "computer_verify_result"} <= tool_names
        status = call("computer_status")
        assert status["version"] == "0.12.0", status
        report["version"] = status["version"]
        call("computer_begin", {"program_ids": [app["id"]], "task_description": "Owned synthetic repeat validation"})
        deadline = time.monotonic() + 15
        while True:
            windows = call("list_windows")["windows"]
            candidates = [w for w in windows if w["pid"] == process.pid and w.get("title") == app["title"]]
            if len(candidates) == 1: break
            if time.monotonic() >= deadline: raise AssertionError("Fixture did not expose a unique window")
            time.sleep(.1)
        target = {k: candidates[0][k] for k in ("pid", "window_id")}
        probe = call("computer_verify_controls", {**target, "selectors": [{"name": "작업 문구", "role": "Edit"}, {"name": "처리 상태", "role": "ComboBox"}]})
        assert probe.get("scope_complete") and len(probe["elements"]) == 2, probe
        recipe = call("computer_save_task", {"name": "Synthetic repeated query", "instructions": "Set synthetic text, apply once and verify fresh result", "expected": "Matching result and independent receipt",
            "program_ids": [app["id"]], "variables": {"value": {"description": "Synthetic value"}}, "steps": [
                {"program_id": app["id"], "operation": "set_value", "selector": {"name": "작업 문구", "role": "Edit"}, "value": "${value}"},
                {"program_id": app["id"], "operation": "click", "selector": {"name": "적용", "role": "Button"},
                 "expect": [{"selector": {"name": "적용 결과", "role": "Edit"}, "property": "value", "equals": "확인: ${value}", "require_change": True}]}]})
        for index, mode in enumerate(["standard", "auto", "standard", "auto"]):
            value = "repeat-" + str(index)
            started = time.monotonic()
            answer = call("computer_run_task", {"task_id": recipe["id"], "inputs": {"value": value},
                "targets": [{"program_id": app["id"], **target}], "delivery_mode": "foreground", "execution_mode": mode})
            receipt = json.loads(Path(app["receipt"]).read_text(encoding="utf-8"))
            assert answer["task_verified"] and receipt["input"] == value and receipt["applied"] == "확인: " + value and receipt["apply_count"] == index+1, (answer, receipt)
            report["runs"].append({"requested": mode, "actual": answer["execution"]["mode"], "elapsed_ms": round((time.monotonic()-started)*1000,2),
                "receipt_apply_count": receipt["apply_count"], "metrics": answer.get("metrics"), "last_metrics": answer.get("last_result", {}).get("metrics")})
        # Existing 'done' text must not count as a fresh completion.
        unchanged = call("computer_perform", {**target, "delivery_mode": "foreground", "step": {"operation": "click", "selector": {"name": "적용", "role": "Button"},
            "verification_timeout_ms": 200, "expect": [{"selector": {"name": "적용 결과", "role": "Edit"}, "property": "value", "equals": "확인: repeat-3", "require_change": True}]}}, allow_error=True)
        assert unchanged["task_verified"] is False, unchanged
        report["unchanged_status_rejected"] = True
        # Verify public MCP result-file routing with an exact known synthetic
        # folder. This fixture file is not evidence of a UI export/query.
        results = folder / "synthetic-export"; results.mkdir()
        (results / "before.csv").write_text("Condition,Value\nW,1\n", encoding="utf-8")
        ticket = call("computer_prepare_result", {"directory": str(results), "pattern": "*.csv"})
        (results / "after.csv").write_text("Condition,Value\nW,1\nW,2\n", encoding="utf-8")
        checked = call("computer_verify_result", {"ticket_id": ticket["ticket_id"], "filename": "after.csv", "column": "Condition", "equals": "W"})
        assert checked["content_verified"] and checked["data_rows"] == 2 and checked["task_verified"] is False, checked
        stale = call("computer_verify_result", {"ticket_id": ticket["ticket_id"], "filename": "before.csv", "column": "Condition", "equals": "W"}, allow_error=True)
        assert not stale.get("content_verified"), stale
        report["result_file_stdio"] = {"fresh_content_verified": True, "unchanged_file_rejected": True, "business_export_tested": False}
        call("computer_end")
        report["passed"] = True
    finally:
        if client:
            try: client.close()
            except Exception as error: report["close_error"] = str(error); report["passed"] = False
        if process.poll() is None: process.terminate(); process.wait(timeout=5)
        report["fixture_exited"] = process.poll() is not None
        report["source_unchanged"] = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha for name, sha in report.get("tested_sha256", {}).items())
        if not report["source_unchanged"]: report["passed"] = False
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(folder / "report.json"), "passed": report["passed"], "runs": report["runs"]}, ensure_ascii=False), flush=True)
    if not report["passed"]: raise SystemExit(1)


if __name__ == "__main__": main()
