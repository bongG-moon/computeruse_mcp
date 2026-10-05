"""Developer-only native Windows MCP acceptance, using synthetic fixtures.

Default invocation only compiles and prepares isolated files. --run explicitly
launches two owned native windows and exercises the real MCP/Driver. Never run
alongside another foreground GUI validation. No downloads or client profiles.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from live_validation import Client, DRIVER, decoded, safe
from native_fixture import DATA, build_fixtures, fixture_folder
from settings import save_config
from vendor.windows import OwnedProcess


MODES = ("probe", "write", "select", "check", "keys", "list", "multi", "windows", "all")


def prepare(folder: Path, driver: Path) -> dict:
    folder = fixture_folder(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if not driver.is_file():
        raise FileNotFoundError("Manually supplied Driver not found; no downloads attempted")
    build = build_fixtures(folder / "binaries")
    token = folder.name
    apps = [{**app, "title": "CUA Native " + token + " " + app["label"],
             "receipt": str(folder / (app["id"] + "-state.json"))} for app in build["apps"]]
    config = {"version": 1, "driver": str(driver.resolve()), "state_dir": str(folder / "state"),
              "programs": [{"id": app["id"], "name": "네이티브 합성 시험 " + app["label"], "exe": app["exe"],
                            "control_exes": [], "hints": "이 실행에서 새로 연 합성 시험 창만 조작합니다.", "enabled": True} for app in apps],
              "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 10,
              "max_actions": 120, "approval_timeout_seconds": 300, "observation_timeout_seconds": 20}
    save_config(folder / "config.json", config)
    manifest = {"folder": str(folder), "prepared": True, "gui_launched": False, "llm_used": False,
                "driver": str(driver.resolve()), "apps": apps, "developer_only": True}
    (folder / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def selector(name, role=None):
    return {"name": name, **({"role": role} if role else {})}


def expect(name, value):
    return {"selector": selector(name, "Edit"), "property": "value", "equals": value}


def run_fixture(manifest, mode, delivery="foreground", bundle=None, trace_exceptions=False):
    folder = Path(manifest["folder"])
    apps = manifest["apps"]
    records, owned = [], []
    client = None
    report = {"folder": str(folder), "mode": mode, "delivery": delivery, "passed": False,
              "llm_used": False, "fixture_type": "native_winforms_standard_controls",
              "program_ids": [app["id"] for app in apps], "scenarios": {}}

    def call(name, args=None, allow_error=False):
        started = time.monotonic()
        answer = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        records.append({"tool": name, "seconds": round(time.monotonic() - started, 4), "result": safe(answer)})
        (folder / "steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if answer.get("isError") and not allow_error:
            raise RuntimeError("MCP failed: " + name + " " + json.dumps(safe(decoded(answer)), ensure_ascii=False)[:2000])
        return decoded(answer)

    def perform(target, step):
        answer = call("computer_perform", {**target, "delivery_mode": delivery, "step": step})
        if answer.get("task_verified") is not True:
            raise AssertionError("Operation postcondition was not verified")
        return answer

    def receipt(app):
        value = json.loads(Path(app["receipt"]).read_text(encoding="utf-8"))
        if value.get("synthetic_fixture") is not True:
            raise AssertionError("Independent fixture receipt was not synthetic")
        return value

    try:
        for app in apps:
            proc = subprocess.Popen([app["exe"], app["title"], app["receipt"]], cwd=folder,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            owned.append(OwnedProcess(proc))
        if bundle:
            from build_portable import runtime_environment
            client = Client(folder / "config.json", bundle / "server.py", bundle / "runtime/python.exe",
                            runtime_environment(bundle / "runtime"))
        else:
            server = Path(__file__).with_name("server.py")
            if trace_exceptions:
                wrapper = folder / "debug-server.py"
                wrapper.write_text("import sys, pathlib, traceback\n"
                    + "sys.path.insert(0, " + repr(str(server.parent)) + ")\n"
                    + "import operations, server, workflows\n"
                    + "original = operations.Operations.execute\n"
                    + "def traced(self, *args, **kwargs):\n"
                    + "    try:\n        return original(self, *args, **kwargs)\n"
                    + "    except Exception:\n"
                    + "        pathlib.Path(" + repr(str(folder / "operation-exception.txt")) + ").write_text(traceback.format_exc(), encoding='utf-8')\n"
                    + "        raise\n"
                    + "operations.Operations.execute = traced\n"
                    + "original_write = workflows.atomic_json\n"
                    + "def traced_write(*args, **kwargs):\n"
                    + "    try:\n        return original_write(*args, **kwargs)\n"
                    + "    except Exception:\n"
                    + "        pathlib.Path(" + repr(str(folder / "checkpoint-exception.txt")) + ").write_text(traceback.format_exc(), encoding='utf-8')\n"
                    + "        raise\n"
                    + "workflows.atomic_json = traced_write\n"
                    + "server.main()\n", encoding="utf-8")
                server = wrapper
            client = Client(folder / "config.json", server)
        schemas = client.request("tools/list")
        report["available_tools"] = [tool["name"] for tool in schemas["tools"]]
        if "computer_inspect" not in report["available_tools"]:
            raise AssertionError("computer_inspect is not implemented by this server")
        call("computer_begin", {"program_ids": report["program_ids"],
            "task_description": "이번 실행에서 새로 연 네이티브 합성 시험 창 A/B의 입력·선택·체크·키와 결과만 검증합니다."})
        deadline = time.monotonic() + 20
        targets = {}
        while time.monotonic() < deadline:
            windows = call("list_windows", {"on_screen_only": True}).get("windows", [])
            for app in apps:
                matches = [window for window in windows if window.get("title") == app["title"]]
                if len(matches) == 1:
                    targets[app["id"]] = {key: matches[0][key] for key in ("pid", "window_id")}
            if len(targets) == len(apps):
                break
            time.sleep(.2)
        if len(targets) != len(apps):
            raise AssertionError("Two unique native fixture windows were not discovered")
        report["targets"] = targets
        report["inspections"] = {}
        for app in apps:
            target = targets[app["id"]]
            view = call("computer_inspect", target)
            inspection = view.get("inspection", {})
            controls = inspection.get("controls", [])
            names = {control.get("name") for control in controls}
            if not {"작업 문구", "처리 상태", "확인 체크", "적용"} <= names:
                raise AssertionError("Native controls missing from computer_inspect: " + str(sorted(str(name) for name in names)))
            report["inspections"][app["id"]] = inspection
        target = targets[apps[0]["id"]]
        if mode in {"write", "all"}:
            written = perform(target, {"operation": "set_value", "selector": selector("작업 문구", "Edit"), "value": "합성 입력 A"})
            applied = perform(target, {"operation": "click", "selector": selector("적용", "Button"),
                "expect": [expect("적용 결과", "확인: 합성 입력 A"), expect("적용 횟수", "1")]})
            proof = receipt(apps[0])
            assert proof["input"] == "합성 입력 A" and proof["applied"] == "확인: 합성 입력 A" and proof["apply_count"] == 1
            report["scenarios"]["write"] = {"passed": True, "operations": [written, applied], "receipt": proof}
        if mode in {"select", "all"}:
            selected = perform(target, {"operation": "select_option", "selector": selector("처리 상태", "ComboBox"),
                "value": "진행", "option_order": ["대기", "진행", "완료"],
                "expect": [expect("선택 결과", "진행")]})
            proof = receipt(apps[0]); assert proof["status"] == "진행" and proof["selection_count"] >= 1
            report["scenarios"]["select"] = {"passed": True, "operation": selected, "receipt": proof,
                                                "option_order_source": "Exact labels/order defined in native_fixture.py; not inferred from an unopened popup"}
        if mode in {"check", "all"}:
            checked = perform(target, {"operation": "set_checked", "selector": selector("확인 체크", "CheckBox"),
                "checked": True, "expect": [expect("체크 결과", "선택")]})
            proof = receipt(apps[0]); assert proof["checked"] is True
            unchanged = perform(target, {"operation": "set_checked", "selector": selector("확인 체크", "CheckBox"), "checked": True})
            assert unchanged.get("input_dispatched") is False, "Already checked state must not toggle again"
            report["scenarios"]["check"] = {"passed": True, "operations": [checked, unchanged], "receipt": proof}
        if mode in {"keys", "all"}:
            keyed = perform(target, {"operation": "press_key", "selector": selector("작업 문구", "Edit"), "key": "home",
                "expect": [expect("키 입력 결과", "Home")]})
            proof = receipt(apps[0]); assert proof["last_key"] == "Home" and proof["key_count"] >= 1
            report["scenarios"]["keys"] = {"passed": True, "operation": keyed, "receipt": proof}
        if mode in {"list", "all"}:
            selected = perform(target, {"operation": "select_item", "selector": selector("합성 작업 2", "ListItem")})
            report["scenarios"]["list"] = {"passed": True, "operation": selected}
        if mode in {"multi", "all"}:
            task = {"id": "native_transfer", "name": "두 네이티브 앱 합성 입력", "instructions": "A와 B에 같은 합성 문구를 입력하고 B에서 적용합니다.",
                    "expected": "각 앱의 입력값과 B의 적용 결과가 일치합니다.", "program_ids": report["program_ids"],
                    "variables": {"word": {"description": "합성 입력"}}, "steps": [
                        {"program_id": "native_a", "operation": "set_value", "selector": selector("작업 문구", "Edit"), "value": "${word}"},
                        {"program_id": "native_b", "operation": "set_value", "selector": selector("작업 문구", "Edit"), "value": "${word}"},
                        {"program_id": "native_b", "operation": "click", "selector": selector("적용", "Button"),
                         "expect": [expect("적용 결과", "확인: ${word}")]}]}
            call("computer_save_task", task)
            arguments = {"task_id": task["id"], "inputs": {"word": "두 앱 합성 123"}, "delivery_mode": delivery,
                         "targets": [{"program_id": app_id, **window} for app_id, window in targets.items()]}
            workflow = call("computer_run_task", arguments)
            assert workflow.get("task_verified") is True and workflow.get("completed_steps") == 3
            a, b = receipt(apps[0]), receipt(apps[1])
            assert a["input"] == b["input"] == "두 앱 합성 123" and b["applied"] == "확인: 두 앱 합성 123"
            resumed = call("computer_run_task", {**arguments, "resume_run_id": workflow["run_id"]})
            assert resumed.get("task_verified") is True and resumed["last_result"].get("input_dispatched") is False
            assert receipt(apps[1])["apply_count"] == b["apply_count"], "Resume replayed the apply button"
            report["scenarios"]["multi"] = {"passed": True, "workflow": workflow, "resume": resumed,
                                             "receipts": [a, b], "resume_did_not_replay": True}
        if mode in {"windows", "all"}:
            task = {"id": "native_windows", "name": "같은 앱 보조 창과 부모 범위", "instructions": "보조 창을 열고 제목으로 연결하여 각 부모 범위의 입력칸을 변경합니다.",
                    "expected": "보조 창 입력값과 두 그룹의 입력값이 각각 일치합니다.", "program_ids": ["native_a"], "steps": [
                        {"program_id": "native_a", "operation": "click", "selector": selector("보조 창 열기", "Button"),
                         "expect": [expect("보조 창 결과", "열림")]},
                        {"program_id": "native_a", "window_ref": "detail", "operation": "set_value",
                         "selector": selector("보조 입력", "Edit"), "value": "보조 합성 입력"},
                        {"program_id": "native_a", "window_ref": "detail", "operation": "set_value",
                         "selector": {"name": "범위 입력", "role": "Edit", "within": {"name": "왼쪽 그룹"}}, "value": "왼쪽 합성"},
                        {"program_id": "native_a", "window_ref": "detail", "operation": "set_value",
                         "selector": {"name": "범위 입력", "role": "Edit", "within": {"name": "오른쪽 그룹"}}, "value": "오른쪽 합성"}]}
            call("computer_save_task", task)
            arguments = {"task_id": task["id"], "delivery_mode": delivery, "targets": [
                {"program_id": "native_a", **target},
                {"program_id": "native_a", "window_ref": "detail", "pid": target["pid"], "window_title": "Native Fixture Detail - A"}]}
            workflow = call("computer_run_task", arguments)
            assert workflow.get("task_verified") is True and workflow.get("completed_steps") == 4
            proof = receipt(apps[0])
            assert proof["detail_open_count"] == 1 and proof["detail_input"] == "보조 합성 입력"
            assert proof["scope_left"] == "왼쪽 합성" and proof["scope_right"] == "오른쪽 합성"
            resumed = call("computer_run_task", {**arguments, "resume_run_id": workflow["run_id"]})
            assert resumed.get("task_verified") is True and resumed["last_result"].get("input_dispatched") is False
            assert receipt(apps[0])["detail_open_count"] == 1
            report["scenarios"]["windows"] = {"passed": True, "workflow": workflow, "resume": resumed,
                                               "receipt": proof, "late_bound_by_exact_title": True, "within_disambiguation": True}
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)[:4000]
        if client is not None and "targets" in locals():
            failure_states = []
            for app in apps:
                if app["id"] not in targets:
                    continue
                try:
                    rows = call("list_windows", {"pid": targets[app["id"]]["pid"], "on_screen_only": True}, allow_error=True)
                    for window in rows.get("windows", []):
                        if window.get("title") in {app["title"], "Native Fixture Detail - " + app["label"]}:
                            state = call("get_window_state", {**{k: window[k] for k in ("pid", "window_id")},
                                                               "max_depth": 32, "max_elements": 5000}, allow_error=True)
                            failure_states.append({"window": window, "state": state})
                except Exception as diagnostic_error:
                    failure_states.append({"program_id": app["id"], "error": str(diagnostic_error)[:1000]})
            (folder / "failure-states.json").write_text(json.dumps(failure_states, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if client is not None:
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = str(error)[:500]
                report["passed"] = False
                if client.child.poll() is None:
                    client.child.kill()
        for owner in owned:
            owner.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=DATA / "generic-validation" / uuid.uuid4().hex[:10])
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--mode", choices=MODES, default="probe")
    parser.add_argument("--delivery", choices=["background", "foreground"], default="foreground")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true", help="Explicitly launch owned test windows and use the real MCP/Driver")
    parser.add_argument("--trace-exceptions", action="store_true", help="Developer-only stack capture for synthetic fixture execution")
    options = parser.parse_args()
    manifest = prepare(options.folder, options.driver)
    answer = run_fixture(manifest, options.mode, options.delivery, options.bundle, options.trace_exceptions) if options.run else manifest
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0 if not options.run or answer["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
