// Isolated structural fixture. Stdin configures only this process's own forms.
// No external SendInput, business application, recording or export is performed.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Windows.Forms.Integration;

sealed class AgentWorkForm : Form {
    public readonly ToolStrip Commands;
    public readonly Panel Body;
    public Form Embedded;
    public Button Query;
    public ToolStripButton OuterQuery;
    public int BodyClicks, ToolbarClicks;
    public string Phase = "empty";
    readonly List<System.Windows.Forms.Timer> timers = new List<System.Windows.Forms.Timer>();
    readonly double scale;
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get { var p = base.CreateParams; p.ExStyle &= ~0x00040000; p.ExStyle |= 0x08000080; return p; }
    }
    public AgentWorkForm(string title, double contentScale) {
        Text = title; Name = "SyntheticWorkHost"; scale = contentScale;
        // Keep the default independent form owner. ShowInTaskbar=false creates
        // a hidden WinForms owner window even when Form.Owner was never set.
        // WS_EX_TOOLWINDOW above avoids a taskbar entry without that mutation.
        AutoScaleMode = AutoScaleMode.None; ShowInTaskbar = true;
        StartPosition = FormStartPosition.Manual; Location = new Point(-12000, -12000);
        ClientSize = new Size((int)(640*scale), (int)(360*scale));
        Font = new Font("Segoe UI", (float)(9*scale));
        Body = new Panel { Name = "psView", AccessibleName = "Business Content", Dock = DockStyle.Fill };
        Commands = new ToolStrip { Name = "tsViewCommand", AccessibleName = "View Commands", Text = "View Commands", Dock = DockStyle.Top };
        Controls.Add(Body); Controls.Add(Commands);
    }
    public void AttachBody(bool wpf) {
        foreach (Control control in new List<Control>(ControlsOf(Body))) control.Dispose();
        Body.Controls.Clear(); Embedded = null; Query = null;
        if (wpf) {
            var panel = new System.Windows.Controls.StackPanel();
            panel.Children.Add(new System.Windows.Controls.TextBlock { Text = "WPF business content" });
            panel.Children.Add(new System.Windows.Controls.Button { Content = "조회" });
            Body.Controls.Add(new ElementHost { Name = "BusinessElementHost", AccessibleName = "WPF Business Host", Dock = DockStyle.Fill, Child = panel });
        } else {
            Embedded = new Form { Name = "DeviceBody", AccessibleName = "Device business form", Text = "Device business form",
                TopLevel = false, FormBorderStyle = FormBorderStyle.None, Dock = DockStyle.Fill, AutoScaleMode = AutoScaleMode.None };
            Query = new Button { Name = "btnSearchData", AccessibleName = "조회", Text = "조회", Location = P(20,50), Size = S(90,30) };
            Query.Click += delegate { BodyClicks++; };
            Embedded.Controls.Add(new Label { Name = "lblFamily", Text = "Family", Location = P(20,15), Size = S(80,25) });
            Embedded.Controls.Add(new ComboBox { Name = "FamilyField", AccessibleName = "Family", Location = P(110,15), Size = S(150,25), Text = "Hybrid" });
            Embedded.Controls.Add(Query); Body.Controls.Add(Embedded); Embedded.Show();
        }
        Phase = "body_attached";
    }
    public void AddCommands(bool enabled, bool reverse) {
        Commands.Items.Clear();
        OuterQuery = new ToolStripButton("조회") { Name = "ObservedQueryCommand", ToolTipText = "Query this work view", Enabled = enabled, DisplayStyle = ToolStripItemDisplayStyle.Text };
        OuterQuery.Click += delegate { ToolbarClicks++; };
        var help = new ToolStripButton("Help") { Name = "ObservedHelpCommand", DisplayStyle = ToolStripItemDisplayStyle.Text };
        if (reverse) { Commands.Items.Insert(0, OuterQuery); Commands.Items.Insert(0, help); }
        else { Commands.Items.Insert(0, help); Commands.Items.Insert(0, OuterQuery); }
        Commands.Visible = true; Phase = enabled ? "ready" : "command_disabled";
    }
    public void Delayed(int delay) {
        Commands.Items.Clear(); Commands.Visible = false;
        foreach (Control control in new List<Control>(ControlsOf(Body))) control.Dispose();
        Body.Controls.Clear(); Embedded = null; Query = null; OuterQuery = null; Phase = "empty";
        After(delay, delegate { AttachBody(false); });
        After(delay*2, delegate { AddCommands(false, false); });
        After(delay*3, delegate { OuterQuery.Enabled = true; Commands.Items.Insert(0, new ToolStripButton("Late command")); Phase = "ready"; });
    }
    void After(int delay, Action action) {
        var timer = new System.Windows.Forms.Timer { Interval = delay }; timers.Add(timer);
        timer.Tick += delegate { timer.Stop(); if (!IsDisposed) action(); timer.Dispose(); }; timer.Start();
    }
    protected override void Dispose(bool disposing) {
        foreach (var timer in timers) timer.Dispose();
        base.Dispose(disposing);
    }
    static IEnumerable<Control> ControlsOf(Control root) { foreach (Control c in root.Controls) yield return c; }
    Point P(int x,int y) { return new Point((int)(x*scale),(int)(y*scale)); }
    Size S(int x,int y) { return new Size((int)(x*scale),(int)(y*scale)); }
}

