// Synthetic local acceptance fixture. This is not an implementation or a
// faithful reproduction of a corporate application or a vendor's controls.
// No network, installation, elevated privileges, or input into other apps.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyVersion("0.18.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("0.18.0.0")]

internal sealed class ComplexBusiness018State {
    internal static readonly string[] Fields = { "Family", "Gen", "Org", "Device", "Den", "Tech Name", "Version",
        "No Of Die", "No Of Ball", "Prod Type", "MCP", "S/C", "Device Grp", "New Gbn" };
    internal static readonly string[] Defaults = { "W", "G1", "ORG-A", "DEV-A", "8G", "T1", "1", "8", "128", "STD", "N", "S", "GRP-A", "Old" };
    internal readonly Dictionary<string, string> Inputs = new Dictionary<string, string>();
    internal readonly Dictionary<string, string> Archive = new Dictionary<string, string>();
    internal readonly List<Dictionary<string, string>> Rows = new List<Dictionary<string, string>>();
    internal int QueryCount, ArchiveQueryCount, LayoutVariant;
    internal bool IncludeInactive, IncludeHeld, RecentOnly;

    internal ComplexBusiness018State() { ResetMain(); ResetArchive(); }
    internal void ResetMain() {
        for (int i = 0; i < Fields.Length; i++) Inputs[Fields[i]] = Defaults[i];
        IncludeInactive = IncludeHeld = RecentOnly = false;
        Rows.Clear();
    }
    internal void ResetArchive() { Archive["Family"] = "P"; Archive["Device"] = "ARCHIVE"; }
    internal bool SetInputValue(string key, bool archive, string value) {
        var values = archive ? Archive : Inputs;
        if (value == null || values[key] == value) return false;
        values[key] = value;
        return true;
    }
    internal void Query(bool archive) {
        if (archive) { ArchiveQueryCount++; return; }
        QueryCount++; Rows.Clear();
        for (int i = 1; i <= 5; i++) Rows.Add(new Dictionary<string, string> {
            { "Family", Inputs["Family"] }, { "Item", Inputs["Device"] + "-" + Inputs["Version"] + "-" + i },
            { "State", (IncludeInactive ? "All" : "Active") + (IncludeHeld ? "+Hold" : "") + (RecentOnly ? "+Recent" : "") }
        });
    }
    internal static int[] Order(int variant) {
        var result = new int[Fields.Length];
        for (int i = 0; i < result.Length; i++) result[i] = variant == 0 ? i : (i + 5) % result.Length;
        return result;
    }
    internal Dictionary<string, object> Snapshot() {
        return new Dictionary<string, object> {
            { "inputs", new Dictionary<string, string>(Inputs) }, { "archive_inputs", new Dictionary<string, string>(Archive) },
            { "query_count", QueryCount }, { "archive_query_count", ArchiveQueryCount }, { "layout_variant", LayoutVariant },
            { "checks", new Dictionary<string, bool> { { "include_inactive", IncludeInactive }, { "include_held", IncludeHeld }, { "recent_only", RecentOnly } } },
            { "rows", Rows.ToArray() }
        };
    }
}

internal sealed class ComplexBusiness018 : Form {
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    private readonly ComplexBusiness018State state = new ComplexBusiness018State();
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly string auditPath, instanceId = Guid.NewGuid().ToString("N");
    private readonly Dictionary<string, Control> fields = new Dictionary<string, Control>();
    private readonly Dictionary<string, Control> archiveFields = new Dictionary<string, Control>();
    private readonly List<Panel> fieldPanels = new List<Panel>();
    private readonly GroupBox conditions = new GroupBox(), archive = new GroupBox();
    private readonly DataGridView grid = new DataGridView();
    private readonly Label status = new Label(), archiveStatus = new Label(), auditStatus = new Label();
    private readonly CheckBox inactive = new CheckBox(), held = new CheckBox(), recent = new CheckBox();
    private readonly Random identifiers = new Random();
    private bool changing;
    private int sequence;

    private string NextId() { return identifiers.Next(100000, 999999999).ToString(); }
    private void Audit(string kind, string field = null) {
        if (changing) return;
        var record = state.Snapshot();
        record["synthetic_fixture"] = true; record["instance_id"] = instanceId;
        record["sequence"] = ++sequence; record["event"] = kind;
        record["field"] = field; record["utc"] = DateTime.UtcNow.ToString("o");
        record["pid"] = Process.GetCurrentProcess().Id;
        record["window_id"] = IsHandleCreated ? Handle.ToInt64() : 0;
        try {
            // One complete snapshot per actual application event. The audit is
            // independent of MCP receipts and never derives success from them.
            using (var output = new StreamWriter(auditPath, true, new UTF8Encoding(false))) {
                output.WriteLine(json.Serialize(record)); output.Flush();
            }
            auditStatus.ForeColor = Color.FromArgb(74, 93, 116);
            auditStatus.Text = "로컬 기록 " + sequence + "건 · 네트워크 사용 없음 · 실제 업무 데이터 없음";
        } catch (Exception error) {
            auditStatus.ForeColor = Color.Firebrick;
            auditStatus.Text = "시험 기록 저장 실패: " + error.GetType().Name + " — 이 실행을 검증 성공으로 판정하지 마세요.";
        }
    }

