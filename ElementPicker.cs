// Read-only human element picker: no input injection, UIA value/text patterns,
// screenshots, child processes, or remote communication.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

internal static class ElementPicker
{
    [StructLayout(LayoutKind.Sequential)] internal struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] internal struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] internal static extern bool GetCursorPos(out POINT point);
    [DllImport("user32.dll")] internal static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] internal static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr hwnd, out RECT bounds);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, uint attribute, out uint value, int size);
    [DllImport("user32.dll")] internal static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] internal static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool MoveFileEx(string source, string destination, uint flags);

    static bool ValidNonce(string value)
    {
        if (value == null || value.Length != 64) return false;
        foreach (char c in value) if (!Uri.IsHexDigit(c)) return false;
        return true;
    }

    internal static void WriteJson(string path, Dictionary<string, object> value, bool replace = false)
    {
        string text = new JavaScriptSerializer().Serialize(value);
        if (Encoding.UTF8.GetByteCount(text) > 32768) throw new InvalidOperationException("response_too_large");
        string temporary = path + ".tmp";
        try {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            using (var writer = new StreamWriter(stream, new UTF8Encoding(false))) writer.Write(text);
            Stopwatch wait = Stopwatch.StartNew();
            while (!MoveFileEx(temporary, path, replace ? 1U : 0U)) {
                int code = Marshal.GetLastWin32Error();
                if ((code != 5 && code != 32 && code != 33) || wait.ElapsedMilliseconds >= 500)
                    throw new System.ComponentModel.Win32Exception(code);
                Thread.Sleep(10);
            }
        } finally { try { File.Delete(temporary); } catch { } }
    }

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 3 || args[0] != "--pick") return 2;
        string nonce = null, response = null, stage = "request_validation";
        try
        {
            if (!Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            string requestPath = Path.GetFullPath(args[1]), responsePath = Path.GetFullPath(args[2]);
            if (!String.Equals(Path.GetDirectoryName(requestPath), Path.GetDirectoryName(responsePath), StringComparison.OrdinalIgnoreCase)) return 2;
            FileInfo info = new FileInfo(requestPath);
            if (info.Length > 8192) return 2;
            var request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(requestPath, Encoding.UTF8));
            string requestedNonce = Convert.ToString(request["nonce"]);
            if (!ValidNonce(requestedNonce)) return 2;
            // Diagnostics may be written only after validating the nonce and the
            // caller's sibling response path. Never echo exception text or paths.
            nonce = requestedNonce; response = responsePath;
            string label = Convert.ToString(request["label"]);
            long hwnd = Convert.ToInt64(request["window_id"]);
            int pid = Convert.ToInt32(request["pid"]), seconds = Convert.ToInt32(request["timeout_seconds"]);
            if (label.Length < 1 || label.Length > 200 || hwnd <= 0 || pid <= 0 || seconds < 10 || seconds > 180)
                throw new ArgumentException("invalid_request");
            stage = "target_validation";
            if (!ValidTarget(pid, new IntPtr(hwnd))) throw new InvalidOperationException("target_unavailable");
            stage = "desktop_validation";
            if (!Environment.UserInteractive) throw new InvalidOperationException("interactive_desktop_unavailable");
            stage = "form_startup";
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
            catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            using (PickerForm form = new PickerForm(pid, new IntPtr(hwnd), label, nonce, response, seconds)) Application.Run(form);
            return File.Exists(response) ? 0 : 3;
        }
        catch (Exception error)
        {
            if (nonce != null && response != null && !File.Exists(response))
            {
                bool shown = File.Exists(response + ".ready.json");
                try { WriteJson(response, new Dictionary<string, object> {
                    { "nonce", nonce }, { "status", shown ? "runtime_failed" : "startup_failed" }, { "stage", shown ? "picker_ui" : stage },
                    { "code", stage == "target_validation" ? "target_unavailable" : stage == "desktop_validation" ? "desktop_unavailable" : "picker_initialization_failed" },
                    { "error_type", error.GetType().Name }, { "hresult", error.HResult },
                    { "winerror", error is System.ComponentModel.Win32Exception ? (object)((System.ComponentModel.Win32Exception)error).NativeErrorCode : null }
                }); } catch { }
            }
            return 2;
        }
    }

    internal static bool ValidTarget(int pid, IntPtr target)
    {
        uint found;
        return IsWindow(target) && GetWindowThreadProcessId(target, out found) != 0 && found == (uint)pid;
    }

    static bool VisibleOnScreen(IntPtr hwnd)
    {
        RECT bounds; uint cloaked;
        return IsWindow(hwnd) && IsWindowVisible(hwnd) && !IsIconic(hwnd)
            && GetWindowRect(hwnd, out bounds) && bounds.Right > bounds.Left && bounds.Bottom > bounds.Top
            && MonitorFromWindow(hwnd, 0) != IntPtr.Zero
            && DwmGetWindowAttribute(hwnd, 14, out cloaked, sizeof(uint)) == 0 && cloaked == 0;
    }

    internal static bool TargetAt(POINT point, int pid, IntPtr expected)
    {
        IntPtr hit = WindowFromPoint(point), root = GetAncestor(hit, 2);
        uint found;
        GetWindowThreadProcessId(root, out found);
        return root == expected && found == (uint)pid;
    }

    internal static Dictionary<string, object> Identity(AutomationElement element, int pid)
    {
        // Check process and protection before reading identity; never read values.
        if (element == null || element.Current.ProcessId != pid) throw new InvalidOperationException("outside_target");
        if (element.Current.IsPassword) throw new InvalidOperationException("protected");
        string role = element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "");
        string id = element.Current.AutomationId ?? "", name = element.Current.Name ?? "";
        if (id.Length > 1000 || name.Length > 1000) throw new InvalidOperationException("unavailable");
        // Pattern availability is metadata only; do not read values or invoke
        // a pattern. Retain even legacy/custom patterns conservatively rather
        // than declaring a usable container unsupported from its role alone.
        bool controlPatterns = false;
        foreach (AutomationPattern pattern in element.GetSupportedPatterns())
            if (pattern != WindowPattern.Pattern && pattern != TransformPattern.Pattern) controlPatterns = true;
        return new Dictionary<string, object> { { "role", role }, { "automation_id", id }, { "name", name },
            { "is_password", false }, { "has_control_patterns", controlPatterns } };
    }

    internal sealed class Candidate
    {
        internal Dictionary<string, object> Identity;
        internal System.Windows.Rect Bounds;
        internal readonly List<Dictionary<string, object>> Ancestors = new List<Dictionary<string, object>>();
        internal string Description;
        public override string ToString() { return Description; }
    }

    internal static List<Candidate> Observe(POINT point, int pid, IntPtr hwnd)
    {
        if (!TargetAt(point, pid, hwnd)) throw new InvalidOperationException("outside_target");
        var found = new List<Candidate>();
        bool patternlessRoot = false;
        AutomationElement element = AutomationElement.FromPoint(new System.Windows.Point(point.X, point.Y));
        for (int level = 0; level < 17 && element != null; level++)
        {
            if (element.Current.ProcessId != pid) break;
            var identity = Identity(element, pid);
            if (Convert.ToString(identity["role"]) == "Window" && element.Current.NativeWindowHandle == hwnd.ToInt64()
                && !Convert.ToBoolean(identity["has_control_patterns"])) patternlessRoot = true;
            System.Windows.Rect bounds = element.Current.BoundingRectangle;
            foreach (Candidate child in found) child.Ancestors.Add(identity);
            // The root window can disambiguate a candidate but is never offered
            // as the action target. The user chooses ancestors explicitly.
            if (!bounds.IsEmpty && bounds.Contains(point.X, point.Y) && Convert.ToString(identity["role"]) != "Window")
            {
                string name = Convert.ToString(identity["name"]);
                if (String.IsNullOrWhiteSpace(name)) name = Convert.ToString(identity["automation_id"]);
                if (String.IsNullOrWhiteSpace(name)) name = "이름 없는 요소";
                name = name.Replace('\r', ' ').Replace('\n', ' ');
                if (name.Length > 70) name = name.Substring(0, 67) + "…";
                found.Add(new Candidate { Identity = identity, Bounds = bounds,
                    Description = (level == 0 ? "가리킨 요소  ·  " : "상위 요소 " + level + "  ·  ") +
                        RoleName(Convert.ToString(identity["role"])) + " (" + Convert.ToString(identity["role"]) + ")  ·  " + name });
            }
            if (element.Current.NativeWindowHandle == hwnd.ToInt64()) break;
            element = TreeWalker.ControlViewWalker.GetParent(element);
        }
        if (found.Count == 0) throw new InvalidOperationException(patternlessRoot ? "controls_not_exposed" : "unavailable");
        bool containersOnly = true;
        foreach (Candidate candidate in found) {
            string role = Convert.ToString(candidate.Identity["role"]);
            if ((role != "Pane" && role != "Group" && role != "Custom") ||
                Convert.ToBoolean(candidate.Identity["has_control_patterns"])) containersOnly = false;
        }
        // Rendered canvases/emulators may expose only unnamed or named panes.
        // Showing one as if it were the visible button leads to repeat F8 loops.
        if (containersOnly) throw new InvalidOperationException("controls_not_exposed");
        if (!TargetAt(point, pid, hwnd)) throw new InvalidOperationException("outside_target");
        return found;
    }

    internal static string RoleName(string role)
    {
        switch (role) {
            case "Button": return "버튼";
            case "Edit": return "입력란";
            case "ComboBox": return "선택 상자";
            case "Text": return "글자";
            case "CheckBox": return "체크 상자";
            case "RadioButton": return "선택 버튼";
            case "List": return "목록";
            case "ListItem": return "목록 항목";
            case "Group": return "그룹";
            case "Pane": return "영역";
            case "TabItem": return "탭";
            case "MenuItem": return "메뉴 항목";
            default: return role;
        }
    }

    internal sealed class PickerForm : Form
    {
        readonly int pid;
        readonly IntPtr target;
        readonly string nonce, response;
        readonly DateTime deadline;
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        readonly Label countdown = new Label(), message = new Label(), step = new Label(), detail = new Label();
        readonly ListBox choices = new ListBox();
        readonly Button save = new Button(), reselect = new Button(), selectLater = new Button();
        readonly Color ink = Color.FromArgb(24, 43, 72), muted = Color.FromArgb(91, 107, 132), blue = Color.FromArgb(37, 99, 235);
        bool done, reading, reviewing, finishing, f8, escape, delayedPick, shownOnce;
        IntPtr hotkeyWindow, readyWindow;
        int invalidAttempts;
        DateTime readingDeadline, releaseDeadline, pickAt;
        Dictionary<string, object> pending;
        POINT pickedPoint;
        RECT pickedWindowBounds;
        float layoutScale;
        int Px(int value) { return (int)Math.Round(value * layoutScale); }
        Rectangle Box(int x, int y, int width, int height) { return new Rectangle(Px(x), Px(y), Px(width), Px(height)); }

        void Identify(Control control, string id, string name) { control.Name = id; control.AccessibleName = name; }
        void StyleButton(Button button, string id, string title, Rectangle bounds, bool primary)
        {
            Identify(button, id, title); button.Text = title; button.Bounds = bounds;
            button.FlatStyle = FlatStyle.Flat; button.UseVisualStyleBackColor = false;
            button.BackColor = primary ? blue : Color.White; button.ForeColor = primary ? Color.White : ink;
            button.FlatAppearance.BorderColor = primary ? blue : Color.FromArgb(213, 222, 236);
            button.FlatAppearance.BorderSize = primary ? 0 : 1; button.Cursor = Cursors.Hand;
            button.Font = new Font(Font.FontFamily, 10, FontStyle.Bold);
        }

        internal PickerForm(int pid, IntPtr target, string label, string nonce, string response, int seconds)
        {
            this.pid = pid; this.target = target; this.nonce = nonce; this.response = response;
            deadline = DateTime.UtcNow.AddSeconds(seconds);
            Text = "화면 요소 가르치기"; BackColor = Color.FromArgb(246, 248, 252);
            ForeColor = ink; Font = new Font("맑은 고딕", 10);
            // A DPI-unaware target reports 96 even on a 200% monitor. Create our
            // own DPI-aware handle on that monitor before measuring the layout.
            Rectangle screen = Screen.FromHandle(target).WorkingArea;
            AutoScaleMode = AutoScaleMode.None; StartPosition = FormStartPosition.Manual;
            Location = new Point(screen.Left + 20, screen.Top + 20);
            uint dpi = 0;
            try { dpi = GetDpiForWindow(Handle); } catch (EntryPointNotFoundException) { }
            if (dpi == 0) { using (Graphics graphics = Graphics.FromHwnd(Handle)) dpi = (uint)graphics.DpiX; }
            layoutScale = Math.Max(1F, dpi / 96F);
            ClientSize = new Size(Px(480), Px(456));
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = false;
            StartPosition = FormStartPosition.Manual; TopMost = true; ShowInTaskbar = true;
            if (Height > screen.Height) { AutoScroll = true; AutoScrollMinSize = ClientSize; Height = screen.Height; }
            if (Width > screen.Width) { AutoScroll = true; AutoScrollMinSize = new Size(Px(480), Px(456)); Width = screen.Width; }
            Location = new Point(Math.Max(screen.Left, screen.Right - Width - Px(20)),
                Math.Max(screen.Top, Math.Min(screen.Top + Px(20), screen.Bottom - Height)));
            var heading = new Label { Text = "화면에서 직접 알려주세요", Font = new Font(Font.FontFamily, 17, FontStyle.Bold), Bounds = Box(24, 20, 432, 38) };
            step.Text = "01  위치 선택    →    02  요소 확인"; step.ForeColor = blue;
            step.Font = new Font(Font.FontFamily, 9, FontStyle.Bold); step.Bounds = Box(24, 67, 432, 25);
            var name = new Label { Text = label, ForeColor = ink, Font = new Font(Font.FontFamily, 12, FontStyle.Bold), AutoEllipsis = true, Bounds = Box(24, 102, 432, 32) };
            message.Bounds = Box(24, 150, 432, 54); Identify(message, "PickerMessage", "선택 안내");
            choices.Bounds = Box(24, 218, 432, 100); choices.Visible = false;
            choices.BorderStyle = BorderStyle.FixedSingle; choices.IntegralHeight = false;
            choices.ItemHeight = Px(27); choices.DrawMode = DrawMode.OwnerDrawFixed;
            choices.DrawItem += DrawCandidate; choices.HorizontalScrollbar = true;
            choices.SelectedIndexChanged += delegate { UpdateCandidate(); }; Identify(choices, "CandidateList", "기억할 요소 후보");
            detail.Bounds = Box(24, 326, 432, 48); detail.ForeColor = muted; detail.Font = new Font(Font.FontFamily, 9);
            countdown.Bounds = Box(24, 377, 432, 25); countdown.ForeColor = muted; countdown.Font = new Font(Font.FontFamily, 9);
            Identify(countdown, "PickerStatus", "요소 선택 상태");
            StyleButton(save, "SaveSelection", "이 요소로 선택", Box(24, 411, 182, 32), true);
            StyleButton(reselect, "ReselectElement", "다시 선택", Box(218, 411, 110, 32), false);
            StyleButton(selectLater, "SelectAfterCountdown", "3초 후 위치 선택", Box(24, 411, 240, 32), true);
            var cancel = new Button(); StyleButton(cancel, "CancelSelection", "취소", Box(340, 411, 116, 32), false);
            save.Click += delegate { Confirm(); }; reselect.Click += delegate { ResetSelection(); };
            selectLater.Click += delegate {
                if (reading || reviewing || finishing) return;
                delayedPick = true; pickAt = DateTime.UtcNow.AddSeconds(3); selectLater.Enabled = false;
                message.Text = "마우스를 원하는 요소 위로 옮겨 주세요.\n3초 뒤 그 위치를 확인합니다. 클릭하지 않아도 됩니다.";
            };
            cancel.Click += delegate { Finish("cancelled"); };
            Controls.AddRange(new Control[] { heading, step, name, message, choices, detail, countdown, save, reselect, selectLater, cancel });
            ResetSelection();
            FormClosing += delegate(object sender, FormClosingEventArgs args) { if (!done) { args.Cancel = true; Finish("cancelled"); } };
            Shown += delegate {
                // An occupied hotkey must not suppress the whole picker.
                shownOnce = true; timer.Interval = 100; timer.Tick += Tick; timer.Start();
                BeginInvoke(new Action(PublishReady));
            };
            HandleDestroyed += delegate { readyWindow = IntPtr.Zero; ReleaseHotkeys(); };
            HandleCreated += delegate { if (shownOnce && !done && !finishing) BeginInvoke(new Action(PublishReady)); };
            VisibleChanged += delegate {
                readyWindow = IntPtr.Zero;
                if (shownOnce && Visible && IsHandleCreated && !done && !finishing) BeginInvoke(new Action(PublishReady));
            };
        }

        void ReleaseHotkeys()
        {
            if (hotkeyWindow != IntPtr.Zero) {
                if (f8) UnregisterHotKey(hotkeyWindow, 1);
                if (escape) UnregisterHotKey(hotkeyWindow, 2);
            }
            hotkeyWindow = IntPtr.Zero; f8 = escape = false;
        }

        void PublishReady()
        {
            if (!shownOnce || done || finishing || IsDisposed || !IsHandleCreated) return;
            if (!ValidTarget(pid, target)) { Finish("target_unavailable"); return; }
            if (!Visible || !VisibleOnScreen(Handle)) { readyWindow = IntPtr.Zero; return; }
            if (hotkeyWindow != Handle) {
                ReleaseHotkeys(); hotkeyWindow = Handle;
                f8 = RegisterHotKey(Handle, 1, 0x4000, 0x77); escape = RegisterHotKey(Handle, 2, 0x4000, 0x1B);
                if (!reading && !reviewing && !delayedPick) ResetSelection();
            }
            if (readyWindow == Handle) return;
            try {
                WriteJson(response + ".ready.json", new Dictionary<string, object> {
                    { "nonce", nonce }, { "status", "ready" }, { "pid", pid }, { "window_id", target.ToInt64() },
                    { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", Handle.ToInt64() },
                    { "f8_available", f8 }, { "escape_available", escape }
                }, true);
                readyWindow = Handle;
            } catch { Finish("ready_failed"); }
        }

        void DrawCandidate(object sender, DrawItemEventArgs e)
        {
            if (e.Index < 0 || e.Index >= choices.Items.Count) return;
            bool selected = (e.State & DrawItemState.Selected) != 0;
            using (var fill = new SolidBrush(selected ? Color.FromArgb(226, 237, 255) : Color.White)) e.Graphics.FillRectangle(fill, e.Bounds);
            Rectangle bounds = e.Bounds; bounds.X += Px(8); bounds.Width -= Px(12);
            TextRenderer.DrawText(e.Graphics, choices.Items[e.Index].ToString(), Font, bounds, selected ? blue : ink,
                TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
            e.DrawFocusRectangle();
        }

        void ResetSelection()
        {
            reviewing = false; delayedPick = false; choices.Items.Clear(); choices.Visible = false;
            save.Visible = reselect.Visible = false; save.Enabled = false; selectLater.Visible = true; selectLater.Enabled = true;
            step.Text = "01  위치 선택    →    02  요소 확인";
            message.Text = f8 ? "원하는 요소 위에 마우스를 올리고 F8을 누르세요.\n또는 아래 버튼을 누른 뒤 3초 안에 마우스를 옮기세요." :
                "아래 버튼을 누른 뒤 원하는 요소 위로 마우스를 옮기세요.\n3초 후 위치를 확인하고, 선택 결과를 보여드립니다.";
            detail.Text = "프로그램의 버튼을 누르거나 내용을 입력하지 않습니다.\n창이 가리면 제목 표시줄을 잡고 옮겨 주세요.";
        }

        void UpdateCandidate()
        {
            var candidate = choices.SelectedItem as Candidate; save.Enabled = reviewing && candidate != null;
            if (candidate == null) return;
            string role = Convert.ToString(candidate.Identity["role"]);
            detail.Text = "선택 유형: " + RoleName(role) + " (" + role + ")\n확인 후 MCP가 식별 가능한지 검사하고 기억합니다.";
        }

        void Tick(object sender, EventArgs args)
        {
            if (finishing) { CompleteAfterRelease(); return; }
            PublishReady();
            if (finishing || done) return;
            if (!ValidTarget(pid, target)) { Finish("target_unavailable"); return; }
            if (reading && DateTime.UtcNow >= readingDeadline) { Finish("read_timeout"); return; }
            if (DateTime.UtcNow >= deadline) { Finish("timeout"); return; }
            if (delayedPick) {
                int seconds = Math.Max(0, (int)Math.Ceiling((pickAt - DateTime.UtcNow).TotalSeconds));
                countdown.Text = seconds + "초 뒤 마우스 위치를 확인합니다";
                if (DateTime.UtcNow >= pickAt) { delayedPick = false; Pick(); } return;
            }
            countdown.Text = reading ? "선택한 요소를 읽고 있습니다…" :
                (reviewing ? "요소 확인 후 ‘이 요소로 선택’을 눌러 주세요  ·  " : "") +
                Math.Max(0, (int)Math.Ceiling((deadline - DateTime.UtcNow).TotalSeconds)) + "초 남음";
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x0312) {
                if (m.WParam.ToInt32() == 2) Finish("cancelled");
                else if (m.WParam.ToInt32() == 1 && !reading && !reviewing && !done && !finishing) { delayedPick = false; Pick(); }
                return;
            } base.WndProc(ref m);
        }

        void InvalidPoint(POINT point)
        {
            invalidAttempts++;
            if (invalidAttempts >= 3) { Finish(new Dictionary<string, object> { { "status", "outside_target" }, { "stage", "point_validation" }, { "attempts", invalidAttempts } }); return; }
            IntPtr hit = GetAncestor(WindowFromPoint(point), 2);
            message.Text = hit == Handle ? "마우스가 이 안내 창 위에 있습니다.\n대상 프로그램의 요소 위로 옮긴 뒤 위치를 선택해 주세요." :
                "지정된 프로그램의 원래 창 밖을 가리켰습니다.\n다른 창이나 열린 팝업 대신 원래 창의 요소를 선택해 주세요.";
            detail.Text = "선택되지 않았습니다 (" + invalidAttempts + "/3). 버튼이나 입력은 실행하지 않았습니다.\n대상 창이 가려졌다면 이 안내 창을 옮겨 주세요.";
            selectLater.Enabled = true;
        }

        void Pick()
        {
            POINT point;
            if (!GetCursorPos(out point)) { Finish("unavailable"); return; }
            if (!TargetAt(point, pid, target)) { InvalidPoint(point); return; }
            if (!GetWindowRect(target, out pickedWindowBounds)) { Finish("target_unavailable"); return; }
            pickedPoint = point; reading = true; readingDeadline = DateTime.UtcNow.AddSeconds(6); selectLater.Enabled = false;
            message.Text = "가리킨 요소와 상위 요소를 확인하고 있습니다.\n완료되면 원하는 요소를 직접 골라 주세요.";
            var worker = new Thread(delegate() {
                List<Candidate> candidates = null; string failure = null;
                try { candidates = Observe(point, pid, target); }
                catch (InvalidOperationException error) { failure = error.Message == "protected" || error.Message == "outside_target" || error.Message == "controls_not_exposed" ? error.Message : "unavailable"; }
                catch { failure = "unavailable"; }
                try { BeginInvoke(new Action(delegate {
                    if (done || finishing) return;
                    reading = false;
                    if (failure != null) { Finish(new Dictionary<string, object> { { "status", failure }, { "stage", "element_observation" } }); return; }
                    reviewing = true; step.Text = "01  위치 선택 완료    →    02  요소 확인";
                    message.Text = "기억할 요소를 확인해 주세요.\n글자가 잡혔다면 목록에서 상위 버튼이나 선택 상자를 고르세요.";
                    choices.Items.Clear(); foreach (Candidate candidate in candidates) choices.Items.Add(candidate);
                    choices.Visible = true; save.Visible = reselect.Visible = true; selectLater.Visible = false;
                    // Show the leaf first; never substitute an ancestor silently.
                    choices.SelectedIndex = 0; UpdateCandidate(); Activate();
                })); } catch (InvalidOperationException) { }
            });
            worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }

        void Confirm()
        {
            if (!reviewing || reading || finishing) return;
            var candidate = choices.SelectedItem as Candidate; if (candidate == null) return;
            if (!ValidTarget(pid, target)) { Finish("target_unavailable"); return; }
            System.Windows.Rect bounds = candidate.Bounds;
            Finish(new Dictionary<string, object> {
                { "status", "selected" }, { "pid", pid }, { "window_id", target.ToInt64() },
                { "element", candidate.Identity }, { "ancestors", candidate.Ancestors },
                { "point", new Dictionary<string, object> { { "x", pickedPoint.X }, { "y", pickedPoint.Y } } },
                { "bounds", new Dictionary<string, object> { { "x", bounds.X }, { "y", bounds.Y }, { "width", bounds.Width }, { "height", bounds.Height } } },
                { "window_bounds", new Dictionary<string, object> { { "x", pickedWindowBounds.Left }, { "y", pickedWindowBounds.Top },
                    { "width", pickedWindowBounds.Right - pickedWindowBounds.Left }, { "height", pickedWindowBounds.Bottom - pickedWindowBounds.Top } } },
                { "human_confirmed", true }, { "candidate_level", choices.SelectedIndex }
            });
        }

        void Finish(string status) { Finish(new Dictionary<string, object> { { "status", status } }); }
        void Finish(Dictionary<string, object> result)
        {
            if (done || finishing) return;
            // Keep registered hotkeys until key-up to avoid leaking a trailing
            // event into the program whose element the user just selected.
            finishing = true; pending = result; releaseDeadline = DateTime.UtcNow.AddSeconds(2);
            timer.Interval = 25; timer.Tick -= Tick; timer.Tick += Tick; timer.Start(); CompleteAfterRelease();
        }

        void CompleteAfterRelease()
        {
            if (!finishing || done) return;
            if (((f8 && (GetAsyncKeyState(0x77) & 0x8000) != 0) || (escape && (GetAsyncKeyState(0x1B) & 0x8000) != 0))
                && DateTime.UtcNow < releaseDeadline) return;
            done = true; timer.Stop();
            ReleaseHotkeys();
            pending["nonce"] = nonce;
            try {
                try { WriteJson(response, pending); }
                catch (InvalidOperationException) { WriteJson(response, new Dictionary<string, object> { { "nonce", nonce }, { "status", "unavailable" }, { "stage", "response_size" } }); }
            } finally { Close(); }
        }
    }
}
