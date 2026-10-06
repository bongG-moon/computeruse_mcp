"""Developer acceptance test through the real stdio MCP, using synthetic data only.

Not shipped in the end-user bundle. Run on the interactive Windows desktop.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from settings import default_config, save_config

HERE = Path(__file__).resolve().parent
DATA = HERE / ".data" / "live-validation"
DRIVER = HERE.parent / "tmp/cua-oneclick/extracted/cua-driver-rs-0.28.2-windows-x86_64/cua-driver.exe"
MARKER = "Computer Use MCP independent test 2026-10-03 / 한글 입력 확인"


def safe(value):
    if isinstance(value, dict):
        return {k: "<image omitted>" if k == "data" and value.get("type") == "image" else safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(x) for x in value]
    return value


class Client:
    def __init__(self, config, server=HERE / "server.py", python=sys.executable, env=None, *, command=None, initialize_timeout=35):
        self.child = subprocess.Popen(command or [str(python), "-B", "-s", str(server), "--config", str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.index = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "local-acceptance-test", "version": "1"}}, timeout=initialize_timeout)
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()  # Only our exact synthetic-test MCP/bridge process.
                self.child.wait(timeout=5)
            raise

    def _read(self):
        for line in self.child.stdout:
            self.messages.put(json.loads(line))

    def send(self, message):
        self.child.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.child.stdin.flush()

    def request(self, method, params=None, timeout=35):
        self.index += 1
        self.send({"jsonrpc": "2.0", "id": self.index, "method": method, "params": params or {}})
        result = self.messages.get(timeout=timeout)
        assert result.get("id") == self.index, result
        assert "error" not in result, result
        return result["result"]

    def tool(self, name, args=None, allow_error=False, timeout=35):
        value = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout)
        if name == "get_window_state" and not value.get("isError"):
            info = decoded(value)
            summary = {"window_title": info.get("window_title"), "snapshot_id": info.get("snapshot_id"),
                       "documents": [e for e in info.get("elements", []) if e.get("role") == "Document"],
                       "image_count": sum(x.get("type") == "image" for x in value.get("content", []))}
        else:
            summary = decoded(value)
        print(json.dumps({"tool": name, "result": safe(summary)}, ensure_ascii=False), flush=True)
        if not allow_error:
            assert not value.get("isError"), value
        return value

    def close(self):
        self.child.stdin.close()
        self.child.wait(timeout=10)
        error = self.child.stderr.read()
        assert self.child.returncode == 0, (self.child.returncode, error)
        assert not error.strip(), error


def decoded(value):
    if "structuredContent" in value:
        return value["structuredContent"]
    for item in value.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except ValueError:
                return item["text"]
    return value


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["snapshot", "input", "visual", "approval", "packaged"])
    options = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    config_file = DATA / ("approval.json" if options.mode == "approval" else "config.json")
    config = default_config(config_file)
    config.update(driver=str(DRIVER), approval="session" if options.mode == "approval" else "client", max_minutes=3, max_actions=12)
    config["programs"] = [p for p in config["programs"] if p["id"] == "notepad"]
    assert len(config["programs"]) == 1
    config["programs"][0]["enabled"] = True
    save_config(config_file, config)
    fixture = DATA / "MCP-SYNTHETIC-TEST.txt"
    if not fixture.exists():
        fixture.write_text("MCP synthetic test document.\n", encoding="utf-8")
        subprocess.Popen([config["programs"][0]["exe"], str(fixture)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
    if options.mode == "packaged":
        from build_portable import runtime_environment
        bundle = HERE / "release/Computer-Use-MCP"
        client = Client(config_file, bundle / "server.py", bundle / "runtime/python.exe", runtime_environment(bundle / "runtime"))
    else:
        client = Client(config_file)
    try:
        tools = client.request("tools/list")["tools"]
        print(json.dumps({"tools": len(tools), "tool_names": [t["name"] for t in tools]}, ensure_ascii=False), flush=True)
        client.tool("computer_status")
        client.tool("computer_programs")
        denied = client.tool("type_text", {"text": "should never be typed", "pid": 1, "window_id": 1}, True)
        assert denied.get("isError") is True
        saved = decoded(client.tool("computer_save_task", {"id": "synthetic-check", "name": "시험용 메모장 확인",
            "instructions": "MCP-SYNTHETIC-TEST.txt 시험 문서만 확인합니다.", "expected": "시험 문구를 다시 읽습니다.", "program_ids": ["notepad"]}))
        client.tool("computer_get_task", {"id": "synthetic-check"})
        begun = client.tool("computer_begin", {"program_ids": ["notepad"], "mode": "visual" if options.mode == "visual" else "uia",
            "task_description": "직접 만든 MCP-SYNTHETIC-TEST.txt 시험 문서의 화면만 읽고 시험 문구를 입력합니다. 기존 개인 문서는 변경하지 않습니다."},
            allow_error=options.mode == "approval", timeout=90 if options.mode == "approval" else 35)
        if options.mode == "approval":
            assert begun.get("isError"), "This test requires declining the real native dialog."
            print("NATIVE_DIALOG_DECLINED: PASS", flush=True)
            return
        windows = decoded(client.tool("list_windows"))
        (DATA / "windows.json").write_text(json.dumps(windows, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = windows if isinstance(windows, list) else windows.get("windows", [])
        selected = [w for w in rows if "MCP-SYNTHETIC-TEST" in w.get("title", "")]
        assert len(selected) == 1, windows
        window = selected[0]
        target = {"pid": window["pid"], "window_id": window["window_id"]}
        state = client.tool("get_window_state", target)
        (DATA / (options.mode + "-state.json")).write_text(json.dumps(safe(state), ensure_ascii=False, indent=2), encoding="utf-8")
        if options.mode == "visual":
            assert any(x.get("type") == "image" for x in state.get("content", [])), "Visual image absent"
        if options.mode == "input":
            info = decoded(state)
            assert "MCP-SYNTHETIC-TEST.txt" in info["window_title"]
            docs = [e for e in info["elements"] if e.get("role") == "Document" and "set_value" in e.get("actions", [])]
            assert len(docs) == 1 and docs[0].get("value", "").startswith(("MCP synthetic test document.", MARKER)), docs
            # The current element handle points only at the synthetic document.
            expected = docs[0]["value"] + MARKER
            client.tool("type_text", dict(target, element_token=docs[0]["element_token"], text=MARKER))
            after = decoded(client.tool("get_window_state", target))
            actual = [e.get("value", "") for e in after["elements"] if e.get("role") == "Document"]
            assert len(actual) == 1 and actual[0] == expected, actual
            (DATA / "input-proof.json").write_text(json.dumps({"passed": True, "expected": expected,
                "actual": actual[0], "window": after["window_title"], "driver": "0.28.2", "llm_used": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        client.tool("computer_end")
        ended = client.tool("get_window_state", target, True)
        assert ended.get("isError") is True
        print("LIVE_STDIO_" + options.mode.upper() + ": PASS", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
