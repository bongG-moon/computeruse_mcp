"""Developer-only close/popup acceptance through the real MCP and Driver.

The default prepares and compiles an isolated synthetic WinForms fixture only.
Use --run on an interactive Windows desktop after other GUI tests have stopped.
No personal applications, user documents, network access, or downloads are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from live_validation import Client, DRIVER, decoded, safe
from settings import save_config
from vendor.windows import OwnedProcess


HERE = Path(__file__).resolve().parent
DATA = HERE / ".data" / "closure-validation"
SOURCE = r'''using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class ClosureContext : ApplicationContext {
    private readonly string outputPath, documentPath, title, mode;
    private Form main, confirm, sibling;
    private int activeForms, closeAttempts, saveCount, discardCount, cancelCount;
    private bool permitMainClose, mainClosed, siblingClosed, confirmedExit, hidden, minimized;
    private string decision = "none";
    private const string SyntheticText = "SYNTHETIC CLOSURE DOCUMENT / no personal data";

    private static void Identify(Control item, string id, string name) {
        item.Name = id; item.AccessibleName = name;
    }
    private void WriteState() {
        var data = new Dictionary<string, object>();
        data["synthetic_fixture"] = true; data["mode"] = mode;
        data["close_attempts"] = closeAttempts; data["save_count"] = saveCount;
        data["discard_count"] = discardCount; data["cancel_count"] = cancelCount;
        data["decision"] = decision; data["main_closed"] = mainClosed;
        data["sibling_closed"] = siblingClosed; data["confirmed_exit"] = confirmedExit;
        data["active_forms"] = activeForms; data["hidden"] = hidden; data["minimized"] = minimized;
        data["dialog_open"] = confirm != null && !confirm.IsDisposed;
        data["main_handle"] = main != null && !main.IsDisposed ? main.Handle.ToInt64() : 0;
        File.WriteAllText(outputPath, new JavaScriptSerializer().Serialize(data), new UTF8Encoding(false));
    }
    private Button Button(Form form, string name, int x, int y, EventHandler action) {
        var button = new Button(); button.Text = name; Identify(button, name.Replace(" ", ""), name);
        button.Location = new Point(x, y); button.Size = new Size(190, 38);
        button.Click += action; form.Controls.Add(button); return button;
    }
    private Form NewForm(string name, bool isMain) {
        var form = new Form(); form.Text = name; Identify(form, isMain ? "ClosureMain" : "ClosureSibling", name);
        form.Font = new Font("Segoe UI", 10F); form.ClientSize = new Size(620, 255);
        form.StartPosition = FormStartPosition.Manual;
        form.Location = isMain ? new Point(100, 100) : new Point(760, 100);
        var label = new Label(); label.Text = SyntheticText; label.Location = new Point(25, 25);
        label.Size = new Size(560, 50); form.Controls.Add(label);
        activeForms++;
        form.FormClosed += delegate {
            if (isMain) mainClosed = true; else siblingClosed = true;
            activeForms--; WriteState();
            if (activeForms == 0) { confirmedExit = true; WriteState(); ExitThread(); }
        };
        return form;
    }
    private void Finish(string choice) {
        decision = choice;
        if (choice == "save") {
            File.WriteAllText(documentPath, SyntheticText, new UTF8Encoding(false)); saveCount++;
        } else if (choice == "discard") discardCount++;
        else cancelCount++;
        main.Enabled = true;
        var dialog = confirm; confirm = null; dialog.Close(); dialog.Dispose();
        WriteState();
        if (choice != "cancel") { permitMainClose = true; main.BeginInvoke(new Action(main.Close)); }
    }
    private void OpenConfirmation() {
        if (confirm != null && !confirm.IsDisposed) return;
        confirm = new Form(); confirm.Text = title + " - Confirm close";
        Identify(confirm, "ClosureConfirmation", confirm.Text);
        confirm.Font = main.Font; confirm.ClientSize = new Size(645, 180);
        confirm.FormBorderStyle = FormBorderStyle.FixedDialog;
        confirm.MinimizeBox = false; confirm.MaximizeBox = false; confirm.ControlBox = false;
        confirm.StartPosition = FormStartPosition.CenterParent;
        var note = new Label(); note.Text = "Save the synthetic document before closing?";
        note.Location = new Point(25, 25); note.Size = new Size(580, 45); confirm.Controls.Add(note);
        Button(confirm, "Save and close", 20, 100, delegate { Finish("save"); });
        Button(confirm, "Discard and close", 227, 100, delegate { Finish("discard"); });
        Button(confirm, "Cancel close", 434, 100, delegate { Finish("cancel"); });
        confirm.Show(main); main.Enabled = false; WriteState();
    }
    public ClosureContext(string fixtureTitle, string receipt, string document, string fixtureMode) {
        title = fixtureTitle; outputPath = receipt; documentPath = document; mode = fixtureMode;
        main = NewForm(title, true);
        Button(main, "Request close", 25, 95, delegate { main.BeginInvoke(new Action(main.Close)); });
        Button(main, "Minimize fixture", 235, 95, delegate { minimized = true; main.WindowState = FormWindowState.Minimized; WriteState(); });
        Button(main, "Hide fixture", 25, 155, delegate { hidden = true; main.Hide(); WriteState(); });
        main.FormClosing += delegate(object sender, FormClosingEventArgs args) {
            if (permitMainClose) return;
            closeAttempts++;
            if (mode == "direct" || mode == "siblings") { permitMainClose = true; WriteState(); return; }
            args.Cancel = true; main.BeginInvoke(new Action(OpenConfirmation)); WriteState();
        };
        main.Show();
        if (mode == "siblings") {
            sibling = NewForm(title + " - Sibling", false);
            Button(sibling, "Close synthetic sibling", 25, 95, delegate { sibling.BeginInvoke(new Action(sibling.Close)); });
            sibling.Show();
        }
        WriteState();
    }
    [STAThread] public static void Main(string[] args) {
        if (args.Length != 4) throw new ArgumentException("Four fixture-only arguments required");
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new ClosureContext(args[0], args[1], args[2], args[3]));
    }
}
'''


def fixture_folder(path: Path) -> Path:
    path = path.resolve()
    try:
        relative = path.relative_to(DATA.resolve())
    except ValueError:
        raise ValueError("Closure fixtures must stay under computer-use-mcp/.data/closure-validation") from None
    if not relative.parts:
        raise ValueError("Use a dedicated closure fixture subfolder")
    for item in [path, *path.parents]:
        if item == DATA.parent:
            break
        if item.exists() and (item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Closure fixtures cannot use linked paths")
    return path


def prepare(folder: Path, driver: Path) -> dict:
    folder = fixture_folder(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if not driver.is_file():
        raise FileNotFoundError("Manually supplied Driver is absent; no download attempted")
    from native_fixture import compiler_path
    compiler = compiler_path()
    source = folder / "ClosureFixture.cs"
    source.write_text(SOURCE, encoding="utf-8-sig")
    executable = folder / "ClosureFixture.exe"
    build = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
        "/codepage:65001", "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
        "/reference:System.Web.Extensions.dll", "/out:" + str(executable), str(source)], cwd=folder,
        capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if build.returncode or not executable.is_file():
        raise RuntimeError("Fixture compilation failed: " + build.stdout.decode("utf-8", "replace") + build.stderr.decode("utf-8", "replace"))
    config = {"version": 1, "driver": str(driver.resolve()), "state_dir": str(folder / "state"),
        "programs": [{"id": "closure_fixture", "name": "Synthetic closure fixture", "exe": str(executable),
                      "control_exes": [], "hints": "Only this execution's synthetic close-confirmation windows.", "enabled": True}],
        "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 10,
        "max_actions": 120, "approval_timeout_seconds": 300, "observation_timeout_seconds": 20}
    save_config(folder / "config.json", config)
    manifest = {"folder": str(folder), "prepared": True, "gui_launched": False, "llm_used": False,
        "driver": str(driver.resolve()), "exe": str(executable), "compiler": str(compiler), "developer_only": True,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    (folder / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def run_fixture(manifest: dict, bundle: Path | None = None) -> dict:
    folder = Path(manifest["folder"])
    records, owners = [], []
    client = None
    report = {"folder": str(folder), "passed": False, "llm_used": False, "gui_launched": True,
              "fixture_type": "isolated_native_winforms_close_dialog", "scenarios": {}}

    def mark(name, details):
        report["scenarios"][name] = details
        (folder / "progress.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"scenario": name, "passed": details["passed"]}), flush=True)

    def call(name, args=None, allow_error=False):
        started = time.monotonic()
        result = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=90)
        records.append({"tool": name, "arguments": args or {}, "seconds": round(time.monotonic() - started, 4),
                        "result": safe(result)})
        (folder / "steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.get("isError") and not allow_error:
            raise RuntimeError(name + ": " + json.dumps(safe(decoded(result)), ensure_ascii=False)[:2000])
        return decoded(result)

    def launch(label, mode):
        title = "CUA Closure " + folder.name + " " + label
        app = {"title": title, "receipt": folder / (label + "-state.json"),
               "document": folder / (label + "-document.txt")}
        process = subprocess.Popen([manifest["exe"], title, str(app["receipt"]), str(app["document"]), mode],
            cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        owners.append(OwnedProcess(process)); app["process"] = process
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = call("list_windows", {"pid": process.pid, "on_screen_only": False}).get("windows", [])
            matching = [row for row in rows if row.get("title") == title and row.get("pid") == process.pid]
            if len(matching) == 1:
                app["target"] = {key: matching[0][key] for key in ("pid", "window_id")}
                return app
            if process.poll() is not None:
                raise AssertionError("Synthetic fixture terminated before its initial observation")
            time.sleep(.1)
        raise AssertionError("No unique synthetic fixture main window")

    def receipt(app):
        value = json.loads(app["receipt"].read_text(encoding="utf-8"))
        assert value.get("synthetic_fixture") is True
        return value

    def click(target, name, target_may_close=False):
        state = call("get_window_state", {**target, "max_depth": 12, "max_elements": 600})
        matches = [e for e in state.get("elements", []) if e.get("name", e.get("label")) == name and e.get("role") == "Button"]
        assert len(matches) == 1 and set(matches[0].get("actions", [])) & {"click", "invoke"}, (name, matches)
        handle = matches[0].get("element_token")
        assert handle, "Fresh native button token missing"
        # A closing target may disappear before Driver acknowledgement. The
        # native close-ticket proof, not this acknowledgement, decides success.
        return call("click", {**target, "element_token": handle, "delivery_mode": "background"},
                    allow_error=target_may_close)

    def dialog(app):
        rows = call("list_windows", {"pid": app["process"].pid, "on_screen_only": False}).get("windows", [])
        matches = [row for row in rows if row.get("title") == app["title"] + " - Confirm close"]
        assert len(matches) == 1, rows
        return {key: matches[0][key] for key in ("pid", "window_id")}

    def prepare_close(app, scope="window"):
        result = call("computer_prepare_close", {**app["target"], "scope": scope})
        assert result.get("close_id"), result
        return result

    def verify(close_id, expected, timeout_ms=1500):
        result = call("computer_verify_closed", {"close_id": close_id, "timeout_ms": timeout_ms})
        assert result.get("task_verified") is expected, result
        if expected:
            assert result.get("status") == "verified" and result.get("window_closed") is True, result
        else:
            assert result.get("status") in {"pending", "needs_dialog"}, result
        return result

    def request_close(app, scope="window"):
        result = call("computer_close", {**app["target"], "scope": scope, "delivery_mode": "background",
            "timeout_ms": 1500, "close_action": {"operation": "click", "selector": {"name": "Request close", "role": "Button"}}})
        assert result.get("close_id"), result
        return result

    try:
        if bundle:
            from build_portable import runtime_environment
            client = Client(folder / "config.json", bundle / "server.py", bundle / "runtime/python.exe", runtime_environment(bundle / "runtime"))
            report["bundle"] = str(bundle)
        else:
            client = Client(folder / "config.json")
        tools = client.request("tools/list")["tools"]
        report["available_tools"] = [tool["name"] for tool in tools]
        assert {"computer_prepare_close", "computer_verify_closed", "computer_close"} <= set(report["available_tools"])
        call("computer_begin", {"program_ids": ["closure_fixture"],
            "task_description": "Only newly launched synthetic WinForms fixtures: verify close dialogs, cancel/save/discard, minimized/hidden windows, and sibling process scope. No personal apps or documents."})

        app = launch("cancel-save", "confirmation")
        requested = request_close(app, "process")
        assert requested.get("task_verified") is False and requested.get("status") == "needs_dialog", requested
        assert receipt(app)["close_attempts"] == 1 and receipt(app)["dialog_open"] is True
        click(dialog(app), "Cancel close")
        cancelled = verify(requested["close_id"], False)
        assert cancelled.get("status") == "pending", "Closed confirmation must not remain a dialog candidate: " + str(cancelled)
        first = receipt(app)
        assert first["cancel_count"] == 1 and first["save_count"] == 0 and first["main_closed"] is False
        assert app["process"].poll() is None and not app["document"].exists()
        # An explicit second request follows a verified cancellation, not an automatic replay.
        second = request_close(app, "process")
        assert second.get("status") == "needs_dialog" and second.get("task_verified") is False
        click(dialog(app), "Save and close", target_may_close=True)
        saved = verify(second["close_id"], True)
        app["process"].wait(timeout=5)
        proof = receipt(app)
        assert saved.get("process_exited") is True and proof["confirmed_exit"] is True
        assert proof["close_attempts"] == 2 and proof["save_count"] == 1 and proof["cancel_count"] == 1
        assert app["document"].read_text(encoding="utf-8") == "SYNTHETIC CLOSURE DOCUMENT / no personal data"
        mark("cancel_then_save", {"passed": True, "initial": requested, "cancelled": cancelled,
            "second_request": second, "verified": saved, "independent_receipt": proof,
            "independent_saved_content_verified": True, "file_proof_separate_from_close_proof": True})

        app = launch("discard", "confirmation")
        requested = request_close(app, "process")
        assert requested.get("status") == "needs_dialog" and requested.get("task_verified") is False
        click(dialog(app), "Discard and close", target_may_close=True)
        discarded = verify(requested["close_id"], True)
        app["process"].wait(timeout=5)
        proof = receipt(app)
        assert proof["discard_count"] == 1 and proof["save_count"] == 0 and not app["document"].exists()
        mark("explicit_discard", {"passed": True, "verified": discarded, "independent_receipt": proof})

        app = launch("direct", "direct")
        direct = request_close(app, "process")
        assert direct.get("task_verified") is True and direct.get("status") == "verified", direct
        app["process"].wait(timeout=5)
        mark("direct_exit", {"passed": True, "verified": direct, "independent_receipt": receipt(app)})

        app = launch("minimized", "direct")
        prepared = prepare_close(app)
        click(app["target"], "Minimize fixture")
        minimized = verify(prepared["close_id"], False)
        assert minimized.get("window_closed") is False and minimized.get("process_exited") is False
        proof = receipt(app)
        assert proof["minimized"] is True and proof["main_closed"] is False and app["process"].poll() is None
        mark("minimized_is_not_closed", {"passed": True, "verified": minimized,
            "independent_receipt": proof, "cleanup_only_terminates_owned_synthetic_process": True})

        app = launch("hidden", "direct")
        prepared = prepare_close(app, "process")
        click(app["target"], "Hide fixture")
        hidden = verify(prepared["close_id"], False)
        assert hidden.get("window_closed") is False and hidden.get("process_exited") is False
        proof = receipt(app)
        assert proof["hidden"] is True and proof["main_closed"] is False and app["process"].poll() is None
        mark("hidden_is_not_closed", {"passed": True, "verified": hidden,
            "independent_receipt": proof, "cleanup_only_terminates_owned_synthetic_process": True})

        app = launch("siblings", "siblings")
        process_check = prepare_close(app, "process")
        window_closed = request_close(app, "window")
        assert window_closed.get("task_verified") is True and window_closed.get("window_closed") is True, window_closed
        assert window_closed.get("process_exited") is False, "Sibling process must remain alive after one window closes"
        remaining = verify(process_check["close_id"], False)
        assert remaining.get("process_exited") is False and receipt(app)["main_closed"] is True
        rows = call("list_windows", {"pid": app["process"].pid, "on_screen_only": False}).get("windows", [])
        siblings = [row for row in rows if row.get("title") == app["title"] + " - Sibling"]
        assert len(siblings) == 1, rows
        sibling_target = {key: siblings[0][key] for key in ("pid", "window_id")}
        click(sibling_target, "Close synthetic sibling", target_may_close=True)
        fully_closed = verify(process_check["close_id"], True)
        app["process"].wait(timeout=5)
        proof = receipt(app)
        assert proof["confirmed_exit"] is True and proof["sibling_closed"] is True
        mark("window_vs_process", {"passed": True, "window_closed": window_closed,
            "remaining_process": remaining, "fully_closed": fully_closed, "independent_receipt": proof})
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)[:6000]
    finally:
        if client is not None:
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = str(error)[:1000]
                report["passed"] = False
                if client.child.poll() is None:
                    client.child.kill()
        report["owned_processes"] = [{"pid": owner.process.pid, "natural_exit_before_cleanup": owner.process.poll() is not None} for owner in owners]
        for owner in owners:
            owner.close()
            try:
                owner.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                report["cleanup_error"] = "Owned synthetic process did not terminate"
                report["passed"] = False
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true", help="Launch only synthetic fixtures and run real MCP/Driver acceptance")
    args = parser.parse_args()
    folder = args.output or DATA / ("run-" + uuid.uuid4().hex[:8])
    manifest = prepare(folder, args.driver)
    result = run_fixture(manifest, args.bundle.resolve() if args.bundle else None) if args.run else manifest
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed", result.get("prepared")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
