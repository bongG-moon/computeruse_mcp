"""Owned native-window adaptive observation and scoped inspection through MCP stdio.

Default compiles synthetic fixtures without displaying them. --run launches only
these fixtures. No personal applications, configuration, credentials or network.
"""
from __future__ import annotations

import argparse
import base64
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
from scoped_controls import HELPER_NAME
from vendor.windows import OwnedProcess

DATA = HERE / ".data" / "observation-validation"
SOURCE = r'''using System;
using System.Drawing;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;
class ObservationFixture : Form {
    readonly string receipt; int inputs;
    readonly Timer lifetime = new Timer();
    static void NameControl(Control c, string value) { c.Name = value.Replace(" ", ""); c.AccessibleName = value; }
    void Save() { File.WriteAllText(receipt, new JavaScriptSerializer().Serialize(new { synthetic_fixture = true, window_id = Handle.ToInt64(), input_count = inputs }), new UTF8Encoding(false)); }
    ObservationFixture(string title, string output, string mode) {
        receipt = output; Text = title; NameControl(this, title); TopMost = true;
        ClientSize = new Size(700, 460); StartPosition = FormStartPosition.Manual; Location = new Point(100, 100);
        Font = new Font("Segoe UI", 10F);
        if (mode == "drawn") {
            Paint += delegate(object sender, PaintEventArgs e) {
                e.Graphics.Clear(Color.FromArgb(240,244,250));
                e.Graphics.DrawString("OWNED SYNTHETIC DRAWN SCREEN", Font, Brushes.Navy, 25, 35);
                e.Graphics.FillRectangle(Brushes.RoyalBlue, 25, 100, 200, 45);
                e.Graphics.DrawString("Sample drawn button", Font, Brushes.White, 38, 111);
            };
        } else {
            var target = new GroupBox(); target.Text = "Conditions"; NameControl(target, "Conditions"); target.SetBounds(20, 15, 640, 95);
            var field = new TextBox(); field.Text = "W"; NameControl(field, "Family"); field.SetBounds(20, 30, 220, 28);
            field.TextChanged += delegate { inputs++; Save(); }; target.Controls.Add(field);
            var query = new Button(); query.Text = "Query"; NameControl(query, "Query"); query.SetBounds(260, 27, 130, 35);
            query.Click += delegate { inputs++; Save(); }; target.Controls.Add(query); Controls.Add(target);
            var other = new Panel(); NameControl(other, "Other area"); other.SetBounds(20, 125, 640, 260); other.AutoScroll = true;
            for (int i=0; i<300; i++) { var item = new TextBox(); item.Text = "unrelated " + i; NameControl(item, "Other field " + i); item.SetBounds(10 + (i%4)*145, 10 + (i/4)*30, 135, 25); other.Controls.Add(item); }
            Controls.Add(other);
            for (int i=0; i<2; i++) { var duplicate = new GroupBox(); NameControl(duplicate, "Duplicate region"); duplicate.SetBounds(20+i*320, 395, 310, 45); Controls.Add(duplicate); }
        }
        Shown += delegate { Activate(); Save(); };
        lifetime.Interval = 180000; lifetime.Tick += delegate { Close(); }; lifetime.Start();
    }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new ObservationFixture(args[0], args[1], args[2]));
    }
}
'''


