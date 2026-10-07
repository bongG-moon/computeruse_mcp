"""Actual public MCP saved-process replay across an owned popup and back.

--run opens only fresh synthetic fixtures. Default compiles without opening UI.
The same five-step stored recipe runs twice, with independent click/value receipts.
An image opener then follows its owned popup screenshot checkpoint and resumes
with a synthetic acknowledgement. This does not test real human usability.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from live_validation import Client, DRIVER, decoded
from native_fixture import compiler_path
from settings import VERSION, save_config
from vendor.windows import OwnedProcess
from PIL import Image

HERE = Path(__file__).resolve().parent
SOURCE = r'''
using System; using System.Collections.Generic; using System.Drawing; using System.IO;
using System.Runtime.InteropServices; using System.Text; using System.Web.Script.Serialization; using System.Windows.Forms;
class PopupWorkflowFixture : Form {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd,StringBuilder text,int count);
 readonly string output,popupTitle; readonly TextBox main=new TextBox(); Form popup; TextBox entry;
 int opened,applied,mainChanges,popupChanges; string appliedValue=""; readonly Timer lifetime=new Timer();
 static void Id(Control c,string id,string label){c.Name=id;c.AccessibleName=label;}
 string ClassOf(IntPtr hwnd){var text=new StringBuilder(256);GetClassName(hwnd,text,text.Capacity);return text.ToString();}
 void Save(){if(!IsHandleCreated)return;var data=new Dictionary<string,object>{{"synthetic_fixture",true},{"window_id",Handle.ToInt64()},
  {"popup_title",popupTitle},{"popup_class",popup!=null&&!popup.IsDisposed?ClassOf(popup.Handle):""},
  {"popup_window_id",popup!=null&&!popup.IsDisposed?popup.Handle.ToInt64():0},{"popup_visible",popup!=null&&!popup.IsDisposed&&popup.Visible},
  {"popup_modal",popup!=null&&!popup.IsDisposed&&popup.Modal},
  {"opened",opened},{"applied",applied},{"main_value",main.Text},{"applied_value",appliedValue},{"main_changes",mainChanges},{"popup_changes",popupChanges},
  {"foreground_at_receipt",GetForegroundWindow().ToInt64()}};
  string temp=output+".tmp";File.WriteAllText(temp,new JavaScriptSerializer().Serialize(data),new UTF8Encoding(false));if(File.Exists(output))File.Replace(temp,output,null);else File.Move(temp,output);}
 void CreatePopup(){popup=new Form{Text=popupTitle,ClientSize=new Size(440,180),StartPosition=FormStartPosition.Manual,Location=new Point(170,350)};
  entry=new TextBox();Id(entry,"PopupValue","Popup value");entry.SetBounds(25,25,380,35);entry.TextChanged+=delegate{popupChanges++;Save();};popup.Controls.Add(entry);
  var apply=new Button{Text="Apply and close"};Id(apply,"ApplyPopup","Apply popup");apply.SetBounds(25,90,230,45);
  apply.Click+=delegate{applied++;appliedValue=entry.Text;main.Text=entry.Text;Save();popup.BeginInvoke(new Action(delegate{popup.Close();popup=null;Save();}));};popup.Controls.Add(apply);
  IntPtr unused=popup.Handle;}
 PopupWorkflowFixture(string title,string receipt){output=receipt;popupTitle=title+" popup";Text=title;ClientSize=new Size(570,215);Location=new Point(80,90);StartPosition=FormStartPosition.Manual;
  Font=new Font("Segoe UI",10);main.Text="ready";Id(main,"MainValue","Main value");main.SetBounds(25,25,515,35);main.TextChanged+=delegate{mainChanges++;Save();};Controls.Add(main);
  var open=new Button{Text="Open popup"};Id(open,"OpenPopup","Open popup");open.SetBounds(25,95,250,45);Controls.Add(open);
  open.Click+=delegate{if(popup==null||popup.IsDisposed)CreatePopup();opened++;main.Text="opened";
   if(opened==3){popup.Shown+=delegate{Save();};popup.ShowDialog(this);}else popup.Show(this);Save();};
  CreatePopup();Shown+=delegate{Save();};lifetime.Interval=180000;lifetime.Tick+=delegate{Application.Exit();};lifetime.Start();}
 [STAThread]static void Main(string[] args){if(args.Length!=2)return;SetProcessDpiAwarenessContext(new IntPtr(-4));Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new PopupWorkflowFixture(args[0],args[1]));}
}'''


def prepare(folder, driver):
    folder=folder.resolve()
    if folder.exists(): raise ValueError("Use a fresh isolated output folder")
    folder.mkdir(parents=True)
    source,exe=folder/"PopupWorkflowFixture.cs",folder/"PopupWorkflowFixture.exe"
    source.write_text(SOURCE,encoding="utf-8-sig")
    subprocess.run([str(compiler_path()),"/nologo","/target:winexe","/optimize+","/codepage:65001",
        "/reference:System.Windows.Forms.dll","/reference:System.Drawing.dll","/reference:System.Web.Extensions.dll","/out:"+str(exe),str(source)],
        check=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    save_config(folder/"config.json",{"version":1,"driver":str(driver.resolve()),"state_dir":str(folder/"state"),
        "programs":[{"id":"popup_replay","name":"Synthetic owned popup replay","exe":str(exe),"enabled":True}],
        "mode":"uia","approval":"client","log_detail":"metadata","max_minutes":10,"max_actions":100,"observation_timeout_seconds":20})
    return folder,exe


def run(folder, exe, bundle=None):
    root=bundle.resolve() if bundle else HERE
    owners=[];client=None;steps=[];started=time.monotonic()
    report={"passed":False,"version":VERSION,"saved_recipe_replay_tested":True,"public_mcp_used":True,
        "private_application_used":False,"external_llm_used":False,"bundle":str(root) if bundle else None,"steps":steps,"runs":[],
        "human_usability_tested":False,"synthetic_checkpoint_acknowledgement":True}
    names=["server.py","session_runtime.py","workflows.py","operations.py","closing.py","window_transitions.py","recording_windows.py"]
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    report["tested_sha256"]=hashes
    def write(): (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def receipt():
        deadline=time.monotonic()+5
        while True:
            try:return json.loads((folder/"receipt.json").read_text(encoding="utf-8"))
            except (OSError,ValueError):
                if time.monotonic()>deadline:raise
                time.sleep(.05)
    def call(name,args=None,*,raw=False,allow_error=False):
        before=time.monotonic()
        response=client.request("tools/call",{"name":name,"arguments":args or {}},timeout=60)
        value=decoded(response)
        steps.append({"tool":name,"arguments":args or {},"elapsed_ms":round((time.monotonic()-before)*1000,2),"result":value,
            "image_count":sum(item.get("type")=="image" for item in response.get("content",[])),"isError":bool(response.get("isError"))});write()
        assert allow_error or not response.get("isError"),(name,value)
        return response if raw else value
    def image_opener(target):
        from picker_validation import Native
        native=Native();hwnd=target["window_id"]
        assert native.pid(hwnd)==target["pid"] and native.user.IsWindowVisible(hwnd)
        native.user.SetWindowPos(hwnd,-1,0,0,0,0,0x43)
        native.user.SetForegroundWindow(hwnd)
        if native.user.GetForegroundWindow()!=hwnd:
            bounds=wintypes.RECT();assert native.user.GetWindowRect(hwnd,ctypes.byref(bounds))
            caption=wintypes.POINT(bounds.left+45,bounds.top+12)
            assert native.pid(hwnd)==target["pid"] and native.user.GetAncestor(native.user.WindowFromPoint(caption),2)==hwnd
            assert native.user.SetCursorPos(caption.x,caption.y)
            native.user.mouse_event(2,0,0,0,0);native.user.mouse_event(4,0,0,0,0)
        deadline=time.monotonic()+5
        while native.user.GetForegroundWindow()!=hwnd and time.monotonic()<deadline:time.sleep(.05)
        assert native.user.GetForegroundWindow()==hwnd,"Owned fixture must be foreground before screenshot"
        response=call("computer_inspect",{**target,"observation":"visual"},raw=True)
        images=[item for item in response.get("content",[]) if item.get("type")=="image"]
        assert len(images)==1,response
        buttons=[row for row in native.windows(target["pid"],hwnd)
            if row["visible"] and row["text"]=="Open popup" and "BUTTON" in row["class"].upper()]
        assert len(buttons)==1,buttons
        # Driver captures the visible DWM frame, excluding resize shadows.
        # Read the actual fixture frame instead of inferring crop offsets.
        dwm=ctypes.WinDLL("dwmapi",use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes=[wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype=ctypes.c_long
        frame=wintypes.RECT();assert dwm.DwmGetWindowAttribute(hwnd,9,ctypes.byref(frame),ctypes.sizeof(frame))==0
        bounds=[frame.left,frame.top,frame.right,frame.bottom]
        button=buttons[0]["bounds"]
        with Image.open(io.BytesIO(base64.b64decode(images[0]["data"]))) as bitmap:
            # Driver and DWM can disagree on their two border pixels. Do not
            # assume which edge was removed: retain an eight-pixel interior
            # margin so the entire template remains within the real button
            # for every offset in that bounded difference.
            difference_x=bounds[2]-bounds[0]-bitmap.width
            difference_y=bounds[3]-bounds[1]-bitmap.height
            assert difference_x in (0,2) and difference_y in (0,2),(bitmap.size,bounds)
            margin=8
            region=(button[0]-bounds[0]+margin,button[1]-bounds[1]+margin,
                button[2]-bounds[0]-margin,button[3]-bounds[1]-margin)
            assert 0<=region[0]<region[2]<=bitmap.width and 0<=region[1]<region[3]<=bitmap.height,region
            bitmap.save(folder/"image-opener-source.png")
            crop=bitmap.crop(region).convert("RGB");crop.save(folder/"image-opener-template.png")
            data=io.BytesIO();crop.save(data,format="PNG")
            image_target={"format":"computer-image-target/v1","template_png":base64.b64encode(data.getvalue()).decode(),
                "width":crop.width,"height":crop.height,"anchor":{"x":.5,"y":.5},
                "capture_window":{"width":bitmap.width,"height":bitmap.height},"min_score":.94,"ambiguity_margin":.03}
        report["image_template_provenance"]={"public_tool":"computer_inspect","observation":"visual","target":target,
            "fixture_button_hwnd":buttons[0]["hwnd"],"actual_screen_bounds":button,"crop":region,
            "screenshot_frame_difference":{"width":difference_x,"height":difference_y},
            "interior_margin_pixels":margin,"frame_edge_alignment_assumed":False,
            "foreground_verified":native.user.GetForegroundWindow()==hwnd}
        return {"program_id":"popup_replay","operation":"image_click","image_target":image_target}
    try:
        title="Synthetic saved popup "+uuid.uuid4().hex[:8]
        fixture=subprocess.Popen([str(exe),title,str(folder/"receipt.json")],cwd=folder,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        owners.append(OwnedProcess(fixture));proof=receipt();assert proof["synthetic_fixture"]
        target={"pid":fixture.pid,"window_id":proof["window_id"]}
        from build_portable import runtime_environment
        python=root/"runtime/python.exe" if bundle else Path(sys.executable)
        client=Client(folder/"config.json",root/"server.py",python,runtime_environment(python.parent));owners.append(OwnedProcess(client.child))
        assert call("computer_status")["version"]==VERSION
        call("computer_begin",{"program_ids":["popup_replay"],"task_description":"Owned synthetic saved-popup workflow only"})
        main={"role":"Edit","name":"Main value"};popup={"role":"Edit","name":"Popup value"}
        recipe=call("computer_save_task",{"name":"Synthetic popup replay","instructions":"Open, bind, fill, apply and return to the main window",
            "expected":"Each popup is opened and applied once, with the final main value verified","program_ids":["popup_replay"],
            "variables":{"choice":{"description":"Synthetic input"}},"steps":[
                {"program_id":"popup_replay","operation":"click","selector":{"role":"Button","name":"Open popup"},
                 "expect":[{"selector":main,"property":"value","equals":"opened"}],"window_transition":{"mode":"same_window"}},
                {"program_id":"popup_replay","operation":"wait_for_window","window_ref":"popup","owner_ref":"main",
                 "title":proof["popup_title"],"class_name":proof["popup_class"],"timeout_ms":5000},
                {"program_id":"popup_replay","window_ref":"popup","operation":"set_value","selector":popup,"value":"${choice}"},
                {"program_id":"popup_replay","window_ref":"popup","operation":"click","selector":{"role":"Button","name":"Apply popup"},
                 "expect":[{"selector":main,"property":"value","equals":"${choice}"}],"window_transition":{"mode":"auto"},"verification_timeout_ms":6000},
                {"program_id":"popup_replay","operation":"set_value","selector":main,"value":"done-${choice}"}]})
        for index,choice in enumerate(("ALPHA","BETA"),1):
            answer=call("computer_run_task",{"task_id":recipe["id"],"inputs":{"choice":choice},
                "targets":[{"program_id":"popup_replay",**target}],"delivery_mode":"background","execution_mode":"standard"})
            final=receipt();report["runs"].append({"result":answer,"receipt":final});write()
            assert answer["task_verified"] is True,answer
            assert final["opened"]==index and final["applied"]==index and not final["popup_visible"],final
            assert final["applied_value"]==choice and final["main_value"]=="done-"+choice,final
        opener=image_opener(target)
        image_recipe=call("computer_save_task",{"name":"Synthetic image opens owned popup","instructions":"Review the popup screenshot, then fill and return",
            "expected":"A single image click opens its popup; synthetic acknowledgement resumes without reopening","program_ids":["popup_replay"],"steps":[opener,
                {"program_id":"popup_replay","operation":"wait_for_window","window_ref":"popup","owner_ref":"main",
                 "title":proof["popup_title"],"class_name":proof["popup_class"],"timeout_ms":5000},
                {"program_id":"popup_replay","window_ref":"popup","operation":"checkpoint","opened_from":"main","message":"Synthetic owned popup review"},
                {"program_id":"popup_replay","window_ref":"popup","operation":"set_value","selector":popup,"value":"IMAGE"},
                {"program_id":"popup_replay","window_ref":"popup","operation":"click","selector":{"role":"Button","name":"Apply popup"},
                 "expect":[{"selector":main,"property":"value","equals":"IMAGE"}],"window_transition":{"mode":"auto"},"verification_timeout_ms":6000},
                {"program_id":"popup_replay","operation":"set_value","selector":main,"value":"done-IMAGE"}]})
        pending_response=call("computer_run_task",{"task_id":image_recipe["id"],"targets":[{"program_id":"popup_replay",**target}],
            "delivery_mode":"background","execution_mode":"standard"},raw=True,allow_error=True)
        pending=decoded(pending_response);pending_receipt=receipt()
        from picker_validation import Native
        report["image_popup_run"]={"initial_pending":pending,"initial_receipt":pending_receipt,
            "foreground_at_checkpoint":int(Native().user.GetForegroundWindow() or 0)};write()
        assert pending["status"]=="needs_review" and pending["pending_step"]==2 and not pending["task_verified"],pending
        if not pending["checkpoint"]["capture_available"]:
            assert pending.get("last_result",{}).get("diagnostic",{}).get("code")=="checkpoint_requires_foreground",pending
            assert pending_receipt["popup_visible"] and pending_receipt["popup_modal"],pending_receipt
            # Explicit test setup requested by the caller, using the public
            # guarded focus tool. Do not change or bypass the capture guard.
            popup_target={"pid":target["pid"],"window_id":pending_receipt["popup_window_id"]}
            call("get_window_state",popup_target)
            call("bring_to_front",popup_target)
            pending_response=call("computer_run_task",{"task_id":image_recipe["id"],"targets":[{"program_id":"popup_replay",**target}],
                "resume_run_id":pending["run_id"],"delivery_mode":"background","execution_mode":"standard"},raw=True,allow_error=True)
            pending=decoded(pending_response);pending_receipt=receipt()
            report["image_popup_run"]["explicit_public_popup_focus_required"]=True
        report["image_popup_run"].update(pending=pending,pending_receipt=pending_receipt);write()
        assert pending["checkpoint"]["capture_available"] is True,pending
        assert any(item.get("type")=="image" for item in pending_response.get("content",[])),"Popup checkpoint PNG missing from public MCP response"
        assert pending_receipt["opened"]==3 and pending_receipt["applied"]==2 and pending_receipt["popup_visible"],pending_receipt
        answer=call("computer_run_task",{"task_id":image_recipe["id"],"targets":[{"program_id":"popup_replay",**target}],
            "resume_run_id":pending["run_id"],"acknowledge_checkpoint":pending["checkpoint"]["id"],
            "delivery_mode":"background","execution_mode":"standard"})
        final=receipt();report["image_popup_run"].update(result=answer,receipt=final);write()
        assert answer["task_verified"] is True,answer
        assert final["opened"]==3 and final["applied"]==3 and not final["popup_visible"],final
        assert final["applied_value"]=="IMAGE" and final["main_value"]=="done-IMAGE",final
        report["passed"]=True
    except Exception as error:
        report["error"]={"type":type(error).__name__,"message":str(error)[:6000],"traceback":traceback.format_exc()[-4000:]}
    finally:
        if client is not None:
            try:call("computer_end");client.close()
            except Exception as error:report["cleanup_error"]=str(error);report["passed"]=False
        for owner in reversed(owners):
            owner.close()
            try:owner.process.wait(timeout=5)
            except subprocess.TimeoutExpired:report["passed"]=False
        report["owned_processes_exited"]=all(owner.process.poll() is not None for owner in owners)
        report["source_unchanged"]=all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())
        report["elapsed_ms"]=round((time.monotonic()-started)*1000,2)
        report["passed"]=report["passed"] and report["source_unchanged"] and report["owned_processes_exited"]
        write()
    return report


if __name__=="__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8",errors="replace")
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--folder",type=Path,required=True)
    parser.add_argument("--driver",type=Path,default=DRIVER);parser.add_argument("--bundle",type=Path);parser.add_argument("--run",action="store_true")
    args=parser.parse_args();folder,exe=prepare(args.folder,args.driver)
    if not args.run: print(str(exe));raise SystemExit(0)
    result=run(folder,exe,args.bundle);print(json.dumps({key:result.get(key) for key in ("passed","error","elapsed_ms","owned_processes_exited")},ensure_ascii=False));raise SystemExit(0 if result["passed"] else 1)
