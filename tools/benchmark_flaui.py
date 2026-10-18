"""Read-only comparison on existing windows. Never activates or opens an app."""
import argparse
import ctypes
import json
from pathlib import Path
import queue
import statistics
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / ".data/flaui018/runtime/Computer Use MCP FlaUI.exe"


def measure(command, request, repeat=3, timeout=6):
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW, cwd=ROOT)
    output = queue.Queue()
    def reader():
        for line in p.stdout:
            output.put(line)
    threading.Thread(target=reader, daemon=True).start()
    results = []
    try:
        for iteration in range(repeat):
            started = time.perf_counter()
            p.stdin.write(json.dumps({"id":uuid.uuid4().hex, **request})+"\n"); p.stdin.flush()
            try:
                response = json.loads(output.get(timeout=timeout))
            except queue.Empty:
                results.append({"ok":False,"code":"process_timeout","elapsed_ms":round((time.perf_counter()-started)*1000,2)})
                break
            data = response.get("data", {}); rows = data.get("elements", [])
            # Do not persist document text, input values or screenshots in the benchmark.
            results.append({"ok":response.get("ok"), "code":response.get("code"),
                "exception_type":response.get("exception_type"), "elapsed_ms":round((time.perf_counter()-started)*1000,2), "count":len(rows),
                "named":sum(bool(e.get("name")) for e in rows),
                "value_count":sum("value" in e for e in rows),
                "pattern_count":sum(bool(e.get("observed_patterns")) for e in rows),
                "roles":sorted(set(e.get("role","") for e in rows)),
                "identity": [{"name":e.get("name"), "automation_id":e.get("automation_id"), "bounds":e.get("bounds")} for e in rows] if request.get("operation")=="point" else None,
                "truncated":data.get("truncated"), "backend":data.get("backend")})
    finally:
        p.terminate()
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=2)
    times = [r["elapsed_ms"] for r in results if r.get("ok")]
    return {"samples":results,"p50_ms":statistics.median(times) if times else None}


def main():
    # Match screenshot and helper physical pixel coordinates on scaled monitors.
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    parser=argparse.ArgumentParser()
    parser.add_argument("--window",action="append",required=True,help="LABEL:HWND")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--point", action="store_true", help="Window format LABEL:HWND:localX:localY; no activation")
    args=parser.parse_args(); report={"read_only":True,"windows":[]}
    for value in args.window:
        parts=value.split(":"); label,raw=parts[:2]; hwnd=int(raw); pid=ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),ctypes.byref(pid))
        title=ctypes.create_unicode_buffer(4096)
        ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(hwnd),title,4096)
        request={"pid":pid.value,"window_id":hwnd,"operation":"inspect","within":{"name":title.value,"role":"Window"},"max_depth":12,"max_elements":600,"timeout_ms":5000}
        native=ROOT/"Computer Use MCP 빠른 확인.exe"
        if args.point:
            from ctypes import wintypes
            rect=wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd),ctypes.byref(rect))
            request={"pid":pid.value,"window_id":hwnd,"operation":"point","x":rect.left+int(parts[2]),"y":rect.top+int(parts[3])}
            native=ROOT/".data/flaui018/FlaUINativePointBaseline.exe"
        window={"label":label,"pid":pid.value,"window_id":hwnd,"results":{}}
        if args.point:
            window["point"]={"x":request["x"],"y":request["y"],"window_rect":[rect.left,rect.top,rect.right,rect.bottom]}
        for backend,command in (("native",[str(native)]),
            ("flaui_uia2",[str(PROBE),"uia2"]),
            ("flaui_uia3",[str(PROBE),"uia3"])):
            window["results"][backend]=measure(command,request)
        report["windows"].append(window)
    args.output.parent.mkdir(exist_ok=True,parents=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True))


if __name__=="__main__": main()
