"""Developer-only acceptance of learned elements through a real MCP and Driver.

Prepare compiles isolated synthetic WinForms fixtures. --run explicitly opens
one owned application, teaches observed controls, changes synthetic values,
closes/reopens it, reconnects MCP, and checks saved selectors against fresh UI.
No private applications, live client profiles, external LLM or F8 helper input.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from generic_validation import prepare, expect, selector
from live_validation import Client, DRIVER, decoded, safe
from native_fixture import DATA


def run_learning(manifest, bundle=None):
    folder = Path(manifest["folder"])
    app = manifest["apps"][0]
    records, processes = [], []
    client = None
    report = {"passed": False, "developer_only": True, "synthetic_fixture": True,
              "external_llm_used": False, "private_application_used": False,
              "native_f8_helper_tested": False, "administrator_gui_tested": False,
              "scenarios": {}}

    def save():
        (folder / "learning-steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "learning-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def call(name, args=None, allow_error=False):
        started = time.monotonic()
        answer = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        records.append({"tool": name, "seconds": round(time.monotonic()-started, 3), "result": safe(answer)})
        save()
        if answer.get("isError") and not allow_error:
            raise RuntimeError(name + " failed: " + json.dumps(safe(decoded(answer)), ensure_ascii=False)[:2500])
        return decoded(answer)

    def connect():
        if bundle:
            from build_portable import runtime_environment
            return Client(folder / "config.json", bundle / "server.py", bundle / "runtime/python.exe",
                          runtime_environment(bundle / "runtime"))
        return Client(folder / "config.json")

    def begin():
        call("computer_begin", {"program_ids": [app["id"]],
             "task_description": "새로 연 합성 WinForms 시험 앱의 입력칸·선택상자·두 영역의 같은 이름 입력칸을 학습하고 검증한 뒤 종료합니다."})

    def spawn(suffix):
        title = app["title"] + " " + suffix
        process = subprocess.Popen([app["exe"], title, app["receipt"]], cwd=folder,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        return process, title

    def target_for(title, pid):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            windows = call("list_windows", {"pid": pid, "on_screen_only": True}).get("windows", [])
            matches = [window for window in windows if window.get("title") == title]
            if len(matches) == 1:
                return {key: matches[0][key] for key in ("pid", "window_id")}
            time.sleep(.15)
        raise AssertionError("Owned fixture window missing: " + title)

    def teach(target, name, role, label, within=None):
        inspection = call("computer_inspect", {**target, "search": name, "max_depth": 32,
                          "max_elements": 5000, **({"within": within} if within else {})})["inspection"]
        found = [item for item in inspection["controls"] if item.get("name") == name and item.get("role") == role]
        if len(found) != 1 or found[0].get("selector_unique") is not True:
            raise AssertionError("Exact live control was not uniquely observed: " + label)
        item = found[0]
        taught = call("computer_teach_element", {**target, "program_id": app["id"], "label": label,
            "screen": "합성 학습 검증", "element_index": item["element_index"], "expected_selector": item["selector"]})
        assert taught["status"] == "learned" and taught["input_dispatched"] is False
        assert taught["model_trained"] is False
        return taught["id"]

    def use(target, element_id, step):
        answer = call("computer_use_element", {**target, "id": element_id, "step": step, "delivery_mode": "foreground"})
        assert answer["task_verified"] is True, answer
        return answer

    def receipt():
        proof = json.loads(Path(app["receipt"]).read_text(encoding="utf-8"))
        assert proof["synthetic_fixture"] is True
        return proof

    def open_detail(target):
        value = call("computer_perform", {**target, "delivery_mode": "foreground", "step": {
            "operation": "click", "selector": selector("보조 창 열기", "Button"),
            "expect": [expect("보조 창 결과", "열림")]}})
        assert value["task_verified"] is True
        return target_for("Native Fixture Detail - " + app["label"], target["pid"])

    def close(target, process):
        value = call("computer_close", {**target, "scope": "process", "delivery_mode": "foreground",
            "close_action": {"operation": "hotkey", "keys": ["alt", "f4"]}, "timeout_ms": 5000})
        assert value["status"] == "verified" and value["task_verified"] is True, value
        assert process.wait(timeout=5) == 0
        return value

    try:
        first, first_title = spawn("first")
        client = connect()
        report["version"] = call("computer_status")["version"]
        available = {tool["name"] for tool in client.request("tools/list")["tools"]}
        assert {"computer_elements", "computer_teach_element", "computer_find_element", "computer_use_element"} <= available
        begin()
        main = target_for(first_title, first.pid)
        ids = {"input": teach(main, "작업 문구", "Edit", "업무 문구"),
               "combo": teach(main, "처리 상태", "ComboBox", "진행 상태")}
        use(main, ids["input"], {"operation": "set_value", "value": "학습 요소 재사용 080"})
        use(main, ids["combo"], {"operation": "select_option", "value": "진행",
            "option_order": ["대기", "진행", "완료"], "expect": [expect("선택 결과", "진행")]})
        proof = receipt()
        assert proof["input"] == "학습 요소 재사용 080" and proof["status"] == "진행"
        report["scenarios"]["input_and_combobox"] = {"passed": True, "receipt": proof,
            "option_order_source": "Exact synthetic fixture source, not guessed from a closed menu"}
        detail = open_detail(main)
        ids["left"] = teach(detail, "범위 입력", "Edit", "왼쪽 업무 코드", {"name": "왼쪽 그룹"})
        ids["right"] = teach(detail, "범위 입력", "Edit", "오른쪽 업무 코드", {"name": "오른쪽 그룹"})
        use(detail, ids["left"], {"operation": "set_value", "value": "왼쪽 080"})
        use(detail, ids["right"], {"operation": "set_value", "value": "오른쪽 080"})
        proof = receipt()
        assert proof["scope_left"] == "왼쪽 080" and proof["scope_right"] == "오른쪽 080"
        report["scenarios"]["duplicate_labels_in_groups"] = {"passed": True, "receipt": proof}
        stored = json.loads((folder / "state/elements.json").read_text(encoding="utf-8"))
        forbidden = {"pid", "window_id", "element_index", "parent_index", "element_token", "snapshot_id", "bounds", "value"}
        def audit(value):
            if isinstance(value, dict):
                assert not (set(value) & forbidden), value
                for child in value.values(): audit(child)
            elif isinstance(value, list):
                for child in value: audit(child)
        audit(stored)
        raw = json.dumps(stored, ensure_ascii=False)
        assert all(value not in raw for value in ("학습 요소 재사용 080", "왼쪽 080", "오른쪽 080"))
        assert len(stored["elements"]) == 4
        report["scenarios"]["inert_persistence"] = {"passed": True, "count": 4,
            "runtime_handles_stored": False, "input_values_stored": False, "screenshots_stored": False}
        first_close = close(main, first)
        call("computer_end")
        client.close()
        client = None

        second, second_title = spawn("reopened")
        assert second.pid != first.pid
        client = connect()
        listed = call("computer_elements", {"program_id": app["id"], "query": "업무"})
        assert listed["total"] == 3 and listed["screen_accessed"] is False
        begin()
        reopened = target_for(second_title, second.pid)
        found = call("computer_find_element", {**reopened, "id": ids["input"]})
        assert found["input_dispatched"] is False
        use(reopened, ids["input"], {"operation": "set_value", "value": "다시 연 창에서도 학습 재사용"})
        use(reopened, ids["combo"], {"operation": "select_option", "value": "완료",
            "option_order": ["대기", "진행", "완료"], "expect": [expect("선택 결과", "완료")]})
        detail = open_detail(reopened)
        use(detail, ids["left"], {"operation": "set_value", "value": "재실행 왼쪽"})
        use(detail, ids["right"], {"operation": "set_value", "value": "재실행 오른쪽"})
        proof = receipt()
        assert proof["input"] == "다시 연 창에서도 학습 재사용" and proof["status"] == "완료"
        assert proof["scope_left"] == "재실행 왼쪽" and proof["scope_right"] == "재실행 오른쪽"
        report["scenarios"]["reopen_and_mcp_reconnect"] = {"passed": True, "first_target": main,
            "new_target": reopened, "receipt": proof, "aliases_relearned": False}
        second_close = close(reopened, second)
        report["scenarios"]["verified_closure"] = {"passed": True, "first": first_close, "second": second_close}
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)[:4000]
    finally:
        if client is not None:
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = str(error)[:1000]
                report["passed"] = False
                if client.child.poll() is None:
                    client.child.kill()  # Exact owned test MCP only.
                    client.child.wait(timeout=5)
        for process in processes:
            if process.poll() is None:
                # Failure cleanup only; never a business-app closure proof.
                process.terminate()
                process.wait(timeout=5)
                report["fixture_failure_cleanup_used"] = True
        save()
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=DATA / "learning-validation" / uuid.uuid4().hex[:10])
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    if (options.folder / "state/elements.json").exists():
        raise ValueError("Use a fresh isolated validation folder; existing taught elements are retained")
    manifest = prepare(options.folder, options.driver)
    result = run_learning(manifest, options.bundle) if options.run else manifest
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not options.run or result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