    private static Label LabelAt(string text, int x, int y, int width) {
        return new Label { Text = text, Left = x, Top = y, Width = width, Height = 28, AutoSize = false };
    }
    private Button AddButton(Control parent, string text, int x, int y, int width, EventHandler action) {
        var result = new Button { Text = text, AccessibleName = text, Name = NextId(),
            Left = x, Top = y, Width = width, Height = 34, UseVisualStyleBackColor = true };
        result.Click += action; parent.Controls.Add(result); return result;
    }
    private string[] Options(string name) {
        switch (name) {
            case "Family": return new[] { "W", "N", "P", "C" };
            case "Gen": return new[] { "G1", "G2", "G3" };
            case "Org": return new[] { "ORG-A", "ORG-B", "ORG-C" };
            case "Den": return new[] { "8G", "16G", "32G" };
            case "Tech Name": return new[] { "T1", "T2", "T3" };
            case "Prod Type": return new[] { "STD", "TEST", "SPECIAL" };
            case "MCP": return new[] { "N", "Y" };
            case "S/C": return new[] { "S", "C" };
            case "Device Grp": return new[] { "GRP-A", "GRP-B", "GRP-C" };
            case "New Gbn": return new[] { "Old", "New" };
            default: return null;
        }
    }
    private Control Editor(string key, bool isArchive) {
        string[] options = Options(key);
        Control input;
        if (options == null) input = new TextBox { MaxLength = 80 };
        else {
            var combo = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, IntegralHeight = true, MaxDropDownItems = 6 };
            combo.Items.AddRange(options); input = combo;
            combo.DropDown += delegate { Audit("dropdown_open", (isArchive ? "archive:" : "main:") + key); };
        }
        // Deliberately identical accessible names; the visible label is not
        // copied to the input's UIA name or AutomationId. IDs change each run.
        input.Name = NextId(); input.AccessibleName = "[Editor] Edit Area";
        input.SetBounds(103, 2, 151, 28);
        input.Text = (isArchive ? state.Archive : state.Inputs)[key];
        var selection = input as ComboBox;
        if (selection != null) {
            // DropDownList can change its displayed selection without raising
            // the base Control.TextChanged event. Read the real selected item
            // on both selection events, before writing the independent audit.
            selection.SelectedIndexChanged += delegate {
                SyncInputValue(key, isArchive, selection.SelectedItem as string);
            };
            selection.SelectionChangeCommitted += delegate {
                SyncInputValue(key, isArchive, selection.SelectedItem as string);
                Audit("selection_committed", (isArchive ? "archive:" : "main:") + key);
            };
        } else {
            input.TextChanged += delegate { SyncInputValue(key, isArchive, input.Text); };
        }
        (isArchive ? archiveFields : fields)[key] = input;
        return input;
    }
    private void SyncInputValue(string key, bool isArchive, string value) {
        // A second native event for the same selection is not another value
        // change. SelectionChangeCommitted itself is still recorded above.
        if (state.SetInputValue(key, isArchive, value))
            Audit("value_changed", (isArchive ? "archive:" : "main:") + key);
    }
    private void ArrangeConditions() {
        conditions.SuspendLayout();
        int[] order = ComplexBusiness018State.Order(state.LayoutVariant);
        for (int slot = 0; slot < order.Length; slot++) {
            var panel = fieldPanels[order[slot]];
            panel.SetBounds(16 + (slot % 4) * 268, 33 + (slot / 4) * 45, 258, 34);
            panel.TabIndex = slot;
            fields[ComplexBusiness018State.Fields[order[slot]]].Name = NextId();
        }
        conditions.ResumeLayout();
    }
    private void RefreshMain() {
        changing = true;
        foreach (var pair in fields) pair.Value.Text = state.Inputs[pair.Key];
        inactive.Checked = state.IncludeInactive; held.Checked = state.IncludeHeld; recent.Checked = state.RecentOnly;
        grid.Rows.Clear();
        foreach (var row in state.Rows) grid.Rows.Add(row["Family"], row["Item"], row["State"]);
        changing = false;
        status.Text = "조회 " + state.QueryCount + "회 · 결과 " + state.Rows.Count + "행 · Family=" + state.Inputs["Family"] +
            " · Device=" + state.Inputs["Device"] + " · Version=" + state.Inputs["Version"];
    }
    private void RefreshArchive() {
        changing = true;
        foreach (var pair in archiveFields) pair.Value.Text = state.Archive[pair.Key];
        changing = false;
        archiveStatus.Text = "보관 조회 " + state.ArchiveQueryCount + "회\r\nFamily=" + state.Archive["Family"] + "\r\nDevice=" + state.Archive["Device"];
    }
    private ComplexBusiness018(string audit, int variant) {
        auditPath = audit; state.LayoutVariant = variant;
        Text = "Computer Use — 복잡한 업무 화면 시험 0.18"; Name = "ComplexBusiness018"; AccessibleName = Text;
        AutoScaleMode = AutoScaleMode.None; StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(1140, 800); MinimumSize = new Size(1156, 839);
        Font = new Font("Malgun Gothic", 15, FontStyle.Regular, GraphicsUnit.Pixel);
        BackColor = Color.FromArgb(244, 247, 251);
        var heading = LabelAt("복잡한 조건 · 중복 버튼 · 기본 드롭다운 시험", 22, 18, 920);
        heading.Height = 40; heading.Font = new Font(Font.FontFamily, 24, FontStyle.Bold, GraphicsUnit.Pixel); Controls.Add(heading);
        Controls.Add(LabelAt("합성 시험 화면입니다. 특정 사내 앱 또는 상용 컨트롤의 정밀 재현을 뜻하지 않습니다.", 24, 65, 1080));

        conditions.Text = "조회 조건"; conditions.AccessibleName = conditions.Text; conditions.Name = NextId();
        conditions.SetBounds(20, 103, 1100, 292); Controls.Add(conditions);
        foreach (string key in ComplexBusiness018State.Fields) {
            var panel = new Panel { Name = NextId() };
            // The label and editor remain separate siblings inside an unnamed
            // layout panel. Geometry/visual text is needed to associate them.
            panel.Controls.Add(LabelAt(key, 0, 5, 101)); panel.Controls.Add(Editor(key, false));
            conditions.Controls.Add(panel); fieldPanels.Add(panel);
        }
        ArrangeConditions();
        inactive.Text = "비활성 포함"; held.Text = "Hold 포함"; recent.Text = "최근 변경만";
        CheckBox[] boxes = { inactive, held, recent };
        for (int i = 0; i < boxes.Length; i++) {
            boxes[i].Name = NextId(); boxes[i].AccessibleName = boxes[i].Text;
            boxes[i].SetBounds(18 + i * 170, 238, 160, 32); conditions.Controls.Add(boxes[i]);
        }
        inactive.CheckedChanged += delegate { state.IncludeInactive = inactive.Checked; Audit("check_changed", "include_inactive"); };
        held.CheckedChanged += delegate { state.IncludeHeld = held.Checked; Audit("check_changed", "include_held"); };
        recent.CheckedChanged += delegate { state.RecentOnly = recent.Checked; Audit("check_changed", "recent_only"); };
        AddButton(conditions, "초기화", 836, 237, 114, delegate { state.ResetMain(); RefreshMain(); Audit("main_reset"); });
        AddButton(conditions, "조회", 964, 237, 114, delegate { state.Query(false); RefreshMain(); Audit("main_query"); });

        archive.Text = "보관 조건"; archive.AccessibleName = archive.Text; archive.Name = NextId();
        archive.SetBounds(20, 410, 312, 300); Controls.Add(archive);
        int archiveIndex = 0;
        foreach (string key in new[] { "Family", "Device" }) {
            var panel = new Panel { Name = NextId(), Left = 18, Top = 38 + archiveIndex++ * 48, Width = 275, Height = 34 };
            panel.Controls.Add(LabelAt(key, 0, 5, 101)); panel.Controls.Add(Editor(key, true)); archive.Controls.Add(panel);
        }
        AddButton(archive, "초기화", 18, 145, 126, delegate { state.ResetArchive(); RefreshArchive(); Audit("archive_reset"); });
        AddButton(archive, "조회", 158, 145, 126, delegate { state.Query(true); RefreshArchive(); Audit("archive_query"); });
        archiveStatus.AccessibleName = "보관 결과 상태"; archiveStatus.SetBounds(18, 195, 275, 88); archive.Controls.Add(archiveStatus);

        grid.Name = NextId(); grid.AccessibleName = "조회 결과"; grid.SetBounds(348, 418, 772, 292);
        grid.ReadOnly = true; grid.AllowUserToAddRows = false; grid.AllowUserToDeleteRows = false;
        grid.RowHeadersVisible = false; grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;
        grid.SelectionMode = DataGridViewSelectionMode.FullRowSelect; grid.MultiSelect = false;
        grid.RowTemplate.Height = 36; grid.ColumnHeadersHeight = 36; grid.ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.DisableResizing;
        grid.Columns.Add("Family", "Family"); grid.Columns.Add("Item", "Item"); grid.Columns.Add("State", "State");
        grid.AlternatingRowsDefaultCellStyle.BackColor = Color.FromArgb(237, 242, 249); Controls.Add(grid);
        status.AccessibleName = "결과 상태"; status.SetBounds(22, 724, 800, 28); Controls.Add(status);
        AddButton(this, "조건 순서 바꾸기", 890, 722, 230, delegate {
            state.LayoutVariant = 1 - state.LayoutVariant; ArrangeConditions(); Audit("layout_changed");
        });
        auditStatus.SetBounds(22, 763, 1098, 28); Controls.Add(auditStatus);
        RefreshMain(); RefreshArchive();
        Shown += delegate { Audit("opened"); };
        FormClosed += delegate { Audit("closed"); };
    }

    private static int SelfTest(string path) {
        var model = new ComplexBusiness018State();
        if (model.Inputs.Count != 14 || model.Rows.Count != 0) throw new InvalidOperationException("initial_state");
        if (!model.SetInputValue("Family", false, "N") || model.Inputs["Family"] != "N"
                || model.SetInputValue("Family", false, "N") || model.SetInputValue("Family", false, null))
            throw new InvalidOperationException("selection_value_sync");
        if (!model.SetInputValue("Family", true, "C") || model.Archive["Family"] != "C"
                || model.Inputs["Family"] != "N") throw new InvalidOperationException("selection_group_isolation");
        var selectedSnapshot = (Dictionary<string, string>)model.Snapshot()["inputs"];
        model.SetInputValue("Family", false, "P");
        if (selectedSnapshot["Family"] != "N" || model.Inputs["Family"] != "P")
            throw new InvalidOperationException("selection_snapshot_independence");
        model.Inputs["Family"] = "C"; model.Inputs["Device"] = "TEST-A"; model.Inputs["Version"] = "3";
        model.IncludeHeld = true; model.Query(false);
        if (model.QueryCount != 1 || model.Rows.Count != 5 || model.Rows[0]["Item"] != "TEST-A-3-1"
                || model.Rows[4]["Family"] != "C" || model.Rows[4]["State"] != "Active+Hold") throw new InvalidOperationException("query_state");
        model.Query(true);
        if (model.ArchiveQueryCount != 1 || model.QueryCount != 1 || model.Rows.Count != 5) throw new InvalidOperationException("group_isolation");
        model.ResetArchive();
        if (model.Inputs["Family"] != "C") throw new InvalidOperationException("archive_reset_isolation");
        model.ResetMain();
        if (model.Rows.Count != 0 || model.Inputs["Family"] != "W" || model.IncludeHeld || model.QueryCount != 1) throw new InvalidOperationException("main_reset");
        var ids = new HashSet<int>(ComplexBusiness018State.Order(1));
        if (ids.Count != 14 || ComplexBusiness018State.Order(1)[0] == ComplexBusiness018State.Order(0)[0]) throw new InvalidOperationException("layout_permutation");
        File.WriteAllText(path, new JavaScriptSerializer().Serialize(new { ok = true, tests = new[] {
            "14_conditions", "selection_value_sync", "selection_group_isolation", "selection_snapshot_independence",
            "5_by_3_query", "independent_group_queries", "group_reset_isolation", "main_reset", "layout_permutation" },
            gui_launched = false, gui_event_binding_tested = false, network_used = false }), new UTF8Encoding(false));
        return 0;
    }

    [STAThread] private static int Main(string[] args) {
        bool selfTest = args.Length == 2 && args[0] == "--self-test";
        try {
            if (selfTest) return SelfTest(Path.GetFullPath(args[1]));
            string audit = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "complex-business018-audit.jsonl"); int variant = 0;
            for (int i = 0; i < args.Length; i++) {
                if (args[i] == "--audit" && i + 1 < args.Length) audit = args[++i];
                else if (args[i] == "--layout-variant" && i + 1 < args.Length && Int32.TryParse(args[++i], out variant) && (variant == 0 || variant == 1)) { }
                else throw new ArgumentException("Use --audit <local.jsonl> [--layout-variant 0|1]");
            }
            audit = Path.GetFullPath(audit);
            if (audit.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(Path.GetDirectoryName(audit)))
                throw new ArgumentException("The audit parent must be an existing local directory.");
            // Manifest is authoritative. This covers a direct unmanaged host on
            // Windows versions that support the API without changing the OS.
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ComplexBusiness018(audit, variant));
            return 0;
        } catch (Exception error) {
            if (selfTest) return 1; // Model checks must never create an error dialog.
            MessageBox.Show(error.Message, "Complex Business 018 fixture", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
