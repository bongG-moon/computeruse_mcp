// Developer-only fixture. Never launches real applications or sends input.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class Phase1RecordingFixture : Form {
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    private readonly string output;
    private readonly string[] rows = { "ALPHA 001", "BETA 002", "GAMMA 003", "DELTA 004" };
    private readonly ComboBox choice = new ComboBox();
    private readonly Label result = new Label();
    private readonly Label counts = new Label();
    private readonly List<string> plays = new List<string>();
    private readonly List<string> selections = new List<string>();
    private readonly List<string> queries = new List<string>();
    private readonly List<Dictionary<string, object>> events = new List<Dictionary<string, object>>();
    private readonly Timer heartbeat = new Timer();
    private Form unrelated;
    private int generation, opened, smallButtonClicks;
    private bool resetting;

    private void Event(string kind, string value) {
        if (resetting) return;
        events.Add(new Dictionary<string, object> {
            { "sequence", events.Count + 1 }, { "kind", kind }, { "value", value },
            { "utc", DateTime.UtcNow.ToString("o") }
        });
        Save();
    }

    private void Save() {
        if (!IsHandleCreated || resetting) return;
        counts.Text = "Play: " + plays.Count + "   Dropdown opens: " + opened +
            "   Selections: " + selections.Count + "   Query: " + queries.Count;
        var receipt = new Dictionary<string, object> {
            { "synthetic_fixture", true }, { "pid", System.Diagnostics.Process.GetCurrentProcess().Id },
            { "window_id", Handle.ToInt64() }, { "title", Text }, { "generation", generation },
            { "selection", choice.Text }, { "dropdown_opened", opened },
            { "play_rows", plays.ToArray() }, { "selections", selections.ToArray() },
            { "queries", queries.ToArray() }, { "small_button_clicks", smallButtonClicks },
            { "result", result.Text }, { "events", events.ToArray() },
            { "window", new Dictionary<string, object> { { "x", Left }, { "y", Top },
                { "width", Width }, { "height", Height }, { "state", WindowState.ToString() } } },
            { "unrelated_visible", unrelated != null && !unrelated.IsDisposed && unrelated.Visible },
            { "utc", DateTime.UtcNow.ToString("o") }
        };
        string temporary = output + ".tmp";
        File.WriteAllText(temporary, new JavaScriptSerializer().Serialize(receipt), new UTF8Encoding(false));
        if (File.Exists(output)) File.Replace(temporary, output, null);
        else File.Move(temporary, output);
    }

    private Button Button(string id, string label, int x, int y, int width, EventHandler action) {
        var button = new Button { Name = id, AccessibleName = label, Text = label };
        button.SetBounds(x, y, width, 36);
        button.Click += action;
        Controls.Add(button);
        return button;
    }

    private void Reset() {
        resetting = true;
        generation++;
        if (unrelated != null && !unrelated.IsDisposed) unrelated.Close();
        choice.SelectedIndex = 0;
        plays.Clear(); selections.Clear(); queries.Clear(); events.Clear();
        opened = smallButtonClicks = 0;
        result.Text = "Ready - all counters reset";
        resetting = false;
        Save();
    }

    private Phase1RecordingFixture(string receipt) {
        output = receipt;
        Text = "Computer Use phase 1 recording fixture";
        Name = "Phase1Window";
        AccessibleName = Text;
        AutoScaleMode = AutoScaleMode.None;
        ClientSize = new Size(900, 575);
        StartPosition = FormStartPosition.Manual;
        Location = new Point(80, 80);
        // Fixed physical-pixel canvas: point fonts double on a 200% monitor even
        // with AutoScaleMode.None, obscuring the actual small-icon test targets.
        Font = new Font("Segoe UI", 16, FontStyle.Regular, GraphicsUnit.Pixel);
        BackColor = Color.FromArgb(244, 247, 251);
        Controls.Add(new Label { Text = "Small icons + native dropdown recording", AutoSize = true,
            Left = 24, Top = 20, Font = new Font("Segoe UI", 23, FontStyle.Bold, GraphicsUnit.Pixel) });
        Controls.Add(new Label { Text = "Synthetic data only. Each action is independently logged in receipt.json.",
            AutoSize = true, Left = 24, Top = 60 });

        // A single owner-drawn surface deliberately exposes no child Button UIA.
        // Identical Play symbols require row context; clicks outside symbols do nothing.
        var surface = new Panel { Name = "ImageRows", AccessibleName = "Painted image rows",
            AccessibleRole = AccessibleRole.Pane, BackColor = Color.FromArgb(43, 43, 43) };
        surface.SetBounds(24, 110, 575, 336);
        surface.Paint += delegate(object sender, PaintEventArgs e) {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            for (int i = 0; i < rows.Length; i++) {
                int top = i * 80;
                using (var fill = new SolidBrush(i % 2 == 0 ? Color.FromArgb(43, 43, 43) : Color.FromArgb(52, 52, 52)))
                    e.Graphics.FillRectangle(fill, 0, top, surface.Width, 80);
                using (var textFont = new Font("Segoe UI", 16, FontStyle.Regular, GraphicsUnit.Pixel)) {
                    e.Graphics.DrawString("#" + (i + 1), textFont, Brushes.Silver, 20, top + 27);
                    e.Graphics.DrawString(rows[i], textFont, Brushes.White, 90, top + 27);
                    e.Graphics.DrawString("Stopped", textFont, Brushes.Silver, 305, top + 27);
                }
                using (var circle = new SolidBrush(Color.FromArgb(83, 83, 83)))
                    e.Graphics.FillEllipse(circle, 481, top + 21, 38, 38);
                e.Graphics.FillPolygon(Brushes.White, new[] {
                    new Point(495, top + 31), new Point(495, top + 49), new Point(509, top + 40) });
            }
        };
        surface.MouseClick += delegate(object sender, MouseEventArgs e) {
            int index = e.Y / 80;
            if (e.Button != MouseButtons.Left || index < 0 || index >= rows.Length) return;
            var hit = new Rectangle(481, index * 80 + 21, 38, 38);
            if (!hit.Contains(e.Location)) return;
            plays.Add(rows[index]); result.Text = "Played " + rows[index]; Event("play", rows[index]);
        };
        Controls.Add(surface);

        Controls.Add(new Label { Text = "Native dropdown", AutoSize = true, Left = 632, Top = 112 });
        choice.Name = "Phase1Choice"; choice.AccessibleName = "Native dropdown";
        choice.DropDownStyle = ComboBoxStyle.DropDownList;
        choice.Items.AddRange(new object[] { "Alpha", "Beta", "Gamma" });
        choice.SelectedIndex = 0;
        choice.SetBounds(632, 145, 235, 32);
        choice.DropDown += delegate { opened++; Event("dropdown_open", choice.Text); };
        choice.SelectionChangeCommitted += delegate { Event("selection_commit", choice.Text); };
        choice.SelectedIndexChanged += delegate {
            if (!resetting) { selections.Add(choice.Text); result.Text = "Selected " + choice.Text; Event("selection", choice.Text); }
        };
        Controls.Add(choice);
        Button("Phase1Query", "Query", 632, 197, 235, delegate {
            queries.Add(choice.Text); result.Text = "Query result: " + choice.Text; Event("query", choice.Text);
        });
        Controls.Add(new Label { Text = "Small UIA button", AutoSize = true, Left = 632, Top = 258 });
        var tiny = Button("SmallSemanticButton", "Small UIA play", 632, 288, 34, delegate {
            smallButtonClicks++; result.Text = "Small UIA button clicked"; Event("small_button", smallButtonClicks.ToString());
        });
        tiny.Text = "\u25b6"; tiny.Size = new Size(34, 30);
        Button("UnrelatedCover", "Open unrelated cover", 632, 354, 235, delegate {
            if (unrelated != null && !unrelated.IsDisposed) { unrelated.Activate(); return; }
            // Deliberately no Owner: same process alone must not authorize it.
            unrelated = new Form { Text = "Phase 1 unrelated cover", ClientSize = new Size(270, 100),
                StartPosition = FormStartPosition.Manual, Location = PointToScreen(new Point(440, 120)), Font = Font };
            unrelated.Controls.Add(new Label { Text = "Unrelated window. Close with X.", AutoSize = true, Left = 12, Top = 25 });
            unrelated.FormClosed += delegate { Save(); };
            unrelated.Show(); Event("unrelated_open", "no_owner");
        });
        Button("ResetFixture", "Reset fixture", 632, 410, 235, delegate { Reset(); });
        result.Name = "Phase1Result"; result.AccessibleName = "Result status";
        result.SetBounds(24, 464, 840, 36); result.BackColor = Color.White; Controls.Add(result);
        counts.Name = "Phase1Counts"; counts.AccessibleName = "Action counters";
        counts.SetBounds(24, 510, 840, 35); Controls.Add(counts);
        Shown += delegate { Reset(); };
        Move += delegate { Save(); }; Resize += delegate { Save(); };
        heartbeat.Interval = 1000; heartbeat.Tick += delegate { Save(); }; heartbeat.Start();
    }

    [STAThread] private static void Main(string[] args) {
        string folder = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string receipt = args.Length == 1 ? Path.GetFullPath(args[0]) : Path.Combine(folder, "receipt.json");
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Phase1RecordingFixture(receipt));
    }
}
