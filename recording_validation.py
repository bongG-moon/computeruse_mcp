"""Developer-only native action recording acceptance (never workplace data).

Default compiles an isolated fixture. --run sends synthetic mouse/key input
only to retained, freshly-created fixture HWNDs. Run serially with other GUI
tests. Native recording + real Driver snapshot + ProcessDraft promotion are
tested; this is not human-recording or saved-process-replay evidence.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from live_validation import Client, DRIVER, decoded
from picker_validation import Native
from process_validation import wait
from process_editor import ProcessDraft, ProcessEditors, VISUAL_HELPER_NAME
from settings import save_config

HERE = Path(__file__).resolve().parent
SOURCE = r'''
using System; using System.Drawing; using System.IO; using System.Collections.Generic;
using System.Runtime.InteropServices; using System.Web.Script.Serialization; using System.Windows.Forms;
class RecordingFixture : Form {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr p);
 readonly TextBox edit=new TextBox(), password=new TextBox();
 readonly ComboBox combo=new ComboBox(), typed=new ComboBox(); readonly CheckBox check=new CheckBox();
 readonly string receipt; readonly JavaScriptSerializer json=new JavaScriptSerializer();
 RecordingFixture(string title,string output) {
  receipt=output; Text=title; AutoScaleMode=AutoScaleMode.None; ClientSize=new Size(740,460);
  StartPosition=FormStartPosition.Manual; Location=new Point(Screen.PrimaryScreen.WorkingArea.Right-Width-30,80);
  BackColor=Color.White; Font=new Font("Segoe UI",11);
  AddLabel("Existing value",30,25); edit.Name="record_edit";edit.AccessibleName="Recording existing value";edit.Text="OLD";edit.SetBounds(30,55,320,30);
  AddLabel("Selection list",30,115); combo.Name="record_list";combo.AccessibleName="Recording selection list";combo.DropDownStyle=ComboBoxStyle.DropDownList;combo.Items.AddRange(new object[]{"Alpha","Beta","Gamma"});combo.SelectedIndex=0;combo.SetBounds(30,145,320,30);
  AddLabel("Editable selection",390,115); typed.Name="record_typed_combo";typed.AccessibleName="Recording editable selection";typed.Items.AddRange(new object[]{"Alpha","Beta","Gamma"});typed.Text="Alpha";typed.SetBounds(390,145,320,30);
  check.Name="record_check";check.AccessibleName="Recording checkbox";check.Text="Include completed";check.SetBounds(30,235,300,35);
  AddLabel("Protected input",390,25);password.Name="record_password";password.AccessibleName="Recording protected input";password.UseSystemPasswordChar=true;password.SetBounds(390,55,320,30);
  var popup=new Button{Text="Open owned popup",Name="record_popup"};popup.SetBounds(30,330,260,40);
  popup.Click+=delegate {var dialog=new Form{Text="Synthetic recording popup",ClientSize=new Size(300,150),StartPosition=FormStartPosition.CenterParent};dialog.Controls.Add(new Label{Text="This popup is outside the exact recording window",Dock=DockStyle.Fill});dialog.Show(this);};
  Controls.AddRange(new Control[]{edit,password,combo,typed,check,popup});
  edit.TextChanged+=delegate{Save();};combo.SelectedIndexChanged+=delegate{Save();};typed.TextChanged+=delegate{Save();};check.CheckedChanged+=delegate{Save();};Shown+=delegate{Save();};
 }
 void AddLabel(string text,int x,int y){var label=new Label{Text=text};label.SetBounds(x,y,310,28);Controls.Add(label);}
 void Save(){string temporary=receipt+".tmp";File.WriteAllText(temporary,json.Serialize(new Dictionary<string,object>{{"synthetic_fixture",true},{"value",edit.Text},{"selection",combo.Text},{"typed_selection",typed.Text},{"checked",check.Checked}}));if(File.Exists(receipt))File.Delete(receipt);File.Move(temporary,receipt);}
 [STAThread] static void Main(string[] args){if(args.Length!=2)return;SetProcessDpiAwarenessContext(new IntPtr(-4));Application.EnableVisualStyles();Application.Run(new RecordingFixture(args[0],args[1]));}
}
'''


def compile_fixture(folder):
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True)
    source, exe = folder / "RecordingFixture.cs", folder / "RecordingFixture.exe"
    source.write_text(SOURCE, encoding="utf-8")
    compiler = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/codepage:65001", "/reference:System.dll",
        "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
        "/out:"+str(exe), str(source)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return exe


def run(folder, driver=DRIVER, bundle=None):
    folder = folder.resolve(); root = bundle.resolve() if bundle else HERE
    if folder.exists(): raise ValueError("Use a fresh output folder")
    folder.mkdir(parents=True); exe = compile_fixture(folder / "fixture")
    config = folder / "config.json"; receipt = folder / "receipt.json"
    save_config(config, {"version": 1, "driver": str(driver.resolve()), "state_dir": str(folder / "state"),
        "programs": [{"id": "recording", "name": "Synthetic recording fixture", "exe": str(exe), "enabled": True}],
        "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 15, "max_actions": 100})
    native = Native(); children = []; helpers = []; client = None
    native.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    native.user.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    report = {"passed": False, "synthetic_fixture": True, "synthetic_injected_input": True,
              "human_recording_tested": False, "saved_recipe_replay_tested": False, "private_application_used": False,
              "external_llm_used": False, "scenarios": {}, "heartbeats": {}}
    paths = [root / VISUAL_HELPER_NAME, root / "VisualTools.cs", root / "process_editor.py", Path(__file__)]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    report["tested_sha256"] = hashes

    def write():
        (folder / "recording-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def read(path):
        try: return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError): return None

    def call(name, args=None):
        result = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=60)
        assert not result.get("isError"), result
        return decoded(result)

    def activate(target):
        hwnd = target["window_id"]; assert native.pid(hwnd) == target["pid"]
        if native.user.GetForegroundWindow() != hwnd:
            native.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)
            foreground = native.user.GetForegroundWindow(); own_thread = native.kernel.GetCurrentThreadId()
            other = native.user.GetWindowThreadProcessId(foreground, None)
            attached = other != own_thread and bool(native.user.AttachThreadInput(own_thread, other, True))
            try: native.user.SetForegroundWindow(hwnd)
            finally:
                if attached: native.user.AttachThreadInput(own_thread, other, False)
            wait(lambda: native.user.GetForegroundWindow() == hwnd)
        time.sleep(.4)

    def click(target, x, y, *, hover=.8):
        activate(target); point = wintypes.POINT(x, y)
        assert native.user.ClientToScreen(target["window_id"], ctypes.byref(point))
        assert native.user.SetCursorPos(point.x, point.y); time.sleep(hover)
        assert native.user.GetAncestor(native.user.WindowFromPoint(point), 2) == target["window_id"]
        native.user.mouse_event(2, 0, 0, 0, 0); native.user.mouse_event(4, 0, 0, 0, 0)

    def key(target, vk, ctrl=False):
        assert native.pid(target["window_id"]) == target["pid"] and native.user.GetForegroundWindow() == target["window_id"]
        if ctrl: native.user.keybd_event(0x11, 0, 0, 0)
        try: native.user.keybd_event(vk, 0, 0, 0); native.user.keybd_event(vk, 0, 2, 0)
        finally:
            if ctrl: native.user.keybd_event(0x11, 0, 2, 0)
        time.sleep(.08)

    def start_record(name, target):
        nonce = uuid.uuid4().hex + uuid.uuid4().hex; request = folder / (name+".req.json"); response = folder / (name+".res.json")
        request.write_text(json.dumps({"nonce": nonce, "timeout_seconds": 120, "max_events": 15,
            "targets": [{**target, "program_id": "recording", "label": "Synthetic recording fixture"}]}), encoding="utf-8")
        child = subprocess.Popen([str(root / VISUAL_HELPER_NAME), "--record", str(request), str(response)],
            cwd=root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)); children.append(child)
        shown = wait(lambda: read(Path(str(response)+".ready.json")))
        assert shown["nonce"] == nonce and shown["helper_pid"] == child.pid
        helper = native.ready_helper(shown, root / VISUAL_HELPER_NAME); helpers.append(helper)
        native.user.SetWindowPos(helper["hwnd"], -1, 20, 20, 0, 0, 0x11)
        progress_path = Path(str(response)+".progress.json")
        def progress():
            value = read(progress_path)
            if value:
                assert value["nonce"] == nonce
                clean = ProcessEditors._recording_progress(value, child.pid)
                history = report["heartbeats"].setdefault(name, [])
                signature = {k: clean[k] for k in ("state", "event_count", "manual_count", "warning_codes", "probe")}
                if not history or signature != history[-1]: history.append(signature); write()
                return clean
        ready = wait(lambda: (p if p and p["state"] == "ready" else None) if (p := progress()) else None)
        assert ready["probe"]["hooks"] == "available" and ready["probe"]["uia"] == "available", ready
        native.click_helper(helper, "기록 시작"); activate(target)
        wait(lambda: (p if p and p["state"] == "recording" else None) if (p := progress()) else None)
        return helper, response, progress

    def finish(recorder):
        helper, response, progress = recorder
        time.sleep(.9); progress(); native.click_helper(helper, "기록 마치고 검토")
        result = wait(lambda: read(response)); assert result["status"] == "recorded" and result["human_confirmed"] is True
        assert native.wait_exited(helper)
        return result

    def snapshot(target):
        return call("get_window_state", {**target, "include_accessibility_tree": True, "include_screenshot": False})

    try:
        title = "Synthetic recording acceptance " + uuid.uuid4().hex[:8]
        fixture = subprocess.Popen([str(exe), title, str(receipt)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)); children.append(fixture)
        window = wait(lambda: next((w for w in native.windows(fixture.pid) if w["visible"] and w["text"] == title), None))
        target = {"pid": fixture.pid, "window_id": window["hwnd"]}; program = {**target, "program_id": "recording", "label": "Synthetic recording fixture"}
        if bundle:
            from build_portable import runtime_environment
            client = Client(config, root / "server.py", root / "runtime/python.exe", runtime_environment(root / "runtime"))
        else: client = Client(config, root / "server.py")
        call("computer_begin", {"program_ids": ["recording"], "task_description": "Synthetic action recording validation"})
        snapshot(target)

        recording = start_record("semantic", target); progress = recording[2]
        # Hover alone is deliberately not an event.
        point = wintypes.POINT(100,250); native.user.ClientToScreen(target["window_id"], ctypes.byref(point)); native.user.SetCursorPos(point.x,point.y)
        time.sleep(.8); assert progress()["event_count"] == 0
        click(target, 45, 250); time.sleep(1.0); assert read(receipt)["checked"] is True; progress()
        click(target, 120, 68); time.sleep(.7); key(target, 0x41, ctrl=True)
        for code in (0x4E,0x45,0x57): key(target,code)
        time.sleep(1.0); progress(); assert read(receipt)["value"].lower() == "new"
        native.click_helper(recording[0], "일시정지"); wait(lambda: progress()["state"] == "paused")
        result = finish(recording); observed = snapshot(target)
        (folder / "semantic-observation.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
        draft = ProcessDraft([program]); draft.recorded(result["events"], snapshots={("recording", "main"): observed}, warning_codes=result.get("warnings", []))
        (folder / "semantic-draft.json").write_text(json.dumps({"steps":draft.steps,"review":draft.recording_review()}, ensure_ascii=False, indent=2), encoding="utf-8")
        assert any(s["operation"] == "set_checked" and s["checked"] is True for s in draft.steps), draft.summaries()
        assert any(s["operation"] == "set_value" and s["value"].lower() == "new" for s in draft.steps), draft.summaries()
        report["scenarios"]["hover_and_semantic_checkbox_existing_edit"] = {"passed":True,"events":len(result["events"]),"steps":len(draft.steps),"selector_and_postcondition_verified":True}

        recording = start_record("typed-combo", target)
        click(target, 450, 160); time.sleep(.7); key(target,0x41,ctrl=True)
        for code in (0x42,0x45,0x54,0x41): key(target,code)
        key(target,0x0D); time.sleep(1)
        result = finish(recording); draft = ProcessDraft([program])
        draft.recorded(result["events"], snapshots={("recording","main"):snapshot(target)},warning_codes=result.get("warnings",[]))
        assert "recording_method_unverified" in draft.recording_warnings, draft.recording_review()
        assert not any(s["operation"] == "select_option" for s in draft.steps)
        assert any(s.get("manual_reason") == "recording_method_unverified" for s in draft.steps)
        report["scenarios"]["typed_combo_does_not_invent_input_method"] = {"passed":True,"manual_required":True}

        recording = start_record("protected-popup",target); progress=recording[2]
        click(target,450,68); time.sleep(.7)
        for code in (0x53,0x45,0x43,0x52,0x45,0x54):key(target,code)
        time.sleep(.9); progress(); click(target,140,350)
        popup=wait(lambda:next((w for w in native.windows(fixture.pid) if w["visible"] and w["text"]=="Synthetic recording popup"),None))
        activate({"pid":fixture.pid,"window_id":popup["hwnd"]})
        wait(lambda:progress()["state"]=="outside_target")
        assert native.pid(popup["hwnd"])==fixture.pid; native.user.PostMessageW(popup["hwnd"],0x10,0,0)
        activate(target); wait(lambda:progress()["state"]=="recording")
        result=finish(recording)
        assert "secret" not in json.dumps(result).lower()
        assert any(e.get("reason")=="protected_input" for e in result["events"]), result
        assert "outside_target_not_recorded" in result["warnings"]
        draft=ProcessDraft([program]);draft.recorded(result["events"],warning_codes=result["warnings"])
        assert draft.recording_review()["partial"] and not draft.recording_review()["acknowledged"]
        report["scenarios"]["protected_input_and_popup_omission"]={"passed":True,"password_not_stored":True,"partial_review_required":True}
        report["passed"] = True
    except Exception as exc:
        report["error"]={"type":type(exc).__name__,"message":str(exc)[:5000]}
        for index, helper in enumerate(helpers):
            if not native.wait_exited(helper,0) and native.user.IsWindowVisible(helper["hwnd"]):
                native.capture_helper(helper,folder/("failure-helper-"+str(index)+".png"))
        raise
    finally:
        if client:
            try: call("computer_end"); client.close()
            except Exception:
                report["passed"]=False
                if client.child.poll() is None: client.child.kill();client.child.wait(timeout=5)
        for helper in helpers:
            if not native.wait_exited(helper,1):native.kernel.TerminateProcess(helper["handle"],1);native.wait_exited(helper,5)
            native.kernel.CloseHandle(helper["handle"])
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
        report["owned_helpers_exited"]=all(h["closed"] for h in helpers)
        report["owned_fixtures_exited"]=all(p.poll() is not None for p in children)
        report["source_unchanged"]=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in hashes.items())
        if not report["source_unchanged"]:report["passed"]=False
        write()
    return report


if __name__ == "__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8",errors="replace")
    parser=argparse.ArgumentParser();parser.add_argument("--folder",type=Path,required=True)
    parser.add_argument("--driver",type=Path,default=DRIVER);parser.add_argument("--bundle",type=Path);parser.add_argument("--run",action="store_true")
    args=parser.parse_args()
    if not args.run:
        print(compile_fixture(args.folder / "fixture"));raise SystemExit(0)
    result=run(args.folder,args.driver,args.bundle);print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result["passed"] else 1)
