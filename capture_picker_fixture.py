"""Build an opt-in, owned-window image-picker regression fixture (never launch it).

Developer use: ``python -B capture_picker_fixture.py``. Open the printed EXE with
the approved Computer Use tool. The fixture only moves/minimizes its own window,
starts the local product helper, and writes receipts under .data/capture-fix-qa.
It never injects input, enumerates user processes, or captures other windows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from build_portable import COMPILER, SOURCE, VISUAL_TOOLS_NAME, compile_visual_tools, run_hidden


ROOT = SOURCE / ".data" / "capture-fix-qa"

CS = r'''
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class CaptureFixture : Form
{
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT rect);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT rect, int size);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    static readonly string Root = @"__ROOT__";
    static readonly string HelperName = @"__HELPER__";
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };
    readonly RadioButton baseline = new RadioButton { Text = "Baseline 0.14.0", AutoSize = true };
    readonly RadioButton current = new RadioButton { Text = "Current fix", AutoSize = true, Checked = true };
    readonly Label status = new Label { AutoSize = false, Height = 74, Dock = DockStyle.Bottom };
    readonly Label countLabel = new Label { Text = "Safe button presses: 0", AutoSize = true };
    readonly Timer poll = new Timer { Interval = 250 };
    Process helper;
    string response, receipt, nonce, scenario, variant;
    int pressCount;
    bool collected;

    static object Box(Rectangle b) { return new Dictionary<string, object> { {"x", b.X}, {"y", b.Y}, {"width", b.Width}, {"height", b.Height} }; }
    static Rectangle Rect(RECT b) { return Rectangle.FromLTRB(b.Left, b.Top, b.Right, b.Bottom); }
    static void Write(string path, object value) { File.WriteAllText(path, Json.Serialize(value), new UTF8Encoding(false)); }
    Button ActionButton(string text, Action action) {
        var b = new Button { Text = text, Name = text.Replace(" ", ""), AccessibleName = text, AutoSize = true, Height = 42, Margin = new Padding(5), Padding = new Padding(8) };
        b.Click += delegate { action(); }; return b;
    }
    public CaptureFixture() {
        Text = "CUA Capture Regression Fixture"; Name = "CaptureRegressionFixture";
        Font = new Font("Segoe UI", 11); BackColor = Color.FromArgb(242, 246, 252);
        ClientSize = new Size(960, 640); MinimumSize = new Size(850, 560);
        StartPosition = FormStartPosition.CenterScreen; AutoScaleMode = AutoScaleMode.Dpi;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 4, ColumnCount = 1, Padding = new Padding(20) };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 72));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 124));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
        var heading = new Label { Text = "Image picker capture regression\nSynthetic window only - no business data", Dock = DockStyle.Fill, Font = new Font("Segoe UI", 16, FontStyle.Bold) }; layout.Controls.Add(heading, 0, 0);
        var controls = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true };
        controls.Controls.Add(baseline); controls.Controls.Add(current); controls.SetFlowBreak(current, true);
        controls.Controls.Add(ActionButton("Normal + picker", delegate { WindowState = FormWindowState.Normal; StartPicker("normal"); }));
        controls.Controls.Add(ActionButton("Maximize + picker", delegate { WindowState = FormWindowState.Maximized; StartPicker("maximized"); }));
        controls.Controls.Add(ActionButton("Minimize + picker", delegate { WindowState = FormWindowState.Minimized; StartPicker("minimized"); }));
        controls.Controls.Add(ActionButton("Restore normal", delegate { WindowState = FormWindowState.Normal; UpdateReceipt("restored", null); }));
        var secondary = ActionButton("Secondary monitor + picker", delegate { Screen[] screens = Screen.AllScreens; if (screens.Length < 2) return; WindowState = FormWindowState.Normal; Screen here = Screen.FromHandle(Handle); Screen next = screens[0].DeviceName == here.DeviceName ? screens[1] : screens[0]; Bounds = new Rectangle(next.WorkingArea.X + 32, next.WorkingArea.Y + 32, Math.Min(960, next.WorkingArea.Width - 64), Math.Min(680, next.WorkingArea.Height - 64)); StartPicker("secondary_monitor_normal"); });
        secondary.Enabled = Screen.AllScreens.Length > 1; controls.Controls.Add(secondary);
        layout.Controls.Add(controls, 0, 1);
        var canvas = new Panel { Dock = DockStyle.Fill, BackColor = Color.White, Name = "OwnerDrawnTarget", AccessibleName = "Owner drawn capture target" };
        canvas.Paint += delegate(object s, PaintEventArgs e) {
            e.Graphics.Clear(Color.White);
            using (var brush = new SolidBrush(Color.FromArgb(31, 68, 111))) e.Graphics.FillRectangle(brush, 28, 25, 340, 170);
            using (var brush = new SolidBrush(Color.FromArgb(73, 214, 173))) e.Graphics.FillPolygon(brush, new Point[] {new Point(54, 64), new Point(100, 64), new Point(100, 42), new Point(139, 86), new Point(100, 130), new Point(100, 108), new Point(54, 108)});
            using (var font = new Font("Segoe UI", 20, FontStyle.Bold)) e.Graphics.DrawString("CAPTURE OK", font, Brushes.White, new PointF(148, 60));
            using (var font = new Font("Segoe UI", 11)) e.Graphics.DrawString("A1  Synthetic button  2048", font, Brushes.White, new PointF(48, 153));
            using (var pen = new Pen(Color.FromArgb(70, 100, 140), 3)) e.Graphics.DrawRectangle(pen, 25, 22, 346, 176);
        };
        var safe = ActionButton("Named safe button", delegate { pressCount++; countLabel.Text = "Safe button presses: " + pressCount; UpdateReceipt("safe_button_clicked", null); });
        safe.Name = "FixtureAction"; safe.AccessibleName = "Named safe button"; safe.Location = new Point(420, 55); safe.Size = new Size(240, 52); canvas.Controls.Add(safe);
        countLabel.Location = new Point(420, 130); canvas.Controls.Add(countLabel); layout.Controls.Add(canvas, 0, 2);
        layout.Controls.Add(new Label { Text = "Choose a helper version and test state. In the image picker, capture and select the CAPTURE OK card.\nEach request and its final response remain in the local fixture receipts folder.", Dock = DockStyle.Fill }, 0, 3);
        Controls.Add(layout); Controls.Add(status);
        poll.Tick += delegate { Collect(); }; poll.Start();
        Shown += delegate { UpdateReceipt("ready", null); };
        Move += delegate { if (IsHandleCreated && Visible) UpdateReceipt("moved", null); };
        ResizeEnd += delegate { UpdateReceipt("resized", null); };
        FormClosed += delegate { poll.Stop(); poll.Dispose(); UpdateReceipt("closed", null); };
    }
    void StartPicker(string selectedScenario) {
        if (helper != null && !helper.HasExited) { status.Text = "Close the previous image picker first."; return; }
        scenario = selectedScenario; variant = baseline.Checked ? "baseline" : "current";
        string exe = Path.Combine(Path.Combine(Root, variant), HelperName);
        if (!File.Exists(exe)) { status.Text = "Missing helper: " + exe; return; }
        nonce = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
        string folder = Path.Combine(Root, "receipts"); Directory.CreateDirectory(folder);
        string prefix = Path.Combine(folder, DateTime.UtcNow.ToString("yyyyMMdd-HHmmss-fff") + "-" + variant + "-" + scenario);
        string request = prefix + ".request.json"; response = prefix + ".response.json"; receipt = prefix + ".receipt.json";
        Write(request, new Dictionary<string, object> { {"nonce", nonce}, {"pid", Process.GetCurrentProcess().Id}, {"window_id", Handle.ToInt64()}, {"program_id", "capture_regression_fixture"}, {"window_ref", "main"}, {"label", "Synthetic capture regression fixture"}, {"timeout_seconds", 180} });
        collected = false;
        helper = Process.Start(new ProcessStartInfo { FileName = exe, Arguments = "--pick \"" + request + "\" \"" + response + "\"", WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = false });
        status.Text = variant + " / " + scenario + "\n" + response;
        UpdateReceipt("picker_opened", new Dictionary<string, object> { {"request_path", request}, {"response_path", response}, {"helper_pid", helper.Id}, {"helper_exe", exe} });
    }
    void Collect() {
        if (helper == null || collected || !File.Exists(response)) return;
        try {
            var data = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(response));
            if (Convert.ToString(data["nonce"]) != nonce) throw new InvalidDataException("response_nonce_mismatch");
            var result = new Dictionary<string, object>();
            foreach (string key in new string[] {"status", "code", "error_type", "human_confirmed", "pid", "window_id", "width", "height", "selection", "capture_window", "anchor"}) if (data.ContainsKey(key)) result[key] = data[key];
            collected = true; status.Text = variant + " / " + scenario + ": " + Convert.ToString(data["status"]) + "\nReceipt: " + receipt;
            UpdateReceipt("picker_responded", result);
        } catch (IOException) { }
    }
    void UpdateReceipt(string state, object detail) {
        try {
            RECT raw, dwm; GetWindowRect(Handle, out raw); bool hasDwm = DwmGetWindowAttribute(Handle, 9, out dwm, Marshal.SizeOf(typeof(RECT))) == 0;
            var screens = new List<object>(); foreach (Screen s in Screen.AllScreens) screens.Add(new Dictionary<string, object> { {"name", s.DeviceName}, {"primary", s.Primary}, {"bounds", Box(s.Bounds)}, {"working_area", Box(s.WorkingArea)} });
            var data = new Dictionary<string, object> { {"utc", DateTime.UtcNow.ToString("o")}, {"state", state}, {"scenario", scenario}, {"variant", variant}, {"pid", Process.GetCurrentProcess().Id}, {"window_id", Handle.ToInt64()}, {"window_state", WindowState.ToString()}, {"raw_bounds", Box(Rect(raw))}, {"dwm_bounds", hasDwm ? Box(Rect(dwm)) : null}, {"client_bounds", Box(RectangleToScreen(ClientRectangle))}, {"virtual_screen", Box(SystemInformation.VirtualScreen)}, {"screens", screens}, {"safe_button_presses", pressCount}, {"detail", detail}, {"response_path", response}, {"receipt_path", receipt} };
            Write(Path.Combine(Root, "fixture-last.json"), data);
            if (receipt != null && (state == "picker_opened" || state == "picker_responded")) { string outPath = state == "picker_opened" ? receipt.Replace(".receipt.json", ".opened.json") : receipt; Write(outPath, data); }
        } catch (Exception e) { status.Text = "Receipt error: " + e.GetType().Name; }
    }
    [STAThread] static void Main() { Directory.CreateDirectory(Root); try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); } Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new CaptureFixture()); }
}
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(*, refresh_current: bool = False, helper_only: bool = False) -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    baseline = ROOT / "baseline" / VISUAL_TOOLS_NAME
    if not baseline.exists():
        baseline.parent.mkdir(exist_ok=True)
        source = SOURCE / "release" / "Computer-Use-MCP" / VISUAL_TOOLS_NAME
        if not source.is_file():
            raise FileNotFoundError("A built 0.14.0 release helper is needed for the baseline.")
        shutil.copy2(source, baseline)
    current = ROOT / "current" / VISUAL_TOOLS_NAME
    current.parent.mkdir(exist_ok=True)
    compiled_metadata = current.parent / "compiled-source.json"
    if refresh_current or not current.exists() or not compiled_metadata.exists():
        source_sha = sha256(SOURCE / "VisualTools.cs")
        compile_visual_tools(current)
        if source_sha != sha256(SOURCE / "VisualTools.cs"):
            raise RuntimeError("VisualTools.cs changed during compilation; compile again after edits settle.")
        compiled_metadata.write_text(json.dumps({"source_sha256": source_sha, "exe_sha256": sha256(current)}) + "\n", encoding="utf-8")
    compiled = json.loads(compiled_metadata.read_text(encoding="utf-8"))
    if compiled["exe_sha256"] != sha256(current):
        raise RuntimeError("Current helper no longer matches its compiled source receipt.")
    source = ROOT / "CaptureRegressionFixture.cs"
    exe = ROOT / "CUA Capture Regression Fixture.exe"
    if not helper_only:
        source.write_text(CS.replace("__ROOT__", str(ROOT).replace('"', '""')).replace("__HELPER__", VISUAL_TOOLS_NAME.replace('"', '""')), encoding="utf-8")
        result = run_hidden([str(COMPILER), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", "/codepage:65001",
                             "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Drawing.dll",
                             "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll", f"/out:{exe}", str(source)], cwd=SOURCE)
        if result.stdout.strip():
            print(result.stdout.strip())
    manifest = {"fixture_exe": str(exe), "fixture_sha256": sha256(exe), "baseline_exe": str(baseline), "baseline_sha256": sha256(baseline),
                "current_exe": str(current), "current_sha256": sha256(current), "current_source_sha256": compiled["source_sha256"],
                "receipts": str(ROOT / "receipts"), "native_execution_performed": False}
    (ROOT / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-current", action="store_true", help="Recompile the current product helper, preserving the baseline.")
    parser.add_argument("--helper-only", action="store_true", help="Do not recompile the fixture itself; safe while its window is open and its helper is closed.")
    args = parser.parse_args()
    print(json.dumps(build(refresh_current=args.refresh_current, helper_only=args.helper_only), ensure_ascii=False, indent=2))
