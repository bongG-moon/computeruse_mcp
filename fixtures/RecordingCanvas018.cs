// Local synthetic acceptance fixture: one painted surface, no UIA child
// inputs/buttons, network, external input injection, or elevated privileges.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyVersion("0.18.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("0.18.0.0")]

internal sealed class RecordingCanvas018State {
    internal string Family = "전체", Device = "전체", Search = "";
    internal int QueryCount, ResetCount;
    internal readonly List<Dictionary<string, object>> Rows = new List<Dictionary<string, object>>();
    internal static string NormalizePaste(string text, int maximum) {
        if (String.IsNullOrEmpty(text) || maximum <= 0) return "";
        var output = new StringBuilder();
        for (int index = 0; index < text.Length && output.Length < maximum; index++) {
            char value = text[index];
            if (value == '\r' || value == '\n' || value == '\t' || value == '\u2028' || value == '\u2029') {
                if (value == '\r' && index + 1 < text.Length && text[index + 1] == '\n') index++;
                output.Append(' ');
            } else if (Char.IsHighSurrogate(value)) {
                if (index + 1 < text.Length && Char.IsLowSurrogate(text[index + 1])) {
                    if (output.Length + 2 > maximum) break;
                    output.Append(value); output.Append(text[++index]);
                }
            } else if (!Char.IsControl(value) && !Char.IsLowSurrogate(value)) output.Append(value);
        }
        return output.ToString();
    }
    internal bool Paste(string text, bool replaceAll) {
        string before = replaceAll ? "" : Search;
        string value = NormalizePaste(text, 80 - before.Length);
        if (value.Length == 0) return false;
        Search = before + value; return true;
    }
    internal void Reset() { Family = Device = "전체"; Search = ""; Rows.Clear(); ResetCount++; }
    internal void Query() {
        QueryCount++; Rows.Clear();
        for (int i = 1; i <= 5; i++) Rows.Add(new Dictionary<string, object> {
            { "number", i }, { "family", Family }, { "device", Device },
            { "name", (Search.Length == 0 ? "항목" : Search) + " " + i.ToString("00") }, { "status", "조회 완료" }
        });
    }
    internal Dictionary<string, object> Snapshot() {
        return new Dictionary<string, object> {
            { "fields", new Dictionary<string, string> { { "family", Family }, { "device", Device }, { "search", Search } } },
            { "query_count", QueryCount }, { "reset_count", ResetCount }, { "rows", Rows.ToArray() }
        };
    }
}

internal sealed class RecordingCanvas018 : Form {
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    private readonly RecordingCanvas018State state = new RecordingCanvas018State();
    private readonly string auditPath, instanceId = Guid.NewGuid().ToString("N");
    private readonly Canvas surface;
    private int sequence;
    private string auditError = "";

    private void Audit(string kind, string field) {
        var entry = state.Snapshot();
        entry["synthetic_fixture"] = true; entry["source"] = "native_canvas_application_event";
        entry["instance_id"] = instanceId; entry["sequence"] = ++sequence;
        entry["event"] = kind; entry["field"] = field; entry["utc"] = DateTime.UtcNow.ToString("o");
        entry["pid"] = Process.GetCurrentProcess().Id; entry["window_id"] = Handle.ToInt64();
        try {
            using (var file = new StreamWriter(auditPath, true, new UTF8Encoding(false))) {
                file.WriteLine(new JavaScriptSerializer().Serialize(entry)); file.Flush();
            }
            auditError = "";
        } catch (Exception error) { auditError = "로컬 기록 저장 실패: " + error.GetType().Name; }
        surface.Invalidate();
    }

    private RecordingCanvas018(string audit) {
        auditPath = audit;
        Text = "Recording Canvas 018 — Windows image-only fixture";
        Name = "RecordingCanvas018Window"; AccessibleName = Text;
        AutoScaleMode = AutoScaleMode.None; StartPosition = FormStartPosition.Manual;
        Rectangle work = Screen.PrimaryScreen.WorkingArea;
        ClientSize = new Size(Math.Min(2840, work.Width - 32), Math.Min(1640, work.Height - 96));
        Location = new Point(work.Left + 12, work.Top + 12); MinimumSize = new Size(1000, 650);
        surface = new Canvas(this); surface.Dock = DockStyle.Fill; Controls.Add(surface);
        Shown += delegate { surface.Focus(); Audit("ready", null); };
        FormClosing += delegate { Audit("closing", null); };
    }

