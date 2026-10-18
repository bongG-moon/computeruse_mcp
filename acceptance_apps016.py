"""Opt-in real-app acceptance harness. Uses public stdio MCP; never fabricates recording events.

Prepare only writes isolated config/fixtures. Serve executes explicit queued tool calls.
GUI recording demonstrations are performed through Computer Use, not this harness.
"""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import sys
import time
import traceback

from live_validation import Client, DRIVER, decoded, safe
from settings import VERSION, save_config

HERE = Path(__file__).resolve().parent

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def prepare(folder):
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("commands", "responses", "images"):
        (folder/name).mkdir()
    from builtin_programs import builtin_catalog
    catalog = builtin_catalog()
    programs = [
        {"id":"mumu", "name":"MuMuPlayer", "exe":r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\nx_main\MuMuNxMain.exe",
         "launch":{"kind":"exe","arguments":["--from-shortcut"]},"enabled":True},
    ]
    for name in ("chrome", "notepad", "calculator"):
        entry = catalog[name]
        if entry["available"]:
            programs.append({key: value for key, value in
                {**entry, "id": name, "enabled": True}.items()
                if key in {"id", "name", "exe", "control_exes", "launch", "enabled"}})
    save_config(folder/"config.json",{"version":1,"driver":str(DRIVER.resolve()),"state_dir":str(folder/"state"),
        "programs":programs,"mode":"uia","approval":"client","max_minutes":30,"max_actions":300,
        "log_detail":"metadata","observation_timeout_seconds":12})
    write(folder/"provenance.json",{"version":VERSION,"real_app_tests":True,"recording_events_fabricated":False,
        "user_profile_modified":False,"config":"isolated acceptance config","corporate_llm_connected":False})

def serve(folder, bundle=None, administrator=False):
    root = bundle.resolve() if bundle else HERE
    python = root/"runtime/python.exe" if bundle else Path(sys.executable)
    from build_portable import runtime_environment
    command = [str(root/"Computer Use MCP 관리자 연결.exe"), "--config", str(folder/"config.json")] if administrator else None
    client=Client(folder/"config.json",root/"server.py",python,runtime_environment(python.parent) if bundle else None,
                  command=command, initialize_timeout=120 if administrator else 20)
    write(folder/"server-ready.json",{"pid":client.child.pid,"root":str(root),"version":VERSION})
    try:
        seen={p.name for p in (folder/"responses").glob("*.json")}
        while not (folder/"stop-server.flag").exists():
            for path in sorted((folder/"commands").glob("*.json")):
                if path.name in seen: continue
                # A developer may still be writing the queue file. Do not close
                # the live MCP/recording session on an incomplete publication.
                try:
                    command=json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                seen.add(path.name); began=time.monotonic()
                try:
                    if command.get("method")=="tools/list":
                        response=client.request("tools/list",timeout=40)
                    else:
                        response=client.request("tools/call",{"name":command["tool"],"arguments":command.get("arguments",{})},timeout=180)
                    images=[]
                    for n,block in enumerate(response.get("content",[])):
                        if block.get("type")=="image" and block.get("mimeType")=="image/png":
                            target=folder/"images"/(path.stem+f"-{n}.png")
                            target.write_bytes(base64.b64decode(block["data"],validate=True)); images.append(str(target))
                    answer={"command":command,"result":safe(decoded(response)),"isError":bool(response.get("isError")),"images":images}
                except Exception as exc:
                    answer={"command":command,"exception":str(exc),"traceback":traceback.format_exc()}
                answer["elapsed_ms"]=round((time.monotonic()-began)*1000,2)
                write(folder/"responses"/path.name,answer)
            time.sleep(.1)
    finally:
        client.close()

if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("mode",choices=["prepare","serve"]); p.add_argument("folder",type=Path);p.add_argument("--bundle",type=Path);p.add_argument("--administrator",action="store_true")
    a=p.parse_args(); prepare(a.folder.resolve()) if a.mode=="prepare" else serve(a.folder.resolve(),a.bundle,a.administrator)
