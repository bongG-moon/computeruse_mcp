"""Interactive acceptance: real recorder + public MCP replay, synthetic data only.

prepare builds an isolated fixture; serve exposes file commands for the test
operator. Demonstration input is supplied through Computer Use, never fabricated
recording events. The application records actual input effects independently.
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
import traceback

from live_validation import Client, DRIVER, decoded, safe
from native_fixture import compiler_path
from settings import VERSION, save_config

HERE = Path(__file__).resolve().parent
SOURCE = r'''
using System; using System.Collections.Generic; using System.Drawing; using System.IO;
using System.Reflection; using System.Runtime.InteropServices; using System.Web.Script.Serialization; using System.Windows.Forms;
class RecordReplayFixture : Form {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr c);
 readonly string folder=Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
 readonly TextBox choice=new TextBox(); readonly Label result=new Label(); readonly Panel card=new Panel();
 readonly List<string> queries=new List<string>(); readonly List<string> values=new List<string>();
 int imageClicks,popupOpened,popupApplied; Form popup; readonly Timer pulse=new Timer();
 void Save(){if(!IsHandleCreated)return; var data=new Dictionary<string,object>{{"synthetic_fixture",true},{"pid",System.Diagnostics.Process.GetCurrentProcess().Id},
  {"window_id",Handle.ToInt64()},{"title",Text},{"value",choice.Text},{"queries",queries.ToArray()},{"values",values.ToArray()},
  {"image_clicks",imageClicks},{"popup_opened",popupOpened},{"popup_applied",popupApplied},{"popup_window_id",popup!=null&&!popup.IsDisposed?popup.Handle.ToInt64():0},
  {"popup_visible",popup!=null&&!popup.IsDisposed&&popup.Visible},{"result",result.Text},{"window_width",Width},{"window_height",Height},
  {"left",Left},{"top",Top},{"utc",DateTime.UtcNow.ToString("o")}};
  string p=Path.Combine(folder,"receipt.json"),t=p+".tmp";File.WriteAllText(t,new JavaScriptSerializer().Serialize(data));if(File.Exists(p))File.Replace(t,p,null);else File.Move(t,p);}
 Button Btn(string name,string text,int x,int y,int w,EventHandler action){var b=new Button{Name=name,AccessibleName=text,Text=text}; b.SetBounds(x,y,w,40);b.Click+=action;Controls.Add(b);return b;}
 void Reset(){queries.Clear();values.Clear();imageClicks=popupOpened=popupApplied=0;choice.Text="READY";values.Clear();result.Text="Ready for recording";Save();}
 void OpenPopup(){if(popup!=null&&!popup.IsDisposed)return;popupOpened++;popup=new Form{Text="Recording acceptance owned popup",ClientSize=new Size(420,170),StartPosition=FormStartPosition.CenterParent,Font=Font};
  var note=new Label{Text="This popup belongs to the test application.",AutoSize=true,Left=20,Top=25};popup.Controls.Add(note);
  var apply=new Button{Name="ApplyPopup",AccessibleName="Apply popup",Text="Apply popup",Left=20,Top=80,Width=220,Height=45};
  apply.Click+=delegate{popupApplied++;result.Text="Popup applied";popup.Close();popup=null;Save();};popup.Controls.Add(apply);popup.FormClosed+=delegate{Save();};popup.Show(this);Save();}
 RecordReplayFixture(){Text="Computer Use 0.15 recording acceptance";ClientSize=new Size(760,460);Font=new Font("Segoe UI",11);BackColor=Color.FromArgb(245,247,251);AutoScaleMode=AutoScaleMode.None;StartPosition=FormStartPosition.Manual;Location=new Point(100,90);
  Controls.Add(new Label{Text="Record actual actions, then replay the saved process",AutoSize=true,Left=25,Top=20,Font=new Font("Segoe UI",14,FontStyle.Bold)});
  Controls.Add(new Label{Text="Condition value",AutoSize=true,Left=25,Top=78});choice.Name="ConditionValue";choice.AccessibleName="Condition value";choice.Text="READY";choice.SetBounds(25,105,330,35);Controls.Add(choice);
  Btn("Query","Query",390,102,160,delegate{queries.Add(choice.Text);result.Text="Query "+queries.Count+": "+choice.Text;Save();});
  Btn("OpenPopup","Open popup",25,170,180,delegate{OpenPopup();});
  Btn("ResetFixture","Reset fixture",555,102,165,delegate{Reset();});
  card.Name="PaintedSurface";card.AccessibleName="Painted surface";card.SetBounds(25,235,380,115);card.BackColor=Color.FromArgb(32,69,124);
  card.Paint+=delegate(object s,PaintEventArgs e){using(var f=new Font("Segoe UI",21,FontStyle.Bold))e.Graphics.DrawString("IMAGE ACTION",f,Brushes.White,50,32);e.Graphics.DrawRectangle(Pens.LightBlue,8,8,363,98);};
  card.MouseClick+=delegate{imageClicks++;result.Text="Image action "+imageClicks;Save();};Controls.Add(card);
  result.Name="ResultStatus";result.AccessibleName="Result status";result.Text="Ready for recording";result.SetBounds(25,385,700,42);result.BackColor=Color.White;Controls.Add(result);
  choice.TextChanged+=delegate{values.Add(choice.Text);Save();};Move+=delegate{Save();};Resize+=delegate{Save();};Shown+=delegate{Save();};
  pulse.Interval=1000;pulse.Tick+=delegate{Save();};pulse.Start();}
 [STAThread]static void Main(){SetProcessDpiAwarenessContext(new IntPtr(-4));Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new RecordReplayFixture());}
}'''


def write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def prepare(folder):
    folder = folder.resolve()
    if folder.exists():
        raise ValueError("Choose a new isolated folder")
    folder.mkdir(parents=True)
    (folder / "commands").mkdir()
    (folder / "responses").mkdir()
    source, exe = folder / "RecordReplayFixture.cs", folder / "RecordReplayFixture.exe"
    source.write_text(SOURCE, encoding="utf-8-sig")
    subprocess.run([str(compiler_path()), "/nologo", "/target:winexe", "/optimize+", "/codepage:65001",
        "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.Web.Extensions.dll",
        "/out:" + str(exe), str(source)], check=True, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    save_config(folder / "config.json", {"version": 1, "driver": str(DRIVER.resolve()), "state_dir": str(folder / "state"),
        "programs": [{"id": "record_replay", "name": "Recording acceptance fixture", "exe": str(exe), "enabled": True}],
        "mode": "uia", "approval": "client", "log_detail": "metadata", "max_minutes": 30, "max_actions": 200,
        "observation_timeout_seconds": 12})
    write(folder / "provenance.json", {"version": VERSION, "synthetic_fixture": True,
        "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(), "exe": str(exe),
        "private_application_used": False, "real_llm_used": False,
        "recording_events_fabricated": False, "demonstration_input": "Computer Use screenshot-backed clicks and typing"})
    print(exe)


def serve(folder, bundle):
    root = bundle.resolve() if bundle else HERE
    python = root / "runtime/python.exe" if bundle else Path(sys.executable)
    from build_portable import runtime_environment
    client = Client(folder / "config.json", root / "server.py", python,
                    runtime_environment(python.parent) if bundle else None)
    write(folder / "server-ready.json", {"pid": client.child.pid, "version": VERSION, "root": str(root)})
    try:
        seen = {path.name for path in (folder / "responses").glob("*.json")}
        while not (folder / "stop-server.flag").exists():
            for path in sorted((folder / "commands").glob("*.json")):
                if path.name in seen:
                    continue
                command = json.loads(path.read_text(encoding="utf-8-sig"))
                seen.add(path.name)
                began = time.monotonic()
                try:
                    response = client.request("tools/call", {"name": command["tool"], "arguments": command.get("arguments", {})}, timeout=90)
                    result = {"command": command, "result": decoded(response), "isError": bool(response.get("isError")),
                              "image_count": sum(c.get("type") == "image" for c in response.get("content", []))}
                except Exception as error:
                    result = {"command": command, "exception": str(error), "traceback": traceback.format_exc()}
                result["elapsed_ms"] = round((time.monotonic()-began)*1000, 2)
                write(folder / "responses" / path.name, result)
            time.sleep(.1)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "serve"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    prepare(args.folder) if args.mode == "prepare" else serve(args.folder.resolve(), args.bundle)
