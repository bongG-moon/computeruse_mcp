// Synthetic local fixture only. Commands change its own model; no external UI input.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Design;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation.Peers;
using System.Windows.Forms;
using System.Windows.Forms.Design;
using System.Windows.Forms.Integration;
using Wpf = System.Windows;

sealed class EmptyPeerControl : System.Windows.Controls.UserControl {
    public EmptyPeerControl() {
        var panel = new System.Windows.Controls.StackPanel();
        panel.Children.Add(new System.Windows.Controls.TextBlock { Text = "Synthetic left menu" });
        panel.Children.Add(new System.Windows.Controls.Button { Content = "Hidden peer child" });
        Content = panel;
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new EmptyPeer(this); }
    sealed class EmptyPeer : FrameworkElementAutomationPeer {
        public EmptyPeer(EmptyPeerControl owner) : base(owner) { }
        protected override string GetNameCore() { return "CustomWindowAutomationPeer"; }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Window; }
        protected override List<AutomationPeer> GetChildrenCore() { return new List<AutomationPeer>(); }
    }
}

sealed class DeviceModel {
    public string Device { get; set; }
    [Editor(typeof(GridComboEditor), typeof(UITypeEditor))]
    public string Family { get; set; }
    [ReadOnly(true)] public string State { get { return "N"; } }
}

sealed class GridComboEditor : UITypeEditor {
    public override UITypeEditorEditStyle GetEditStyle(ITypeDescriptorContext context) { return UITypeEditorEditStyle.DropDown; }
    public override object EditValue(ITypeDescriptorContext context, IServiceProvider provider, object value) {
        var service = provider.GetService(typeof(IWindowsFormsEditorService)) as IWindowsFormsEditorService;
        if (service != null) {
            var grid = new DataGridView { AccessibleName = "Property editor choices", Dock = DockStyle.Fill,
                AllowUserToAddRows = false, ReadOnly = true, AutoGenerateColumns = false };
            grid.Columns.Add("Code", "Code"); grid.Columns.Add("Display", "Display");
            grid.Rows.Add("H", "Hybrid"); grid.Rows.Add("S", "Standard");
            service.DropDownControl(grid);
        }
        return value;
    }
}

