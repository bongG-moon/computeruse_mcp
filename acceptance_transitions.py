"""Owned native-window transitions and chat registration through actual MCP stdio.

Default: compile isolated synthetic fixtures only. --run: exercise the product's
MCP/Driver, never an ad-hoc UIA client. No personal config, apps, files or network.
Run alone on the interactive desktop; --bundle selects the packaged server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid

from live_validation import Client, DRIVER, HERE, decoded, safe
from native_fixture import compiler_path
from settings import VERSION, save_config
from vendor.windows import OwnedProcess

DATA = HERE / ".data" / "transitions-validation"
SOURCE = r'''using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class TransitionFixture : ApplicationContext {
    readonly string title, receipt, mode;
    readonly List<Form> forms = new List<Form>();
    readonly Timer lifetime = new Timer();
    Form original;
    int transitions, textChanges;
    string text = "ready";
    bool originalClosed;
    long originalHandle;
    static void Identify(Control c, string name) { c.Name = name.Replace(" ", ""); c.AccessibleName = name; }
    void Save() {
        var windows = new List<object>();
        foreach (var f in forms) if (!f.IsDisposed && f.IsHandleCreated)
            windows.Add(new { title = f.Text, window_id = f.Handle.ToInt64(), visible = f.Visible });
        var data = new { synthetic_fixture = true, mode = mode, click_count = transitions,
            text_change_count = textChanges, input = text, original_closed = originalClosed,
            original_window_id = originalHandle, windows = windows };
        string tmp = receipt + ".tmp";
        File.WriteAllText(tmp, new JavaScriptSerializer().Serialize(data), new UTF8Encoding(false));
        if (File.Exists(receipt)) File.Replace(tmp, receipt, null); else File.Move(tmp, receipt);
    }
    Form Make(string caption, bool destination, int offset) {
        var form = new Form(); form.Text = caption; Identify(form, caption);
        form.Font = new Font("Segoe UI", 10F); form.ClientSize = new Size(590, 210);
        form.StartPosition = FormStartPosition.Manual; form.Location = new Point(90 + offset, 100 + offset);
        var note = new Label(); note.Text = "OWNED SYNTHETIC WINDOW / no personal data";
        note.Location = new Point(20, 20); note.Size = new Size(550, 30); form.Controls.Add(note);
        if (destination) {
            var edit = new TextBox(); Identify(edit, "Transition result"); edit.Text = "ready";
            edit.Location = new Point(20, 70); edit.Size = new Size(530, 30);
            edit.TextChanged += delegate { text = edit.Text; textChanges++; Save(); };
            form.Controls.Add(edit);
        } else {
            var button = new Button(); button.Text = "Transition once"; Identify(button, "Transition once");
            button.Location = new Point(20, 70); button.Size = new Size(260, 40);
            button.Click += delegate { transitions++; Save(); form.BeginInvoke(new Action(Transition)); };
            form.Controls.Add(button);
        }
        form.FormClosed += delegate { if (form == original) originalClosed = true; Save(); };
        forms.Add(form); return form;
    }
    void Transition() {
        if (mode != "missing") {
            var next = Make(title + " - Destination", true, 35);
            if (mode == "popup") next.Show(original); else next.Show();
            if (mode == "ambiguous") Make(title + " - Destination", true, 70).Show();
        }
        if (mode != "popup") original.Close();
        Save();
    }
    TransitionFixture(string fixtureTitle, string output, string fixtureMode) {
        title = fixtureTitle; receipt = output; mode = fixtureMode;
        original = Make(title, false, 0); original.Show(); originalHandle = original.Handle.ToInt64(); Save();
        // Last-resort lifetime bound; the harness normally closes only its owned process.
        lifetime.Interval = 240000; lifetime.Tick += delegate { lifetime.Stop(); Application.Exit(); };
        lifetime.Start();
    }
    [STAThread] static void Main(string[] args) {
        if (args.Length != 3) throw new ArgumentException("Three synthetic fixture arguments required");
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new TransitionFixture(args[0], args[1], args[2]));
    }
}
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned_folder(path):
    path = Path(path).absolute()
    base = DATA.resolve()
    if not path.resolve().is_relative_to(base) or path.resolve() == base:
        raise ValueError("Use a dedicated .data/transitions-validation subfolder")
    for item in [path, *path.parents]:
        if item == DATA.parent:
            break
        if item.exists() and (item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Synthetic fixture paths must not use links")
    return path


def prepare(folder, driver):
    folder = owned_folder(folder)
    folder.mkdir(parents=True, exist_ok=True)
    driver = Path(driver).resolve(strict=True)
    source = folder / "TransitionFixture.cs"
    source.write_text(SOURCE, encoding="utf-8-sig")
    apps = []
    for suffix in ("A", "B"):
        exe = folder / ("TransitionFixture" + suffix + ".exe")
        result = subprocess.run([str(compiler_path()), "/nologo", "/target:winexe", "/optimize+",
            "/codepage:65001", "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
            "/reference:System.Web.Extensions.dll", "/out:" + str(exe), str(source)],
            capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not exe.is_file():
            raise RuntimeError((result.stdout + result.stderr).decode("utf-8", "replace"))
        apps.append({"id": "transition_" + suffix.lower(), "name": "Synthetic transition " + suffix,
                     "exe": str(exe), "enabled": True, "control_exes": [], "hints": "Only this run's owned fixture"})
    config = {"version": 1, "driver": str(driver), "state_dir": str(folder / "state"), "programs": [apps[0]],
              "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 10,
              "max_actions": 80, "approval_timeout_seconds": 300, "observation_timeout_seconds": 20}
    save_config(folder / "config.json", config)
    manifest = {"prepared": True, "gui_launched": False, "developer_only": True, "folder": str(folder),
                "apps": apps, "driver": str(driver), "fixture_source_sha256": sha(source),
                "fixture_exe_sha256": {app["id"]: sha(app["exe"]) for app in apps}}
    (folder / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def run(manifest, bundle=None, seconds=300, scenario="all"):
    folder, root = Path(manifest["folder"]), Path(bundle).resolve() if bundle else HERE
    client = None
    owners, steps = [], []
    expired = threading.Event()
    started = time.monotonic()
    deadline = started + seconds
    report = {"passed": False, "llm_used": False, "personal_apps_used": False, "gui_launched": True,
              "bundle": str(root) if bundle else None, "folder": str(folder), "requested_scenario": scenario, "scenarios": {}, "steps": steps}
    names = ["server.py", "programs.py", "program_registration.py", "settings.py", "operations.py", "workflows.py", "session_runtime.py",
             "vendor/guard.py", "vendor/windows.py", "closing.py", "window_transitions.py", "configuration_state.py"]
    report["tested_sha256"] = {name: sha(root / name) for name in names if (root / name).is_file()}

    def write_report():
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def stop_owned():
        expired.set()
        for owner in list(owners):
            owner.close()

    watchdog = threading.Timer(seconds, stop_owned)
    watchdog.daemon = True
    watchdog.start()

    def remaining(maximum=55):
        value = deadline - time.monotonic()
        if expired.is_set() or value <= 0:
            raise TimeoutError("Whole synthetic acceptance deadline expired")
        return min(maximum, value)

    def call(name, arguments=None, allow_error=False):
        before = time.monotonic()
        response = client.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=remaining())
        value = decoded(response)
        steps.append({"tool": name, "arguments": arguments or {}, "seconds": round(time.monotonic() - before, 3),
                      "isError": bool(response.get("isError")), "result": safe(value)})
        write_report()
        if response.get("isError") and not allow_error:
            raise AssertionError((name, value))
        return value

    def mark(name, **details):
        report["scenarios"][name] = {"passed": True, **details}
        write_report()
        print(json.dumps({"scenario": name, "passed": True}), flush=True)

    def receipt(app):
        # Producer publishes atomically; a brief read failure must not masquerade as an app failure.
        end = time.monotonic() + remaining(3)
        while True:
            try:
                value = json.loads(app["receipt"].read_text(encoding="utf-8"))
                assert value["synthetic_fixture"] is True
                return value
            except (OSError, json.JSONDecodeError):
                if time.monotonic() >= end:
                    raise
                time.sleep(.05)

    def launch(app, mode, label):
        title = "CUA Transition " + folder.name + " " + label
        output = folder / (label + "-receipt.json")
        process = subprocess.Popen([app["exe"], title, str(output), mode], cwd=folder,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        owner = OwnedProcess(process)
        owners.append(owner)
        fixture = {"title": title, "receipt": output, "owner": owner, "process": process}
        proof = receipt(fixture)
        fixture["target"] = {"pid": process.pid, "window_id": proof["original_window_id"]}
        return fixture

    def close_fixture(fixture):
        fixture["owner"].close()
        fixture["process"].wait(timeout=remaining(5))

    def initial_target(fixture):
        rows = call("list_windows", {"pid": fixture["process"].pid, "on_screen_only": False}).get("windows", [])
        rows = [row for row in rows if row["pid"] == fixture["process"].pid and row.get("title") == fixture["title"]]
        assert len(rows) == 1, rows
        target = {key: rows[0][key] for key in ("pid", "window_id")}
        assert int(target["window_id"]) == fixture["target"]["window_id"]
        return target

    def transition_step(mode, fixture):
        policy = {"mode": "new_window", "title": fixture["title"] + " - Destination"} if mode == "popup" else {"mode": "auto"}
        return {"operation": "click", "selector": {"name": "Transition once", "role": "Button"},
                "expect": [{"selector": {"name": "Transition result", "role": "Edit"}, "property": "value", "equals": "ready"}],
                "window_transition": policy, "verification_timeout_ms": 6000, "step_timeout_ms": 16000}

    try:
        from build_portable import runtime_environment
        python = root / "runtime/python.exe" if bundle else Path(sys.executable)
        client = Client(folder / "config.json", root / "server.py", python, runtime_environment(python.parent),
                        initialize_timeout=remaining(40))
        owners.append(OwnedProcess(client.child))
        available = {item["name"] for item in client.request("tools/list", timeout=remaining())["tools"]}
        assert {"computer_register_program", "computer_program_candidates", "computer_perform", "computer_run_task"} <= available, available
        status = call("computer_status")
        assert status["version"] == VERSION, status
        report["version"] = status["version"]
        app_a, app_b = manifest["apps"]
        call("computer_begin", {"program_ids": [app_a["id"]], "task_description": "Owned offline window transitions and registration fixtures only"})
        if scenario in ("all", "registration"):
            extra = launch(app_b, "popup", "registration")
            candidates = call("computer_program_candidates")["candidates"]
            matching = [item for item in candidates if os.path.normcase(item["exe"]) == os.path.normcase(app_b["exe"])]
            assert len(matching) == 1, matching
            registered = call("computer_register_program", {"candidate_id": matching[0]["candidate_id"], "program_id": app_b["id"], "name": app_b["name"]})
            assert registered["status"] == "registered" and registered["saved"] and registered["applied_to_next_session"]
            assert registered["active_session_scope_changed"] is False and registered["reconnect_required"] is False
            assert call("computer_status")["session"]["program_ids"] == [app_a["id"]]
            refused = call("get_window_state", extra["target"], allow_error=True)
            assert steps[-1]["isError"], refused
            again = call("computer_register_program", {"exe": app_b["exe"], "program_id": app_b["id"], "name": app_b["name"]})
            assert again["status"] == "already_present", again
            disk = json.loads((folder / "config.json").read_text(encoding="utf-8"))
            assert len(disk["programs"]) == 2 and disk["programs"][0] == app_a, disk
            call("computer_end")
            call("computer_begin", {"program_ids": [app_a["id"], app_b["id"]], "task_description": "New session explicitly selects both owned synthetic apps"})
            assert set(call("computer_status")["session"]["program_ids"]) == {app_a["id"], app_b["id"]}
            call("get_window_state", initial_target(extra))
            mark("chat_registration", candidate_inventory=True, existing_scope_unchanged=True,
                 active_access_denied=True, next_session_access_verified=True, existing_config_preserved=True, idempotent=True)
            close_fixture(extra)

        for mode in ("replace", "popup", "ambiguous", "missing"):
            if scenario not in ("all", mode):
                continue
            fixture = launch(app_a, mode, mode)
            target = initial_target(fixture)
            step = transition_step(mode, fixture)
            before = time.monotonic()
            if mode == "replace":
                recipe = call("computer_save_task", {"name": "Synthetic HWND replacement", "instructions": "One click, then write only in the freshly verified successor", "expected": "One transition and one final value", "program_ids": [app_a["id"]],
                    "steps": [{"program_id": app_a["id"], **step}, {"program_id": app_a["id"], "operation": "set_value",
                    "selector": {"name": "Transition result", "role": "Edit"}, "value": "workflow-successor"}]})
                answer = call("computer_run_task", {"task_id": recipe["id"], "targets": [{"program_id": app_a["id"], **target}],
                    "delivery_mode": "foreground", "execution_mode": "standard"})
                assert answer["task_verified"] is True, answer
                proof = receipt(fixture)
                assert proof["input"] == "workflow-successor" and proof["original_closed"] is True, proof
                assert all(int(window["window_id"]) != int(target["window_id"]) for window in proof["windows"]), proof
            else:
                answer = call("computer_perform", {**target, "delivery_mode": "foreground", "step": step}, allow_error=mode != "popup")
                proof = receipt(fixture)
                if mode == "popup":
                    assert answer["task_verified"] is True and answer["transition"]["state"] == "verified", answer
                    successor = answer["target"]
                    assert successor["pid"] == target["pid"] and int(successor["window_id"]) != int(target["window_id"])
                    assert proof["original_closed"] is False and len(proof["windows"]) == 2, proof
                else:
                    assert answer["task_verified"] is False and answer.get("input_dispatched") is True, answer
                    assert answer["transition"]["state"] == "needs_target", answer
                    assert proof["text_change_count"] == 0 and proof["original_closed"] is True, proof
                    assert len(proof["windows"]) == (2 if mode == "ambiguous" else 0), proof
            assert proof["click_count"] == 1, proof
            duration = round(time.monotonic() - before, 3)
            assert duration < 35, (mode, duration)
            mark(mode, elapsed_seconds=duration, independent_receipt=proof, result=answer, input_replayed=False)
            close_fixture(fixture)
        call("computer_end")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)[:6000]
        report["traceback"] = traceback.format_exc()[-6000:]
    finally:
        watchdog.cancel()
        if client is not None and client.child.poll() is None and not expired.is_set():
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = str(error)
                report["passed"] = False
        for owner in reversed(owners):
            owner.close()
            try:
                owner.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                report["passed"] = False
        report["owned_processes_exited"] = all(owner.process.poll() is not None for owner in owners)
        report["source_unchanged"] = all(sha(root / name) == digest for name, digest in report["tested_sha256"].items())
        report["deadline_expired"] = expired.is_set()
        report["seconds"] = round(time.monotonic() - started, 3)
        report["passed"] = report["passed"] and report["owned_processes_exited"] and report["source_unchanged"] and not expired.is_set()
        write_report()
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--scenario", choices=("all", "registration", "replace", "popup", "ambiguous", "missing"), default="all")
    parser.add_argument("--deadline-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 60 <= args.deadline_seconds <= 600:
        parser.error("--deadline-seconds must be 60..600")
    manifest = prepare(args.output or DATA / uuid.uuid4().hex[:10], args.driver)
    report = run(manifest, args.bundle, args.deadline_seconds, args.scenario) if args.run else manifest
    print(json.dumps({"passed": report.get("passed"), "prepared": report.get("prepared"),
                      "folder": report["folder"], "gui_launched": report["gui_launched"], "error": report.get("error")}, ensure_ascii=False))
    return 0 if report.get("passed", report.get("prepared")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