    private sealed class Canvas : Control {
        private readonly RecordingCanvas018 owner;
        private readonly RectangleF family = new RectangleF(60, 205, 360, 56), device = new RectangleF(470, 205, 360, 56), search = new RectangleF(880, 205, 480, 56);
        private readonly RectangleF query = new RectangleF(1150, 310, 210, 56), reset = new RectangleF(910, 310, 210, 56);
        private readonly string[] families = { "전체", "W", "N", "P", "C" }, devices = { "전체", "D100", "D200", "D300" };
        private string expanded, focused;
        private bool selectAll;
        private float ScaleFactor { get { return Math.Max(.1f, Math.Min(ClientSize.Width / 1420f, ClientSize.Height / 820f)); } }
        internal Canvas(RecordingCanvas018 form) {
            owner = form; TabStop = true; Name = "OpaqueCanvas"; AccessibleName = "단일 시험 Canvas";
            AccessibleRole = AccessibleRole.Pane;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.Selectable | ControlStyles.ResizeRedraw, true);
        }
        protected override AccessibleObject CreateAccessibilityInstance() { return new SurfaceAccessibility(this); }
        private sealed class SurfaceAccessibility : ControlAccessibleObject {
            internal SurfaceAccessibility(Control value) : base(value) { }
            public override int GetChildCount() { return 0; }
            public override AccessibleObject GetChild(int index) { return null; }
            public override AccessibleRole Role { get { return AccessibleRole.Pane; } }
            public override string Name { get { return "단일 시험 Canvas"; } set { } }
            public override string Value { get { return ""; } set { } }
        }
        private static void PaintText(Graphics graphics, string text, float x, float y, float size, Color color, bool bold) {
            using (var font = new Font("Malgun Gothic", size, bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Pixel))
            using (var brush = new SolidBrush(color)) graphics.DrawString(text, font, brush, x, y);
        }
        private static void Fill(Graphics graphics, RectangleF box, Color color) { using (var brush = new SolidBrush(color)) graphics.FillRectangle(brush, box); }
        private void Field(Graphics graphics, RectangleF box, string label, string value, bool input) {
            Color ink = Color.FromArgb(28, 49, 77);
            PaintText(graphics, label, box.X, box.Y - 34, 20, ink, true);
            Fill(graphics, box, Color.White);
            using (var pen = new Pen(focused == label ? Color.FromArgb(47, 103, 212) : Color.FromArgb(178, 193, 213), 2))
                graphics.DrawRectangle(pen, box.X, box.Y, box.Width, box.Height);
            if (input && focused == "Search" && selectAll && value.Length > 0) {
                using (var font = new Font("Malgun Gothic", 22, FontStyle.Regular, GraphicsUnit.Pixel)) {
                    float width = Math.Min(box.Width - 32, graphics.MeasureString(value, font).Width);
                    Fill(graphics, new RectangleF(box.X + 14, box.Y + 11, width, 34), Color.FromArgb(211, 228, 255));
                }
            }
            var clip = graphics.Save(); graphics.SetClip(new RectangleF(box.X + 12, box.Y + 4, box.Width - 42, box.Height - 8));
            PaintText(graphics, value, box.X + 16, box.Y + 13, 22, ink, false); graphics.Restore(clip);
            if (!input) PaintText(graphics, "▼", box.Right - 36, box.Y + 16, 17, ink, false);
        }
        private void Button(Graphics graphics, RectangleF box, string caption, bool primary) {
            Fill(graphics, box, primary ? Color.FromArgb(45, 104, 216) : Color.White);
            using (var pen = new Pen(Color.FromArgb(162, 183, 210), 1)) graphics.DrawRectangle(pen, box.X, box.Y, box.Width, box.Height);
            PaintText(graphics, caption, box.X + 70, box.Y + 14, 22, primary ? Color.White : Color.FromArgb(28, 49, 77), true);
        }
        protected override void OnPaint(PaintEventArgs args) {
            base.OnPaint(args); Graphics g = args.Graphics; g.Clear(Color.FromArgb(241, 245, 250)); g.ScaleTransform(ScaleFactor, ScaleFactor);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            PaintText(g, "Windows Canvas 녹화 시험", 60, 30, 32, Color.FromArgb(22, 46, 78), true);
            PaintText(g, "합성 데이터만 사용합니다 · 모든 입력칸과 버튼은 그림이며 UIA 자식 요소를 제공하지 않습니다", 60, 86, 17, Color.FromArgb(89, 107, 129), false);
            Fill(g, new RectangleF(32, 142, 1356, 254), Color.FromArgb(232, 239, 249));
            Field(g, family, "Family", owner.state.Family, false); Field(g, device, "Device", owner.state.Device, false);
            Field(g, search, "Search", owner.state.Search, true); Button(g, reset, "초기화", false); Button(g, query, "조회", true);
            PaintText(g, "조회 결과", 60, 420, 24, Color.FromArgb(22, 46, 78), true);
            PaintText(g, "실제 조회 " + owner.state.QueryCount + "회  ·  결과 " + owner.state.Rows.Count + "행", 1060, 423, 18, Color.FromArgb(55, 93, 125), true);
            string[] headers = { "순서", "Family", "Device", "결과 이름", "상태" };
            int[] columns = { 78, 200, 420, 650, 1100 };
            Fill(g, new RectangleF(60, 466, 1300, 48), Color.FromArgb(212, 224, 241));
            for (int i = 0; i < headers.Length; i++) PaintText(g, headers[i], columns[i], 477, 19, Color.FromArgb(22, 46, 78), true);
            for (int row = 0; row < 5; row++) {
                float y = 514 + row * 46; Fill(g, new RectangleF(60, y, 1300, 46), row % 2 == 0 ? Color.White : Color.FromArgb(233, 240, 249));
                if (row >= owner.state.Rows.Count) continue;
                var data = owner.state.Rows[row]; string[] keys = { "number", "family", "device", "name", "status" };
                for (int col = 0; col < keys.Length; col++) PaintText(g, Convert.ToString(data[keys[col]]), columns[col], y + 10, 18, Color.FromArgb(28, 49, 77), false);
            }
            PaintText(g, owner.auditError.Length == 0 ? "로컬 이벤트 " + owner.sequence + "건 · 외부 연결 없음 · 초기화해도 조회 횟수 기록은 유지합니다" : owner.auditError,
                 60, 768, 16, owner.auditError.Length == 0 ? Color.FromArgb(89, 107, 129) : Color.Firebrick, false);
            if (expanded != null) {
                RectangleF box = expanded == "Family" ? family : device; string[] values = expanded == "Family" ? families : devices;
                Fill(g, new RectangleF(box.X + 6, box.Bottom + 7, box.Width, values.Length * 48), Color.FromArgb(193, 202, 217));
                for (int index = 0; index < values.Length; index++) {
                    var item = new RectangleF(box.X, box.Bottom + index * 48, box.Width, 48); Fill(g, item, Color.White);
                    using (var pen = new Pen(Color.FromArgb(178, 193, 213), 1)) g.DrawRectangle(pen, item.X, item.Y, item.Width, item.Height);
                    PaintText(g, values[index], item.X + 16, item.Y + 11, 21, Color.FromArgb(28, 49, 77), false);
                }
            }
        }
        protected override void OnMouseDown(MouseEventArgs args) {
            base.OnMouseDown(args); if (args.Button != MouseButtons.Left) return; Focus();
            PointF point = new PointF(args.X / ScaleFactor, args.Y / ScaleFactor);
            if (expanded != null) {
                RectangleF box = expanded == "Family" ? family : device; string[] values = expanded == "Family" ? families : devices;
                for (int index = 0; index < values.Length; index++) if (new RectangleF(box.X, box.Bottom + index * 48, box.Width, 48).Contains(point)) {
                    string field = expanded; if (field == "Family") owner.state.Family = values[index]; else owner.state.Device = values[index];
                    expanded = null; focused = field; selectAll = false; owner.Audit("dropdown_commit", field); return;
                }
                expanded = null;
            }
            if (family.Contains(point) || device.Contains(point)) {
                focused = family.Contains(point) ? "Family" : "Device"; expanded = focused; selectAll = false; owner.Audit("dropdown_open", focused);
            } else if (search.Contains(point)) { focused = "Search"; selectAll = false; owner.Audit("field_focus", focused); }
            else if (query.Contains(point)) { focused = null; selectAll = false; owner.state.Query(); owner.Audit("query", null); }
            else if (reset.Contains(point)) { focused = null; selectAll = false; owner.state.Reset(); owner.Audit("reset", null); }
            else { focused = null; selectAll = false; Invalidate(); }
        }
        protected override void OnKeyDown(KeyEventArgs args) {
            base.OnKeyDown(args);
            if (args.KeyCode == Keys.Escape) { expanded = null; selectAll = false; Invalidate(); args.SuppressKeyPress = true; return; }
            if (focused != "Search") return;
            if (args.Control && args.KeyCode == Keys.A) { selectAll = true; args.SuppressKeyPress = true; Invalidate(); return; }
            if (!args.Alt && (args.Control && args.KeyCode == Keys.V || args.Shift && !args.Control && args.KeyCode == Keys.Insert)) {
                args.SuppressKeyPress = true; PasteFocusedSearch(); return;
            }
            if (args.KeyCode == Keys.Back || args.KeyCode == Keys.Delete) {
                if (selectAll) owner.state.Search = "";
                else if (args.KeyCode == Keys.Back && owner.state.Search.Length > 0) owner.state.Search = owner.state.Search.Substring(0, owner.state.Search.Length - 1);
                selectAll = false; args.SuppressKeyPress = true; owner.Audit("text_change", "Search");
            } else if (args.KeyCode == Keys.Enter) { args.SuppressKeyPress = true; owner.Audit("text_commit", "Search"); }
        }
        protected override void OnKeyPress(KeyPressEventArgs args) {
            base.OnKeyPress(args); if (focused != "Search" || Char.IsControl(args.KeyChar)) return;
            string before = selectAll ? "" : owner.state.Search;
            if (before.Length < 80) owner.state.Search = before + args.KeyChar;
            selectAll = false; args.Handled = true; owner.Audit("text_change", "Search");
        }
        private void PasteFocusedSearch() {
            // No timer, listener, or global clipboard monitoring. Read plain
            // text only in response to an actual paste command to this input.
            if (focused != "Search" || !Focused || !owner.ContainsFocus) return;
            try {
                string text = Clipboard.GetText(TextDataFormat.UnicodeText);
                if (owner.state.Paste(text, selectAll)) {
                    selectAll = false; owner.Audit("paste", "Search");
                }
            } catch (ExternalException) { owner.Audit("paste_unavailable", "Search"); }
        }
        protected override void WndProc(ref Message message) {
            if (message.Msg == 0x0302) { PasteFocusedSearch(); return; } // WM_PASTE
            base.WndProc(ref message);
        }
    }

    private static int SelfTest(string path) {
        var state = new RecordingCanvas018State(); var checks = new List<bool>();
        checks.Add(state.QueryCount == 0 && state.Rows.Count == 0 && state.Search == "");
        state.Family = "W"; state.Device = "D200"; state.Search = "NATIVEREC"; state.Query();
        checks.Add(state.QueryCount == 1 && state.Rows.Count == 5 && Convert.ToString(state.Rows[4]["name"]) == "NATIVEREC 05");
        state.Reset(); checks.Add(state.QueryCount == 1 && state.Rows.Count == 0 && state.Search == "" && state.Family == "전체");
        state.Search = "SECOND"; state.Query(); checks.Add(state.QueryCount == 2 && state.Rows.Count == 5 && Convert.ToString(state.Rows[0]["name"]) == "SECOND 01");
        checks.Add(RecordingCanvas018State.NormalizePaste("A\r\nB\tC\0D\u0003", 80) == "A B CD");
        checks.Add(RecordingCanvas018State.NormalizePaste(new string('X', 100), 80).Length == 80);
        checks.Add(RecordingCanvas018State.NormalizePaste("\uD83D\uDE00Z", 1) == "" && RecordingCanvas018State.NormalizePaste("\uD83D\uDE00Z", 2) == "\uD83D\uDE00");
        state.Search = "OLD"; checks.Add(state.Paste("NATREC", true) && state.Search == "NATREC");
        state.Search = new string('A', 79); checks.Add(state.Paste("BC", false) && state.Search.Length == 80 && state.Search.EndsWith("B"));
        state.Search = "KEEP"; checks.Add(!state.Paste("\0\u0003", true) && state.Search == "KEEP");
        bool ok = checks.TrueForAll(value => value);
        File.WriteAllText(path, new JavaScriptSerializer().Serialize(new { ok = ok, tests = checks.Count, gui_launched = false, input_sent = false }), new UTF8Encoding(false));
        return ok ? 0 : 1;
    }
    [STAThread] private static int Main(string[] args) {
        if (args.Length == 2 && args[0] == "--self-test") return SelfTest(Path.GetFullPath(args[1]));
        if (args.Length != 2 || args[0] != "--audit") return 2;
        string audit = Path.GetFullPath(args[1]); if (!Directory.Exists(Path.GetDirectoryName(audit))) return 3;
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new RecordingCanvas018(audit)); return 0;
    }
}
