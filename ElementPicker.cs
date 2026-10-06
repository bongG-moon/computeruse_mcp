// Read-only human element picker. No SendInput, automation patterns, values,
// screenshots, arbitrary commands, downloads, or child processes.
using System;
using System.Collections.Generic;
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
    [DllImport("user32.dll")] internal static extern bool GetCursorPos(out POINT point);
    [DllImport("user32.dll")] internal static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] internal static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] internal static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] internal static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 3 || args[0] != "--pick") return 2;
        try
        {
            FileInfo info = new FileInfo(args[1]);
            if (info.Length > 8192 || !Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            var request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(args[1], Encoding.UTF8));
            string nonce = Convert.ToString(request["nonce"]), label = Convert.ToString(request["label"]);
            long hwnd = Convert.ToInt64(request["window_id"]);
            int pid = Convert.ToInt32(request["pid"]), seconds = Convert.ToInt32(request["timeout_seconds"]);
            if (nonce.Length != 64 || label.Length < 1 || label.Length > 200 || hwnd <= 0 || pid <= 0 || seconds < 10 || seconds > 180) return 2;
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
            catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (PickerForm form = new PickerForm(pid, new IntPtr(hwnd), label, nonce, args[2], seconds)) Application.Run(form);
            return 0;
        }
        catch { return 2; }
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
        // Read process and protection before any text identity. ValuePattern and
        // TextPattern are deliberately absent from this helper.
        if (element == null || element.Current.ProcessId != pid) throw new InvalidOperationException("outside_target");
        if (element.Current.IsPassword) throw new InvalidOperationException("protected");
        string role = element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "");
        string id = element.Current.AutomationId ?? "", name = element.Current.Name ?? "";
        if (id.Length > 1000 || name.Length > 1000) throw new InvalidOperationException("unavailable");
        return new Dictionary<string, object> { { "role", role }, { "automation_id", id }, { "name", name }, { "is_password", false } };
    }

    internal static Dictionary<string, object> Observe(POINT point, int pid, IntPtr hwnd)
    {
        if (!TargetAt(point, pid, hwnd)) throw new InvalidOperationException("outside_target");
        AutomationElement element = AutomationElement.FromPoint(new System.Windows.Point(point.X, point.Y));
        var identity = Identity(element, pid);
        var ancestors = new List<Dictionary<string, object>>();
        AutomationElement parent = element;
        for (int i = 0; i < 16; i++)
        {
            parent = TreeWalker.ControlViewWalker.GetParent(parent);
            if (parent == null || parent.Current.ProcessId != pid) break;
            ancestors.Add(Identity(parent, pid));
            if (parent.Current.NativeWindowHandle == hwnd.ToInt64()) break;
        }
        System.Windows.Rect bounds = element.Current.BoundingRectangle;
        if (bounds.IsEmpty || !bounds.Contains(point.X, point.Y) || !TargetAt(point, pid, hwnd))
            throw new InvalidOperationException("outside_target");
        return new Dictionary<string, object> {
            { "status", "selected" }, { "pid", pid }, { "window_id", hwnd.ToInt64() },
            { "element", identity }, { "ancestors", ancestors },
            { "point", new Dictionary<string, object> { { "x", point.X }, { "y", point.Y } } },
            { "bounds", new Dictionary<string, object> { { "x", bounds.X }, { "y", bounds.Y }, { "width", bounds.Width }, { "height", bounds.Height } } }
        };
    }

    internal sealed class PickerForm : Form
    {
        readonly int pid;
        readonly IntPtr target;
        readonly string nonce, response;
        readonly DateTime deadline;
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        readonly Label countdown = new Label();
        readonly Label message = new Label();
        bool done, reading, finishing, f8, escape;
        DateTime readingDeadline, releaseDeadline;
        Dictionary<string, object> pending;
        float layoutScale;
        int Px(int value) { return (int)Math.Round(value * layoutScale); }
        Rectangle Box(int x, int y, int width, int height) { return new Rectangle(Px(x), Px(y), Px(width), Px(height)); }

        internal PickerForm(int pid, IntPtr target, string label, string nonce, string response, int seconds)
        {
            this.pid = pid; this.target = target; this.nonce = nonce; this.response = response;
            deadline = DateTime.UtcNow.AddSeconds(seconds);
            Text = "화면 요소 가르치기"; BackColor = Color.FromArgb(244, 247, 252);
            ForeColor = Color.FromArgb(24, 43, 72); Font = new Font("맑은 고딕", 10);
            // The portable .NET Framework host has no WinForms app.config DPI
            // switch. Scale layout explicitly; point-sized fonts already scale.
            using (Graphics graphics = Graphics.FromHwnd(IntPtr.Zero)) layoutScale = Math.Max(1F, graphics.DpiX / 96F);
            AutoScaleMode = AutoScaleMode.None;
            ClientSize = new Size(Px(430), Px(320));
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = false;
            StartPosition = FormStartPosition.Manual; TopMost = true; ShowInTaskbar = true;
            Rectangle screen = Screen.PrimaryScreen.WorkingArea;
            Location = new Point(Math.Max(screen.Left, screen.Right - Width - Px(28)),
                Math.Max(screen.Top, Math.Min(screen.Top + Px(30), screen.Bottom - Height)));
            var heading = new Label { Text = "이 요소를 기억할게요", Font = new Font(Font.FontFamily, 17, FontStyle.Bold), AutoSize = false, Bounds = Box(24, 24, 380, 40) };
            var name = new Label { Text = label, ForeColor = Color.FromArgb(37, 99, 235), Font = new Font(Font.FontFamily, 12, FontStyle.Bold), AutoEllipsis = true, Bounds = Box(24, 74, 380, 30) };
            message.Text = "1  대상 프로그램의 요소 위에 마우스를 올리세요.\n2  클릭하지 말고 F8 키를 누르세요.";
            message.Bounds = Box(24, 120, 380, 72);
            var help = new Label { Text = "창이 가리면 제목 표시줄을 잡고 옮기세요.\n값 입력이나 버튼 실행 없이 위치만 확인합니다.", ForeColor = Color.FromArgb(91, 107, 132), Bounds = Box(24, 202, 380, 48), Font = new Font(Font.FontFamily, 9) };
            countdown.Bounds = Box(24, 270, 240, 28); countdown.ForeColor = Color.FromArgb(91, 107, 132);
            var cancel = new Button { Text = "취소 · Esc", FlatStyle = FlatStyle.Flat, BackColor = Color.White, Bounds = Box(306, 264, 100, 36) };
            cancel.FlatAppearance.BorderColor = Color.FromArgb(213, 222, 236);
            cancel.Click += delegate { Finish("cancelled"); };
            Controls.AddRange(new Control[] { heading, name, message, help, countdown, cancel });
            FormClosing += delegate(object sender, FormClosingEventArgs args) { if (!done) { args.Cancel = true; Finish("cancelled"); } };
            Shown += delegate {
                f8 = RegisterHotKey(Handle, 1, 0x4000, 0x77);
                escape = RegisterHotKey(Handle, 2, 0x4000, 0x1B);
                if (!f8 || !escape) { Finish("hotkey_unavailable"); return; }
                timer.Interval = 100; timer.Tick += Tick; timer.Start();
            };
        }

        void Tick(object sender, EventArgs args)
        {
            if (finishing) { CompleteAfterRelease(); return; }
            if (reading && DateTime.UtcNow >= readingDeadline) { Finish("read_timeout"); return; }
            if (!reading && DateTime.UtcNow >= deadline) { Finish("timeout"); return; }
            countdown.Text = reading ? "선택한 요소 확인 중…" : "남은 시간 " + Math.Max(0, (int)Math.Ceiling((deadline - DateTime.UtcNow).TotalSeconds)) + "초";
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x0312)
            {
                if (m.WParam.ToInt32() == 2) Finish("cancelled");
                else if (m.WParam.ToInt32() == 1 && !reading && !done && !finishing) Pick();
                return;
            }
            base.WndProc(ref m);
        }

        void Pick()
        {
            POINT point;
            if (!GetCursorPos(out point)) { Finish("unavailable"); return; }
            if (!TargetAt(point, pid, target))
            {
                message.Text = "지정한 프로그램 창 안에서 가리켜 주세요.\n원하는 요소 위에서 F8 키를 다시 누르세요.";
                return;
            }
            reading = true; readingDeadline = DateTime.UtcNow.AddSeconds(6);
            message.Text = "선택한 요소를 확인하고 있습니다.\nEsc 키를 누르면 취소할 수 있습니다.";
            var worker = new Thread(delegate() {
                Dictionary<string, object> result;
                try { result = Observe(point, pid, target); }
                catch (InvalidOperationException error) { result = new Dictionary<string, object> { { "status", error.Message == "protected" || error.Message == "outside_target" ? error.Message : "unavailable" } }; }
                catch { result = new Dictionary<string, object> { { "status", "unavailable" } }; }
                try { BeginInvoke(new Action(delegate { Finish(result); })); } catch (InvalidOperationException) { }
            });
            worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }

        void Finish(string status) { Finish(new Dictionary<string, object> { { "status", status } }); }
        void Finish(Dictionary<string, object> result)
        {
            if (done || finishing) return;
            // Keep hotkeys registered through key-up so a very fast cancel or
            // selection does not deliver the remaining key event to the app.
            finishing = true; pending = result; releaseDeadline = DateTime.UtcNow.AddSeconds(2);
            timer.Interval = 25; timer.Tick -= Tick; timer.Tick += Tick; timer.Start();
            CompleteAfterRelease();
        }

        void CompleteAfterRelease()
        {
            if (!finishing || done) return;
            if (((GetAsyncKeyState(0x77) & 0x8000) != 0 || (GetAsyncKeyState(0x1B) & 0x8000) != 0)
                && DateTime.UtcNow < releaseDeadline) return;
            done = true; timer.Stop();
            if (f8) UnregisterHotKey(Handle, 1);
            if (escape) UnregisterHotKey(Handle, 2);
            pending["nonce"] = nonce;
            try
            {
                string text = new JavaScriptSerializer().Serialize(pending);
                if (Encoding.UTF8.GetByteCount(text) > 32768) text = new JavaScriptSerializer().Serialize(new Dictionary<string, object> { { "nonce", nonce }, { "status", "unavailable" } });
                string temporary = response + ".tmp";
                File.WriteAllText(temporary, text, new UTF8Encoding(false));
                File.Move(temporary, response);
            }
            finally { Close(); }
        }
    }
}