sealed class MenuAgentFixture : ApplicationContext {
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint flag);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly Form resident = new Form { Name = "ResidentAgent", Text = "Hidden resident agent", ShowInTaskbar = false };
    readonly List<AgentWorkForm> forms = new List<AgentWorkForm>();
    AgentWorkForm first, second;
    int generation;
    double scale = 1;
    long retired;
    public MenuAgentFixture() {
        resident.Handle.ToInt64();
        Emit(new { ready = true, pid = Process.GetCurrentProcess().Id, resident_window_id = resident.Handle.ToInt64() });
        var reader = new Thread(delegate() {
            string line;
            while ((line = Console.ReadLine()) != null) {
                var command = json.Deserialize<Dictionary<string,object>>(line);
                resident.BeginInvoke((Action)delegate {
                    try { Command(command); } catch (Exception error) { Emit(new { error = error.GetType().Name, message = error.Message }); }
                });
            }
        }); reader.IsBackground = true; reader.Start();
        var expiry = new System.Windows.Forms.Timer { Interval = 600000 };
        expiry.Tick += delegate { Quit(); }; expiry.Start();
    }
    void Emit(object value) { Console.WriteLine(json.Serialize(value)); Console.Out.Flush(); }
    AgentWorkForm Create(string prefix, bool reverse) {
        var form = new AgentWorkForm(prefix + " - [smartMMDM, fixture-user-" + generation + "] - FxMenuAgent fixture", scale);
        form.AttachBody(false); form.AddCommands(true, reverse); forms.Add(form); form.Show(); return form;
    }
    void Command(Dictionary<string,object> command) {
        string state = Convert.ToString(command["state"]);
        if (state == "quit") { Quit(); return; }
        if (state == "reset") {
            foreach (var form in forms) { form.Close(); form.Dispose(); }
            forms.Clear(); generation++; retired = 0;
            scale = command.ContainsKey("scale") ? Convert.ToDouble(command["scale"]) : 1;
            if (scale != 1 && scale != 1.25 && scale != 1.5) throw new ArgumentException("scale");
            first = Create("Device Setup Standard (CJTEST)", generation % 2 == 0);
            second = Create("Equipment Setup Standard", generation % 2 != 0);
        } else if (state == "new") {
            Create("Inventory Setup", false);
        } else if (state == "duplicate") {
            Create("Device Setup Standard (CJTEST)", false);
        } else if (state == "reopen") {
            retired = first.Handle.ToInt64(); first.Close(); first.Dispose(); forms.Remove(first); generation++;
            first = Create("Device Setup Standard (CJTEST)", generation % 2 == 0);
        } else if (state == "delayed") {
            first.Delayed(command.ContainsKey("delay_ms") ? Convert.ToInt32(command["delay_ms"]) : 350);
        } else if (state == "body_only") {
            first.AttachBody(false); first.Commands.Items.Clear(); first.Commands.Visible = false;
        } else if (state == "wpf") {
            second.AttachBody(true);
        } else if (state != "inspect") throw new ArgumentException("state");
        // One queued message permits layout to settle; it never activates a window.
        resident.BeginInvoke((Action)delegate { Snapshot(state); });
    }
    object Window(AgentWorkForm form) {
        var buttons = new List<object>();
        foreach (ToolStripItem item in form.Commands.Items) buttons.Add(new { name = item.Name, text = item.Text, enabled = item.Enabled });
        return new { window_id = form.Handle.ToInt64(), title = form.Text, owner_window_id = GetWindow(form.Handle,4).ToInt64(),
            visible = form.Visible, phase = form.Phase, embedded_window_id = form.Embedded == null ? 0 : form.Embedded.Handle.ToInt64(),
            body_window_id = form.Body.Handle.ToInt64(), toolbar_window_id = form.Commands.Handle.ToInt64(),
            body_clicks = form.BodyClicks, toolbar_clicks = form.ToolbarClicks, command_items = buttons.ToArray() };
    }
    void Snapshot(string state) {
        uint dpi = 0; try { dpi = GetDpiForWindow(first.Handle); } catch (EntryPointNotFoundException) { }
        var windows = new List<object>(); foreach (var form in forms) windows.Add(Window(form));
        Emit(new { state = state, generation = generation, pid = Process.GetCurrentProcess().Id,
            resident_window_id = resident.Handle.ToInt64(), resident_visible = resident.Visible,
            first_window_id = first.Handle.ToInt64(), second_window_id = second.Handle.ToInt64(), retired_window_id = retired,
            scale = scale, actual_window_dpi = dpi, scale_kind = "synthetic_content_not_OS_DPI", windows = windows.ToArray(),
            foreground = GetForegroundWindow().ToInt64(), input_path = "fixture_internal_setup_no_CUA_input" });
    }
    void Quit() { foreach (var form in forms) { form.Close(); form.Dispose(); } resident.Dispose(); ExitThread(); }
    [STAThread] static void Main() {
        Console.InputEncoding = new System.Text.UTF8Encoding(false);
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new MenuAgentFixture());
    }
}