def prepare(folder, driver):
    folder = Path(folder).resolve()
    if not folder.is_relative_to(DATA.resolve()) or folder == DATA.resolve():
        raise ValueError("Use a dedicated .data/observation-validation subfolder")
    for parent in [folder, *folder.parents]:
        if parent == DATA.parent: break
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Synthetic fixture paths must not use links")
    folder.mkdir(parents=True, exist_ok=True)
    source, exe = folder / "ObservationFixture.cs", folder / "ObservationFixture.exe"
    source.write_text(SOURCE, encoding="utf-8-sig")
    answer = subprocess.run([str(compiler_path()), "/nologo", "/target:winexe", "/optimize+", "/codepage:65001",
        "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.Web.Extensions.dll",
        "/out:" + str(exe), str(source)], capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
    if answer.returncode: raise RuntimeError((answer.stdout + answer.stderr).decode("utf-8", "replace"))
    config = {"version": 1, "driver": str(Path(driver).resolve(strict=True)), "state_dir": str(folder / "state"),
              "programs": [{"id": "observation_fixture", "name": "Owned observation fixture", "exe": str(exe), "enabled": True, "control_exes": []}],
              "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 5,
              "max_actions": 20, "approval_timeout_seconds": 90, "observation_timeout_seconds": 20}
    save_config(folder / "config.json", config)
    return {"prepared": True, "folder": str(folder), "exe": str(exe), "gui_launched": False}


def run(manifest, bundle=None):
    folder, root = Path(manifest["folder"]), Path(bundle).resolve() if bundle else HERE
    report = {"passed": False, "version": VERSION, "llm_used": False, "gui_launched": True, "personal_apps_used": False,
              "scenarios": {}, "steps": [], "tested_sha256": {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                for name in ("inspection.py", "scoped_controls.py", "ScopedControls.cs", HELPER_NAME, "session_runtime.py", "server.py") if (root/name).is_file()}}
    client = None; owners = []; fixtures = []
    watchdog = threading.Timer(150, lambda: [owner.close() for owner in list(owners)]); watchdog.daemon = True; watchdog.start()
    def save(): (folder/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    def call(name, args=None, allow_error=False):
        began = time.monotonic(); result = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=30)
        report["steps"].append({"tool": name, "arguments": args or {}, "seconds": round(time.monotonic()-began, 3),
                                "isError": bool(result.get("isError")), "result": safe(decoded(result)),
                                "image_count": sum(item.get("type") == "image" for item in result.get("content", []))})
        save()
        if not allow_error and result.get("isError"): raise AssertionError(report["steps"][-1])
        return result
    def image_evidence(result):
        images = [item for item in result.get("content", []) if item.get("type") == "image"]
        assert len(images) >= 1
        payload = base64.b64decode(images[0]["data"], validate=True)
        assert images[0]["mimeType"] == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"), "Expected decodable PNG image content"
        width, height = int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")
        assert width > 100 and height > 100
        return {"mime_type": images[0]["mimeType"], "bytes": len(payload), "width": width, "height": height,
                "sha256": hashlib.sha256(payload).hexdigest()}
    def launch(mode):
        receipt = folder/(mode+"-receipt.json"); title = "CUA Observation " + folder.name + " " + mode
        process = subprocess.Popen([manifest["exe"], title, str(receipt), mode], cwd=folder,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        owner = OwnedProcess(process); owners.append(owner); fixtures.append((process, receipt, owner))
        deadline = time.monotonic()+8
        while True:
            try: data = json.loads(receipt.read_text(encoding="utf-8")); break
            except (OSError, ValueError):
                if time.monotonic() >= deadline: raise
                time.sleep(.05)
        return {"pid": process.pid, "window_id": data["window_id"]}
    try:
        from build_portable import runtime_environment
        python = root/"runtime/python.exe" if bundle else Path(sys.executable)
        client = Client(folder/"config.json", root/"server.py", python, runtime_environment(python.parent))
        owners.append(OwnedProcess(client.child))
        call("computer_begin", {"program_ids": ["observation_fixture"], "task_description": "Read only owned synthetic observation fixtures"})
        target = launch("normal")
        initial = decoded(call("computer_inspect", {**target, "observation": "uia", "max_controls": 200}))
        full_seconds = report["steps"][-1]["seconds"]
        scoped = decoded(call("computer_inspect", {**target, "within": {"name": "Conditions", "role": "Group"}, "observation": "uia"}))["inspection"]
        scoped_seconds = report["steps"][-1]["seconds"]
        assert scoped["filters_reduce_uia_property_reads"] and scoped["observation_scope"]["kind"] == "native_selected_subtree", scoped
        assert {row["name"] for row in scoped["controls"]} == {"Family", "Query"}, scoped
        assert all(row.get("selector", {}).get("within") == {"name": "Conditions", "role": "Group"} for row in scoped["controls"]), scoped
        assert all("element_token" not in row and "element_index" not in row for row in scoped["controls"]), scoped
        report["scenarios"]["scoped_subtree"] = {"passed": True, "whole_window_observed_controls": initial["inspection"]["observed_control_count"],
            "scoped_observed_controls": scoped["observed_control_count"], "scope_lookup_control_count": scoped["scope_lookup_control_count"], "whole_window_seconds": full_seconds, "scoped_seconds": scoped_seconds,
            "timing_scope": "one_synthetic_first_call_each_not_a_production_benchmark"}
        call("bring_to_front", target)
        both = call("computer_inspect", {**target, "observation": "both"})
        assert decoded(both)["inspection"]["status"] == "combined_observation", decoded(both)["inspection"].get("image_capture")
        assert report["steps"][-1]["image_count"] > 0
        report["scenarios"]["both_image_content"] = {"passed": True, "client_rendering_verified": False, "model_image_understanding_verified": False, "image": image_evidence(both)}
        ambiguous = call("computer_inspect", {**target, "within": {"name": "Duplicate region"}, "observation": "both"}, allow_error=True)
        assert ambiguous.get("isError") and report["steps"][-1]["image_count"] == 0, safe(ambiguous)
        report["scenarios"]["ambiguous_no_fallback"] = {"passed": True}
        fixtures[-1][2].close(); fixtures[-1][0].wait(timeout=5)
        drawn = launch("drawn")
        call("computer_inspect", {**drawn, "observation": "uia"})
        call("bring_to_front", drawn)
        automatic = call("computer_inspect", drawn)
        assert decoded(automatic)["inspection"]["status"] == "combined_observation", decoded(automatic)["inspection"].get("image_capture")
        assert decoded(automatic)["inspection"]["image_capture"]["reason"] == "weak_accessibility"
        assert report["steps"][-1]["image_count"] > 0
        report["scenarios"]["auto_image_for_owner_drawn"] = {"passed": True, "image": image_evidence(automatic)}
        no_image = call("computer_inspect", {**drawn, "observation": "uia"})
        assert report["steps"][-1]["image_count"] == 0
        assert decoded(no_image)["inspection"]["input_mode_unchanged"]
        report["scenarios"]["explicit_uia_only"] = {"passed": True}
        assert all(json.loads(receipt.read_text(encoding="utf-8"))["input_count"] == 0 for process, receipt, owner in fixtures)
        report["input_count"] = 0
        call("computer_end")
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if client:
            try: client.close()
            except Exception as error: report["mcp_close_error"] = str(error); report["passed"] = False
        for owner in reversed(owners):
            try: owner.close()
            except Exception as error: report["cleanup_error"] = str(error); report["passed"] = False
        for process, receipt, owner in fixtures:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: report["cleanup_error"] = "Owned fixture did not exit"; report["passed"] = False
        report["owned_fixtures_closed"] = all(process.poll() is not None for process, receipt, owner in fixtures)
        if not report["owned_fixtures_closed"]: report["passed"] = False
        watchdog.cancel(); save()
    return report


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--run", action="store_true")
    parser.add_argument("--bundle"); parser.add_argument("--driver", default=str(DRIVER)); parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = prepare(args.output or DATA/uuid.uuid4().hex[:10], args.driver)
    report = run(manifest, args.bundle) if args.run else manifest
    print(json.dumps({"passed": report.get("passed"), "prepared": report.get("prepared"), "folder": manifest["folder"],
                      "scenarios": report.get("scenarios"), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report.get("passed", report.get("prepared")) else 1

if __name__ == "__main__": raise SystemExit(main())
