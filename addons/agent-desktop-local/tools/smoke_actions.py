"""Exercise only the bundled synthetic fixture; never discovers or acts on other apps.

Windows requires a locally installed .NET Framework C# compiler. No downloads.
The fixture shows its own test window. --include-headed permits focusing that
window and sending text to its observed editor. Evidence stays in --work-dir.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Fixture:
    def __init__(self, executable):
        self.process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                if len(line) < 1024 * 1024:
                    self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read, daemon=True).start()
        self.ready = self.receive()
        if self.ready.get("ready") is not True or self.ready.get("pid") != self.process.pid:
            self.close()
            raise RuntimeError("fixture_identity_invalid")
        time.sleep(0.5)

    def receive(self):
        line = self.lines.get(timeout=15)
        if line is None:
            raise RuntimeError("fixture_exited")
        value = json.loads(line)
        if value.get("error"):
            raise RuntimeError("fixture_command_failed")
        return value

    def command(self, name):
        self.process.stdin.write(json.dumps({"command": name}) + "\n")
        self.process.stdin.flush()
        value = self.receive()
        if value.get("pid") != self.process.pid:
            raise RuntimeError("fixture_identity_changed")
        return value

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('{"command":"quit"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class Suite:
    def __init__(self, addon, work, fixture):
        self.addon, self.work, self.fixture = addon, work, fixture
        self.results = []

    def wrapper(self, *arguments):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B",
            str(self.addon / "scripts/interact.py"), *map(str, arguments)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            receipt = json.loads(result.stdout)
        except ValueError:
            receipt = {"status": "invalid_wrapper_response"}
        return {"exit_code": result.returncode, "receipt": receipt,
                "stderr_present": bool(result.stderr)}

    def inspect(self, hwnd=None):
        result = self.wrapper("inspect", "--exe-name", "ActionFixture.exe",
            "--pid", self.fixture.process.pid, "--hwnd", hwnd or self.fixture.ready["hwnd"],
            "--output", self.work)
        if result["exit_code"] != 0:
            raise RuntimeError("inspect_failed:" + json.dumps(result["receipt"]))
        session = self.work / result["receipt"]["receipt"]
        inspection = json.loads((session / "inspection.json").read_text(encoding="utf-8"))
        nodes = inspection.get("nodes", inspection.get("recognition", {}).get("nodes", []))
        return session, nodes

    def node(self, nodes, *, name=None, role=None, value=None, native_id=None):
        matches = []
        for row in nodes:
            native = row.get("native_id")
            if (row.get("ref") and (name is None or row.get("name") == name)
                    and (role is None or row.get("role") == role)
                    and (value is None or row.get("value") == value)
                    and (native_id is None or isinstance(native, dict) and native.get("value") == native_id)):
                matches.append(row)
        if len(matches) != 1:
            raise RuntimeError("fixture_target_not_unique")
        return matches[0]

    def act(self, session, node, action, value=None, headed=False):
        command = ["act", "--session", session, "--ref", node["ref"], "--action", action]
        if value is not None:
            command += ["--value", value]
        if headed:
            command += ["--headed"]
        result = self.wrapper(*command)
        time.sleep(0.15)
        result["fixture_state"] = self.fixture.command("state")
        return result

    def record(self, name, passed, evidence):
        item = {"case": name, "passed": bool(passed), **evidence}
        self.results.append(item)
        save(self.work / (name + ".json"), item)
        print(json.dumps({"case": name, "passed": bool(passed)}), flush=True)

    def change(self, case, selector, action, expected_key, expected_value, value=None, headed=False):
        session, nodes = self.inspect()
        result = self.act(session, self.node(nodes, **selector), action, value, headed)
        passed = (result["fixture_state"].get(expected_key) == expected_value
                  and result["receipt"].get("input_dispatched") is True
                  and result["receipt"].get("after_snapshot_available") is True)
        if action in {"set-value", "clear"}:
            passed = passed and result["receipt"].get("status") == "completed" and result["receipt"].get("effect_verification") == "matched"
        self.record(case, passed, result)
        return session, nodes, result

    def run(self, include_headed):
        before = self.fixture.command("state")
        self.inspect()
        after = self.fixture.command("state")
        self.record("inspect_does_not_mutate", before == after, {"before": before, "after": after})
        self.change("combo_set_value", {"name": "MES Setup", "role": "combobox"},
                    "set-value", "combo", "N", value="N")
        self.change("text_set_value", {"native_id": "NotesEditor"},
                    "set-value", "notes", "local text", value="local text")
        self.change("checkbox_check", {"native_id": "EnabledOption"}, "check", "option", True)
        self.change("checkbox_uncheck", {"native_id": "EnabledOption"}, "uncheck", "option", False)
        session, nodes, result = self.change("button_click_once", {"native_id": "QueryButton"},
                    "click", "query_count", 1)
        replay = self.act(session, self.node(nodes, native_id="QueryButton"), "click")
        self.record("consumed_session_rejected", replay["exit_code"] != 0
                    and replay["fixture_state"]["query_count"] == 1
                    and replay["receipt"].get("input_dispatched") is False, replay)
        self.change("text_clear", {"native_id": "NotesEditor"}, "clear", "notes", "")
        session, nodes = self.inspect()
        result = self.act(session, self.node(nodes, native_id="DisabledButton"), "click")
        self.record("disabled_button_rejected", result["exit_code"] != 0
                    and result["fixture_state"]["disabled_count"] == 0
                    and result["receipt"].get("input_dispatched") is False, result)
        session, nodes = self.inspect()
        result = self.act(session, self.node(nodes, name="MES Setup", role="combobox"), "select", "Y")
        self.record("unsupported_select_fixture_unchanged", result["exit_code"] != 0
                    and result["fixture_state"]["combo"] == "N"
                    and result["receipt"].get("input_dispatched") in (False, None),
                    {**result, "expected_limitation": "Select is not exposed; receipt may conservatively mark possible dispatch"})
        self.change("menu_open", {"name": "Operations", "role": "menuitem"},
                    "click", "menu_open", True)
        self.change("submenu_open", {"name": "Export", "role": "menuitem"},
                    "click", "submenu_open", True)
        session, nodes = self.inspect()
        menu_node = self.node(nodes, name="Active sheet", role="menuitem")
        result = self.act(session, menu_node, "click")
        self.record("menu_leaf_offscreen_guard", "offscreen" in menu_node.get("states", [])
                    and result["exit_code"] != 0 and result["receipt"].get("input_dispatched") is False
                    and result["fixture_state"]["active_count"] == 0,
                    {**result, "expected_limitation": "This WinForms nested leaf is reported offscreen; selection was not executed"})
        self.fixture.command("reset")
        self.fixture.command("grid-editor")
        self.change("grid_editor_value", {"name": "Grid value editor", "role": "textfield"},
                    "set-value", "editor_value", "edited cell", value="edited cell")
        state = self.fixture.command("grid-commit")
        self.record("grid_editor_commit_by_fixture", state["row_value"] == "edited cell",
                    {"fixture_state": state, "commit_method": "fixture_internal_EndEdit_not_agent_business_save"})
        self.fixture.command("reset")
        sibling = self.fixture.command("sibling")
        main_session, main_nodes = self.inspect()
        sibling_session, sibling_nodes = self.inspect(sibling["sibling_hwnd"])
        result = self.act(main_session, self.node(sibling_nodes, native_id="QueryButton"), "click")
        self.record("other_window_ref_rejected", result["exit_code"] != 0
                    and result["fixture_state"]["query_count"] == 0
                    and result["fixture_state"]["sibling_count"] == 0
                    and result["receipt"].get("input_dispatched") is False, result)
        self.change("same_pid_main_window_only", {"native_id": "QueryButton"}, "click", "query_count", 1)
        state = self.fixture.command("state")
        self.record("sibling_not_affected", state["sibling_count"] == 0 and state["sibling_combo"] == "Y",
                    {"fixture_state": state})
        if include_headed:
            self.fixture.command("reset")
            self.change("headed_text_prepare_empty", {"native_id": "NotesEditor"},
                        "clear", "notes", "")
            self.change("headed_text_focus", {"native_id": "NotesEditor"},
                        "focus", "notes_focused", True, headed=True)
            self.change("headed_text_type", {"native_id": "NotesEditor"},
                        "type", "notes", "typed", value="typed", headed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addon-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--include-headed", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute():
        parser.error("Windows and an absolute local work directory are required")
    addon = args.addon_dir.resolve(strict=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work = args.work_dir.resolve(strict=True)
    sys.path.insert(0, str(addon / "scripts"))
    from local_core import validate_local_directory
    validate_local_directory(work)
    compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        parser.error("The local .NET Framework C# compiler is required; no download is performed")
    executable = work / "ActionFixture.exe"
    built = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(executable),
        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll",
        str(addon / "tests/fixtures/ActionFixture.cs")], capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if built.returncode:
        parser.error("synthetic fixture compilation failed")
    fixture = Fixture(executable)
    suite = Suite(addon, work, fixture)
    error = None
    try:
        suite.run(args.include_headed)
    except Exception as exc:
        # Keep exception paths and arbitrary provider text out of console output.
        error = type(exc).__name__
    finally:
        fixture.close()
    summary = {"synthetic_only": True, "actual_fx_tested": False,
        "headed_enabled": args.include_headed, "passed": sum(r["passed"] for r in suite.results),
        "total": len(suite.results), "complete": error is None, "error": error, "results": suite.results}
    save(work / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}), flush=True)
    return 0 if error is None and all(r["passed"] for r in suite.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