class MatrixForm : Form, IServiceProvider, IWindowsFormsEditorService {
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct COMBOBOXINFO {
        public int cbSize; public RECT item, button; public uint buttonState;
        public IntPtr hwndCombo, hwndItem, hwndList;
    }
    [DllImport("user32.dll")] static extern bool GetComboBoxInfo(IntPtr hwnd, ref COMBOBOXINFO info);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(Accessibility.IAccessible accessible, out IntPtr hwnd);
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly List<Form> children = new List<Form>();
    ContextMenuStrip menu;
    ToolStripMenuItem export;
    ComboBox family;
    DataGridView grid;
    PropertyGrid properties;
    DeviceModel model;
    string currentState;
    double currentScale;
    int generation;
    long primary;
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get { var value = base.CreateParams; value.ExStyle |= 0x08000000; return value; }
    }
    public MatrixForm() {
        Text = "CUA Accessibility Matrix Fixture"; Name = "AccessibilityMatrix";
        StartPosition = FormStartPosition.Manual; Location = new Point(-12000, -12000);
        AutoScaleMode = AutoScaleMode.None; ShowInTaskbar = false;
        Shown += delegate { Emit(new { ready = true, pid = Process.GetCurrentProcess().Id, window_id = Handle.ToInt64() }); };
    }
    void Emit(object value) { Console.WriteLine(json.Serialize(value)); Console.Out.Flush(); }
    void Reset(double scale) {
        if (menu != null) { menu.Close(); menu.Dispose(); menu = null; }
        foreach (var child in children.ToArray()) if (!child.IsDisposed) { child.Close(); child.Dispose(); }
        children.Clear();
        foreach (Control control in new List<Control>(ControlsAsList())) control.Dispose();
        Controls.Clear();
        ClientSize = new Size((int)(800 * scale), (int)(520 * scale));
        Font = new Font("Segoe UI", (float)(9 * scale));
        model = new DeviceModel { Device = "DEV-000", Family = "Standard" };
        var peer = new EmptyPeerControl();
        peer.LayoutTransform = new System.Windows.Media.ScaleTransform(scale, scale);
        var host = new ElementHost { Name = "SyntheticWpfHost", Location = P(8, 8), Size = S(180, 100), Child = peer };
        Controls.Add(host);
        var label = new Label { Text = "Family", Location = P(205, 12), Size = S(80, 25) };
        Controls.Add(label);
        family = new ComboBox { Name = "FamilyField", AccessibleName = "Family", Location = P(285, 10),
            Size = S(160, 28), DropDownStyle = ComboBoxStyle.DropDownList };
        family.Items.AddRange(new object[] { "Standard", "Hybrid", "N", "P" }); family.SelectedIndex = 0;
        Controls.Add(family);
        grid = new DataGridView { Name = "Results", AccessibleName = "Device results", Location = P(205, 55),
            Size = S(575, 220), AllowUserToAddRows = false, ReadOnly = true, RowHeadersVisible = false,
            SelectionMode = DataGridViewSelectionMode.FullRowSelect, MultiSelect = false };
        grid.Columns.Add("Device", "Device"); grid.Columns.Add("Family", "Family"); grid.Columns.Add("Code", "Code");
        for (int row = 0; row < 30; row++) grid.Rows.Add("DEV-" + row.ToString("000"), row % 2 == 0 ? "Hybrid" : "Standard", "N");
        Controls.Add(grid);
        properties = new PropertyGrid { Name = "PropertyPanel", AccessibleName = "Device properties",
            Location = P(205, 290), Size = S(575, 210), SelectedObject = model, ToolbarVisible = false };
        Controls.Add(properties);
        PerformLayout();
    }
    IEnumerable<Control> ControlsAsList() { foreach (Control item in Controls) yield return item; }
    Point P(int x, int y) { return new Point((int)(x * currentScale), (int)(y * currentScale)); }
    Size S(int w, int h) { return new Size((int)(w * currentScale), (int)(h * currentScale)); }
    sealed class PassivePopup : Form {
        protected override bool ShowWithoutActivation { get { return true; } }
        protected override CreateParams CreateParams { get { var p = base.CreateParams; p.ExStyle |= 0x08000000; return p; } }
    }
    Form MakePopup(string name, Control content, bool dropdown) {
        var form = new PassivePopup { Name = name, Text = dropdown ? "" : name, ShowInTaskbar = false,
            StartPosition = FormStartPosition.Manual, Location = new Point(-11000, -11000), Size = S(340, 190),
            FormBorderStyle = dropdown ? FormBorderStyle.None : FormBorderStyle.FixedDialog };
        form.AccessibleName = name; content.Dock = DockStyle.Fill; form.Controls.Add(content); children.Add(form);
        return form;
    }
    public void Command(Dictionary<string, object> command) {
        string state = Convert.ToString(command["state"]);
        if (state == "quit") { Close(); return; }
        currentScale = command.ContainsKey("scale") ? Convert.ToDouble(command["scale"]) : 1;
        if (currentScale != 1 && currentScale != 1.25 && currentScale != 1.5) throw new ArgumentException("scale");
        currentState = state; generation++; Reset(currentScale); primary = Handle.ToInt64();
        if (state == "B") {
            family.SelectedItem = "N"; family.DroppedDown = true;
            var info = new COMBOBOXINFO(); info.cbSize = Marshal.SizeOf(info);
            if (GetComboBoxInfo(family.Handle, ref info) && info.hwndList != IntPtr.Zero) primary = info.hwndList.ToInt64();
        } else if (state == "C") {
            var list = new ListBox { AccessibleName = "Searchable modal choices" }; list.Items.AddRange(new object[] { "Hybrid", "Standard" });
            var form = MakePopup("Synthetic Searchable modal", list, false);
            form.Shown += delegate { primary = form.Handle.ToInt64(); ScheduleState(); };
            // Own-process deterministic setup only; this is an actual modal
            // nested message loop, not a CUA click or foreign app interaction.
            form.ShowDialog(this); return;
        } else if (state == "D") {
            var entry = properties.SelectedGridItem;
            while (entry != null && entry.Parent != null) entry = entry.Parent;
            if (entry != null) SelectFamily(entry);
            new GridComboEditor().EditValue(null, this, model.Family);
        } else if (state == "E") {
            grid.ClearSelection(); grid.CurrentCell = grid.Rows[17].Cells[1];
            grid.Rows[17].Selected = true; grid.FirstDisplayedScrollingRowIndex = 12;
            model.Device = "DEV-017"; model.Family = "Standard"; properties.Refresh();
        } else if (state == "F") {
            menu = new ContextMenuStrip { AccessibleName = "Recreated result menu" };
            export = new ToolStripMenuItem("Export to excel") { Name = "ExportToExcelContextMenuItem" };
            export.DropDownItems.Add(new ToolStripMenuItem("Active sheet") { Name = "ExportToExcelActiveSheetContextMenuItem" });
            export.DropDownItems.Add(new ToolStripMenuItem("All sheet") { Name = "ExportToExcelAllSheetContextMenuItem" });
            menu.Items.Add(export); menu.Show(this, P(250, 120)); export.ShowDropDown();
            menu.Location = new Point(-10000, -10000); export.DropDown.Location = new Point(-9500, -10000);
            primary = export.DropDown.Handle.ToInt64();
        } else if (state != "A") throw new ArgumentException("state");
        ScheduleState();
    }
    void SelectFamily(GridItem item) {
        if (item.Label == "Family") { properties.SelectedGridItem = item; return; }
        foreach (GridItem child in item.GridItems) SelectFamily(child);
    }
    void ScheduleState() {
        var timer = new System.Windows.Forms.Timer { Interval = 80 };
        timer.Tick += delegate {
            timer.Stop(); timer.Dispose();
            // ToolStrip can recreate its HWND while processing ShowDropDown.
            // The receipt always reports the live post-layout handle.
            if (currentState == "F" && export != null && !export.IsDisposed) primary = export.DropDown.Handle.ToInt64();
            uint dpi = 0; try { dpi = GetDpiForWindow(Handle); } catch (EntryPointNotFoundException) { }
            Emit(new { state = currentState, generation = generation, scale = currentScale,
                scale_kind = "synthetic_content_scale_not_OS_DPI", actual_window_dpi = dpi,
                pid = Process.GetCurrentProcess().Id, window_id = primary, main_window_id = Handle.ToInt64(),
                owner_window_id = GetWindow(new IntPtr(primary), 4).ToInt64(),
                family = family.Text, selected_device = model.Device, property_family = model.Family,
                modal = children.Count > 0 && children[0].Modal, foreground_requested = false,
                menu_visible = menu != null && menu.Visible,
                menu_handle = menu != null ? menu.Handle.ToInt64() : 0,
                submenu_visible = export != null && !export.IsDisposed && export.DropDown.Visible,
                submenu_children = MenuChildren(),
                input_path = "fixture_internal_state_transition_no_CUA_input" });
        }; timer.Start();
    }
    object[] MenuChildren() {
        var rows = new List<object>();
        if (menu == null || export == null || export.IsDisposed) return rows.ToArray();
        var root = export.DropDown.AccessibilityObject;
        for (int index = 0; index < root.GetChildCount(); index++) {
            var child = root.GetChild(index); if (child == null) continue;
            IntPtr handle; int hr = WindowFromAccessibleObject((Accessibility.IAccessible)child, out handle);
            var bounds = child.Bounds;
            uint pid; GetWindowThreadProcessId(handle, out pid);
            var accessibleParent = child.Parent; IntPtr parentHandle = IntPtr.Zero;
            if (accessibleParent != null) WindowFromAccessibleObject((Accessibility.IAccessible)accessibleParent, out parentHandle);
            rows.Add(new { name = child.Name, hwnd = handle.ToInt64(), hresult = hr, is_window = IsWindow(handle), pid = pid,
                parent = GetParent(handle).ToInt64(), owner = GetWindow(handle, 4).ToInt64(),
                role = Convert.ToInt32(child.Role), root_role = Convert.ToInt32(root.Role),
                root_parent_name = root.Parent == null ? null : root.Parent.Name,
                root_parent_same_as_child_parent = Object.ReferenceEquals(root.Parent, accessibleParent),
                accessible_parent_name = accessibleParent == null ? null : accessibleParent.Name,
                accessible_parent_hwnd = parentHandle.ToInt64(), parent_reference_is_root = Object.ReferenceEquals(accessibleParent, root),
                bounds = new { x = bounds.X, y = bounds.Y, width = bounds.Width, height = bounds.Height } });
        }
        return rows.ToArray();
    }
    public object GetService(Type type) { return type == typeof(IWindowsFormsEditorService) ? this : null; }
    public void DropDownControl(Control control) {
        var popup = MakePopup("Synthetic dynamic property editor", control, true);
        popup.Show(this); primary = popup.Handle.ToInt64();
    }
    public void CloseDropDown() { foreach (var child in children.ToArray()) child.Close(); }
    public DialogResult ShowDialog(Form dialog) { return dialog.ShowDialog(this); }
    [STAThread] static void Main() {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        var form = new MatrixForm();
        form.Handle.ToInt64();
        var reader = new Thread(delegate() {
            string line;
            while ((line = Console.ReadLine()) != null) {
                var request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
                form.BeginInvoke((Action)delegate { try { form.Command(request); } catch (Exception e) { form.Emit(new { error = e.GetType().Name, message = e.Message }); } });
            }
        }); reader.IsBackground = true; reader.Start();
        var expiry = new System.Windows.Forms.Timer { Interval = 600000 };
        expiry.Tick += delegate { form.Close(); }; expiry.Start();
        Application.Run(form);
    }
}
