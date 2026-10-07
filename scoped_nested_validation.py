"""Owned nested-form validation. Default only compiles; --run opens the fixture."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from build_portable import COMPILER, SOURCE, run_hidden
from operations import _unique


FIXTURE = r'''
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Web.Script.Serialization;
using System.Windows.Forms;
class NestedFixture {
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles();
        var form = new Form { Text="Computer Use MCP owned nested scope fixture", Width=600, Height=360 };
        var outer = new GroupBox { Text="Outer scope", AccessibleName="Outer scope", Bounds=new Rectangle(20,20,530,260) };
        var inner = new GroupBox { Text="Inner scope", AccessibleName="Inner scope", Bounds=new Rectangle(20,40,460,150) };
        var field = new TextBox { AccessibleName="Scoped field", Text="ready", Bounds=new Rectangle(20,50,360,28) };
        inner.Controls.Add(field); outer.Controls.Add(inner); form.Controls.Add(outer);
        form.Shown += delegate {
            File.WriteAllText(args[0]+".tmp", new JavaScriptSerializer().Serialize(new {
                pid=Process.GetCurrentProcess().Id, window_id=form.Handle.ToInt64() }));
            File.Move(args[0]+".tmp", args[0]);
        };
        Application.Run(form);
    }
}
'''


def prepare(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source, executable = directory / "NestedFixture.cs", directory / "NestedFixture.exe"
    source.write_text(FIXTURE, encoding="utf-8")
    run_hidden([str(COMPILER), "/nologo", "/target:winexe", "/reference:System.dll",
                "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
                "/out:" + str(executable), str(source)], cwd=directory)
    return executable


def validate(directory, *, bundle=None):
    """Only a coordinator holding the GUI slot should invoke this function."""
    directory = Path(directory).resolve()
    helper_root = Path(bundle).resolve() if bundle is not None else SOURCE
    helper_path = helper_root / "Computer Use MCP 빠른 확인.exe"
    if not helper_path.is_file():
        raise FileNotFoundError(helper_path)
    executable = prepare(directory)
    ready = directory / "fixture-ready.json"
    ready.unlink(missing_ok=True)
    fixture = subprocess.Popen([str(executable), str(ready)], cwd=directory)
    helper = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if fixture.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("fixture readiness failed")
            time.sleep(.05)
        target = json.loads(ready.read_text())
        inner = {"name": "Scoped field", "within": {"name": "Inner scope"}}
        outer = {"name": "Scoped field", "within": {"name": "Outer scope"}}
        requests = [{"id": str(index).zfill(32), **target, "selectors": selectors}
                    for index, selectors in enumerate(([inner, outer], [outer, inner]), 1)]
        helper = subprocess.Popen([str(helper_path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW, cwd=helper_root)
        output, stderr = helper.communicate("".join(json.dumps(item) + "\n" for item in requests), timeout=20)
        answers = [json.loads(line) for line in output.splitlines()]
        if helper.returncode != 0 or len(answers) != 2:
            raise RuntimeError("native helper response failed")
        for answer, request in zip(answers, requests):
            assert answer["ok"] is True and answer["id"] == request["id"]
            data = answer["data"]
            assert data["scope_complete"] and data["read_only"]
            assert all(data[key] == target[key] for key in target)
            for selector in request["selectors"]:
                assert _unique(data, selector)["value"] == "ready"
        report = {"passed": True, "query_orders": 2, "uia_properties_verified": 4, "input_dispatched": False,
                  "helper_path": str(helper_path), "helper_sha256": hashlib.sha256(helper_path.read_bytes()).hexdigest()}
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        for process in (helper, fixture):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--bundle", type=Path, help="Validate the helper from this portable bundle directory")
    args = parser.parse_args()
    print(json.dumps(validate(args.directory, bundle=args.bundle) if args.run else {"prepared": str(prepare(args.directory)), "gui_started": False}))
