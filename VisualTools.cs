// Offline visual authoring. Never injects application input or uses the network.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

internal static class VisualTools
{
    [StructLayout(LayoutKind.Sequential)] internal struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] internal struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct COMBOINFO { public int Size; public RECT Item, Button; public uint State; public IntPtr Combo, Edit, List; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSE { public POINT point; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KEY { public uint code, scan, flags, time; public UIntPtr extra; }
    delegate IntPtr Hook(int code, IntPtr message, IntPtr data);
    delegate void WindowEvent(IntPtr hook, uint eventType, IntPtr hwnd, int objectId, int childId, uint thread, uint time);
    [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint first, uint last, IntPtr module, WindowEvent callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetWindowDC(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("gdi32.dll")] static extern bool BitBlt(IntPtr destination, int x, int y, int width, int height, IntPtr source, int sourceX, int sourceY, uint operation);
    [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int GetSystemMetricsForDpi(int index, uint dpi);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr GetWindowDpiAwarenessContext(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int GetAwarenessFromDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromPoint(POINT point, uint flags);
    [DllImport("shcore.dll")] static extern int GetScaleFactorForMonitor(IntPtr monitor, out int scale);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT point);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int size);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int size);
    delegate bool EnumWindow(IntPtr hwnd, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumWindow callback, IntPtr data);
    [DllImport("user32.dll")] static extern bool GetComboBoxInfo(IntPtr hwnd, ref COMBOINFO info);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id, Hook callback, IntPtr module, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr handle, out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool MoveFileEx(string a, string b, uint flags);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] static extern int DwmGetWindowRectangle(IntPtr hwnd, int attribute, out RECT value, int size);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
    internal const int MaximumRecordingEvents = 300;
    internal const int MaximumRecordedWindows = 300;
    internal static bool ValidRecordingEventLimit(int value) { return value >= 1 && value <= MaximumRecordingEvents; }
    internal static bool RecordingPopupCapacity(int active, int total) { return active >= 0 && active < 5 && total >= 0 && total < MaximumRecordedWindows; }
    internal sealed class RecordingPayloadBudget {
        // Reserve the remaining 6 MiB for popup metadata and the response
        // envelope. JSON string sizes are cached by identity without retaining
        // discarded screenshots; repeated key targets still count each copy.
        internal const long MaximumBytes = 10L * 1024 * 1024;
        sealed class SizeValue { internal long Bytes; }
        readonly System.Runtime.CompilerServices.ConditionalWeakTable<string, SizeValue> strings = new System.Runtime.CompilerServices.ConditionalWeakTable<string, SizeValue>();
        internal long Bytes(object value) {
            if (value == null) return 4;
            string text = value as string;
            if (text != null) return strings.GetValue(text, delegate(string key) { return new SizeValue { Bytes = Encoding.UTF8.GetByteCount(Json.Serialize(key)) }; }).Bytes;
            var map = value as IDictionary; long total = 2;
            if (map != null) { foreach (DictionaryEntry pair in map) total += Bytes(Convert.ToString(pair.Key)) + 1 + Bytes(pair.Value) + 1; return total; }
            var sequence = value as IEnumerable;
            if (sequence != null) { foreach (object item in sequence) total += Bytes(item) + 1; return total; }
            return Encoding.UTF8.GetByteCount(Json.Serialize(value));
        }
        internal static void RequireRepair(Dictionary<string, object> item) {
            string program = Str(item, "program_id", ""), window = Str(item, "window_ref", "main");
            string requested = Str(item, "requested_operation", Str(item, "operation", "manual_entry"));
            item.Clear(); item["program_id"] = program; item["window_ref"] = window;
            item["operation"] = "manual_entry"; item["requested_operation"] = requested;
            item["reason"] = item["capture_issue"] = "recording_payload_limit";
            item["verification"] = "manual_required";
        }
        internal bool Bound(List<Dictionary<string, object>> events) {
            // A repaired row contains only bounded program/window/action
            // identifiers and fixed messages. Reserve 2 KiB for every row so
            // retaining an overflow suffix cannot itself cross this budget.
            long ceiling = MaximumBytes - (long)events.Count * 2048;
            long used = 2; bool exceeded = false;
            foreach (var item in events) {
                long bytes = Bytes(item) + 1;
                if (exceeded || used + bytes > ceiling) { exceeded = true; RequireRepair(item); bytes = Bytes(item) + 1; }
                used += bytes;
            }
            return exceeded;
        }
    }
    internal static string Str(Dictionary<string, object> d, string k, string fallback) { object v; return d != null && d.TryGetValue(k, out v) ? Convert.ToString(v) : fallback; }
    internal static int Num(Dictionary<string, object> d, string k, int fallback) { int v; return Int32.TryParse(Str(d, k, ""), out v) ? v : fallback; }
    internal static double Real(Dictionary<string, object> d, string k, double fallback) { object v; return d != null && d.TryGetValue(k, out v) ? Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture) : fallback; }
    internal static Dictionary<string, object> Map(Dictionary<string, object> d, string k) { object v; return d != null && d.TryGetValue(k, out v) ? v as Dictionary<string, object> : null; }
    static bool Nonce(string n) { if (n.Length != 64) return false; foreach (char c in n) if (!Uri.IsHexDigit(c)) return false; return true; }
    static string Read(string path)
    {
        using (FileStream s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
            if (s.Length > 24 * 1024 * 1024) throw new InvalidDataException("request_too_large");
            using (StreamReader r = new StreamReader(s, Encoding.UTF8)) return r.ReadToEnd();
        }
    }
    internal static void Write(string path, Dictionary<string, object> data)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(Json.Serialize(data));
        if (bytes.Length > 16 * 1024 * 1024) throw new InvalidDataException("response_too_large");
        string temp = path + ".tmp";
        try {
            using (FileStream s = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None)) s.Write(bytes, 0, bytes.Length);
            Stopwatch sw = Stopwatch.StartNew();
            while (!MoveFileEx(temp, path, 1)) {
                int code = Marshal.GetLastWin32Error();
                if ((code != 5 && code != 32 && code != 33) || sw.ElapsedMilliseconds >= 500) throw new IOException("ipc_replace_failed");
                Thread.Sleep(20);
            }
        } finally { try { if (File.Exists(temp)) File.Delete(temp); } catch { } }
    }
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 3 || (args[0] != "--pick" && args[0] != "--match" && args[0] != "--record" && args[0] != "--crop")) return 2;
        string nonce = null, response = null;
        try {
            if (!Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            string request = Path.GetFullPath(args[1]); response = Path.GetFullPath(args[2]);
            if (!String.Equals(Path.GetDirectoryName(request), Path.GetDirectoryName(response), StringComparison.OrdinalIgnoreCase)) return 2;
            Dictionary<string, object> data = Json.Deserialize<Dictionary<string, object>>(Read(request));
            string token = Str(data, "nonce", ""); if (!Nonce(token)) return 2; nonce = token;
            if (args[0] == "--match") { Write(response, Match(data)); return 0; }
            if (args[0] == "--crop") { Write(response, Crop(data)); return 0; }
            if (!Environment.UserInteractive) throw new InvalidOperationException("interactive_desktop_unavailable");
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            using (Form form = args[0] == "--pick" ? (Form)new Picker(data, response) : new Recorder(data, response)) Application.Run(form);
            return File.Exists(response) ? 0 : 3;
        } catch (Exception e) {
            if (nonce != null && response != null && !File.Exists(response)) try { Write(response, new Dictionary<string, object> {
                { "nonce", nonce }, { "status", "failed" }, { "code", e is InvalidOperationException ? e.Message : "visual_helper_failed" }, { "error_type", e.GetType().Name } }); } catch { }
            return 2;
        }
    }
    internal sealed class Target
    {
        internal int Pid; internal IntPtr Hwnd; internal string Program, Window, Label, PreflightMessage; internal bool ImageAllowed = true;
        internal Target Owner; internal IntPtr Lifetime; internal bool OwnLifetime, Retired; internal string Title, ClassName; internal uint ThreadId;
        internal int HostedPid; internal IntPtr HostedHwnd, HostedLifetime; internal uint HostedThread;
        internal Target(Dictionary<string, object> d) {
            Pid = Num(d, "pid", 0); Hwnd = new IntPtr(Convert.ToInt64(Str(d, "window_id", "0")));
            Program = Str(d, "program_id", ""); Window = Str(d, "window_ref", "main"); Label = Str(d, "label", "선택한 프로그램");
            ImageAllowed = !d.ContainsKey("image_allowed") || Object.Equals(d["image_allowed"], true); PreflightMessage = Str(d, "preflight_message", "");
            if (Pid <= 0 || Hwnd == IntPtr.Zero || Label.Length > 200 || !Valid()) throw new InvalidOperationException("target_unavailable");
            Lifetime = OpenProcess(0x100000 | 0x1000, false, (uint)Pid); if (Lifetime == IntPtr.Zero) throw new InvalidOperationException("process_identity_unavailable"); OwnLifetime = true;
            uint ignored; ThreadId = GetWindowThreadProcessId(Hwnd, out ignored); Title = WindowText(Hwnd); ClassName = WindowClass(Hwnd);
            var hosted = Map(d, "hosted_target");
            if (hosted != null) try {
                HostedPid = Num(hosted, "app_pid", 0); HostedHwnd = new IntPtr(Convert.ToInt64(Str(hosted, "app_window_id", "0")));
                HostedThread = Convert.ToUInt32(Str(hosted, "app_thread", "0"));
                if (Num(hosted, "pid", 0) != Pid || Convert.ToInt64(Str(hosted, "window_id", "0")) != Hwnd.ToInt64()
                        || Convert.ToUInt32(Str(hosted, "host_thread", "0")) != ThreadId || HostedPid <= 0 || HostedPid == Pid || HostedHwnd == IntPtr.Zero
                        || !ProcessStarted(Lifetime, Convert.ToInt64(Str(hosted, "host_started", "0")))) throw new InvalidOperationException("hosted_target_changed");
                HostedLifetime = OpenProcess(0x100000 | 0x1000, false, (uint)HostedPid);
                if (!ProcessStarted(HostedLifetime, Convert.ToInt64(Str(hosted, "app_started", "0"))) || !Valid()) throw new InvalidOperationException("hosted_target_changed");
            } catch { Release(); throw; }
        }
        internal Target(Target owner, IntPtr hwnd, string reference) {
            Owner = owner; Pid = owner.Pid; Hwnd = hwnd; Program = owner.Program; Window = reference; Lifetime = owner.Lifetime;
            ImageAllowed = owner.ImageAllowed && ImageCoordinatesReady(hwnd); PreflightMessage = owner.PreflightMessage;
            Title = WindowText(hwnd); ClassName = WindowClass(hwnd); Label = (Title.Length > 0 ? Title : ClassName); if (Label.Length > 180) Label = Label.Substring(0, 180);
            uint ignored; ThreadId = GetWindowThreadProcessId(hwnd, out ignored);
        }
        internal bool Valid() { uint pid; try {
            if (Retired || (Lifetime != IntPtr.Zero && WaitForSingleObject(Lifetime, 0) != 258) || !IsWindow(Hwnd) || GetWindowThreadProcessId(Hwnd, out pid) == 0 || pid != (uint)Pid) return false;
            if (ThreadId != 0 && (ThreadId != GetWindowThreadProcessId(Hwnd, out pid) || ClassName != WindowClass(Hwnd))) return false;
            if (HostedHwnd != IntPtr.Zero && !HostedValid()) return false;
            return Owner == null || (Owner.Valid() && RelatedTo(Hwnd, Owner.Hwnd, Pid));
        } catch { return false; } }
        static bool ProcessStarted(IntPtr process, long expected) {
            long created, exited, kernel, user;
            return process != IntPtr.Zero && expected > 0 && GetProcessTimes(process, out created, out exited, out kernel, out user) && created == expected;
        }
        bool HostedValid() {
            uint pid;
            return HostedLifetime != IntPtr.Zero && WaitForSingleObject(HostedLifetime, 0) == 258
                && IsWindow(HostedHwnd) && GetWindowThreadProcessId(HostedHwnd, out pid) == HostedThread && pid == (uint)HostedPid
                && ClassName == "ApplicationFrameWindow" && WindowClass(HostedHwnd) == "Windows.UI.Core.CoreWindow"
                && GetAncestor(HostedHwnd, 2) == Hwnd && IsChild(Hwnd, HostedHwnd);
        }
        internal bool AcceptsWindow(IntPtr window) {
            if (window == IntPtr.Zero || !Valid()) return false;
            uint pid; if (GetWindowThreadProcessId(window, out pid) == 0) return false;
            if (window == Hwnd) return pid == (uint)Pid;
            if (pid == (uint)Pid) return GetAncestor(window, 2) == Hwnd;
            return HostedHwnd != IntPtr.Zero && pid == (uint)HostedPid && GetAncestor(window, 2) == Hwnd
                && (window == HostedHwnd || IsChild(HostedHwnd, window));
        }
        internal bool AcceptsForeground(IntPtr window) {
            if (!Valid()) return false;
            return window == Hwnd || (HostedHwnd != IntPtr.Zero && AcceptsWindow(window))
                || (Owner != null && (window == Owner.Hwnd || RelatedTo(Hwnd, window, Pid)));
        }
        internal bool AcceptsSemanticPid(int pid) { return pid == Pid || HostedHwnd != IntPtr.Zero && pid == HostedPid; }
        internal Rectangle Bounds() { RECT r; if (!Valid() || !GetWindowRect(Hwnd, out r)) throw new InvalidOperationException("target_unavailable"); return Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom); }
        internal Rectangle CaptureBounds() {
            Rectangle raw = Bounds(); RECT visible; Rectangle? frame = null;
            if (DwmGetWindowRectangle(Hwnd, 9, out visible, Marshal.SizeOf(typeof(RECT))) == 0 && visible.Right > visible.Left && visible.Bottom > visible.Top)
                frame = Rectangle.FromLTRB(visible.Left, visible.Top, visible.Right, visible.Bottom);
            var monitors = new List<Rectangle>(); foreach (Screen screen in Screen.AllScreens) monitors.Add(screen.Bounds);
            int border;
            try { uint dpi = GetDpiForWindow(Hwnd); if (dpi == 0) dpi = 96; border = Math.Max(GetSystemMetricsForDpi(32, dpi), GetSystemMetricsForDpi(33, dpi)) + GetSystemMetricsForDpi(92, dpi); }
            catch (EntryPointNotFoundException) { border = Math.Max(GetSystemMetrics(32), GetSystemMetrics(33)) + GetSystemMetrics(92); }
            return ResolveCaptureBounds(raw, frame, IsZoomed(Hwnd), Screen.FromHandle(Hwnd).Bounds, Math.Max(1, border), monitors);
        }
        internal void EnsureWindowReady() {
            if (!Valid()) throw new InvalidOperationException("target_unavailable");
            if (IsIconic(Hwnd)) throw new InvalidOperationException("target_minimized");
            if (!IsWindowVisible(Hwnd)) throw new InvalidOperationException("target_hidden");
            if (!AcceptsForeground(GetForegroundWindow())) throw new InvalidOperationException("target_requires_foreground");
            int cloaked; if (DwmGetWindowAttribute(Hwnd, 14, out cloaked, 4) == 0 && cloaked != 0) throw new InvalidOperationException("target_cloaked");
        }
        internal void EnsureCaptureReady() { EnsureWindowReady(); CaptureBounds(); }
        internal bool Foreground() { try { if (ImageAllowed) EnsureCaptureReady(); else EnsureWindowReady(); return true; } catch { return false; } }
        internal bool Contains(POINT p) { return AcceptsWindow(WindowFromPoint(p)); }
        internal void Release() {
            if (HostedLifetime != IntPtr.Zero) { CloseHandle(HostedLifetime); HostedLifetime = IntPtr.Zero; }
            if (OwnLifetime && Lifetime != IntPtr.Zero) { CloseHandle(Lifetime); Lifetime = IntPtr.Zero; Retired = true; }
        }
    }
    // Popup HWNDs can have another DPI context even inside the same process.
    // Check them at discovery while they exist; a closed popup cannot be
    // re-inspected by the parent when recording ends. No registry is changed.
    static bool ImageCoordinatesReady(IntPtr hwnd) {
        IntPtr previous = IntPtr.Zero;
        try {
            IntPtr context = GetWindowDpiAwarenessContext(hwnd);
            int awareness = GetAwarenessFromDpiAwarenessContext(context);
            if (awareness == 1 || awareness == 2) return true;
            if (awareness != 0) return false;
            previous = SetThreadDpiAwarenessContext(new IntPtr(-4)); if (previous == IntPtr.Zero) return false;
            RECT physical, logical; if (!GetWindowRect(hwnd, out physical)) return false;
            IntPtr monitor = MonitorFromPoint(new POINT { X = physical.Left, Y = physical.Top }, 0);
            if (monitor == IntPtr.Zero) return false;
            foreach (int x in new int[] { physical.Left, physical.Right - 1 }) foreach (int y in new int[] { physical.Top, physical.Bottom - 1 })
                if (MonitorFromPoint(new POINT { X = x, Y = y }, 0) != monitor) return false;
            int scale; if (GetScaleFactorForMonitor(monitor, out scale) != 0 || scale != 100) return false;
            if (SetThreadDpiAwarenessContext(context) == IntPtr.Zero || !GetWindowRect(hwnd, out logical)) return false;
            return physical.Left == logical.Left && physical.Top == logical.Top && physical.Right == logical.Right && physical.Bottom == logical.Bottom;
        } catch (EntryPointNotFoundException) { return false; }
        finally { if (previous != IntPtr.Zero) SetThreadDpiAwarenessContext(previous); }
    }
    // GetWindowRect includes invisible resize borders. In particular a maximized
    // window can start at (-8,-8) although every visible pixel is on a monitor.
    // Keep Bounds() for UIA identities; image pixels and their screen origin use
    // the DWM visible frame instead. Never silently crop ordinary off-screen UI.
    internal static Rectangle ResolveCaptureBounds(Rectangle raw, Rectangle? visible, bool maximized, Rectangle monitor, int border, List<Rectangle> monitors)
    {
        Rectangle bounds = visible.HasValue ? visible.Value : raw;
        if (!visible.HasValue && maximized && bounds.IntersectsWith(monitor)) {
            Rectangle clipped = Rectangle.Intersect(bounds, monitor);
            if (clipped.Left - bounds.Left <= border && clipped.Top - bounds.Top <= border && bounds.Right - clipped.Right <= border && bounds.Bottom - clipped.Bottom <= border)
                bounds = clipped;
        }
        if (bounds.Width < 8 || bounds.Height < 8) throw new InvalidOperationException("target_capture_size_invalid");
        if (bounds.Width > 4096 || bounds.Height > 4096) throw new InvalidOperationException("target_capture_too_large");
        // VirtualScreen is a bounding rectangle, not the union of monitors. A
        // window in a gap between differently arranged displays is not visible.
        var remaining = new List<Rectangle> { bounds };
        foreach (Rectangle screen in monitors) {
            var next = new List<Rectangle>();
            foreach (Rectangle part in remaining) {
                Rectangle overlap = Rectangle.Intersect(part, screen);
                if (overlap.IsEmpty || overlap.Width == 0 || overlap.Height == 0) { next.Add(part); continue; }
                if (overlap.Top > part.Top) next.Add(Rectangle.FromLTRB(part.Left, part.Top, part.Right, overlap.Top));
                if (overlap.Bottom < part.Bottom) next.Add(Rectangle.FromLTRB(part.Left, overlap.Bottom, part.Right, part.Bottom));
                if (overlap.Left > part.Left) next.Add(Rectangle.FromLTRB(part.Left, overlap.Top, overlap.Left, overlap.Bottom));
                if (overlap.Right < part.Right) next.Add(Rectangle.FromLTRB(overlap.Right, overlap.Top, part.Right, overlap.Bottom));
            }
            remaining = next; if (remaining.Count == 0) return bounds;
        }
        throw new InvalidOperationException("target_offscreen");
    }
    static string WindowText(IntPtr hwnd) { var text = new StringBuilder(1025); GetWindowText(hwnd, text, text.Capacity); return text.ToString(); }
    static string WindowClass(IntPtr hwnd) { var text = new StringBuilder(257); if (GetClassName(hwnd, text, text.Capacity) == 0) return ""; return text.ToString(); }
    static bool OwnedBy(IntPtr hwnd, IntPtr owner, int pid) {
        return HasWindowRelationship(hwnd.ToInt64(), owner.ToInt64(), pid,
            delegate(long handle) { uint found; return GetWindowThreadProcessId(new IntPtr(handle), out found) == 0 ? 0 : (int)found; },
            delegate(long handle) { return GetWindow(new IntPtr(handle), 4).ToInt64(); },
            delegate(long handle) { return GetAncestor(new IntPtr(handle), 1).ToInt64(); });
    }
    internal static bool HasWindowRelationship(long hwnd, long owner, int pid,
            Func<long,int> process, Func<long,long> getOwner, Func<long,long> getParent) {
        // Follow actual owner AND parent edges. Jumping straight to GA_ROOT
        // loses intermediate business windows when a popup opens another popup.
        if (hwnd == 0 || owner == 0 || hwnd == owner || pid <= 0) return false;
        var seen = new HashSet<long>(); var pending = new Queue<long>(); pending.Enqueue(hwnd);
        while (pending.Count > 0 && seen.Count < 64) {
            long current = pending.Dequeue(); if (current == 0 || !seen.Add(current) || process(current) != pid) continue;
            if (current == owner) return true;
            pending.Enqueue(getOwner(current)); pending.Enqueue(getParent(current));
        }
        return false;
    }
    static bool RelatedTo(IntPtr hwnd, IntPtr owner, int pid) {
        if (OwnedBy(hwnd, owner, pid)) return true;
        if (WindowClass(hwnd) != "ComboLBox") return false;
        uint listPid, parentPid;
        if (GetWindowThreadProcessId(hwnd, out listPid) == 0 || GetWindowThreadProcessId(owner, out parentPid) == 0 || listPid != (uint)pid || parentPid != (uint)pid) return false;
        bool related = false;
        EnumChildWindows(owner, delegate(IntPtr child, IntPtr ignored) {
            uint childPid; if (GetWindowThreadProcessId(child, out childPid) == 0 || childPid != (uint)pid || GetAncestor(child, 2) != owner) return true;
            COMBOINFO info = new COMBOINFO { Size = Marshal.SizeOf(typeof(COMBOINFO)) };
            if (GetComboBoxInfo(child, ref info) && info.Combo == child && info.List == hwnd) { related = true; return false; }
            return true;
        }, IntPtr.Zero); return related;
    }
    static List<Rectangle> Occluders(Target target, bool excludeLayered = false)
    { return Occluders(target, excludeLayered, false); }
    static List<Rectangle> Occluders(Target target, bool excludeLayered, bool onlyLayered)
    {
        var found = new List<Rectangle>(); IntPtr window = GetWindow(target.Hwnd, 3); int count = 0; Rectangle targetBounds = target.CaptureBounds();
        while (window != IntPtr.Zero) {
            if (++count > 512) throw new InvalidOperationException("window_order_unavailable");
            bool layered = (GetWindowLong(window, -20) & 0x80000) != 0;
            if (IsWindowVisible(window) && !IsIconic(window) && !(excludeLayered && layered) && (!onlyLayered || layered)) {
                int cloaked; RECT r;
                if ((DwmGetWindowAttribute(window, 14, out cloaked, 4) != 0 || cloaked == 0) &&
                    (DwmGetWindowRectangle(window, 9, out r, Marshal.SizeOf(typeof(RECT))) == 0 || GetWindowRect(window, out r))) {
                    Rectangle bounds = Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom); if (bounds.Width > 0 && bounds.Height > 0 && bounds.IntersectsWith(targetBounds)) found.Add(bounds);
                }
            }
            window = GetWindow(window, 3);
        }
        return found;
    }
    static bool Covered(Rectangle region, List<Rectangle> rectangles) { foreach (Rectangle r in rectangles) if (r.IntersectsWith(region)) return true; return false; }
    internal static bool RecordingFrameFullyCovered(Rectangle bounds, List<Rectangle> rectangles) {
        if (bounds.Width <= 0 || bounds.Height <= 0) return false;
        var clipped = new List<Rectangle>(); var xs = new SortedSet<int> { bounds.Left, bounds.Right };
        foreach (Rectangle candidate in rectangles) {
            Rectangle overlap = Rectangle.Intersect(bounds, candidate);
            if (overlap.Width <= 0 || overlap.Height <= 0) continue;
            if (overlap == bounds) return true;
            clipped.Add(overlap); xs.Add(overlap.Left); xs.Add(overlap.Right);
        }
        int previous = bounds.Left;
        foreach (int right in xs) {
            if (right <= previous) continue;
            var intervals = new List<Rectangle>();
            foreach (Rectangle r in clipped) if (r.Left <= previous && r.Right >= right) intervals.Add(r);
            intervals.Sort(delegate(Rectangle a, Rectangle b) { return a.Top.CompareTo(b.Top); });
            int bottom = bounds.Top;
            foreach (Rectangle r in intervals) { if (r.Top > bottom) break; bottom = Math.Max(bottom, r.Bottom); }
            if (bottom < bounds.Bottom) return false;
            previous = right;
        }
        return true;
    }
    static List<Dictionary<string, object>> OcclusionDetails(Target target, bool excludeLayered) {
        var found = new List<Dictionary<string, object>>(); IntPtr hwnd = GetWindow(target.Hwnd, 3); Rectangle targetBounds = target.CaptureBounds(); int walked = 0;
        while (hwnd != IntPtr.Zero && ++walked <= 512 && found.Count < 12) {
            if (IsWindowVisible(hwnd) && !IsIconic(hwnd) && !(excludeLayered && (GetWindowLong(hwnd, -20) & 0x80000) != 0)) {
                int cloaked; RECT raw; uint pid;
                if ((DwmGetWindowAttribute(hwnd, 14, out cloaked, 4) != 0 || cloaked == 0) && GetWindowThreadProcessId(hwnd, out pid) != 0 && GetWindowRect(hwnd, out raw)) {
                    Rectangle bounds = Rectangle.FromLTRB(raw.Left, raw.Top, raw.Right, raw.Bottom);
                    if (bounds.IntersectsWith(targetBounds)) found.Add(new Dictionary<string, object> {
                        { "window_id", hwnd.ToInt64() }, { "pid", pid }, { "class_name", WindowClass(hwnd) },
                        { "kind", pid == Process.GetCurrentProcess().Id ? "recorder_helper" : RelatedTo(hwnd, target.Hwnd, target.Pid) ? "related_popup" : pid == target.Pid ? "same_process_other_window" : "external_window" },
                        { "bounds", new Dictionary<string, object> { { "x", bounds.X }, { "y", bounds.Y }, { "width", bounds.Width }, { "height", bounds.Height } } } });
                }
            }
            hwnd = GetWindow(hwnd, 3);
        }
        return found;
    }
    internal static bool RecordedEditConfirmed(Dictionary<string, object> before, Dictionary<string, object> after, bool replaced) {
        var native = Map(after, "native_target"); var element = Map(native, "element");
        return before != null && after != null && Str(before, "identity", "").Length > 0
            && Str(before, "identity", "before") == Str(after, "identity", "after")
            && Str(before, "protected", "False") != "True" && Str(after, "protected", "False") != "True"
            && EditableTextRole(Str(element, "role", "")) && Str(element, "is_password", "False") != "True"
            && before.ContainsKey("value") && before["value"] is string && after.ContainsKey("value") && after["value"] is string
            && Str(before, "value", "").Length <= 2000 && Str(after, "value", "").Length <= 2000
            && (Str(before, "value", "").Length == 0 || replaced) && Str(before, "value", "") != Str(after, "value", "");
    }
    internal static bool EditableTextRole(string role) { return role == "Edit" || role == "Document"; }
    internal sealed class CapturedFrameInfo {
        internal readonly string Mode; internal readonly DateTime At,ReceivedAt,RequestAt;internal readonly double FutureLeadMilliseconds;internal readonly int CaptureEpoch;
        internal DateTime ObservedAt {get{return RequestAt==DateTime.MinValue?At:RequestAt;}}
        internal DateTime InputAt {get{return ObservedAt<ReceivedAt?ObservedAt:ReceivedAt;}}
        internal CapturedFrameInfo(string mode, DateTime at):this(mode,at,at,0) { }
        internal CapturedFrameInfo(string mode,DateTime at,DateTime receivedAt,double futureLead):this(mode,at,receivedAt,futureLead,DateTime.MinValue,-1){ }
        internal CapturedFrameInfo(string mode,DateTime at,DateTime receivedAt,double futureLead,DateTime requestAt,int epoch) {Mode=mode;At=at;ReceivedAt=receivedAt;FutureLeadMilliseconds=futureLead;RequestAt=requestAt;CaptureEpoch=epoch;}
    }
    internal sealed class CaptureChangedException : InvalidOperationException {
        internal readonly string Reason;internal readonly double OffsetMilliseconds;
        internal CaptureChangedException(string reason):this(reason,0){ }
        internal CaptureChangedException(string reason,double offset):base("target_changed"){Reason=reason;OffsetMilliseconds=offset;}
    }
    internal static DateTime CaptureTimeFromQpc(long frameTicks, long nowCounter, long frequency, DateTime utcNow) {
        return CaptureTimeFromQpc(frameTicks,nowCounter,frequency,utcNow,false);
    }
    static DateTime CaptureTimeFromQpc(long frameTicks,long nowCounter,long frequency,DateTime utcNow,bool firstRequestFrame) {
        if (frameTicks < 0 || nowCounter < 0 || frequency <= 0) throw new CaptureChangedException("capture_time_invalid");
        long nowTicks = (long)((double)nowCounter * TimeSpan.TicksPerSecond / frequency);
        long age = nowTicks - frameTicks;
        if(age<0)throw new CaptureChangedException("capture_time_future",-(double)age/TimeSpan.TicksPerMillisecond);
        if(age>utcNow.Ticks||(!firstRequestFrame&&age>2*TimeSpan.TicksPerSecond))throw new CaptureChangedException("capture_time_stale");
        return utcNow.Subtract(TimeSpan.FromTicks(age));
    }
    internal static DateTime AwaitCaptureTime(long frameTicks,long frequency,Func<long> counter,Func<DateTime> utcNow,Action<int> delay,out double futureLead) {
        return AwaitCaptureTime(frameTicks,frequency,counter,utcNow,delay,false,out futureLead);
    }
    internal static DateTime AwaitCaptureTime(long frameTicks,long frequency,Func<long> counter,Func<DateTime> utcNow,Action<int> delay,bool firstRequestFrame,out double futureLead) {
        futureLead=0;Stopwatch budget=Stopwatch.StartNew();
        for(int attempt=0;attempt<64;attempt++) {
            long nowCounter=counter();
            try{return CaptureTimeFromQpc(frameTicks,nowCounter,frequency,utcNow(),firstRequestFrame);}
            catch(CaptureChangedException error) {
                if(error.Reason!="capture_time_future")throw;
                futureLead=Math.Max(futureLead,error.OffsetMilliseconds);
                // DWM may label a delivered frame with its upcoming display
                // time. Wait for that real QPC time; never clamp/relabel it.
                if(futureLead>50||budget.ElapsedMilliseconds>=75)throw;
                delay(1);
            }
        }
        throw new CaptureChangedException("capture_time_future",futureLead);
    }
    internal static bool CapturedFrameFresh(DateTime capturedAt, DateTime now) {
        double age = now.Subtract(capturedAt).TotalMilliseconds;
        return capturedAt != DateTime.MinValue && age >= 0 && age <= 250;
    }
    internal static bool CapturedFrameAfterInput(DateTime capturedAt, DateTime inputAt) { return capturedAt >= inputAt; }
    internal static bool FirstCaptureFrame(ref bool pending,bool hasFrame) {
        if(!hasFrame||!pending)return false;pending=false;return true;
    }
    internal static bool CapturedObservationUsable(CapturedFrameInfo frame,DateTime now,DateTime inputAt,int epoch) {
        return frame!=null&&CapturedFrameFresh(frame.ObservedAt,now)&&CapturedFrameAfterInput(frame.InputAt,inputAt)
            &&(frame.CaptureEpoch<0||frame.CaptureEpoch==epoch);
    }
    internal static bool RecordingFrameFresh(CapturedFrameInfo frame,DateTime observedAt,DateTime now) {
        // A recorder keeps its last independently requested first-frame snapshot
        // while the next WGC request is in flight. Input epochs and acquisition
        // bounds still invalidate it immediately after any recorded input.
        // Ordinary captures and frames without this provenance retain 250 ms.
        if(frame==null||frame.Mode!="windows_graphics_capture"||frame.RequestAt==DateTime.MinValue||frame.CaptureEpoch<0)
            return CapturedFrameFresh(observedAt,now);
        double age=now.Subtract(frame.RequestAt).TotalMilliseconds;
        return observedAt==frame.RequestAt&&age>=0&&age<=1000;
    }
    internal static bool RecordingObservationUsable(CapturedFrameInfo frame,DateTime now,DateTime inputAt,int epoch) {
        return frame!=null&&RecordingFrameFresh(frame,frame.ObservedAt,now)&&CapturedFrameAfterInput(frame.InputAt,inputAt)
            &&(frame.CaptureEpoch<0||frame.CaptureEpoch==epoch);
    }
    internal static string RecordingFrameIssue(bool hasFrame, bool sameTarget, DateTime frameAt, DateTime now, DateTime inputAt, bool sameBounds, bool pointInside) {
        return RecordingFrameIssue(hasFrame,sameTarget,frameAt,now,inputAt,sameBounds,pointInside,null);
    }
    internal static string RecordingFrameIssue(bool hasFrame, bool sameTarget, DateTime frameAt, DateTime now, DateTime inputAt, bool sameBounds, bool pointInside,CapturedFrameInfo capture) {
        if (!hasFrame) return "frame_missing";
        if (!sameTarget) return "frame_target_changed";
        if (!RecordingFrameFresh(capture,frameAt,now)) return "frame_stale";
        if (!CapturedFrameAfterInput(frameAt,inputAt)) return "frame_predates_input";
        if (!sameBounds) return "frame_geometry_changed";
        return pointInside ? null : "point_outside_target";
    }
    // Windows Graphics Capture's HWND interop captures the window's own GPU
    // surface. No desktop duplication, overlay exemptions, SDK installation,
    // WinRT projection package, or capture-border suppression is used.
    internal static class GraphicsWindowCapture {
        [StructLayout(LayoutKind.Sequential)] internal struct Size32 { internal int Width, Height; }
        [StructLayout(LayoutKind.Sequential)] struct Sample { internal uint Count, Quality; }
        [StructLayout(LayoutKind.Sequential)] struct TextureDescription {
            internal uint Width, Height, MipLevels, ArraySize, Format; internal Sample Sample;
            internal uint Usage, BindFlags, CPUAccessFlags, MiscFlags;
        }
        [StructLayout(LayoutKind.Sequential)] struct Mapped { internal IntPtr Data; internal uint RowPitch, DepthPitch; }
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int GetPointer(IntPtr self, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int GetSize(IntPtr self, out Size32 result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int GetFrameTime(IntPtr self, out long result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int CreateItem(IntPtr self, IntPtr hwnd, ref Guid iid, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int CreatePool(IntPtr self, IntPtr device, int format, int buffers, Size32 size, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int CreateSession(IntPtr self, IntPtr item, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int SimpleCall(IntPtr self);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int SetBoolean(IntPtr self, byte value);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int GetInterface(IntPtr self, ref Guid iid, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void GetTextureDescription(IntPtr self, out TextureDescription description);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int CreateTexture(IntPtr self, ref TextureDescription description, IntPtr initialData, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void CopyTexture(IntPtr self, IntPtr destination, IntPtr source);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void VoidCall(IntPtr self);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int MapTexture(IntPtr self, IntPtr resource, uint subresource, uint type, uint flags, out Mapped mapped);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void UnmapTexture(IntPtr self, IntPtr resource, uint subresource);
        [DllImport("combase.dll")] static extern int RoInitialize(uint type);
        [DllImport("combase.dll")] static extern void RoUninitialize();
        [DllImport("combase.dll", CharSet=CharSet.Unicode)] static extern int WindowsCreateString(string value, int length, out IntPtr result);
        [DllImport("combase.dll")] static extern int WindowsDeleteString(IntPtr value);
        [DllImport("combase.dll")] static extern int RoGetActivationFactory(IntPtr name, ref Guid iid, out IntPtr result);
        [DllImport("d3d11.dll")] static extern int D3D11CreateDevice(IntPtr adapter, uint driverType, IntPtr software, uint flags, IntPtr levels, uint levelCount, uint sdkVersion, out IntPtr device, out uint level, out IntPtr context);
        [DllImport("d3d11.dll")] static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr dxgi, out IntPtr device);
        static T Method<T>(IntPtr instance, int slot) where T : class {
            if (instance == IntPtr.Zero) throw new InvalidOperationException("target_render_unavailable");
            return (T)(object)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(instance), slot * IntPtr.Size), typeof(T));
        }
        static void Check(int result) { if (result < 0) Marshal.ThrowExceptionForHR(result); }
        static IntPtr Query(IntPtr source, string iid) { IntPtr result; Guid id = new Guid(iid); Check(Marshal.QueryInterface(source, ref id, out result)); return result; }
        static void Release(ref IntPtr value) { if (value != IntPtr.Zero) { Marshal.Release(value); value = IntPtr.Zero; } }
        static void Close(ref IntPtr value) {
            if (value == IntPtr.Zero) return; IntPtr closable = IntPtr.Zero;
            try { closable = Query(value, "30d5a829-7fa4-4026-83bb-d75bae4ea99e"); Check(Method<SimpleCall>(closable, 6)(closable)); }
            catch { } finally { Release(ref closable); Release(ref value); }
        }
        static IntPtr Factory(string name, string iid) {
            IntPtr text = IntPtr.Zero, result; Guid id = new Guid(iid);
            try { Check(WindowsCreateString(name, name.Length, out text)); Check(RoGetActivationFactory(text, ref id, out result)); return result; }
            finally { if (text != IntPtr.Zero) WindowsDeleteString(text); }
        }
        static void OptionalFalse(IntPtr session, string iid, int slot) {
            IntPtr optional = IntPtr.Zero; Guid id = new Guid(iid);
            try { if (Marshal.QueryInterface(session, ref id, out optional) >= 0) Check(Method<SetBoolean>(optional, slot)(optional, 0)); }
            finally { Release(ref optional); }
        }
        internal static Rectangle SourceBounds(Size size, Rectangle raw, Rectangle visible) {
            // WGC uses either the complete window surface or its DWM frame.
            // Dimensions are evidence, never an invitation to scale pixels.
            if (size.Width < 8 || size.Height < 8 || size.Width > 4128 || size.Height > 4128 || !raw.Contains(visible))
                throw new InvalidOperationException("target_capture_size_invalid");
            if (size == visible.Size) return visible;
            if (size == raw.Size) return raw;
            throw new InvalidOperationException("target_capture_size_invalid");
        }
        internal static Bitmap ReadRows(IntPtr data, uint pitch, Size size) {
            if (data == IntPtr.Zero || size.Width < 8 || size.Height < 8 || size.Width > 4128 || size.Height > 4128
                    || pitch < (uint)size.Width * 4 || pitch > 65536) throw new InvalidOperationException("target_capture_size_invalid");
            Bitmap image = new Bitmap(size.Width, size.Height, PixelFormat.Format32bppRgb); BitmapData locked = null;
            try {
                locked = image.LockBits(new Rectangle(Point.Empty, size), ImageLockMode.WriteOnly, PixelFormat.Format32bppRgb);
                byte[] row = new byte[size.Width * 4];
                for (int y = 0; y < size.Height; y++) {
                    Marshal.Copy(IntPtr.Add(data, checked(y * (int)pitch)), row, 0, row.Length);
                    for (int x = 3; x < row.Length; x += 4) row[x] = 255;
                    Marshal.Copy(row, 0, IntPtr.Add(locked.Scan0, y * locked.Stride), row.Length);
                }
                image.UnlockBits(locked); locked = null; Bitmap result = image; image = null; return result;
            } finally { if (locked != null) image.UnlockBits(locked); if (image != null) image.Dispose(); }
        }
        // The recorder owns one session for its whole active target. Device,
        // frame pool and staging allocation stay on the same MTA worker.
        internal sealed class Session : IDisposable {
            IntPtr itemFactory=IntPtr.Zero,poolFactory=IntPtr.Zero,item=IntPtr.Zero,device=IntPtr.Zero,context=IntPtr.Zero;
            IntPtr dxgi=IntPtr.Zero,projected=IntPtr.Zero,winDevice=IntPtr.Zero,pool=IntPtr.Zero,session=IntPtr.Zero,staging=IntPtr.Zero;
            bool initialized; readonly Action ensure; readonly Action<string,int> report; readonly Rectangle visible,sourceBounds;
            readonly Size32 itemSize; string stage="initialize";DateTime requestAt;bool firstPending;
            internal int Epoch {get;private set;}
            internal bool FirstConsumed {get{return !firstPending;}}
            internal Session(Target target, Rectangle raw, Rectangle visible, Action<string,int> report, Action ensure,int epoch) {
                this.report=report;this.ensure=ensure;this.visible=visible;
                try {
                report(stage, 0);
                Check(RoInitialize(1)); initialized=true;
                ensure();
                stage="create_item"; report(stage,0);
                itemFactory=Factory("Windows.Graphics.Capture.GraphicsCaptureItem","3628e81b-3cac-4c60-b7f4-23ce0e0c3356");
                Guid itemId=new Guid("79c3f95b-31f7-4ec2-a464-632ef5d30760");
                Check(Method<CreateItem>(itemFactory,3)(itemFactory,target.Hwnd,ref itemId,out item));
                Size32 size; Check(Method<GetSize>(item,7)(item,out size)); itemSize=size;
                sourceBounds=SourceBounds(new Size(itemSize.Width,itemSize.Height),raw,visible);
                stage="create_device"; report(stage,0);
                uint level; Check(D3D11CreateDevice(IntPtr.Zero,1,IntPtr.Zero,0x20,IntPtr.Zero,0,7,out device,out level,out context));
                dxgi=Query(device,"54ec77fa-1377-44e6-8c32-88fd5f44c84c");
                Check(CreateDirect3D11DeviceFromDXGIDevice(dxgi,out projected));
                winDevice=Query(projected,"a37624ab-8d5f-4650-9d3e-9eae3d9bc670");
                poolFactory=Factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool","589b103f-6bbc-5df5-a991-02e28b3b66d5");
                Restart(epoch);
                } catch(COMException error) { report(stage,error.ErrorCode);Dispose();throw new InvalidOperationException("target_render_unavailable"); }
                catch(DllNotFoundException) { Dispose();throw new InvalidOperationException("target_render_unavailable"); }
                catch(EntryPointNotFoundException) { Dispose();throw new InvalidOperationException("target_render_unavailable"); }
                catch { Dispose();throw; }
            }
            internal void Restart(int epoch) {
                try {
                ensure();Close(ref session);Close(ref pool);
                stage="create_pool";report(stage,0);
                Check(Method<CreatePool>(poolFactory,6)(poolFactory,winDevice,87,2,itemSize,out pool));
                Check(Method<CreateSession>(pool,10)(pool,item,out session));
                stage="configure_session"; report(stage,0);
                // A real cursor is not target content. Keep the system capture
                // border and exclude secondary windows when the OS supports it.
                OptionalFalse(session,"2c39ae40-7d2e-5044-804e-8b6799d4cf9e",7);
                OptionalFalse(session,"d7419236-be20-5e9f-bcd6-c4e98fd6afdc",7);
                stage="start_capture"; report(stage,0);
                // New pool has no frames from the previous session. Fix the
                // request/epoch before StartCapture; only its first actual
                // dequeued frame may carry this observation provenance.
                requestAt=DateTime.UtcNow;Epoch=epoch;firstPending=true;
                Check(Method<SimpleCall>(session,6)(session));
                } catch(COMException error) {report(stage,error.ErrorCode);throw new InvalidOperationException("target_render_unavailable");}
            }
            internal Bitmap TryRead() {
                IntPtr frame=IntPtr.Zero,surface=IntPtr.Zero,access=IntPtr.Zero,texture=IntPtr.Zero;bool mapped=false;
                try {
                ensure();
                stage="wait_frame"; report(stage,0);
                // Conservatively precede the first dequeue, including any
                // drain to a newer frame. An input during dequeue/wait must
                // not promote these pixels into the next input epoch.
                DateTime receivedAt=DateTime.UtcNow;
                Check(Method<GetPointer>(pool,7)(pool,out frame));
                bool first=FirstCaptureFrame(ref firstPending,frame!=IntPtr.Zero);
                if(!first)return null;
                // Drain the bounded two-frame pool: copying an older frame
                // while a newer one is ready needlessly consumes freshness.
                for(int i=0;i<2;i++) {
                    IntPtr newer=IntPtr.Zero;Check(Method<GetPointer>(pool,7)(pool,out newer));
                    if(newer==IntPtr.Zero)break;Close(ref frame);frame=newer;
                }
                long frameTicks; Check(Method<GetFrameTime>(frame,7)(frame,out frameTicks));
                double futureLead;
                DateTime capturedAt=AwaitCaptureTime(frameTicks,Stopwatch.Frequency,Stopwatch.GetTimestamp,delegate{return DateTime.UtcNow;},delegate(int milliseconds){ensure();Thread.Sleep(milliseconds);},true,out futureLead);
                stage="frame_geometry"; report(stage,0);
                Size32 content; Check(Method<GetSize>(frame,8)(frame,out content));
                if (content.Width!=itemSize.Width || content.Height!=itemSize.Height) throw new CaptureChangedException("capture_content_size_changed");
                Check(Method<GetPointer>(frame,6)(frame,out surface));
                stage="surface_access"; report(stage,0);
                access=Query(surface,"a9b3d012-3df2-4ee3-b8d1-8695f457d3c1");
                Guid textureId=new Guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c");
                Check(Method<GetInterface>(access,3)(access,ref textureId,out texture));
                TextureDescription description; Method<GetTextureDescription>(texture,10)(texture,out description);
                if (description.Width!=(uint)content.Width || description.Height!=(uint)content.Height || description.Format!=87
                        || description.MipLevels!=1 || description.ArraySize!=1 || description.Sample.Count!=1)
                    throw new InvalidOperationException("target_capture_size_invalid");
                description.Usage=3; description.BindFlags=0; description.CPUAccessFlags=0x20000; description.MiscFlags=0;
                stage="copy_texture"; report(stage,0);
                if(staging==IntPtr.Zero)Check(Method<CreateTexture>(device,5)(device,ref description,IntPtr.Zero,out staging));
                Method<CopyTexture>(context,47)(context,staging,texture);
                Method<VoidCall>(context,111)(context);
                stage="map_texture"; report(stage,0);
                Mapped pixels=new Mapped(); int status;Stopwatch mapDeadline=Stopwatch.StartNew();
                do {
                    status=Method<MapTexture>(context,14)(context,staging,0,1,0x100000,out pixels);
                    if (status==unchecked((int)0x887A000A)) Thread.Sleep(2);
                    else break;
                } while (mapDeadline.ElapsedMilliseconds<50);
                if (status==unchecked((int)0x887A000A)) throw new InvalidOperationException("target_render_waiting");
                Check(status); mapped=true;
                stage="read_pixels"; report(stage,0);
                using (Bitmap full=ReadRows(pixels.Data,pixels.RowPitch,new Size(content.Width,content.Height))) {
                    ensure();
                    Bitmap result=ValidateRenderedFrame(full,sourceBounds,visible);
                    result.Tag=new CapturedFrameInfo("windows_graphics_capture",capturedAt,receivedAt,futureLead,requestAt,Epoch); report("complete",0); return result;
                }
            } catch (COMException error) { report(stage,error.ErrorCode); throw new InvalidOperationException("target_render_unavailable"); }
            finally {
                if (mapped) Method<UnmapTexture>(context,15)(context,staging,0);
                Release(ref texture); Release(ref access); Release(ref surface); Close(ref frame);
            }
            }
            public void Dispose() {
                Release(ref staging);
                Close(ref session); Close(ref pool); Release(ref item); Release(ref poolFactory); Release(ref itemFactory);
                Release(ref winDevice); Release(ref projected); Release(ref dxgi); Release(ref context); Release(ref device);
                if (initialized) { RoUninitialize();initialized=false; }
            }
        }
        internal static Bitmap Capture(Target target,Rectangle raw,Rectangle visible,Action<string,int> report) {
            Action ensure=delegate {target.EnsureCaptureReady();if(target.Bounds()!=raw||target.CaptureBounds()!=visible)throw new InvalidOperationException("target_changed");};
            using(var session=new Session(target,raw,visible,report,ensure,-1)) {
                Stopwatch deadline=Stopwatch.StartNew();
                do {Bitmap image=session.TryRead();if(image!=null)return image;Thread.Sleep(5);}while(deadline.ElapsedMilliseconds<240);
                throw new InvalidOperationException("target_render_waiting");
            }
        }
    }
    // Only the latest immutable frame is retained. Readers lease its pixels
    // under a short lock, then clone outside that lock; publication and Stop
    // never wait for a full-window bitmap copy or GPU work.
    internal sealed class CapturePixels {
        internal readonly Bitmap Image; int references=1; internal static long RetainedBytes;
        internal CapturePixels(Bitmap image) { Image=image;Interlocked.Add(ref RetainedBytes,(long)image.Width*image.Height*4); }
        internal CapturePixels Retain() { Interlocked.Increment(ref references);return this; }
        internal void Release() { if(Interlocked.Decrement(ref references)==0){Interlocked.Add(ref RetainedBytes,-(long)Image.Width*Image.Height*4);Image.Dispose();} }
    }
    internal sealed class LatestCapture : IDisposable {
        readonly object gate=new object();CapturePixels latest;bool stopped;
        internal bool Publish(Bitmap image) {
            CapturePixels next=new CapturePixels(image),old;
            lock(gate) {if(stopped){next.Release();return false;}old=latest;latest=next;}
            if(old!=null)old.Release();return true;
        }
        internal CapturePixels Lease() { lock(gate){return stopped||latest==null?null:latest.Retain();} }
        internal void Clear() {CapturePixels old;lock(gate){old=latest;latest=null;}if(old!=null)old.Release();}
        public void Dispose() { CapturePixels old;lock(gate){stopped=true;old=latest;latest=null;}if(old!=null)old.Release(); }
    }
    sealed class CapturePump {
        internal readonly Target Owner;internal readonly Rectangle Raw,Visible;
        readonly object scopeGate=new object();readonly LatestCapture latest=new LatestCapture();
        volatile bool stopping;internal volatile bool Finished;internal volatile string Stage="initialize";internal volatile int HResult;
        internal Exception Error;internal static int Active;
        internal volatile int FrameCount,EmptyPolls,RefreshCount;long latestTicks,observedTicks,renderedTicks;int requestedEpoch;
        internal readonly DateTime Started=DateTime.UtcNow;internal readonly int Generation;static int nextGeneration;
        internal readonly string PreviousFailure;internal readonly double PreviousFutureLead;internal volatile int ReceivedCount,LastReadMilliseconds;long receivedTicks;
        internal CapturePump(Target owner,Rectangle raw,Rectangle visible,string previousFailure,double previousFutureLead,int epoch) {
            Owner=owner;Raw=raw;Visible=visible;
            requestedEpoch=epoch;
            Generation=Interlocked.Increment(ref nextGeneration);PreviousFailure=previousFailure??"none";PreviousFutureLead=previousFutureLead;
            lock(renderLock) {
                int running=0;foreach(RenderJob job in renderJobs.Values)if(!job.Completed.WaitOne(0))running++;
                if(Active+running>=4)throw new InvalidOperationException("target_capture_busy");
                Active++;capturePumps[owner]=this;
            }
            Thread worker=new Thread(Run);worker.IsBackground=true;worker.SetApartmentState(ApartmentState.MTA);
            try {worker.Start();}catch {Stop();Finished=true;lock(renderLock){Active--;}throw;}
        }
        void Ensure() {
            // Stop synchronizes only with these bounded identity checks. Once
            // it returns, the worker cannot touch Target's released handles.
            lock(scopeGate) {
                if(stopping)throw new InvalidOperationException("target_unavailable");
                Owner.EnsureCaptureReady();
                if(Owner.Bounds()!=Raw)throw new CaptureChangedException("capture_raw_bounds_changed");
                if(Owner.CaptureBounds()!=Visible)throw new CaptureChangedException("capture_visible_bounds_changed");
            }
        }
        void Run() {
            try {
                using(var session=new GraphicsWindowCapture.Session(Owner,Raw,Visible,delegate(string stage,int status){Stage=stage;HResult=status;},Ensure,Interlocked.CompareExchange(ref requestedEpoch,0,0))) {
                    DateTime lastRequest=DateTime.UtcNow;bool awaitingRefresh=false;
                    while(!stopping) {
                        Bitmap image=null;
                        try {
                            int wanted=Interlocked.CompareExchange(ref requestedEpoch,0,0);
                            if(session.Epoch!=wanted){latest.Clear();session.Restart(wanted);lastRequest=DateTime.UtcNow;awaitingRefresh=true;if(RefreshCount<Int32.MaxValue)RefreshCount++;}
                            Stopwatch read=Stopwatch.StartNew();image=session.TryRead();LastReadMilliseconds=(int)Math.Min(60000,read.ElapsedMilliseconds);
                            if(image==null){
                                if(EmptyPolls<Int32.MaxValue)EmptyPolls++;
                                // WGC may stop emitting frames when the scene
                                // is static. A live poll is NOT fresh pixels.
                                // Request a new real capture, retaining the
                                // device/staging and the last immutable frame.
                                DateTime now=DateTime.UtcNow;long ticks=Interlocked.Read(ref observedTicks);
                                if(RecordingCaptureRefreshDue(ticks==0?DateTime.MinValue:new DateTime(ticks,DateTimeKind.Utc),lastRequest,now,awaitingRefresh)
                                        ||ticks==0&&session.FirstConsumed&&now.Subtract(lastRequest).TotalMilliseconds>=120) {
                                    session.Restart(Interlocked.CompareExchange(ref requestedEpoch,0,0));lastRequest=DateTime.UtcNow;awaitingRefresh=true;
                                    if(RefreshCount<Int32.MaxValue)RefreshCount++;
                                }
                                Thread.Sleep(5);continue;
                            }
                            Ensure();
                            CapturedFrameInfo source=image.Tag as CapturedFrameInfo;
                            if(source!=null){Interlocked.Exchange(ref receivedTicks,source.ReceivedAt.Ticks);Interlocked.Exchange(ref renderedTicks,source.At.Ticks);if(ReceivedCount<Int32.MaxValue)ReceivedCount++;}
                            if(!RecordingObservationUsable(source,DateTime.UtcNow,DateTime.MinValue,Interlocked.CompareExchange(ref requestedEpoch,0,0))){image.Dispose();image=null;continue;}
                            Interlocked.Exchange(ref latestTicks,source.At.Ticks);Interlocked.Exchange(ref observedTicks,source.ObservedAt.Ticks);if(FrameCount<Int32.MaxValue)FrameCount++;awaitingRefresh=false;
                            Bitmap publish=image;image=null;latest.Publish(publish);
                        } catch(InvalidOperationException error) {
                            if(error.Message!="target_render_waiting")throw;
                            Thread.Sleep(5);
                        } finally {if(image!=null)image.Dispose();}
                    }
                }
            } catch(Exception error) {Error=error;}
            finally {latest.Dispose();Finished=true;lock(renderLock){Active--;}}
        }
        internal bool Matches(Target owner,Rectangle raw,Rectangle visible) {return !stopping&&Owner==owner&&Raw==raw&&Visible==visible;}
        internal void RequestEpoch(int epoch) {Interlocked.Exchange(ref requestedEpoch,epoch);}
        internal Bitmap Snapshot(Target owner,Rectangle raw,Rectangle visible,DateTime notBefore) {
            if(!Matches(owner,raw,visible))throw new InvalidOperationException("target_changed");
            Exception error=Error;if(error!=null)throw new InvalidOperationException(error is InvalidOperationException?error.Message:"target_render_unavailable");
            CapturePixels lease=latest.Lease();if(lease==null)throw new InvalidOperationException("target_render_waiting");
            try {
                CapturedFrameInfo source=lease.Image.Tag as CapturedFrameInfo;
                if(!RecordingObservationUsable(source,DateTime.UtcNow,notBefore,Interlocked.CompareExchange(ref requestedEpoch,0,0)))throw new InvalidOperationException("target_render_waiting");
                Bitmap copy=(Bitmap)lease.Image.Clone();copy.Tag=source;
                try {Ensure();Bitmap result=copy;copy=null;return result;}finally{if(copy!=null)copy.Dispose();}
            } finally {lease.Release();}
        }
        internal void Stop() {
            lock(scopeGate){stopping=true;}latest.Dispose();
            lock(renderLock){CapturePump existing;if(capturePumps.TryGetValue(Owner,out existing)&&existing==this)capturePumps.Remove(Owner);}
        }
        internal double LatestContentAge() {long ticks=Interlocked.Read(ref latestTicks);return ticks==0?-1:Math.Round(Math.Max(-1,Math.Min(60000,DateTime.UtcNow.Subtract(new DateTime(ticks,DateTimeKind.Utc)).TotalMilliseconds)));}
        internal void AppendLifecycle(Dictionary<string,object> diagnostic) {
            long ticks=Interlocked.Read(ref receivedTicks);
            diagnostic["capture_pump_generation"]=Generation;
            diagnostic["capture_pump_age_ms"]=Math.Round(Math.Max(0,Math.Min(60000,DateTime.UtcNow.Subtract(Started).TotalMilliseconds)));
            diagnostic["capture_previous_failure"]=PreviousFailure;
            diagnostic["capture_previous_future_lead_ms"]=Math.Round(Math.Max(0,Math.Min(60000,PreviousFutureLead)),3);
            diagnostic["capture_received_frame_count"]=ReceivedCount;
            diagnostic["capture_last_received_age_ms"]=ticks==0?-1:Math.Round(Math.Max(-1,Math.Min(60000,DateTime.UtcNow.Subtract(new DateTime(ticks,DateTimeKind.Utc)).TotalMilliseconds)));
            long rendered=Interlocked.Read(ref renderedTicks);diagnostic["capture_last_render_age_ms"]=rendered==0?-1:Math.Round(Math.Max(-1,Math.Min(60000,DateTime.UtcNow.Subtract(new DateTime(rendered,DateTimeKind.Utc)).TotalMilliseconds)));
            diagnostic["capture_read_duration_ms"]=LastReadMilliseconds;
            diagnostic["capture_requested_epoch"]=Interlocked.CompareExchange(ref requestedEpoch,0,0);
            CapturePixels current=latest.Lease();
            try {
                var captured=current==null?null:current.Image.Tag as CapturedFrameInfo;
                diagnostic["capture_latest_frame_epoch"]=captured==null?-1:captured.CaptureEpoch;
                diagnostic["capture_latest_request_age_ms"]=captured==null||captured.RequestAt==DateTime.MinValue?-1:
                    Math.Round(Math.Max(-1,Math.Min(60000,DateTime.UtcNow.Subtract(captured.RequestAt).TotalMilliseconds)));
            } finally {if(current!=null)current.Release();}
        }
    }
    internal static bool RecordingCaptureRefreshDue(DateTime contentAt,DateTime lastRequest,DateTime now,bool awaiting) {
        if(contentAt==DateTime.MinValue||contentAt>now||lastRequest>now)return false;
        return now.Subtract(contentAt).TotalMilliseconds>=120&&now.Subtract(lastRequest).TotalMilliseconds>=(awaiting?500:120);
    }
    static readonly Dictionary<Target,CapturePump> capturePumps=new Dictionary<Target,CapturePump>();
    sealed class RenderJob {
        internal DateTime Started; internal Target Owner; internal Rectangle Raw, Visible; internal Bitmap Image; internal Exception Error;
        internal string GraphicsStage; internal int GraphicsHResult;
        internal readonly ManualResetEvent Completed = new ManualResetEvent(false);
    }
    static readonly object renderLock = new object();
    static readonly Dictionary<IntPtr, RenderJob> renderJobs = new Dictionary<IntPtr, RenderJob>();
    static bool HasOwnedRenderJob(Target target) {
        lock(renderLock) { RenderJob job; return renderJobs.TryGetValue(target.Hwnd,out job) && job.Owner==target; }
    }
    static bool RenderJobFresh(RenderJob job, DateTime now) {
        CapturedFrameInfo capture=job.Image==null ? null : job.Image.Tag as CapturedFrameInfo;
        return capture!=null && CapturedFrameFresh(capture.ObservedAt,now);
    }
    // PrintWindow can hang inside an application's renderer. Never call it on
    // the input-hook/UI thread; keep at most four outstanding target workers.
    // We render the target's pixels rather than ignoring overlay rectangles.
    static Bitmap RenderWindow(Target target, Rectangle visible) { return RenderWindow(target,visible,DateTime.MinValue); }
    static Bitmap RenderWindow(Target target, Rectangle visible, DateTime notBefore) {
        RenderJob job; Rectangle raw = target.Bounds();
        if (raw.Width < 8 || raw.Height < 8 || raw.Width > 4128 || raw.Height > 4128 || !raw.Contains(visible)) throw new InvalidOperationException("target_capture_size_invalid");
        lock (renderLock) {
            if (renderJobs.TryGetValue(target.Hwnd, out job) && job.Completed.WaitOne(0)
                && ((job.Image!=null ? !RenderJobFresh(job,DateTime.UtcNow) || !CapturedFrameAfterInput(((CapturedFrameInfo)job.Image.Tag).InputAt,notBefore) : DateTime.UtcNow.Subtract(job.Started).TotalMilliseconds > 250)
                    || job.Raw != raw || job.Visible != visible || job.Owner != target)) {
                if (job.Image != null) job.Image.Dispose(); job.Completed.Dispose(); renderJobs.Remove(target.Hwnd); job = null;
            }
            if (job == null) {
                // Reclaim completed captures belonging to other targets, but
                // never create another worker for an outstanding same HWND.
                var expired = new List<IntPtr>(); foreach (var pair in renderJobs) if (pair.Value.Completed.WaitOne(0)) expired.Add(pair.Key);
                foreach (IntPtr key in expired) { var previous = renderJobs[key]; if (previous.Image != null) previous.Image.Dispose(); previous.Completed.Dispose(); renderJobs.Remove(key); }
                if (renderJobs.Count + CapturePump.Active >= 4) throw new InvalidOperationException("target_capture_busy");
                job = new RenderJob { Started = DateTime.UtcNow, Owner = target, Raw = raw, Visible = visible }; renderJobs[target.Hwnd] = job; RenderJob owned = job;
                Thread worker = new Thread(delegate() {
                    try {
                        try { owned.Image = GraphicsWindowCapture.Capture(target, raw, visible, delegate(string stage, int code) { owned.GraphicsStage=stage; owned.GraphicsHResult=code; }); return; }
                        catch (InvalidOperationException unavailable) {
                            // Preserve source/identity failures. Only unavailable
                            // WGC permits the older target-only PrintWindow path.
                            if (unavailable.Message != "target_render_unavailable" && unavailable.Message != "target_render_incomplete") throw;
                        }
                        using (Bitmap full = new Bitmap(raw.Width, raw.Height, PixelFormat.Format32bppRgb)) {
                            DateTime printStarted=DateTime.UtcNow;
                            using (Graphics graphics = Graphics.FromImage(full)) { graphics.Clear(Color.Magenta); IntPtr dc = graphics.GetHdc(); bool rendered;
                                try { rendered = PrintWindow(target.Hwnd, dc, 2); } finally { graphics.ReleaseHdc(dc); }
                                if (!rendered) throw new InvalidOperationException("target_render_unavailable");
                            }
                            if (!target.Valid() || target.Bounds() != raw || target.CaptureBounds() != visible) throw new InvalidOperationException("target_changed");
                            owned.Image = ValidateRenderedFrame(full, raw, visible); owned.Image.Tag = new CapturedFrameInfo("print_window",printStarted);
                        }
                    } catch (Exception error) { owned.Error = error; }
                    finally { owned.Completed.Set(); }
                }); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
            }
        }
        if (!job.Completed.WaitOne(120)) throw new InvalidOperationException("target_render_waiting");
        lock (renderLock) {
            if (job.Error != null) throw new InvalidOperationException(job.Error is InvalidOperationException ? job.Error.Message : "target_render_unavailable");
            if (job.Owner != target || job.Raw != raw || job.Visible != visible || job.Image == null || !RenderJobFresh(job,DateTime.UtcNow)
                    || !target.Foreground() || target.Bounds() != raw || target.CaptureBounds() != visible) throw new InvalidOperationException("target_changed");
            if (!CapturedFrameAfterInput(((CapturedFrameInfo)job.Image.Tag).InputAt,notBefore)) throw new InvalidOperationException("target_render_waiting");
            Bitmap copy = (Bitmap)job.Image.Clone(); copy.Tag = job.Image.Tag; return copy;
        }
    }
    static string CaptureBackendDiagnostic(Target target) {
        lock(renderLock) {
            RenderJob job;
            if (!renderJobs.TryGetValue(target.Hwnd,out job) || job.Owner!=target || String.IsNullOrEmpty(job.GraphicsStage)) return "";
            return "\n캡처 진단: Windows Graphics Capture / " + job.GraphicsStage
                + (job.GraphicsHResult==0 ? "" : " / HRESULT 0x" + unchecked((uint)job.GraphicsHResult).ToString("X8"));
        }
    }
    static void AppendCaptureBackendDiagnostic(Target target, Dictionary<string, object> diagnostic) {
        if (target == null) return;
        lock(renderLock) {
            CapturePump pump;
            if(capturePumps.TryGetValue(target,out pump)) {
                diagnostic["capture_backend_stage"]=pump.Stage;
                diagnostic["capture_backend_hresult"]=unchecked((uint)pump.HResult).ToString("X8");
                diagnostic["capture_backend_pending"]=!pump.Finished;
                diagnostic["capture_frame_count"]=pump.FrameCount;
                diagnostic["capture_latest_content_age_ms"]=pump.LatestContentAge();
                diagnostic["capture_empty_polls"]=pump.EmptyPolls;
                diagnostic["capture_refresh_count"]=pump.RefreshCount;
                diagnostic["capture_previous_failure"]=pump.PreviousFailure;
                diagnostic["capture_previous_future_lead_ms"]=Math.Round(Math.Max(0,Math.Min(60000,pump.PreviousFutureLead)),3);
                return;
            }
            RenderJob job;
            if (!renderJobs.TryGetValue(target.Hwnd,out job) || job.Owner!=target || String.IsNullOrEmpty(job.GraphicsStage)) return;
            // Fixed implementation-stage names and numeric API status only.
            // This does not expose screenshots, titles, paths or input values.
            diagnostic["capture_backend_stage"] = job.GraphicsStage;
            diagnostic["capture_backend_hresult"] = unchecked((uint)job.GraphicsHResult).ToString("X8");
            diagnostic["capture_backend_pending"] = !job.Completed.WaitOne(0);
        }
    }
    internal static Bitmap ValidateRenderedFrame(Bitmap full, Rectangle raw, Rectangle visible) {
        if (full.Width != raw.Width || full.Height != raw.Height || !raw.Contains(visible)) throw new InvalidOperationException("target_capture_size_invalid");
        Bitmap image = full.Clone(new Rectangle(visible.X-raw.X, visible.Y-raw.Y, visible.Width, visible.Height), PixelFormat.Format32bppArgb);
        try {
            Pixels pixels = new Pixels(image); int unset = 0, sampled = 0;
            for (int y = 0; y < pixels.H; y += 8) for (int x = 0; x < pixels.W; x += 8) { int at = y*pixels.Stride+x*4; sampled++; if (pixels.Data[at] == 255 && pixels.Data[at+1] == 0 && pixels.Data[at+2] == 255) unset++; }
            if (sampled == 0 || unset > sampled / 100 || !Detailed(pixels)) throw new InvalidOperationException("target_render_incomplete");
            Bitmap result = image; image = null; return result;
        } finally { if (image != null) image.Dispose(); }
    }
    static Bitmap Capture(Target target) { return Capture(target, false); }
    static Bitmap CaptureWindowSurface(Target target) {
        // A window DC with SRCCOPY (without CAPTUREBLT) excludes layered
        // overlays. Do not treat a click-through cursor's full-screen bounds
        // as opaque; ordinary windows remain checked separately per crop.
        target.EnsureCaptureReady(); Rectangle raw = target.Bounds(), visible = target.CaptureBounds();
        IntPtr source = GetWindowDC(target.Hwnd); if (source == IntPtr.Zero) throw new InvalidOperationException("window_surface_unavailable");
        // GDI SRCCOPY does not establish an alpha channel. Treat its result
        // as opaque RGB so a zero alpha byte cannot erase valid pixels.
        Bitmap image = new Bitmap(visible.Width, visible.Height, PixelFormat.Format32bppRgb);
        try {
            using (Graphics graphics = Graphics.FromImage(image)) {
                IntPtr destination = graphics.GetHdc();
                try { if (!BitBlt(destination, 0, 0, image.Width, image.Height, source, visible.X - raw.X, visible.Y - raw.Y, 0x00CC0020)) throw new InvalidOperationException("window_surface_unavailable"); }
                finally { graphics.ReleaseHdc(destination); }
            }
            if (!target.Foreground() || target.Bounds() != raw || target.CaptureBounds() != visible) throw new InvalidOperationException("target_changed");
            if (!Detailed(new Pixels(image))) throw new InvalidOperationException("target_render_incomplete");
            Bitmap result = image; image = null; return result;
        } finally { ReleaseDC(target.Hwnd, source); if (image != null) image.Dispose(); }
    }
    internal static Bitmap RecordingSurfaceCapture(Func<Bitmap> surface, Func<Bitmap> render, out bool rendered) {
        rendered = false;
        try { return surface(); }
        catch (InvalidOperationException error) {
            // GPU/UWP windows can have a black GDI surface. Only a validated
            // target-only renderer may replace it; scope/geometry failures
            // must propagate, and there is no desktop/occluder bypass here.
            if (error.Message != "target_render_incomplete" && error.Message != "window_surface_unavailable") throw;
        }
        Bitmap image = render(); rendered = true; return image;
    }
    internal static Bitmap RecordingLayeredCapture(Func<Bitmap> surface, Func<Bitmap> render, Func<Bitmap> screen, out bool rendered, out bool excludesLayered) {
        rendered = false; excludesLayered = true;
        try {
            Bitmap image = RecordingSurfaceCapture(surface, render, out rendered);
            if (rendered) excludesLayered = false;
            return image;
        } catch (InvalidOperationException error) {
            // A GPU surface or PrintWindow may be unavailable without making
            // the visible, uncovered portion of the window unusable. Only
            // availability failures permit a screen capture. Scope, focus and
            // geometry failures must still propagate unchanged.
            if (error.Message != "target_render_waiting" && error.Message != "target_capture_busy"
                    && error.Message != "target_render_unavailable" && error.Message != "target_render_incomplete"
                    && error.Message != "window_surface_unavailable") throw;
        }
        // Screen pixels include layered windows. Keep all old/current masks
        // and never claim renderer isolation: covered click anchors remain
        // rejected by CaptureClick and recognition context by AuthorImage.
        rendered = false; excludesLayered = false;
        return screen();
    }
    static Bitmap Capture(Target target, bool allowOcclusion) { bool rendered; return Capture(target, allowOcclusion, out rendered); }
    static Bitmap Capture(Target target, bool allowOcclusion, out bool rendered) {
        target.EnsureCaptureReady(); Rectangle r = target.CaptureBounds(); rendered = !allowOcclusion && Covered(r, Occluders(target));
        if (rendered) { Bitmap isolated = RenderWindow(target, r); if (!target.Foreground() || target.CaptureBounds() != r) { isolated.Dispose(); throw new InvalidOperationException("target_changed"); } return isolated; }
        Bitmap b = new Bitmap(r.Width, r.Height, PixelFormat.Format32bppArgb);
        try { using (Graphics g = Graphics.FromImage(b)) g.CopyFromScreen(r.Location, Point.Empty, r.Size, CopyPixelOperation.SourceCopy);
            if (!target.Foreground() || target.CaptureBounds() != r) throw new InvalidOperationException("target_changed");
            // The recorder validates both old and current occluders against
            // the exact click crop. A small recorder panel must not force a
            // whole Qt/GPU window through unsupported PrintWindow rendering.
            if (!allowOcclusion && Covered(r, Occluders(target))) throw new InvalidOperationException("target_occluded"); return b;
        } catch { b.Dispose(); throw; }
    }
    static string Encode(Bitmap b) { using (MemoryStream s = new MemoryStream()) { b.Save(s, ImageFormat.Png); if (s.Length > 524288) throw new InvalidOperationException("template_too_large"); return Convert.ToBase64String(s.ToArray()); } }
    internal static Dictionary<string, object> Crop(Dictionary<string, object> request) {
        object rawRegions; var regions = request.TryGetValue("regions", out rawRegions) ? rawRegions as ArrayList : null;
        if (regions == null || regions.Count < 1 || regions.Count > 16) throw new InvalidOperationException("invalid_crop_regions");
        var crops = new List<string>(); long encodedSize = 0;
        using (Bitmap screenshot = Decode(Str(request, "screenshot_png", ""), false)) {
            foreach (object entry in regions) {
                var region = entry as Dictionary<string, object>;
                if (region == null || region.Count != 4) throw new InvalidOperationException("invalid_crop_region");
                foreach (string key in new string[] { "x", "y", "width", "height" }) if (!region.ContainsKey(key) || !(region[key] is int)) throw new InvalidOperationException("invalid_crop_region");
                int x = Num(region, "x", -1), y = Num(region, "y", -1), width = Num(region, "width", -1), height = Num(region, "height", -1);
                if (x < 0 || y < 0 || width < 1 || height < 1 || x > screenshot.Width - width || y > screenshot.Height - height) throw new InvalidOperationException("invalid_crop_region");
                using (Bitmap crop = screenshot.Clone(new Rectangle(x, y, width, height), PixelFormat.Format32bppArgb)) using (MemoryStream stream = new MemoryStream()) {
                    crop.Save(stream, ImageFormat.Png); string png = Convert.ToBase64String(stream.ToArray()); encodedSize += png.Length;
                    if (encodedSize > 15 * 1024 * 1024) throw new InvalidOperationException("crop_response_too_large");
                    crops.Add(png);
                }
            }
        }
        return new Dictionary<string, object> { { "nonce", Str(request, "nonce", "") }, { "status", "cropped" }, { "crops", crops } };
    }
    static Bitmap Decode(string png, bool template)
    {
        if (png.Length > (template ? 710000 : 24 * 1024 * 1024)) throw new InvalidOperationException("image_too_large");
        byte[] bytes = Convert.FromBase64String(png);
        if (bytes.Length < 24 || bytes[0] != 137 || bytes[1] != 80 || bytes[2] != 78 || bytes[3] != 71) throw new InvalidOperationException("invalid_png");
        int bound = template ? 512 : 4096;
        long declaredWidth = ((long)bytes[16] << 24) | ((long)bytes[17] << 16) | ((long)bytes[18] << 8) | bytes[19];
        long declaredHeight = ((long)bytes[20] << 24) | ((long)bytes[21] << 16) | ((long)bytes[22] << 8) | bytes[23];
        if (declaredWidth < 8 || declaredHeight < 8 || declaredWidth > bound || declaredHeight > bound) throw new InvalidOperationException("image_dimensions_invalid");
        using (MemoryStream s = new MemoryStream(bytes)) using (Image image = Image.FromStream(s, true, true)) {
            int maximum = bound;
            if (image.Width < 8 || image.Height < 8 || image.Width > maximum || image.Height > maximum) throw new InvalidOperationException("image_dimensions_invalid");
            return new Bitmap(image);
        }
    }
    internal static Dictionary<string, object> Template(Bitmap frame, Rectangle r, Point anchor)
    {
        if (r.Width < 8 || r.Height < 8 || r.Width > 4096 || r.Height > 4096 || !new Rectangle(Point.Empty, frame.Size).Contains(r)) throw new InvalidOperationException("selection_size_invalid");
        using (Bitmap crop = frame.Clone(r, PixelFormat.Format32bppArgb)) {
            double factor = Math.Min(1, 512.0 / Math.Max(r.Width, r.Height)); int width = (int)Math.Round(r.Width * factor), height = (int)Math.Round(r.Height * factor);
            if (width < 8 || height < 8) throw new InvalidOperationException("selection_too_thin");
            using (Bitmap resized = new Bitmap(width, height)) {
            using (Graphics g = Graphics.FromImage(resized)) using (ImageAttributes attributes = new ImageAttributes()) { attributes.SetWrapMode(System.Drawing.Drawing2D.WrapMode.TileFlipXY); g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic; if (width == crop.Width && height == crop.Height) g.DrawImageUnscaled(crop, 0, 0); else g.DrawImage(crop, new Rectangle(0, 0, width, height), 0, 0, crop.Width, crop.Height, GraphicsUnit.Pixel, attributes); }
            if (!Detailed(new Pixels(resized))) throw new InvalidOperationException("template_low_detail");
            return new Dictionary<string, object> { { "template_png", Encode(resized) }, { "width", width }, { "height", height },
                { "anchor", new Dictionary<string, object> { { "x", (double)(anchor.X - r.X) / r.Width }, { "y", (double)(anchor.Y - r.Y) / r.Height } } },
                { "source_size", new Dictionary<string, object> { { "width", r.Width }, { "height", r.Height } } },
                { "capture_window", new Dictionary<string, object> { { "width", frame.Width }, { "height", frame.Height } } } }; }
        }
    }
    sealed class Pixels
    {
        internal int W, H, Stride; internal byte[] Data;
        internal Pixels(Bitmap original) {
            W = original.Width; H = original.Height;
            using (Bitmap b = new Bitmap(W, H, PixelFormat.Format32bppArgb)) {
                using (Graphics g = Graphics.FromImage(b)) g.DrawImageUnscaled(original, 0, 0);
                BitmapData locked = b.LockBits(new Rectangle(0, 0, W, H), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
                try { Stride = locked.Stride; Data = new byte[Stride * H]; Marshal.Copy(locked.Scan0, Data, 0, Data.Length); }
                finally { b.UnlockBits(locked); }
            }
        }
    }
    static Dictionary<string, object> DetailMetrics(Pixels p)
    {
        double sum = 0, sq = 0, edgeX = 0, edgeY = 0; int n = 0, edgesX = 0, edgesY = 0, step = Math.Max(1, Math.Min(p.W, p.H) / 64);
        for (int y = 0; y < p.H; y += step) for (int x = 0; x < p.W; x += step) {
            int i = y * p.Stride + x * 4; double v = (p.Data[i] + p.Data[i + 1] + p.Data[i + 2]) / 3.0;
            sum += v; sq += v * v; n++;
            if (x + step < p.W) { int j = i + step * 4; edgeX += Math.Abs(v - (p.Data[j] + p.Data[j + 1] + p.Data[j + 2]) / 3.0); edgesX++; }
            if (y + step < p.H) { int j = i + step * p.Stride; edgeY += Math.Abs(v - (p.Data[j] + p.Data[j + 1] + p.Data[j + 2]) / 3.0); edgesY++; }
        }
        return new Dictionary<string, object> { { "variance", n == 0 ? 0 : sq / n - (sum / n) * (sum / n) },
            { "horizontal_edge", edgesX == 0 ? 0 : edgeX / edgesX }, { "vertical_edge", edgesY == 0 ? 0 : edgeY / edgesY } };
    }
    static bool Detailed(Pixels p) {
        var detail = DetailMetrics(p);
        // A list icon must meet the same criterion as its rotated version.
        // Flat/low-variance captures still fail; matching and ambiguity limits
        // remain independent and unchanged.
        return Real(detail, "variance", 0) >= 36 && Math.Max(Real(detail, "horizontal_edge", 0), Real(detail, "vertical_edge", 0)) >= 1.2;
    }
    internal static Rectangle RecordingRegion(Size frame, Point anchor, bool semantic, bool identified, bool expanded) {
        int width = Math.Min(expanded ? 640 : identified ? 160 : 384, frame.Width);
        int height = Math.Min(expanded ? 192 : semantic && !identified ? 128 : 96, frame.Height);
        return new Rectangle(Math.Max(0, Math.Min(frame.Width - width, anchor.X - width / 2)),
            Math.Max(0, Math.Min(frame.Height - height, anchor.Y - height / 2)), width, height);
    }
    internal static bool RecordingFrameFits(long retainedBytes, Size next) {
        return retainedBytes >= 0 && next.Width >= 8 && next.Height >= 8 && next.Width <= 4096 && next.Height <= 4096
            && retainedBytes <= 256L * 1024 * 1024 - (long)next.Width * next.Height * 4;
    }
    // Clip only recognition context, never move the user's action point. Both
    // the frame-time and input-time obstruction lists must be supplied: pixels
    // hidden in the old frame are not repaired by an unobstructed later view.
    internal static Rectangle UncoveredContext(Rectangle candidate, Point anchor, List<Rectangle> blocked) {
        if (!candidate.Contains(anchor)) return Rectangle.Empty;
        foreach (Rectangle obstruction in blocked) {
            Rectangle overlap = Rectangle.Intersect(candidate, obstruction);
            if (overlap.Width <= 0 || overlap.Height <= 0) continue;
            if (overlap.Contains(anchor)) return Rectangle.Empty;
            Rectangle best = Rectangle.Empty;
            Rectangle[] parts = { Rectangle.FromLTRB(candidate.Left, candidate.Top, overlap.Left, candidate.Bottom),
                Rectangle.FromLTRB(overlap.Right, candidate.Top, candidate.Right, candidate.Bottom),
                Rectangle.FromLTRB(candidate.Left, candidate.Top, candidate.Right, overlap.Top),
                Rectangle.FromLTRB(candidate.Left, overlap.Bottom, candidate.Right, candidate.Bottom) };
            foreach (Rectangle part in parts) if (part.Contains(anchor) && part.Width >= 8 && part.Height >= 8 && (long)part.Width * part.Height > (long)best.Width * best.Height) best = part;
            if (best.IsEmpty) return Rectangle.Empty; candidate = best;
        }
        return candidate;
    }
    static List<Rectangle> ContextCandidates(Size frame, Rectangle selected, Point anchor, List<Rectangle> blocked, bool contextFirst) {
        var result = new List<Rectangle>(); var canvas = new Rectangle(Point.Empty, frame);
        Rectangle first = UncoveredContext(Rectangle.Intersect(canvas, selected), anchor, blocked);
        if (!contextFirst && first.Width >= 8 && first.Height >= 8) result.Add(first);
        // Empty inputs often have their identifying label above the field.
        // Try bounded two-dimensional context before creating a long strip
        // whose only detail comes from an unrelated neighbour. These are the
        // same pre-input pixels and the actual action point never moves.
        foreach (Size context in new Size[] { new Size(384, 192), new Size(640, 320), new Size(1024, 448) }) {
            int w = Math.Min(frame.Width, Math.Max(context.Width, selected.Width));
            int h = Math.Min(frame.Height, Math.Max(context.Height, selected.Height));
            foreach (int horizontalShare in context.Width == 384 ? new int[] { 50 } : new int[] { 70, 50 }) {
                var region = new Rectangle(Math.Max(0, Math.Min(frame.Width - w, anchor.X - w * horizontalShare / 100)),
                    Math.Max(0, Math.Min(frame.Height - h, anchor.Y - h * 2 / 3)), w, h);
                region = UncoveredContext(region, anchor, blocked);
                if (region.Width >= 8 && region.Height >= 8 && !result.Contains(region)) result.Add(region);
            }
        }
        // Automatic recording has no human-selected recognition region. Prefer
        // nearby labels even when a narrow field happens to be unique in this
        // frame. The manual picker still tries its explicit selection first.
        if (contextFirst && first.Width >= 8 && first.Height >= 8 && !result.Contains(first)) result.Add(first);
        // Keep the compact horizontal fallback for row labels next to repeated
        // icons. Every candidate still has to pass the same uniqueness test.
        foreach (int width in new int[] { 160, 320, 640, 1280, 2048 }) {
            int w = Math.Min(frame.Width, Math.Max(width, selected.Width));
            int h = Math.Min(frame.Height, Math.Max(96, selected.Height));
            var region = new Rectangle(Math.Max(0, Math.Min(frame.Width - w, anchor.X - w / 2)),
                Math.Max(0, Math.Min(frame.Height - h, anchor.Y - h / 2)), w, h);
            region = UncoveredContext(region, anchor, blocked);
            if (region.Width >= 8 && region.Height >= 8 && !result.Contains(region)) result.Add(region);
        }
        return result;
    }
    // Source-scale uniqueness is checked against this exact frame. Replay
    // still checks all allowed scales and its normal ambiguity threshold.
    struct AuthoringSample {
        internal int Offset, B, G, R;
        internal double Importance;
    }
    sealed class AuthoringSamples {
        readonly AuthoringSample[] samples;
        readonly int channels;
        internal readonly int StepX, StepY, Columns, Rows;
        internal readonly long Sum, Squares;
        internal AuthoringSamples(Pixels screen, Pixels pattern, List<Feature> features) {
            var points = new List<AuthoringSample>();
            if (features != null) foreach (Feature point in features) Add(points, screen, pattern, point.X, point.Y);
            else {
                int dx = Math.Max(1, pattern.W / 100), dy = Math.Max(1, pattern.H / 100);
                StepX = dx; StepY = dy; Columns = (pattern.W-1-dx/2)/dx+1; Rows = (pattern.H-1-dy/2)/dy+1;
                for (int y = dy / 2; y < pattern.H; y += dy)
                    for (int x = dx / 2; x < pattern.W; x += dx) Add(points, screen, pattern, x, y);
                // The same complete Score(false) sample set, ordered only to
                // reach an already-exceeded error budget sooner. No sample is
                // dropped and the final sum is an exact 64-bit integer.
                double b = 0, g = 0, r = 0;
                foreach (AuthoringSample point in points) { b += point.B; g += point.G; r += point.R; }
                if (points.Count > 0) { b /= points.Count; g /= points.Count; r /= points.Count; }
                for (int i = 0; i < points.Count; i++) { var point = points[i];
                    point.Importance = (point.B-b)*(point.B-b)+(point.G-g)*(point.G-g)+(point.R-r)*(point.R-r); points[i] = point; }
                points.Sort(delegate(AuthoringSample a, AuthoringSample z) { return z.Importance.CompareTo(a.Importance); });
            }
            samples = points.ToArray(); channels = Math.Max(1, samples.Length * 3);
            foreach (AuthoringSample point in samples) { Sum += point.B+point.G+point.R; Squares += point.B*point.B+point.G*point.G+point.R*point.R; }
        }
        static void Add(List<AuthoringSample> points, Pixels screen, Pixels pattern, int x, int y) {
            int at = y * pattern.Stride + x * 4;
            points.Add(new AuthoringSample { Offset = y * screen.Stride + x * 4,
                B = pattern.Data[at], G = pattern.Data[at+1], R = pattern.Data[at+2] });
        }
        internal bool AtLeast(Pixels screen, int origin, double floor) {
            double error = (1 - floor) * 255, budget = error * error * channels;
            long sum = 0;
            for (int i = 0; i < samples.Length; i++) {
                AuthoringSample point = samples[i]; int at = origin + point.Offset;
                int b = point.B-screen.Data[at], g = point.G-screen.Data[at+1], r = point.R-screen.Data[at+2];
                sum += b*b + g*g + r*r;
                // One integer error unit is conservative at a floating-point
                // boundary. Equality still goes through the original RMS test.
                if (sum > budget + 1) return false;
            }
            return 1.0 - Math.Sqrt((double)sum / channels) / 255.0 >= floor;
        }
    }
    // Summed-area tables over each residue of the *same* Score sample grid.
    // One pair of buffers is reused when a new candidate changes grid spacing;
    // no full-frame screenshot is persisted and no candidate stride accumulates.
    sealed class AuthoringMomentIndex : IDisposable {
        const long MaximumBytes = 128L * 1024 * 1024;
        readonly Pixels screen;
        long[] sums, squares;
        int stepX, stepY, columns, rows, channels;
        long templateSum;
        double budget, templateLow, templateHigh, templateRoundoff;
        internal AuthoringMomentIndex(Pixels image) { screen = image; }
        internal bool Prepare(AuthoringSamples samples, double floor, Stopwatch elapsed) {
            long count = (long)screen.W*screen.H;
            if (count*16 > MaximumBytes) return false;
            if (elapsed.ElapsedMilliseconds > 8000) throw new InvalidOperationException("image_match_timeout");
            if (sums == null) { sums = new long[(int)count]; squares = new long[(int)count]; }
            if (stepX != samples.StepX || stepY != samples.StepY) {
                stepX = samples.StepX; stepY = samples.StepY;
                for (int y = 0; y < screen.H; y++) {
                    if (elapsed.ElapsedMilliseconds > 8000) throw new InvalidOperationException("image_match_timeout");
                    int row = y*screen.W, above = row-stepY*screen.W;
                    for (int x = 0; x < screen.W; x++) {
                        int at = y*screen.Stride+x*4, b = screen.Data[at], g = screen.Data[at+1], r = screen.Data[at+2];
                        long sum = b+g+r, sq = b*b+g*g+r*r;
                        if (x >= stepX) { sum += sums[row+x-stepX]; sq += squares[row+x-stepX]; }
                        if (y >= stepY) { sum += sums[above+x]; sq += squares[above+x]; }
                        if (x >= stepX && y >= stepY) { sum -= sums[above+x-stepX]; sq -= squares[above+x-stepX]; }
                        sums[row+x] = sum; squares[row+x] = sq;
                    }
                }
            }
            columns = samples.Columns; rows = samples.Rows; channels = columns*rows*3; templateSum = samples.Sum;
            double error = (1-floor)*255; budget = error*error*channels;
            templateRoundoff = Math.Max(1e-7, samples.Squares*1e-10);
            double centered = Math.Max(0, samples.Squares-(double)samples.Sum*samples.Sum/channels);
            templateLow = Math.Sqrt(Math.Max(0, centered-templateRoundoff));
            templateHigh = Math.Sqrt(centered+templateRoundoff);
            return true;
        }
        long WindowSum(long[] table, int firstX, int firstY, int lastX, int lastY) {
            long sum = table[lastY*screen.W+lastX]; int beforeX = firstX-stepX, beforeY = firstY-stepY;
            if (beforeX >= 0) sum -= table[lastY*screen.W+beforeX];
            if (beforeY >= 0) sum -= table[beforeY*screen.W+lastX];
            if (beforeX >= 0 && beforeY >= 0) sum += table[beforeY*screen.W+beforeX];
            return sum;
        }
        internal bool MayReach(int x, int y) {
            int firstX = x+stepX/2, firstY = y+stepY/2;
            int lastX = firstX+(columns-1)*stepX, lastY = firstY+(rows-1)*stepY;
            long sum = WindowSum(sums,firstX,firstY,lastX,lastY), sq = WindowSum(squares,firstX,firstY,lastX,lastY);
            double roundoff = Math.Max(1e-7,sq*1e-10), difference = sum-templateSum;
            double meanBound = Math.Max(0,difference*difference/channels-roundoff-templateRoundoff);
            if (meanBound > budget+1) return false;
            double centered = Math.Max(0,sq-(double)sum*sum/channels);
            double low = Math.Sqrt(Math.Max(0,centered-roundoff)), high = Math.Sqrt(centered+roundoff);
            double gap = Math.Max(0,Math.Max(low-templateHigh,templateLow-high));
            // Cauchy-Schwarz: SSE >= n*(mean difference)^2 +
            // (norm(centered image)-norm(centered template))^2. Widen the
            // moment intervals for floating roundoff; only impossible scores
            // are excluded. Every survivor still uses the original RGB RMS.
            return meanBound+gap*gap <= budget+1;
        }
        public void Dispose() { sums = squares = null; }
    }
    static bool SameAuthoringOrigin(Rectangle expected, int x, int y) {
        int dx = Math.Abs(x - expected.X), dy = Math.Abs(y - expected.Y);
        double tolerance = Math.Max(2, Math.Min(expected.Width, expected.Height) * .10);
        if (dx > tolerance || dy > tolerance) return false;
        double area = (double)Math.Max(0, expected.Width-dx) * Math.Max(0, expected.Height-dy);
        double union = (double)expected.Width*expected.Height*2-area;
        return area / Math.Max(1, union) >= .72;
    }
    static bool DistinctInFrame(Pixels screen, Bitmap normalized, Rectangle region, Stopwatch elapsed, AuthoringMomentIndex moments) {
        using (var reconstructed = new Bitmap(region.Width, region.Height)) {
            using (Graphics graphics = Graphics.FromImage(reconstructed)) using (var attributes = new ImageAttributes()) {
                attributes.SetWrapMode(System.Drawing.Drawing2D.WrapMode.TileFlipXY);
                graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                if (normalized.Size == region.Size) graphics.DrawImageUnscaled(normalized, 0, 0);
                else graphics.DrawImage(normalized, new Rectangle(Point.Empty, region.Size), 0, 0, normalized.Width, normalized.Height, GraphicsUnit.Pixel, attributes);
            }
            Pixels pattern = new Pixels(reconstructed); var features = Features(pattern);
            double originalScore = Score(screen, pattern, region.X, region.Y, false);
            if (originalScore < .94 || features.Count == 0) return false;
            double competitor = originalScore - .03;
            var featureSamples = new AuthoringSamples(screen, pattern, features);
            var scoreSamples = new AuthoringSamples(screen, pattern, null);
            bool useMoments = moments.Prepare(scoreSamples,competitor,elapsed);
            for (int y = 0; y <= screen.H - region.Height; y++) {
                if (elapsed.ElapsedMilliseconds > 8000) throw new InvalidOperationException("image_match_timeout");
                for (int x = 0; x <= screen.W - region.Width; x++) {
                    if ((x & 255) == 0 && elapsed.ElapsedMilliseconds > 8000) throw new InvalidOperationException("image_match_timeout");
                    if (SameAuthoringOrigin(region, x, y)) continue;
                    if (useMoments && !moments.MayReach(x,y)) continue;
                    int origin = y * screen.Stride + x * 4;
                    if (!featureSamples.AtLeast(screen, origin, .79)) continue;
                    if (scoreSamples.AtLeast(screen, origin, competitor)) return false;
                }
            }
            if (elapsed.ElapsedMilliseconds > 8000) throw new InvalidOperationException("image_match_timeout");
            return true;
        }
    }
    internal static Dictionary<string, object> AuthorImage(Bitmap frame, Rectangle selected, Point anchor, List<Rectangle> blocked, bool contextFirst = false) {
        if (!new Rectangle(Point.Empty, frame.Size).Contains(anchor)) throw new InvalidOperationException("selection_size_invalid");
        Pixels screen = null; AuthoringMomentIndex moments = null; Stopwatch elapsed = Stopwatch.StartNew(); string failure = "template_low_detail"; int tried = 0;
        try {
        foreach (Rectangle candidate in ContextCandidates(frame.Size, selected, anchor, blocked, contextFirst)) {
            tried++;
            try {
                var result = Template(frame, candidate, anchor);
                if (screen == null) { screen = new Pixels(frame); moments = new AuthoringMomentIndex(screen); }
                using (Bitmap normalized = Decode(Str(result, "template_png", ""), true)) {
                    if (!DistinctInFrame(screen, normalized, candidate, elapsed, moments)) { failure = "image_ambiguous"; continue; }
                }
                result["selection"] = new Dictionary<string, object> { { "x", candidate.X }, { "y", candidate.Y }, { "width", candidate.Width }, { "height", candidate.Height } };
                result["capture_diagnostic"] = new Dictionary<string, object> { { "status", "retained" }, { "before_input", true },
                    { "candidate_count", tried }, { "context_expanded", candidate != selected }, { "source_frame_unique", true }, { "uniqueness_scope", "captured_frame_source_scale" } };
                return result;
            } catch (InvalidOperationException error) { if (error.Message == "image_match_timeout") throw; if (failure != "image_ambiguous") failure = error.Message; }
        }
        if (tried == 0) failure = "image_occluded_requires_selection";
        throw new InvalidOperationException(failure);
        } finally { if (moments != null) moments.Dispose(); }
    }
    internal static Dictionary<string, object> RecordedImage(Bitmap primary, Point anchor, Bitmap context, Point contextAnchor, Size frameSize, string mode) {
        Bitmap[] images = context == null ? new Bitmap[] { primary } : new Bitmap[] { primary, context };
        Point[] anchors = context == null ? new Point[] { anchor } : new Point[] { anchor, contextAnchor };
        string failure = "template_low_detail";
        for (int index = 0; index < images.Length; index++) {
            try {
                var result = Template(images[index], new Rectangle(Point.Empty, images[index].Size), anchors[index]);
                result["capture_window"] = new Dictionary<string, object> { { "width", frameSize.Width }, { "height", frameSize.Height } };
                result["capture_diagnostic"] = new Dictionary<string, object> { { "status", "retained" }, { "capture_mode", mode },
                    { "candidate_count", index + 1 }, { "context_expanded", index > 0 }, { "detail", DetailMetrics(new Pixels(images[index])) } };
                return result;
            } catch (InvalidOperationException error) { failure = error.Message; }
        }
        var rejected = new Dictionary<string, object> { { "operation", "manual_entry" }, { "reason", failure }, { "capture_issue", failure },
            { "capture_diagnostic", new Dictionary<string, object> { { "status", "rejected" }, { "capture_mode", mode }, { "candidate_count", images.Length },
                { "width", primary.Width }, { "height", primary.Height }, { "detail", DetailMetrics(new Pixels(primary)) } } } };
        // Keep actual rejected pixels in the private recording receipt. They
        // never become a replay target and are not exposed by status metadata.
        try { rejected["rejected_capture"] = new Dictionary<string, object> { { "format", "computer-rejected-capture/v1" },
            { "png", Encode(primary) }, { "width", primary.Width }, { "height", primary.Height }, { "before_input", true } }; }
        catch (InvalidOperationException) { }
        return rejected;
    }
    sealed class Candidate { internal int X, Y, W, H, ClipLeft, ClipTop, OriginalW, OriginalH; internal double Score, Scale; }
    sealed class Feature { internal int X, Y; internal double Importance; }
    static List<Feature> Features(Pixels template)
    {
        double red = 0, green = 0, blue = 0; int pixels = template.W * template.H;
        for (int y = 0; y < template.H; y++) for (int x = 0; x < template.W; x++) {
            int at = y * template.Stride + x * 4; blue += template.Data[at]; green += template.Data[at + 1]; red += template.Data[at + 2];
        }
        red /= pixels; green /= pixels; blue /= pixels; var result = new List<Feature>();
        // Select distinctive pixels throughout the crop. A fixed sparse grid
        // can miss all thin text/borders in a mostly empty input field.
        for (int cy = 0; cy < 6; cy++) for (int cx = 0; cx < 8; cx++) {
            Feature best = null;
            for (int y = cy * template.H / 6; y < (cy + 1) * template.H / 6; y++) for (int x = cx * template.W / 8; x < (cx + 1) * template.W / 8; x++) {
                int at = y * template.Stride + x * 4;
                double db = template.Data[at] - blue, dg = template.Data[at + 1] - green, dr = template.Data[at + 2] - red;
                double importance = db * db + dg * dg + dr * dr;
                if (best == null || importance > best.Importance) best = new Feature { X = x, Y = y, Importance = importance };
            }
            if (best != null && best.Importance >= 75) result.Add(best);
        }
        result.Sort(delegate(Feature a, Feature b) { return b.Importance.CompareTo(a.Importance); }); return result;
    }
    static double FeatureScore(Pixels image, Pixels template, List<Feature> features, int x, int y, double floor)
    {
        double sum = 0, budget = (1 - floor) * 255; budget = budget * budget * features.Count * 3;
        foreach (Feature point in features) {
            int a = point.Y * template.Stride + point.X * 4, b = (y + point.Y) * image.Stride + (x + point.X) * 4;
            for (int channel = 0; channel < 3; channel++) { int d = template.Data[a + channel] - image.Data[b + channel]; sum += d * d; }
            if (sum > budget) return 0;
        }
        return 1 - Math.Sqrt(sum / Math.Max(1, features.Count * 3)) / 255;
    }
    static double Score(Pixels image, Pixels template, int x, int y, bool coarse)
    {
        double sum = 0; int n = 0, dx = coarse ? Math.Max(1, template.W / 12) : Math.Max(1, template.W / 100), dy = coarse ? Math.Max(1, template.H / 10) : Math.Max(1, template.H / 100);
        for (int py = dy / 2; py < template.H; py += dy) for (int px = dx / 2; px < template.W; px += dx) {
            int a = py * template.Stride + px * 4, b = (y + py) * image.Stride + (x + px) * 4;
            for (int c = 0; c < 3; c++) { double d = template.Data[a + c] - image.Data[b + c]; sum += d * d; n++; }
            if (coarse && n >= 36 && sum / n > 16000) return 0;
        }
        return 1.0 - Math.Sqrt(sum / Math.Max(1, n)) / 255.0;
    }
    static bool Same(Candidate a, Candidate b) {
        double tolerance = Math.Max(2, Math.Min(Math.Min(a.W, b.W), Math.Min(a.H, b.H)) * .10);
        Rectangle overlap = Rectangle.Intersect(new Rectangle(a.X, a.Y, a.W, a.H), new Rectangle(b.X, b.Y, b.W, b.H));
        double area = (double)overlap.Width * overlap.Height, union = (double)a.W * a.H + (double)b.W * b.H - area;
        return area / Math.Max(1, union) >= .72 && Math.Abs(a.X + a.W / 2.0 - b.X - b.W / 2.0) <= tolerance && Math.Abs(a.Y + a.H / 2.0 - b.Y - b.H / 2.0) <= tolerance;
    }
    static void AddBest(List<Candidate> list, Candidate item, int maximum, bool merge)
    {
        if (merge) for (int i = 0; i < list.Count; i++) if (Same(list[i], item)) { if (list[i].Score >= item.Score) return; list.RemoveAt(i); break; }
        list.Add(item); list.Sort(delegate(Candidate a, Candidate b) { return b.Score.CompareTo(a.Score); }); if (list.Count > maximum) list.RemoveAt(list.Count - 1);
    }
    static Dictionary<string, object> Match(Dictionary<string, object> request)
    {
        var result = new Dictionary<string, object> { { "nonce", Str(request, "nonce", "") }, { "status", "not_found" } };
        double minimum = Real(request, "min_score", .94), margin = Real(request, "ambiguity_margin", .03);
        if (minimum < .85 || minimum > 1 || margin < .01 || margin > .15) throw new InvalidOperationException("invalid_match_threshold");
        using (Bitmap original = Decode(Str(request, "template_png", ""), true)) using (Bitmap screenshot = Decode(Str(request, "screenshot_png", ""), false)) {
            Pixels image = new Pixels(screenshot);
            result["screenshot"] = new Dictionary<string, object> { { "width", image.W }, { "height", image.H } };
            if (!Detailed(new Pixels(original))) { result["code"] = "template_low_detail"; return result; }
            List<double> scales = new List<double>(); double ratio = 1;
            Dictionary<string, object> capture = Map(request, "capture_window");
            if (capture != null && Num(capture, "width", 0) > 0 && Num(capture, "height", 0) > 0) {
                double rx = (double)image.W / Num(capture, "width", 1), ry = (double)image.H / Num(capture, "height", 1);
                if (Math.Abs(rx - ry) / Math.Max(rx, ry) < .12) ratio = (rx + ry) / 2;
            }
            Dictionary<string, object> source = Map(request, "source_size"); double sourceScale = source == null ? 1 : (double)Num(source, "width", original.Width) / original.Width;
            int sourceWidth = source == null ? original.Width : Num(source, "width", original.Width);
            int sourceHeight = source == null ? original.Height : Num(source, "height", original.Height);
            bool nearBorder = capture != null && Math.Abs(Num(capture, "width", 0) - image.W) <= 4 && Math.Abs(Num(capture, "height", 0) - image.H) <= 4;
            if (nearBorder) ratio = 1; // Frame trimming is not proven DPI scaling.
            HashSet<long> sampledSizes = new HashSet<long>();
            // A different capture border can change the window ratio without
            // changing the control's pixels. Always preserve the original
            // source size; only equal raster dimensions are duplicate scales.
            foreach (double baseScale in new double[] { 1, ratio, .75, .85, 1.15, 1.25, 1.5, ratio * .9, ratio * 1.1 }) {
                double scale = baseScale * sourceScale;
                if (scale < .2 || scale > 16) continue;
                int width = (int)Math.Round(sourceWidth * baseScale), height = (int)Math.Round(sourceHeight * baseScale);
                bool clipBorder = nearBorder && Math.Abs(baseScale - 1) < .00001
                    && (width <= image.W || width == sourceWidth && width == Num(capture, "width", 0) && width - image.W <= 4)
                    && (height <= image.H || height == sourceHeight && height == Num(capture, "height", 0) && height - image.H <= 4);
                if (width < 8 || height < 8 || (width > image.W || height > image.H) && !clipBorder) continue;
                if (sampledSizes.Add(((long)width << 32) | (uint)height)) scales.Add(scale);
            }
            List<Candidate> best = new List<Candidate>(); Stopwatch elapsed = Stopwatch.StartNew();
            foreach (double scale in scales) {
                int w = (int)Math.Round(sourceWidth * scale / sourceScale), h = (int)Math.Round(sourceHeight * scale / sourceScale);
                int originalWidth = w, originalHeight = h, clipLeft = Math.Max(0, w - image.W) / 2, clipTop = Math.Max(0, h - image.H) / 2;
                if (w < 8 || h < 8) continue;
                using (Bitmap resized = new Bitmap(w, h)) {
                    using (Graphics g = Graphics.FromImage(resized)) using (ImageAttributes attributes = new ImageAttributes()) { attributes.SetWrapMode(System.Drawing.Drawing2D.WrapMode.TileFlipXY); g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic; if (w == original.Width && h == original.Height) g.DrawImageUnscaled(original, 0, 0); else g.DrawImage(original, new Rectangle(0, 0, w, h), 0, 0, original.Width, original.Height, GraphicsUnit.Pixel, attributes); }
                    w = Math.Min(w, image.W); h = Math.Min(h, image.H);
                    if (w < 8 || h < 8) continue;
                    using (Bitmap visiblePattern = resized.Clone(new Rectangle(clipLeft, clipTop, w, h), PixelFormat.Format32bppArgb)) {
                    Pixels pattern = new Pixels(visiblePattern); List<Feature> features = Features(pattern); if (features.Count == 0) continue;
                    List<Candidate> coarse = new List<Candidate>();
                    // Check every possible origin: even a one-pixel offset can
                    // change the sparse text in a wide, mostly blank field.
                    for (int y = 0; y <= image.H - h; y++) {
                        if (elapsed.ElapsedMilliseconds > 15000) throw new InvalidOperationException("match_timeout");
                        for (int x = 0; x <= image.W - w; x++) {
                            double score = FeatureScore(image, pattern, features, x, y, minimum - .12);
                            if (score >= minimum - .12) AddBest(coarse, new Candidate { X = x, Y = y, W = w, H = h, Score = score, Scale = scale,
                                ClipLeft = clipLeft, ClipTop = clipTop, OriginalW = originalWidth, OriginalH = originalHeight }, 128, true);
                        }
                    }
                    foreach (Candidate c in coarse) {
                        // Sparse feature ranking can prefer an adjacent edge
                        // over the true antialiased-text alignment. Refine
                        // before merging final candidates; never lower the
                        // final similarity or distinct-target margin.
                        Candidate local = null;
                        for (int fy = Math.Max(0, c.Y - 2); fy <= Math.Min(image.H - h, c.Y + 2); fy++)
                            for (int fx = Math.Max(0, c.X - 2); fx <= Math.Min(image.W - w, c.X + 2); fx++) {
                                double exact = Score(image, pattern, fx, fy, false);
                                if (exact >= minimum - margin && (local == null || exact > local.Score)) local = new Candidate { X = fx, Y = fy, W = w, H = h, Score = exact, Scale = scale,
                                    ClipLeft = clipLeft, ClipTop = clipTop, OriginalW = originalWidth, OriginalH = originalHeight };
                            }
                        if (local != null) AddBest(best, local, 8, true);
                    }
                    }
                }
            }
            result["candidate_count"] = best.Count;
            if (best.Count > 1) result["second_score"] = best[1].Score;
            if (best.Count == 0 || best[0].Score < minimum) { result["code"] = "image_not_found"; result["score"] = best.Count > 0 ? best[0].Score : 0; return result; }
            Candidate winner = best[0]; result["score"] = winner.Score;
            if (best.Count > 1 && winner.Score - best[1].Score < margin) { result["status"] = "ambiguous"; result["code"] = "image_ambiguous"; return result; }
            result["status"] = "matched"; result["scale"] = winner.Scale;
            result["rect"] = new Dictionary<string, object> { { "x", winner.X }, { "y", winner.Y }, { "width", winner.W }, { "height", winner.H } };
            if (winner.OriginalW != winner.W || winner.OriginalH != winner.H)
                result["template_clip"] = new Dictionary<string, object> { { "left", winner.ClipLeft }, { "top", winner.ClipTop },
                    { "right", winner.OriginalW - winner.W - winner.ClipLeft }, { "bottom", winner.OriginalH - winner.H - winner.ClipTop },
                    { "original_width", winner.OriginalW }, { "original_height", winner.OriginalH } };
            return result;
        }
    }
    static Button Button(string text) { return new Button { Text = text, AutoSize = true, MinimumSize = new Size(110, 38), FlatStyle = FlatStyle.Flat, BackColor = Color.White, ForeColor = Color.FromArgb(30, 55, 90), Margin = new Padding(8) }; }
    internal static string CaptureMessage(string code) {
        string message;
        switch (code) {
            case "target_unavailable": message = "선택한 창이 닫혔거나 다른 창으로 바뀌었습니다. 취소한 뒤 현재 창을 다시 선택하세요."; break;
            case "target_minimized": message = "대상 창이 최소화되어 있습니다. 작업 표시줄에서 창을 복원한 뒤 다시 캡처하세요."; break;
            case "target_hidden": case "target_cloaked": message = "대상 창이 숨겨져 있거나 다른 가상 데스크톱에 있습니다. 현재 바탕화면에 창을 연 뒤 다시 캡처하세요."; break;
            case "target_requires_foreground": message = "대상 창을 앞으로 가져오지 못했습니다. 작업 표시줄에서 대상 창을 한 번 선택한 뒤 다시 캡처하세요. 계속 실패하면 대상 프로그램과 MCP의 실행 권한을 확인하세요."; break;
            case "target_offscreen": message = "대상 창의 일부가 모니터 밖이나 모니터 사이 빈 공간에 있습니다. 창을 한 모니터 안으로 옮기거나 최대화한 뒤 다시 캡처하세요."; break;
            case "target_capture_too_large": message = "대상 창이 캡처 가능한 크기(가로·세로 4096픽셀)를 넘었습니다. 최대화를 해제하고 창 크기를 줄인 뒤 다시 캡처하세요."; break;
            case "target_capture_size_invalid": message = "대상 창의 크기를 확인하지 못했습니다. 창을 복원하거나 크기를 조절한 뒤 다시 캡처하세요."; break;
            case "target_render_waiting": case "target_capture_busy": message = "대상 프로그램의 캡처 응답을 기다리고 있습니다. 잠시 뒤 다시 캡처하세요. 응답하지 않는 캡처를 중복 실행하지 않습니다."; break;
            case "target_render_unavailable": case "target_render_incomplete": message = "대상 창만의 이미지를 가져오지 못했습니다. 겹친 창이나 화면 표시 도구를 정리한 뒤 다시 캡처하세요. 가려진 화면을 임의로 저장하지 않았습니다."; break;
            case "target_occluded": message = "대상 창 위에 다른 창이나 툴팁이 겹쳐 있습니다. 가리는 창을 옮기거나 닫은 뒤 다시 캡처하세요."; break;
            case "target_changed": message = "캡처하는 동안 창 위치나 화면 상태가 바뀌었습니다. 창이 멈춘 뒤 다시 캡처하세요."; break;
            case "window_order_unavailable": message = "겹친 창의 순서를 확인하지 못했습니다. 대상 창 위의 다른 창을 정리한 뒤 다시 캡처하세요."; break;
            default: code = "screen_capture_failed"; message = "화면 이미지를 가져오지 못했습니다. 화면 잠금이나 원격 연결 상태를 확인하세요. 계속되면 이 오류 코드를 전달해 주세요."; break;
        }
        return message + "\n[" + code + "]";
    }
    internal static string RecordingStartMessage(string code) {
        string explanation;
        if (code == "target_render_incomplete" || code == "target_render_unavailable" || code == "window_surface_unavailable")
            explanation = "프로그램 화면 캡처가 비어 있거나 완성되지 않아 기록을 시작하지 못했습니다.\n화면 로딩이 끝난 뒤에도 반복되면 아래 오류 코드를 전달해 주세요.";
        else if (code == "recording_capture_backlog")
            explanation = "이전 화면 기록을 처리 중이라 새 녹화를 시작하지 못했습니다. 잠시 뒤 ‘기록 시작’을 다시 누르세요.";
        else {
            explanation = CaptureMessage(code); int suffix = explanation.LastIndexOf("\n[");
            if (suffix >= 0) explanation = explanation.Substring(0, suffix);
            explanation = explanation.Replace("다시 캡처하세요", "‘기록 시작’을 다시 누르세요");
        }
        return "아직 녹화를 시작하지 않았습니다.\n" + explanation + "\n[" + code + "]";
    }
    static void Style(Form form, string title, Size size) { form.Text = title; form.ClientSize = size; form.StartPosition = FormStartPosition.CenterScreen; form.Font = new Font("맑은 고딕", 10); form.BackColor = Color.FromArgb(244, 247, 251); form.ForeColor = Color.FromArgb(25, 45, 75); form.AutoScaleMode = AutoScaleMode.Dpi; }
    abstract class SessionForm : Form
    {
        internal readonly string NonceValue, Response; internal readonly System.Windows.Forms.Timer Clock = new System.Windows.Forms.Timer();
        internal readonly DateTime Deadline; internal bool Done;
        internal SessionForm(Dictionary<string, object> data, string response, int maximum) {
            NonceValue = Str(data, "nonce", ""); Response = response; int seconds = Num(data, "timeout_seconds", 180);
            if (seconds < 10 || seconds > maximum) throw new InvalidOperationException("invalid_timeout"); Deadline = DateTime.UtcNow.AddSeconds(seconds);
            KeyPreview = true; KeyDown += delegate(object s, KeyEventArgs e) { if (e.KeyCode == Keys.Escape) { e.Handled = true; Escape(); } };
            Clock.Interval = 80; Clock.Tick += delegate { if (DateTime.UtcNow > Deadline) TimedOut(); };
            Shown += delegate { Ready(); Clock.Start(); };
            VisibleChanged += delegate { if (Visible) Ready(); };
            FormClosing += delegate { if (!Done) Finish("cancelled", null); }; FormClosed += delegate { Clock.Stop(); Clock.Dispose(); };
        }
        internal void Finish(string status, Dictionary<string, object> value) {
            if (Done) return; Done = true; Clock.Stop(); if (value == null) value = new Dictionary<string, object>(); value["nonce"] = NonceValue; value["status"] = status;
            Write(Response, value); Close();
        }
        internal virtual void Escape() { Finish("cancelled", null); }
        internal virtual void TimedOut() { Finish("cancelled", new Dictionary<string, object> { { "code", "timeout" } }); }
        void Ready() {
            if (Done || !Visible || !IsHandleCreated) return;
            Write(Response + ".ready.json", new Dictionary<string, object> { { "nonce", NonceValue }, { "status", "ready" }, { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", Handle.ToInt64() } });
        }
    }
    sealed class Picker : SessionForm
    {
        readonly Target target; readonly PictureBox picture = new PictureBox(), preview = new PictureBox(); readonly Label info = new Label();
        readonly Button confirm = Button("이 이미지로 선택"), capture = Button("대상 창을 가져와 캡처"), again = Button("다시 선택");
        Bitmap frame, crop; Rectangle selection, display, capturedBounds; Point start, anchor; bool dragging, anchorChosen; DateTime captureAt, captureDeadline, autoCaptureAt;
        internal Picker(Dictionary<string, object> data, string response) : base(data, response, 180) {
            target = new Target(data); Style(this, "화면에서 대상 선택", new Size(1050, 760)); MinimumSize = new Size(780, 580);
            bool includeNative = Object.Equals(data.ContainsKey("include_native") ? data["include_native"] : null, true);
            bool includeText = Object.Equals(data.ContainsKey("include_text") ? data["include_text"] : null, true);
            confirm.Text = "이 대상으로 선택";
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 3, Padding = new Padding(20) };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 76)); layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 24));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 86)); layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 100));
            info.Name = info.AccessibleName = "CaptureStatus";
            info.Text = target.Label + "\n화면을 캡처한 다음, 누를 곳을 한 번 클릭하거나 원하는 영역을 드래그하세요.\n이 선택창에서는 프로그램에 클릭이나 입력을 보내지 않습니다."; info.Dock = DockStyle.Fill; layout.Controls.Add(info, 0, 0); layout.SetColumnSpan(info, 2);
            picture.Name = picture.AccessibleName = "TargetScreenshot"; picture.Dock = DockStyle.Fill; picture.BackColor = Color.FromArgb(226, 233, 242); picture.Paint += Draw; picture.MouseDown += Begin; picture.MouseMove += DragSelection; picture.MouseUp += End; layout.Controls.Add(picture, 0, 1);
            preview.Name = preview.AccessibleName = "TargetPreview"; preview.Dock = DockStyle.Fill; preview.SizeMode = PictureBoxSizeMode.Zoom; preview.BackColor = Color.White; layout.Controls.Add(preview, 1, 1);
            preview.MouseClick += delegate(object sender, MouseEventArgs e) {
                if (crop == null) return; double scale = Math.Min((double)preview.Width / crop.Width, (double)preview.Height / crop.Height);
                int left = (preview.Width - (int)(crop.Width * scale)) / 2, top = (preview.Height - (int)(crop.Height * scale)) / 2;
                int x = (int)((e.X - left) / scale), y = (int)((e.Y - top) / scale);
                if (x < 0 || y < 0 || x >= crop.Width || y >= crop.Height) return;
                anchor = new Point(selection.X + x, selection.Y + y); anchorChosen = true; confirm.Enabled = true;
                info.Text = "클릭할 위치를 지정했습니다. 오른쪽의 파란 표시를 확인하고 선택을 완료하세요.\n같은 버튼이 여러 개면 주변 이름까지 포함해 선택해야 구별할 수 있습니다."; preview.Invalidate();
            };
            preview.Paint += delegate(object sender, PaintEventArgs e) {
                if (crop == null || !anchorChosen) return; double scale = Math.Min((double)preview.Width / crop.Width, (double)preview.Height / crop.Height);
                float x = (preview.Width - (float)(crop.Width * scale)) / 2 + (float)((anchor.X - selection.X) * scale), y = (preview.Height - (float)(crop.Height * scale)) / 2 + (float)((anchor.Y - selection.Y) * scale);
                using (Pen pen = new Pen(Color.FromArgb(37, 99, 235), 3)) { e.Graphics.DrawEllipse(pen, x - 7, y - 7, 14, 14); e.Graphics.DrawLine(pen, x - 12, y, x + 12, y); e.Graphics.DrawLine(pen, x, y - 12, x, y + 12); }
            };
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true }; buttons.Controls.Add(capture); buttons.Controls.Add(again); buttons.Controls.Add(confirm); Button cancel = Button("취소"); buttons.Controls.Add(cancel); layout.Controls.Add(buttons, 0, 2); layout.SetColumnSpan(buttons, 2); Controls.Add(layout);
            confirm.Enabled = false; again.Enabled = false;
            capture.Click += delegate {
                // Retire both the screenshot and its confirmation before trying
                // again. A failed capture must never leave an old crop saveable.
                ClearCapture();
                if (!target.Valid()) { info.Text = CaptureMessage("target_unavailable"); return; }
                capture.Enabled = false; Hide();
                if (IsIconic(target.Hwnd)) ShowWindow(target.Hwnd, 9);
                SetForegroundWindow(target.Hwnd);
                captureAt = DateTime.UtcNow.AddMilliseconds(200); captureDeadline = DateTime.UtcNow.AddSeconds(2);
            };
            Clock.Tick += delegate {
                if (Done || IsDisposed) { captureAt = DateTime.MinValue; return; }
                if (autoCaptureAt != DateTime.MinValue && DateTime.UtcNow >= autoCaptureAt) { autoCaptureAt = DateTime.MinValue; capture.PerformClick(); return; }
                if (captureAt == DateTime.MinValue || DateTime.UtcNow < captureAt) return;
                try {
                    frame = Capture(target);
                    capturedBounds = target.CaptureBounds();
                    info.Text = "누를 곳을 한 번 클릭하거나 원하는 영역을 드래그하세요.\n주변 문맥은 자동으로 보완합니다. 오른쪽 파란 표시가 실제 누를 위치입니다.";
                } catch (Exception e) {
                    // Re-observe within the same deadline while activation or
                    // the existing bounded capture worker finishes. Neither
                    // activation nor application input is repeated here.
                    bool settling = e.Message == "target_requires_foreground" || e.Message == "target_minimized" || e.Message == "target_changed" || e.Message == "target_occluded"
                        || e.Message == "target_render_waiting" || e.Message == "target_capture_busy";
                    if (settling && DateTime.UtcNow < captureDeadline) { captureAt = DateTime.UtcNow.AddMilliseconds(80); return; }
                    info.Text = CaptureMessage(e.Message) + CaptureBackendDiagnostic(target);
                }
                captureAt = DateTime.MinValue; capture.Enabled = true; Show(); Activate(); picture.Invalidate();
            };
            again.Click += delegate { selection = Rectangle.Empty; confirm.Enabled = false; preview.Image = null; picture.Invalidate(); };
            cancel.Click += delegate { Finish("cancelled", null); };
            confirm.Click += delegate {
                try { if (frame == null || !target.Valid() || target.CaptureBounds().Size != frame.Size) throw new InvalidOperationException("target_changed");
                    if (!anchorChosen) throw new InvalidOperationException("anchor_required");
                    Dictionary<string, object> semantic = null;
                    if (includeNative) {
                        // The same chosen point supplies both recognition paths.
                        // Never ask for another F8 selection when UIA is absent.
                        Hide(); SetForegroundWindow(target.Hwnd);
                        if (target.Foreground() && target.CaptureBounds() == capturedBounds) {
                            bool protectedInput = false;
                            var pickedPoint = new POINT { X = capturedBounds.X + anchor.X, Y = capturedBounds.Y + anchor.Y };
                            var worker = new Thread(delegate() {
                                try { semantic = ReadPickedNative(target, pickedPoint, includeText); }
                                catch (InvalidOperationException error) { protectedInput = error.Message == "protected"; }
                                catch { }
                            });
                            worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
                            if (worker.Join(600)) {
                                if (protectedInput) { Show(); Activate(); info.Text = "비밀번호나 보호된 입력은 저장하지 않습니다. 다른 대상을 선택하세요."; return; }
                            }
                        }
                    }
                    // UIA is independent of bitmap detail. A tiny but valid
                    // button must reach Driver validation even if its pixels
                    // are not a safe standalone replay template.
                    if (!Visible) { Show(); Activate(); }
                    confirm.Enabled = capture.Enabled = again.Enabled = false;
                    info.Text = "선택한 위치는 그대로 두고, 주변 이름과 그림으로 구별할 수 있는지 확인하고 있습니다.";
                    Bitmap chosenFrame = (Bitmap)frame.Clone(); Rectangle chosenRegion = selection; Point chosenAnchor = anchor;
                    Dictionary<string, object> chosenNative = semantic;
                    var imageWorker = new Thread(delegate() {
                        Dictionary<string, object> result = null; string error = null;
                        try { using (chosenFrame) result = AuthorImage(chosenFrame, chosenRegion, chosenAnchor, new List<Rectangle>()); }
                        catch (Exception problem) { error = problem is InvalidOperationException ? problem.Message : "image_authoring_failed"; }
                        if (Done || IsDisposed) return;
                        try { BeginInvoke((Action)delegate {
                            if (Done || IsDisposed) return;
                            if (result == null && chosenNative == null) {
                                confirm.Enabled = capture.Enabled = again.Enabled = true;
                                info.Text = error == "image_ambiguous" ? "같은 모양을 구별할 이름이 부족합니다. 누를 위치는 유지했습니다. 해당 행의 이름까지 포함해 영역만 넓혀 주세요."
                                    : error == "image_match_timeout" ? "주변 이미지 비교 시간이 길어졌습니다. 행 이름과 버튼이 함께 들어오도록 범위를 좁혀 주세요."
                                    : error == "template_low_detail" ? "주변까지 확인했지만 구별할 그림이나 글자가 없습니다. 해당 버튼과 가까운 이름을 함께 포함해 주세요."
                                    : "선택한 영역을 확인하지 못했습니다. 다시 캡처해 주세요. [" + error + "]";
                                return;
                            }
                            if (result == null) result = new Dictionary<string, object> { { "capture_issue", error } };
                            if (chosenNative != null) result["native_target"] = chosenNative;
                            result["human_confirmed"] = true; result["pid"] = target.Pid; result["window_id"] = target.Hwnd.ToInt64();
                            result["chosen_point"] = new Dictionary<string, object> { { "x", chosenAnchor.X }, { "y", chosenAnchor.Y } };
                            Finish("selected", result);
                        }); } catch (InvalidOperationException) { }
                    }); imageWorker.IsBackground = true; imageWorker.Start();
                } catch (Exception e) { if (!Visible) { Show(); Activate(); } info.Text = "선택한 크기 또는 창 상태가 바뀌었습니다. 다시 캡처해 주세요. [" + e.Message + "]"; }
            };
            if (Object.Equals(data.ContainsKey("auto_capture") ? data["auto_capture"] : null, true))
                Shown += delegate { autoCaptureAt = DateTime.UtcNow.AddMilliseconds(800); };
            FormClosed += delegate { if (frame != null) frame.Dispose(); if (crop != null) crop.Dispose(); target.Release(); };
        }
        internal static Dictionary<string, object> ReadPickedNative(Target target, POINT point, bool includeText) {
            if (!target.Valid() || !target.Contains(point)) return null;
            var chain = ReadOwnedSemanticChain(AutomationElement.FromPoint(new System.Windows.Point(point.X, point.Y)), target, true);
            if (chain == null) return null;
            int chosen = -1;
            for (int i = 0; i < chain.Count; i++) {
                var type = chain[i].Current.ControlType;
                // An emulator's play icon can expose a containing ListItem.
                // Promoting that row into a Button repeats the original bug.
                // Only a real action control may replace the chosen pixels;
                // collection rows/containers stay image targets.
                bool actionControl = type == ControlType.Button || type == ControlType.Edit || type == ControlType.ComboBox
                    || type == ControlType.CheckBox || type == ControlType.RadioButton || type == ControlType.Hyperlink
                    || type == ControlType.Slider || type == ControlType.ScrollBar || type == ControlType.TabItem
                    || type == ControlType.MenuItem || i == 0 && (type == ControlType.Document || includeText && type == ControlType.Text);
                if (actionControl) { chosen = i; break; }
                if (type != ControlType.Text && type != ControlType.Pane && type != ControlType.Custom) break;
                if (i >= 3) break;
            }
            if (chosen < 0) return null;
            AutomationElement element = chain[chosen]; System.Windows.Rect bounds = element.Current.BoundingRectangle;
            if (bounds.IsEmpty || !bounds.Contains(point.X, point.Y)) return null;
            var ancestors = new List<Dictionary<string, object>>();
            for (int i = chosen + 1; i < chain.Count; i++) ancestors.Add(PickedIdentity(chain[i]));
            Rectangle window = target.Bounds();
            return new Dictionary<string, object> { { "status", "selected" }, { "pid", target.Pid }, { "window_id", target.Hwnd.ToInt64() },
                { "human_confirmed", true }, { "element", PickedIdentity(element) }, { "ancestors", ancestors },
                { "point", new Dictionary<string, object> { { "x", point.X }, { "y", point.Y } } },
                { "bounds", new Dictionary<string, object> { { "x", bounds.X }, { "y", bounds.Y }, { "width", bounds.Width }, { "height", bounds.Height } } },
                { "window_bounds", new Dictionary<string, object> { { "x", window.X }, { "y", window.Y }, { "width", window.Width }, { "height", window.Height } } } };
        }
        static Dictionary<string, object> PickedIdentity(AutomationElement element) {
            string name = element.Current.Name ?? "", id = element.Current.AutomationId ?? "";
            if (name.Length > 1000 || id.Length > 1000) throw new InvalidOperationException("identity_too_long");
            return new Dictionary<string, object> { { "role", element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") }, { "name", name }, { "automation_id", id }, { "is_password", false } };
        }
        void ClearCapture() {
            preview.Image = null; if (crop != null) { crop.Dispose(); crop = null; } if (frame != null) { frame.Dispose(); frame = null; }
            selection = display = Rectangle.Empty; dragging = anchorChosen = false; picture.Capture = false;
            confirm.Enabled = again.Enabled = false; picture.Invalidate(); preview.Invalidate();
        }
        Point ImagePoint(Point p) { return new Point(Math.Max(0, Math.Min(frame.Width, (p.X - display.X) * frame.Width / Math.Max(1, display.Width))), Math.Max(0, Math.Min(frame.Height, (p.Y - display.Y) * frame.Height / Math.Max(1, display.Height)))); }
        void Draw(object sender, PaintEventArgs e) {
            if (frame == null) return; double scale = Math.Min((double)picture.Width / frame.Width, (double)picture.Height / frame.Height);
            display = new Rectangle((picture.Width - (int)(frame.Width * scale)) / 2, (picture.Height - (int)(frame.Height * scale)) / 2, (int)(frame.Width * scale), (int)(frame.Height * scale)); e.Graphics.DrawImage(frame, display);
            if (!selection.IsEmpty) using (Pen pen = new Pen(Color.FromArgb(37, 99, 235), 3)) e.Graphics.DrawRectangle(pen, display.X + (float)(selection.X * scale), display.Y + (float)(selection.Y * scale), (float)(selection.Width * scale), (float)(selection.Height * scale));
        }
        void Begin(object s, MouseEventArgs e) { if (frame == null || e.Button != MouseButtons.Left || !display.Contains(e.Location)) return; start = ImagePoint(e.Location); dragging = true; picture.Capture = true; }
        void DragSelection(object s, MouseEventArgs e) { if (!dragging) return; Point p = ImagePoint(e.Location); selection = Rectangle.FromLTRB(Math.Min(start.X, p.X), Math.Min(start.Y, p.Y), Math.Max(start.X, p.X), Math.Max(start.Y, p.Y)); picture.Invalidate(); }
        void End(object s, MouseEventArgs e) {
            if (!dragging) return; DragSelection(s, e); dragging = false; picture.Capture = false;
            if (selection.Width < 8 || selection.Height < 8) {
                anchor = new Point(Math.Min(frame.Width - 1, start.X), Math.Min(frame.Height - 1, start.Y));
                int width = Math.Min(48, frame.Width), height = Math.Min(48, frame.Height);
                selection = new Rectangle(Math.Max(0, Math.Min(frame.Width - width, anchor.X - width / 2)), Math.Max(0, Math.Min(frame.Height - height, anchor.Y - height / 2)), width, height);
            } else anchor = new Point(selection.X + selection.Width / 2, selection.Y + selection.Height / 2);
            anchorChosen = true; confirm.Enabled = again.Enabled = true; preview.Image = null; if (crop != null) crop.Dispose();
            crop = frame.Clone(selection, PixelFormat.Format32bppArgb); preview.Image = crop;
            info.Text = "파란 표시가 실제 누를 위치입니다. 맞으면 ‘이 대상으로 선택’을 누르세요.\n위치를 바꾸려면 오른쪽 미리보기를 클릭하세요. 주변 이름은 자동으로 확인합니다.";
            picture.Invalidate(); preview.Invalidate();
        }
    }
    // A provider can block indefinitely. Finishing a recording must not block
    // the UI thread, accept a late value, or require the user to wait first.
    internal sealed class RecordingFinalRead {
        readonly object sync = new object(); readonly DateTime deadline;
        bool ready, discarded; Dictionary<string, object> result;
        internal RecordingFinalRead(Func<Dictionary<string, object>> read, DateTime expires) {
            deadline = expires;
            var worker = new Thread(delegate() {
                Dictionary<string, object> value = null; try { value = read(); } catch { }
                lock (sync) { if (!discarded) { result = value; ready = true; } }
            }); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }
        internal void Cancel() { lock (sync) { discarded = true; result = null; } }
        internal bool TryTake(DateTime now, out Dictionary<string, object> value) {
            lock (sync) {
                value = null;
                if (discarded || now >= deadline) { discarded = true; result = null; return true; }
                if (!ready) return false;
                value = result; result = null; discarded = true; return true;
            }
        }
    }
    internal static bool CanFinalizeRecordedEdit(Dictionary<string, object> before, Dictionary<string, object> after,
            bool replaced, string reason, bool targetCurrent, bool inputUnchanged) {
        object ancestors;
        var native = Map(after, "native_target");
        if (native != null && native.TryGetValue("ancestors", out ancestors) && ancestors is IEnumerable)
            foreach (object ancestor in (IEnumerable)ancestors)
                if (Str(ancestor as Dictionary<string, object>, "role", "") == "ComboBox") return false;
        return targetCurrent && inputUnchanged && reason != "protected_input" && reason != "recording_method_unverified"
            && reason != "shortcut_requires_manual_setup" && reason != "recording_payload_limit"
            && RecordedEditConfirmed(before, after, replaced);
    }
    internal static bool RecordingPointerMatches(long hitRoot, int hitPid, long targetWindow, int targetPid,
            bool valid, bool visible, bool minimized, Rectangle bounds, Point point) {
        // Foreground deliberately is not a prerequisite: the hook observes the
        // first activating click before Windows changes the foreground HWND.
        return hitRoot != 0 && hitRoot == targetWindow && hitPid > 0 && hitPid == targetPid
            && valid && visible && !minimized && bounds.Width > 0 && bounds.Height > 0 && bounds.Contains(point);
    }
    internal static bool RecordingDesiredCheckWitness(bool exactChild, bool latest, bool foreground, int clickedVersion, int currentVersion,
            DateTime clickAt, DateTime observedAt, DateTime now, string role, string property, object value) {
        return exactChild && latest && foreground && clickedVersion == currentVersion && role == "CheckBox" && property == "selected" && value is bool
            && observedAt >= clickAt && observedAt <= now && (now - clickAt).TotalMilliseconds <= 1200;
    }
    internal static bool RecordingNativeComboWitness(bool exactOwner, bool exactList, bool consecutiveTail, bool released, bool ordinaryClicks,
            int openerVersion, int popupVersion, int currentVersion, DateTime beforeAt, DateTime popupAt, DateTime afterAt, object before, object after) {
        return exactOwner && exactList && consecutiveTail && released && ordinaryClicks && popupVersion == openerVersion + 1 && currentVersion == popupVersion
            && beforeAt < popupAt && afterAt >= popupAt && before is string && after is string
            && ((string)after).Length > 0 && ((string)after).Length <= 2000 && !Object.Equals(before, after);
    }
    internal static bool RecordingValueObservationAllowed(double ageSeconds, bool exactPendingComboPair) {
        return ageSeconds >= 0 && (ageSeconds <= 5 || exactPendingComboPair);
    }
    internal static bool RecordingDraftInputCandidate(bool sameTarget, bool currentWindow, bool foreground, bool ordinaryClick,
            bool focusUnchanged, bool protectedInput, double ageSeconds) {
        // This permits only a review candidate, never a replayable input or
        // a claim that the clicked surface is an editable field.
        return sameTarget && currentWindow && foreground && ordinaryClick && focusUnchanged && !protectedInput
            && ageSeconds >= 0 && ageSeconds <= 30;
    }
    internal static bool RecordingSemanticOwnership(int[] pids, long[] windows, bool[] passwords,
            int targetPid, long targetWindow, int hostedPid, long hostedWindow, bool current) {
        if (!current || pids == null || windows == null || passwords == null || pids.Length == 0 || pids.Length > 17
                || pids.Length != windows.Length || pids.Length != passwords.Length || targetPid <= 0 || targetWindow <= 0) return false;
        bool hostedSeen = false, hostedRoot = false, hostSeen = false;
        for (int i = 0; i < pids.Length; i++) {
            if (passwords[i]) return false;
            if (pids[i] == targetPid) {
                if (hostedSeen && !hostedRoot) return false;
                hostSeen = true;
            } else if (hostedPid > 0 && hostedPid != targetPid && hostedWindow > 0 && pids[i] == hostedPid) {
                if (hostSeen) return false;
                hostedSeen = true;
                if (windows[i] == hostedWindow) hostedRoot = true;
            } else return false;
        }
        int last = pids.Length - 1;
        return pids[last] == targetPid && windows[last] == targetWindow && (!hostedSeen || hostedRoot);
    }
    static List<AutomationElement> ReadOwnedSemanticChain(AutomationElement leaf, Target target, bool throwProtected = false) {
        if (leaf == null || !target.Valid()) return null;
        var chain = new List<AutomationElement>(); var pids = new List<int>(); var windows = new List<long>(); var protectedStates = new List<bool>();
        AutomationElement item = leaf;
        for (int level = 0; level < 17 && item != null; level++) {
            int pid = item.Current.ProcessId; if (!target.AcceptsSemanticPid(pid)) return null;
            bool secret = item.Current.IsPassword;
            if (secret) { if (throwProtected) throw new InvalidOperationException("protected"); return null; }
            long window = item.Current.NativeWindowHandle;
            chain.Add(item); pids.Add(pid); windows.Add(window); protectedStates.Add(secret);
            if (window == target.Hwnd.ToInt64()) break;
            item = TreeWalker.ControlViewWalker.GetParent(item);
        }
        return RecordingSemanticOwnership(pids.ToArray(), windows.ToArray(), protectedStates.ToArray(), target.Pid, target.Hwnd.ToInt64(),
            target.HostedPid, target.HostedHwnd.ToInt64(), target.Valid()) ? chain : null;
    }
    internal static bool RecordingPreHoverWitness(DateTime captured, DateTime clicked, int capturedInput, int currentInput,
            Rectangle capturedBounds, Rectangle currentBounds, bool cursorKnown, bool sameCaptureMode, bool occluded) {
        double age = clicked.Subtract(captured).TotalMilliseconds;
        return age >= 0 && age <= 5000 && capturedInput == currentInput && capturedBounds == currentBounds
            && capturedBounds.Width > 0 && capturedBounds.Height > 0 && cursorKnown && sameCaptureMode && !occluded;
    }
    static Pixels RecordingPixels(Bitmap image) {
        // Witnesses use physical source pixels, independent of PNG/display DPI.
        using (var normalized = new Bitmap(image.Width, image.Height, PixelFormat.Format32bppArgb)) {
            using (Graphics graphics = Graphics.FromImage(normalized))
                graphics.DrawImage(image, new Rectangle(0, 0, image.Width, image.Height), 0, 0, image.Width, image.Height, GraphicsUnit.Pixel);
            return new Pixels(normalized);
        }
    }
    // Accept only a witnessed local background-brightness transition. No
    // pixels are synthesized and the replay matcher keeps its normal limits.
    internal static bool RecordingPreHoverFrame(Bitmap before, Bitmap current, Point anchor, Point previousCursor) {
        if (before == null || current == null || before.Size != current.Size) return false;
        var older = RecordingPixels(before); var latest = RecordingPixels(current);
        int left = older.W, top = older.H, right = -1, bottom = -1, changed = 0;
        var colors = new Dictionary<int, int>();
        for (int y = 0; y < older.H; y++) for (int x = 0; x < older.W; x++) {
            int a = y * older.Stride + x * 4, b = y * latest.Stride + x * 4;
            if (older.Data[a] == latest.Data[b] && older.Data[a + 1] == latest.Data[b + 1] && older.Data[a + 2] == latest.Data[b + 2]) continue;
            // Conservative neutral-button hover support. Changed glyphs,
            // coloured state changes, scrolling, and content updates fail closed.
            if (older.Data[a] != older.Data[a + 1] || older.Data[a] != older.Data[a + 2]
                    || latest.Data[b] != latest.Data[b + 1] || latest.Data[b] != latest.Data[b + 2]) return false;
            if (Math.Abs(x - anchor.X) > 160 || Math.Abs(y - anchor.Y) > 160) return false;
            int pair = older.Data[a] * 256 + latest.Data[b], count; colors.TryGetValue(pair, out count); colors[pair] = count + 1;
            left = Math.Min(left, x); top = Math.Min(top, y); right = Math.Max(right, x); bottom = Math.Max(bottom, y); changed++;
        }
        if (changed < 16 || right - left + 1 > 160 || bottom - top + 1 > 160) return false;
        var area = Rectangle.FromLTRB(left, top, right + 1, bottom + 1); var pointerArea = area; pointerArea.Inflate(4, 4);
        if (!pointerArea.Contains(anchor) || pointerArea.Contains(previousCursor)) return false;
        int dominant = 0, maximum = 0; foreach (var pair in colors) if (pair.Value > maximum) { dominant = pair.Key; maximum = pair.Value; }
        if (maximum < changed * .75) return false;
        int oldBackground = dominant / 256, newBackground = dominant % 256;
        if (Math.Abs(oldBackground - newBackground) < 3 || Math.Abs(oldBackground - newBackground) > 80) return false;
        bool white = oldBackground < 250, black = oldBackground > 5;
        for (int y = top; y <= bottom; y++) for (int x = left; x <= right; x++) {
            int oldColor = older.Data[y * older.Stride + x * 4], newColor = latest.Data[y * latest.Stride + x * 4];
            if (oldColor == newColor) continue;
            // Rounded outer borders blend with the unchanged surrounding
            // background rather than the glyph colour. Only that narrow
            // border may use the same-direction, bounded brightness change.
            bool border = x - left < 8 || right - x < 8 || y - top < 8 || bottom - y < 8;
            if (border && Math.Sign(newColor - oldColor) == Math.Sign(newBackground - oldBackground)
                    && Math.Abs(newColor - oldColor) <= Math.Abs(newBackground - oldBackground) + 2) continue;
            if (white && Math.Abs((255.0 - oldColor) * (255.0 - newBackground) / (255.0 - oldBackground) - (255.0 - newColor)) > 2.0) white = false;
            if (black && Math.Abs(oldColor * (double)newBackground / oldBackground - newColor) > 2.0) black = false;
        }
        return white || black;
    }
    sealed class Recorder : SessionForm
    {
        sealed class SharedFrame {
            internal readonly Bitmap Image; int references = 1; internal static long RetainedBytes;
            internal SharedFrame(Bitmap image) { Image = image; System.Threading.Interlocked.Add(ref RetainedBytes, (long)image.Width * image.Height * 4); }
            internal SharedFrame Retain() { System.Threading.Interlocked.Increment(ref references); return this; }
            internal void Release() { if (System.Threading.Interlocked.Decrement(ref references) == 0) { System.Threading.Interlocked.Add(ref RetainedBytes, -(long)Image.Width * Image.Height * 4); Image.Dispose(); } }
        }
        sealed class CachedFrame {
            internal SharedFrame Source; internal string SourceMode; internal Rectangle Bounds; internal DateTime At,InputAt; internal bool Rendered, ExcludesLayered; internal List<Rectangle> Occluders;
            internal List<Dictionary<string, object>> OcclusionDetails; internal Point Cursor; internal bool CursorKnown; internal int InputVersion;
        }
        readonly Dictionary<Target,CapturePump> recordingPumps=new Dictionary<Target,CapturePump>();
        Bitmap RecordingRender(Target target,Rectangle visible) {
            Rectangle raw=target.Bounds();CapturePump pump;string previousFailure=null;double previousFutureLead=0;
            if(recordingPumps.TryGetValue(target,out pump)&&(!pump.Matches(target,raw,visible)
                    ||pump.Finished&&pump.Error is InvalidOperationException&&pump.Error.Message!="target_render_unavailable"&&pump.Error.Message!="target_render_incomplete")) {
                var changed=pump.Error as CaptureChangedException;
                if(changed!=null)previousFutureLead=changed.OffsetMilliseconds;
                previousFailure=pump.Error==null?"target_changed":changed==null?RecordingStartCode(pump.Error.Message):changed.Reason;pump.Stop();recordingPumps.Remove(target);pump=null;
            }
            if(pump==null){pump=new CapturePump(target,raw,visible,previousFailure,previousFutureLead,inputVersion);recordingPumps[target]=pump;}
            pump.RequestEpoch(inputVersion);
            try {return pump.Snapshot(target,raw,visible,inputChangedAt);}
            catch(InvalidOperationException error) {
                // Older Windows / unsupported renderers retain the bounded
                // legacy path. Waiting, identity and geometry never fall back.
                if(error.Message!="target_render_unavailable"&&error.Message!="target_render_incomplete")throw;
                return RenderWindow(target,visible,inputChangedAt);
            }
        }
        sealed class Pending {
            internal Target Target; internal string Operation, RequestedOperation, Reason; internal Point ScreenPoint; internal Size FrameSize; internal string CaptureMode;
            internal int Wheel; internal string Key; internal string[] Keys; internal Dictionary<string, object> Baseline; internal bool ReplaceAll; internal AutomationElement EditElement;
            internal Dictionary<string, object> Saved; internal int Index = -1;
            internal SemanticSample Semantic; internal IntPtr HitWindow; internal Rectangle HitBounds; internal uint HitThread; internal int HitControlId; internal string HitClass; internal DateTime ClickAt; internal bool HitEnabled, ProbeRequested, ForegroundBeforeInput;
            internal bool SemanticBeforeInput; internal int InputVersion; internal IntPtr PointWindow, ComboListWindow; internal uint PointThread; internal string PointClass;
            internal SharedFrame Source; internal Rectangle Region; internal Point SourceAnchor; internal List<Rectangle> Blocked;
            internal Dictionary<string, object> CaptureDiagnostic;
            internal readonly List<CachedFrame> BeforeHover = new List<CachedFrame>();
            internal Pending ImageOwner, DraftPreparation; internal bool DraftInputTarget, DraftInputFocusValid = true;
            internal readonly List<Pending> ImageDependants = new List<Pending>(); internal bool AuthorCompleted, ValueConfirmed, ValueSuperseded, ImageFocusValid = true; internal DateTime LastInputAt;
        }
        sealed class SemanticSample { internal AutomationElement Element; internal Dictionary<string, object> Native; internal string Identity, Role, Property; internal object Value; internal bool Enabled; internal DateTime ObservedAt; internal IntPtr NativeWindow; }
        readonly List<Target> targets = new List<Target>(); readonly List<Dictionary<string, object>> events = new List<Dictionary<string, object>>();
        readonly Queue<Pending> pending = new Queue<Pending>(); readonly List<string> warnings = new List<string>(); readonly int maximum;
        readonly Queue<Pending> authorQueue = new Queue<Pending>(); bool authorBusy, completeRequested, payloadLimited; SharedFrame sharedFrame;
        readonly RecordingPayloadBudget payloadBudget = new RecordingPayloadBudget();
        readonly Dictionary<Target, CachedFrame> frameCache = new Dictionary<Target, CachedFrame>();
        readonly Dictionary<Target, List<CachedFrame>> hoverHistory = new Dictionary<Target, List<CachedFrame>>(); int inputVersion; DateTime inputChangedAt;
        string lastCaptureFailure; DateTime lastCaptureFailureAt;
        Target preparingTarget; DateTime prepareAt, prepareDeadline;
        Dictionary<string, object> startDiagnostic; bool startActivationReturned, startActivationRequested, startToolbarMinimized;
        readonly string popupPrefix = "popup_" + Guid.NewGuid().ToString("N").Substring(0, 8) + "_";
        readonly Label info = new Label(); readonly ListBox list = new ListBox(); readonly Button start = Button("기록 시작"), pause = Button("일시정지"), stop = Button("기록 마치고 검토");
        Hook mouseProc, keyProc; WindowEvent windowProc; IntPtr mouseHook, keyHook, windowHook; bool recording, textBusy, outside, popupWakePending, ticking; int generation, injected, sampleVersion, activeWorkers;
        readonly Dictionary<Target, DateTime> sampleRetryAt = new Dictionary<Target, DateTime>();
        readonly Dictionary<Target, int> sampleWorkers = new Dictionary<Target, int>(); Target samplingTarget;
        bool compact, recorderSizing; double normalClientWidth = 650; TableLayoutPanel recorderLayout; FlowLayoutPanel recorderButtons;
        string recordState = "checking", hookProbe = "checking", uiaProbe = "checking"; DateTime heartbeatAt, probeAt;
        volatile bool probeDone, probeGood; bool probeSettled;
        DateTime frameAt, frameInputAt, dirtyAt, lastSample, workerStarted, typingConfirmedAt;int frameInputVersion; Target frameTarget; Rectangle frameBounds; Bitmap frame; string frameSourceMode; bool frameRendered, frameExcludesLayered; List<Rectangle> frameOccluders = new List<Rectangle>();
        List<Dictionary<string, object>> frameOcclusionDetails = new List<Dictionary<string, object>>();
        Dictionary<string, object> lastImage, focusedState; Target lastImageTarget, focusedTarget; DateTime focusedAt; Point lastImagePoint;
        AutomationElement focusedEdit, completedEdit; RecordingFinalRead finalRead; Pending finalTyping; bool finalReadAttempted;
        Pending mouseDown, typing, lastPointer, selectAllPreparation; readonly List<Pending> clickEvidence = new List<Pending>(); List<KeyValuePair<Pending, SemanticSample>> completedClicks, completedValues; Point mouseStart; readonly object stateLock = new object(); Dictionary<string, object> completedState; Target completedTarget; int completedGeneration; bool workerCompleted;
        SemanticSample hovered, completedHover, tracked, completedTracked; Target hoverTarget, trackedTarget; DateTime hoverAt, trackedAt; Dictionary<string, object> trackedEvent; int trackedIndex; bool trackedKeyboard;
        internal Recorder(Dictionary<string, object> data, string response) : base(data, response, 1800) {
            maximum = Num(data, "max_events", MaximumRecordingEvents); if (!ValidRecordingEventLimit(maximum)) throw new InvalidOperationException("invalid_event_limit");
            object values; if (!data.TryGetValue("targets", out values) || !(values is IEnumerable)) throw new InvalidOperationException("targets_required");
            foreach (object item in (IEnumerable)values) { if (targets.Count >= 10) throw new InvalidOperationException("too_many_targets"); targets.Add(new Target(item as Dictionary<string, object>)); } if (targets.Count == 0) throw new InvalidOperationException("targets_required");
            SuspendLayout();
            Style(this, "동작 기록 — 선택한 프로그램만 기록", new Size(650, 390)); AutoScaleDimensions = new SizeF(96, 96);
            MinimumSize = new Size(540, 210); TopMost = true;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, Padding = new Padding(16) };
            recorderLayout = layout;
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 118)); layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 70));
            info.Dock = DockStyle.Fill; info.Text = "기록 중 클릭과 입력은 실제 프로그램에 적용됩니다.\n같은 프로그램이 연 팝업도 함께 기록합니다.\n암호와 알 수 없는 입력칸의 글자는 저장하지 않습니다.\n관련 없는 창에서는 자동으로 멈춥니다. Esc: 기록 일시정지";
            list.Dock = DockStyle.Fill; var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true, Margin = Padding.Empty };
            recorderButtons = buttons; Button cancel = Button("취소"), tuck = Button("접어두기");
            start.Width = pause.Width = cancel.Width = tuck.Width = 92; stop.Width = 174;
            tuck.Click += delegate { WindowState = FormWindowState.Minimized; };
            buttons.Controls.Add(start); buttons.Controls.Add(pause); buttons.Controls.Add(stop); buttons.Controls.Add(tuck); buttons.Controls.Add(cancel);
            layout.Controls.Add(info, 0, 0); layout.Controls.Add(list, 0, 1); layout.Controls.Add(buttons, 0, 2); Controls.Add(layout);
            start.Enabled = pause.Enabled = stop.Enabled = false;
            start.Click += delegate { if (payloadLimited) return; try { PrepareRecording(); } catch (Exception error) {
                if (error.Message == "recording_hook_unavailable") Failure("recording_hook_unavailable");
                else RecordingStartFailed(error.Message);
            } };
            pause.Click += delegate { Drain(); Pause(); };
            stop.Click += delegate { CompleteRecording(); };
            cancel.Click += delegate { Finish("cancelled", null); };
            Clock.Tick += Tick; FormClosed += delegate { if (finalRead != null) finalRead.Cancel(); UninstallHooks(); ReleaseFrame(); ClearFrames(); while (pending.Count > 0) DisposePending(pending.Dequeue()); while (authorQueue.Count > 0) DisposePending(authorQueue.Dequeue()); foreach (Target t in targets) t.Release(); focusedState = null; focusedEdit = null; };
            ResumeLayout(false);
            ResizeEnd += delegate {
                if (WindowState != FormWindowState.Normal || recorderSizing) return;
                float dpi; using (Graphics graphics = CreateGraphics()) dpi = graphics.DpiY / 96f;
                normalClientWidth = Math.Max(540, ClientSize.Width / dpi); Compact(compact);
            };
            Shown += delegate {
                Compact(false);
                StartProbe();
            };
        }
        static void DisposePending(Pending p) { if (p.Source != null) { p.Source.Release(); p.Source = null; } foreach (CachedFrame old in p.BeforeHover) old.Source.Release(); p.BeforeHover.Clear(); }
        static void RetireFoldedPointer(Pending p) {
            p.Saved = null; p.Index = -1; p.ValueSuperseded = true;
            // A queued/running image author owns these pixels until its
            // existing completion/finally path releases them. Its callback
            // already ignores a removed event. Do not invalidate that work.
            if (p.AuthorCompleted) DisposePending(p);
        }
        void ReleaseFrame() { if (sharedFrame != null) { sharedFrame.Release(); sharedFrame = null; } else if (frame != null) frame.Dispose(); frame = null; }
        void ClearHoverHistory() { foreach (List<CachedFrame> history in hoverHistory.Values) foreach (CachedFrame old in history) old.Source.Release(); hoverHistory.Clear(); }
        void ClearFrames() { foreach(CapturePump pump in recordingPumps.Values)pump.Stop();recordingPumps.Clear();foreach (CachedFrame cached in frameCache.Values) cached.Source.Release(); frameCache.Clear(); ClearHoverHistory(); }
        void InputChanged() { inputVersion++; inputChangedAt=DateTime.UtcNow;foreach(CapturePump pump in recordingPumps.Values)pump.RequestEpoch(inputVersion); ClearHoverHistory(); }
        static string RecordingStartCode(string value) {
            if (String.IsNullOrEmpty(value) || value.Length > 80) return "recording_start_failed";
            foreach (char c in value) if ((c < 'a' || c > 'z') && c != '_') return "recording_start_failed";
            return value;
        }
        void RecordingStartFailed(string failure) {
            string code = RecordingStartCode(failure); Target target = preparingTarget;
            IntPtr foreground = GetForegroundWindow(); uint foregroundPid; GetWindowThreadProcessId(foreground, out foregroundPid);
            // Snapshot before restoring this toolbar: Activate() changes the evidence.
            // Store only window/process scope metadata, never titles or captured content.
            startDiagnostic = new Dictionary<string, object> {
                { "code", code }, { "target_window_id", target == null ? 0L : target.Hwnd.ToInt64() },
                { "target_pid", target == null ? 0 : target.Pid }, { "target_valid", target != null && target.Valid() },
                { "target_visible", target != null && IsWindowVisible(target.Hwnd) },
                { "target_minimized", target != null && IsIconic(target.Hwnd) },
                { "foreground_window_id", foreground.ToInt64() }, { "foreground_pid", foregroundPid },
                { "foreground_root_id", GetAncestor(foreground, 2).ToInt64() },
                { "foreground_matches_target", target != null && target.AcceptsForeground(foreground) },
                { "activation_requested", startActivationRequested }, { "activation_returned", startActivationReturned },
                { "toolbar_minimized", startToolbarMinimized }
            };
            if (target != null) try {
                var occlusion = OcclusionDetails(target, false); int layered = 0;
                foreach (var item in occlusion) {
                    bool isLayered = (GetWindowLong(new IntPtr(Convert.ToInt64(item["window_id"])), -20) & 0x80000) != 0;
                    item["layered"] = isLayered; if (isLayered) layered++;
                }
                startDiagnostic["occlusion_details"] = occlusion;
                startDiagnostic["occlusion_count_reported"] = occlusion.Count;
                startDiagnostic["layered_occlusion_count_reported"] = layered;
                startDiagnostic["occlusion_limit_reached"] = occlusion.Count == 12;
            } catch { startDiagnostic["occlusion_details_unavailable"] = true; }
            AppendCaptureBackendDiagnostic(target, startDiagnostic);
            // Restore before resizing the compact layout. ClientSize on a
            // minimized form describes its icon strip and can clip all tools.
            WindowState = FormWindowState.Normal; Pause(); Activate(); stop.Enabled = events.Count > 0;
            info.Text = RecordingStartMessage(code) + (target == null ? "" : CaptureBackendDiagnostic(target));
            Heartbeat(true);
        }
        void PrepareRecording() {
            startDiagnostic = null; startActivationRequested = startActivationReturned = startToolbarMinimized = false;
            InstallHooks(); finalReadAttempted = false; recording = false; generation++; outside = false; InputChanged();
            preparingTarget = null;
            foreach (Target target in targets) if (target.Owner == null && target.Valid()) { preparingTarget = target; break; }
            if (preparingTarget == null) { RecordingStartFailed("target_unavailable"); return; }
            recordState = "checking"; start.Enabled = pause.Enabled = stop.Enabled = false; Compact(true);
            info.Text = "대상 창을 앞으로 가져와 준비하고 있습니다.\n‘기록 중’으로 바뀌면 동작하세요. 아직 입력은 기록하지 않습니다."; Heartbeat(true);
            if (IsIconic(preparingTarget.Hwnd)) ShowWindow(preparingTarget.Hwnd, 9);
            // Transfer focus while the clicked toolbar is still the foreground window.
            // Minimizing it first can let Windows hand foreground ownership elsewhere.
            startActivationRequested = true; startActivationReturned = SetForegroundWindow(preparingTarget.Hwnd);
            if (Bounds.IntersectsWith(preparingTarget.Bounds())) { WindowState = FormWindowState.Minimized; startToolbarMinimized = true; }
            prepareAt = DateTime.UtcNow.AddMilliseconds(160); prepareDeadline = DateTime.UtcNow.AddSeconds(2);
        }
        void PrepareTick() {
            if (DateTime.UtcNow < prepareAt) return;
            string failure = "target_requires_foreground";
            try {
                if (!preparingTarget.Valid()) throw new InvalidOperationException("target_changed");
                if (preparingTarget.ImageAllowed) preparingTarget.EnsureCaptureReady(); else preparingTarget.EnsureWindowReady();
                if (preparingTarget.ImageAllowed) CaptureFor(preparingTarget, true);
                preparingTarget = null; recording = true; recordState = "recording";
                pause.Enabled = stop.Enabled = true; info.Text = ActiveText(); Heartbeat(true); return;
            } catch (Exception error) { failure = error.Message; }
            if (DateTime.UtcNow < prepareDeadline) { prepareAt = DateTime.UtcNow.AddMilliseconds(80); return; }
            RecordingStartFailed(failure);
        }
        bool UseFrame(Target target) {
            CachedFrame cached;
            if (!frameCache.TryGetValue(target, out cached) || !RecordingFrameFresh(cached.Source.Image.Tag as CapturedFrameInfo,cached.At,DateTime.UtcNow)
                || !CapturedFrameAfterInput(cached.InputAt,inputChangedAt) || cached.InputVersion!=inputVersion) return false;
            ReleaseFrame(); sharedFrame = cached.Source.Retain(); frame = sharedFrame.Image; frameTarget = target; frameAt = cached.At;frameInputAt=cached.InputAt;frameInputVersion=cached.InputVersion;
            frameBounds = cached.Bounds; frameRendered = cached.Rendered; frameExcludesLayered = cached.ExcludesLayered; frameSourceMode = cached.SourceMode; frameOccluders = cached.Occluders; frameOcclusionDetails = cached.OcclusionDetails; return true;
        }
        void CaptureUnavailable(Target target,Exception error) {
            // A pending replacement is not evidence against an existing exact
            // input-epoch snapshot. Recheck its original time and epoch, without
            // extending either. Identity/focus/geometry failures still clear it.
            if(error is InvalidOperationException&&error.Message=="target_render_waiting"&&UseFrame(target))return;
            ReleaseFrame();
        }
        void RestoreClickFrame(Target target) {
            if(frame==null||frameTarget!=target||frameInputVersion!=inputVersion)UseFrame(target);
        }
        void AppendFrameEpochDiagnostic(Target target,Dictionary<string,object> diagnostic,DateTime now) {
            diagnostic["recording_input_epoch"]=inputVersion;
            diagnostic["recording_active_frame_epoch"]=frame==null?-1:frameInputVersion;
            CachedFrame cached;bool found=frameCache.TryGetValue(target,out cached);
            diagnostic["recording_cached_frame_epoch"]=found?cached.InputVersion:-1;
            diagnostic["recording_cached_frame_age_ms"]=found?Math.Round(Math.Max(-1,Math.Min(60000,now.Subtract(cached.At).TotalMilliseconds))):-1;
            diagnostic["recording_cached_time_epoch_valid"]=found&&cached.InputVersion==inputVersion
                &&CapturedFrameAfterInput(cached.InputAt,inputChangedAt)&&RecordingFrameFresh(cached.Source.Image.Tag as CapturedFrameInfo,cached.At,now);
        }
        void CaptureFor(Target target, bool select) {
            try { CaptureForCore(target,select); lastCaptureFailure=null; }
            catch(Exception error) { lastCaptureFailure=RecordingStartCode(error.Message); lastCaptureFailureAt=DateTime.UtcNow; throw; }
        }
        void CaptureForCore(Target target, bool select) {
            // Semantic-only targets never allocate frames, including popup
            // SHOW notifications that arrive outside the regular timer path.
            if (!target.ImageAllowed) { if (select) ReleaseFrame(); return; }
            foreach (Target historyTarget in new List<Target>(hoverHistory.Keys)) {
                List<CachedFrame> history = hoverHistory[historyTarget];
                for (int i = history.Count - 1; i >= 0; i--) if (historyTarget.Retired || history[i].InputVersion != inputVersion || DateTime.UtcNow.Subtract(history[i].At).TotalSeconds > 5) { history[i].Source.Release(); history.RemoveAt(i); }
                if (history.Count == 0) hoverHistory.Remove(historyTarget);
            }
            foreach (Target stale in new List<Target>(frameCache.Keys)) if (stale != target && (stale.Retired || DateTime.UtcNow.Subtract(frameCache[stale].At).TotalSeconds > 2)) {
                frameCache[stale].Source.Release(); frameCache.Remove(stale);
                List<CachedFrame> oldHistory; if (hoverHistory.TryGetValue(stale, out oldHistory)) { foreach (CachedFrame old in oldHistory) old.Source.Release(); hoverHistory.Remove(stale); }
            }
            // Foreground work has one active target; an old target's pump is
            // stopped before starting another, including geometry changes.
            foreach(Target oldTarget in new List<Target>(recordingPumps.Keys))if(oldTarget!=target){recordingPumps[oldTarget].Stop();recordingPumps.Remove(oldTarget);}
            List<Rectangle> all = Occluders(target), ordinary = Occluders(target, true); Rectangle bounds = target.CaptureBounds(); bool rendered = false;
            if (!RecordingFrameFits(System.Threading.Interlocked.Read(ref SharedFrame.RetainedBytes), bounds.Size)) throw new InvalidOperationException("recording_capture_backlog");
            // A layered window elsewhere on the desktop must not select a
            // different capture path for an unrelated ordinary occluder.
            bool exclude = Covered(bounds, Occluders(target, false, true));
            POINT cursorBefore, cursorAfter; bool beforeKnown = GetCursorPos(out cursorBefore);
            DateTime captureStarted=DateTime.UtcNow; Bitmap next;
            // Harvest the already-running target renderer before another GDI
            // probe or desktop copy can consume the frame's freshness budget.
            // A fully covered screen has no usable fallback pixels at all.
            if (recordingPumps.ContainsKey(target) || HasOwnedRenderJob(target) || (exclude && RecordingFrameFullyCovered(bounds,all))) {
                next=RecordingRender(target,bounds); rendered=true; exclude=false;
            } else if (exclude) {
                next = RecordingLayeredCapture(delegate { return CaptureWindowSurface(target); }, delegate {
                    target.EnsureCaptureReady(); Rectangle expected = target.CaptureBounds();
                    Bitmap isolated = RecordingRender(target, expected);
                    try {
                        target.EnsureCaptureReady();
                        if (target.CaptureBounds() != expected) throw new InvalidOperationException("target_changed");
                        Bitmap result = isolated; isolated = null; return result;
                    } finally { if (isolated != null) isolated.Dispose(); }
                }, delegate { bool screenRendered; return Capture(target, true, out screenRendered); }, out rendered, out exclude);
            } else next = Capture(target, true, out rendered);
            bool afterKnown = GetCursorPos(out cursorAfter);
            SharedFrame captured = null;
            try {
                List<Rectangle> blocked = exclude ? ordinary : all; blocked.AddRange(Occluders(target, exclude));
                if (target.CaptureBounds() != bounds) throw new InvalidOperationException("target_changed");
                // A screen fallback entirely hidden by overlays cannot seed a
                // usable recording. Keep the existing preparation deadline and
                // re-observe its WGC worker instead of announcing false readiness.
                // Partial toolbar overlap still permits uncovered click context.
                if (preparingTarget == target && !rendered && !exclude && RecordingFrameFullyCovered(bounds, blocked))
                    throw new InvalidOperationException("target_render_waiting");
                List<Dictionary<string, object>> details = OcclusionDetails(target, exclude);
                CapturedFrameInfo capturedInfo=rendered ? next.Tag as CapturedFrameInfo : null;
                string sourceMode=capturedInfo==null ? null : capturedInfo.Mode;
                DateTime capturedAt=capturedInfo==null ? captureStarted : capturedInfo.ObservedAt;
                DateTime inputAt=capturedInfo==null?captureStarted:capturedInfo.InputAt;
                int capturedEpoch=capturedInfo==null||capturedInfo.CaptureEpoch<0?inputVersion:capturedInfo.CaptureEpoch;
                if (!RecordingFrameFresh(capturedInfo,capturedAt,DateTime.UtcNow) || !CapturedFrameAfterInput(inputAt,inputChangedAt)
                    || capturedEpoch!=inputVersion) throw new InvalidOperationException("target_render_waiting");
                captured = new SharedFrame(next); next = null;
                CachedFrame previous; frameCache.TryGetValue(target, out previous);
                bool cursorKnown = inputAt >= captureStarted && beforeKnown && afterKnown && cursorBefore.X == cursorAfter.X && cursorBefore.Y == cursorAfter.Y;
                List<CachedFrame> history; if (!hoverHistory.TryGetValue(target, out history)) { history = new List<CachedFrame>(); hoverHistory[target] = history; }
                for (int i = history.Count - 1; i >= 0; i--) if (history[i].InputVersion != inputVersion || history[i].Bounds != bounds || DateTime.UtcNow.Subtract(history[i].At).TotalSeconds > 5) { history[i].Source.Release(); history.RemoveAt(i); }
                if (previous != null && previous.CursorKnown && cursorKnown && previous.InputVersion == inputVersion && previous.Bounds == bounds
                        && DateTime.UtcNow.Subtract(previous.At).TotalMilliseconds <= 250
                        && (Math.Abs(previous.Cursor.X - cursorAfter.X) > 12 || Math.Abs(previous.Cursor.Y - cursorAfter.Y) > 12)) {
                    previous.Source.Retain(); history.Add(previous);
                    while (history.Count > 2) { history[0].Source.Release(); history.RemoveAt(0); }
                }
                frameCache[target] = new CachedFrame { Source = captured, SourceMode = sourceMode, Bounds = bounds, At = capturedAt,InputAt=inputAt, Rendered = rendered, ExcludesLayered = exclude, Occluders = blocked, OcclusionDetails = details,
                    CursorKnown = cursorKnown, Cursor = new Point(cursorAfter.X, cursorAfter.Y), InputVersion = capturedEpoch };
                captured = null; // ownership transferred to frameCache
                if (previous != null) previous.Source.Release();
                if (select && !UseFrame(target)) throw new InvalidOperationException("target_render_waiting");
            } finally {
                // Occluders, window geometry and metadata can all disappear
                // after capture. Every pre-transfer failure releases pixels.
                if (captured != null) captured.Release();
                if (next != null) next.Dispose();
            }
        }
        static int ToolbarPreferredHeight(FlowLayoutPanel buttons, int width) {
            // A fixed row height clips Cancel when DPI/text sizing wraps the
            // last buttons. Measure the real flow layout at its client width.
            return Math.Max(1, buttons.GetPreferredSize(new Size(Math.Max(1, width), 0)).Height);
        }
        void Compact(bool value) {
            compact = value; list.Visible = !value;
            if (recorderSizing || WindowState != FormWindowState.Normal) return;
            recorderSizing = true;
            try {
                float dpi; using (Graphics graphics = CreateGraphics()) dpi = graphics.DpiY / 96f;
                Rectangle working = Screen.FromControl(this).WorkingArea;
                int nonclientWidth = Width - ClientSize.Width, nonclientHeight = Height - ClientSize.Height;
                int availableWidth = Math.Max(1, working.Width - nonclientWidth - (int)Math.Round(16 * dpi));
                // Never inherit minimized/icon-strip dimensions. Remember the
                // normal logical width and apply the same DPI to both axes.
                int width = Math.Min((int)Math.Round(normalClientWidth * dpi), availableWidth);
                int infoHeight = (int)Math.Round((value ? 86 : 118) * dpi);
                int toolbarHeight = ToolbarPreferredHeight(recorderButtons, width - recorderLayout.Padding.Horizontal);
                int contentHeight = infoHeight + toolbarHeight + recorderLayout.Padding.Vertical + (int)Math.Round((value ? 0 : 80) * dpi);
                int height = Math.Max((int)Math.Round((value ? 205 : 390) * dpi), contentHeight);
                int availableHeight = Math.Max(1, working.Height - nonclientHeight - (int)Math.Round(16 * dpi));
                if (height > availableHeight) {
                    toolbarHeight = ToolbarPreferredHeight(recorderButtons, width - recorderLayout.Padding.Horizontal - SystemInformation.VerticalScrollBarWidth);
                    contentHeight = infoHeight + toolbarHeight + recorderLayout.Padding.Vertical + (int)Math.Round((value ? 0 : 80) * dpi);
                    height = Math.Max(height, contentHeight);
                }
                recorderLayout.RowStyles[0].Height = infoHeight;
                recorderLayout.RowStyles[1].SizeType = value ? SizeType.Absolute : SizeType.Percent;
                recorderLayout.RowStyles[1].Height = value ? 0 : 100;
                recorderLayout.RowStyles[2].Height = toolbarHeight;
                AutoScroll = height > availableHeight;
                AutoScrollMinSize = AutoScroll ? new Size(0, height) : Size.Empty;
                recorderLayout.Dock = AutoScroll ? DockStyle.Top : DockStyle.Fill;
                if (AutoScroll) recorderLayout.Height = height;
                ClientSize = new Size(width, Math.Min(height, availableHeight));
                recorderLayout.PerformLayout(); PlaceToolbar();
            } finally { recorderSizing = false; }
        }
        void PlaceToolbar() {
            Point selected = DesktopLocation; long best = Int64.MaxValue;
            foreach (Screen display in Screen.AllScreens) {
                Rectangle screen = display.WorkingArea;
                Point[] positions = { new Point(screen.Right - Width - 8, screen.Bottom - Height - 8), new Point(screen.Left + 8, screen.Bottom - Height - 8), new Point(screen.Right - Width - 8, screen.Top + 8), new Point(screen.Left + 8, screen.Top + 8) };
                foreach (Point p in positions) { long covered = 0; Rectangle candidate = new Rectangle(p, Size);
                    foreach (Target t in targets) if (t.Valid()) { Rectangle overlap = Rectangle.Intersect(candidate, t.Bounds()); covered += (long)overlap.Width * overlap.Height; }
                    if (covered < best) { best = covered; selected = p; }
                }
            }
            DesktopLocation = selected;
        }
        internal override void Escape() { WindowState = FormWindowState.Normal; Drain(); Pause(); }
        internal override void TimedOut() { Warn("recording_timeout_partial"); CompleteRecording(); }
        void Warn(string code) { if (!warnings.Contains(code)) warnings.Add(code); }
        void Failure(string code) { recording = false; generation++; Warn(code); Drain(); FinalizePendingInput(); recordState = "failed"; start.Enabled = false; pause.Enabled = false; stop.Enabled = events.Count > 0; info.Text = "기록 오류로 중지했습니다. 지금까지 기록한 부분만 검토할 수 있습니다.\n" + code; Heartbeat(true); }
        void CompleteRecording() {
            if (finalRead != null) return;
            Drain();
            if (!finalReadAttempted) {
                finalReadAttempted = true;
                if (typing != null && typing.Saved != null && typing.EditElement != null && typing.Baseline != null && activeWorkers < 4
                        && dirtyAt > typingConfirmedAt && CanFinalizeReason(typing) && FinalTargetCurrent(typing.Target)) {
                    // Freeze collection first. Read the exact previously owned
                    // Edit, never the recorder's newly focused button or a point.
                    recording = false; generation++; recordState = "review";
                    start.Enabled = pause.Enabled = stop.Enabled = false;
                    finalTyping = typing;
                    Pending candidate = typing;
                    System.Threading.Interlocked.Increment(ref activeWorkers);
                    finalRead = new RecordingFinalRead(delegate { try { return ReadFinalEdit(candidate); } finally { System.Threading.Interlocked.Decrement(ref activeWorkers); } }, DateTime.UtcNow.AddMilliseconds(1200));
                    info.Text = "녹화를 마쳤습니다. 마지막 입력값을 확인하고 있습니다…"; Heartbeat(true); return;
                }
            }
            Drain(); Pause(); recordState = "review"; Heartbeat(true);
            if (authorBusy || authorQueue.Count > 0) {
                completeRequested = true; start.Enabled = pause.Enabled = stop.Enabled = false;
                info.Text = "녹화는 마쳤습니다. 클릭 전에 확보한 화면으로 대상을 정리하고 있습니다.\n새로 선택하거나 다시 녹화하지 않아도 됩니다.";
                return;
            }
            EnforcePayloadBudget();
            var windows = new List<Dictionary<string, object>>();
            foreach (Target t in targets) if (t.Owner != null) windows.Add(new Dictionary<string, object> {
                { "program_id", t.Program }, { "window_ref", t.Window }, { "owner_ref", t.Owner.Window }, { "pid", t.Pid }, { "window_id", t.Hwnd.ToInt64() },
                { "owner_window_id", t.Owner.Hwnd.ToInt64() }, { "title", t.Title }, { "class_name", t.ClassName }, { "owner_verified", true },
                { "closed", !t.Valid() || !IsWindowVisible(t.Hwnd) } });
            Finish("recorded", new Dictionary<string, object> { { "events", events }, { "windows", windows }, { "human_confirmed", true }, { "review_required", true }, { "warnings", warnings }, { "partial", warnings.Count > 0 }, { "injected_events", injected } });
        }
        bool CanFinalizeReason(Pending value) {
            return value.Reason != "protected_input" && value.Reason != "recording_method_unverified"
                && value.Reason != "shortcut_requires_manual_setup" && Str(value.Saved, "reason", "") != "recording_payload_limit"
                && !trackedKeyboard;
        }
        bool FinalTargetCurrent(Target target) {
            if (target == null || !target.Valid()) return false;
            IntPtr front = GetForegroundWindow();
            return target.Foreground() || front == Handle;
        }
        Dictionary<string, object> ReadFinalEdit(Pending value) {
            if (!FinalTargetCurrent(value.Target) || value.EditElement == null || ProtectedOrUnowned(value.EditElement, value.Target)) return null;
            AutomationElement element = value.EditElement;
            if (!EditableTextRole(element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "")) || !element.Current.IsEnabled) return null;
            System.Windows.Rect bounds = element.Current.BoundingRectangle;
            if (bounds.IsEmpty) return null;
            var sample = ReadSemantic(element, value.Target, new POINT { X = (int)(bounds.X + bounds.Width / 2), Y = (int)(bounds.Y + bounds.Height / 2) }, false);
            if (sample == null || !EditableTextRole(sample.Role) || sample.Property != "value" || !FinalTargetCurrent(value.Target)) return null;
            return new Dictionary<string, object> { { "value", sample.Value }, { "identity", sample.Identity }, { "native_target", sample.Native } };
        }
        void FinishFinalRead() {
            Heartbeat(false);
            if (finalTyping == null || !FinalTargetCurrent(finalTyping.Target)) finalRead.Cancel();
            Dictionary<string, object> state;
            if (!finalRead.TryTake(DateTime.UtcNow, out state)) return;
            Pending candidate = finalTyping; finalRead = null; finalTyping = null;
            if (candidate != null && Object.ReferenceEquals(candidate, typing) && events.Contains(candidate.Saved)
                    && CanFinalizeRecordedEdit(candidate.Baseline, state, candidate.ReplaceAll, candidate.Reason,
                        FinalTargetCurrent(candidate.Target), CanFinalizeReason(candidate))) {
                string after = Str(state, "value", "");
                candidate.Saved["operation"] = "set_value"; candidate.Saved["value"] = after; candidate.Saved["input_role"] = Str(Map(Map(state, "native_target"), "element"), "role", "Edit"); candidate.Saved.Remove("reason");
                candidate.Saved["native_target"] = Map(state, "native_target");
                candidate.Saved["after"] = new Dictionary<string, object> { { "property", "value" }, { "equals", after } };
                candidate.Saved["event_evidence"] = new Dictionary<string, object> { { "source", "uia_recording" }, { "identity", Str(state, "identity", "") }, { "after_input", true } };
                typingConfirmedAt = DateTime.UtcNow;
                list.Items[candidate.Index] = (candidate.Index + 1).ToString() + "  마지막 입력값 확인 · 저장 전 Driver 대조";
                if (candidate.ReplaceAll && selectAllPreparation != null && selectAllPreparation.Saved != null
                        && selectAllPreparation.Index == candidate.Index - 1 && candidate.Index == events.Count - 1) {
                    events.RemoveAt(selectAllPreparation.Index); list.Items.RemoveAt(selectAllPreparation.Index); candidate.Index--; selectAllPreparation = null;
                }
            }
            CompleteRecording();
        }
        void FinalizePendingInput() {
            if (typing != null && typing.Saved != null && Str(typing.Saved, "reason", "") != "recording_payload_limit" && dirtyAt > typingConfirmedAt && typing.Reason != "protected_input" && typing.Reason != "recording_method_unverified") {
                if (typing.DraftInputTarget && typing.Baseline == null && typing.Reason == "unknown_input") {
                    typing.Saved["operation"] = "manual_entry"; typing.Saved["reason"] = "unknown_input"; typing.Saved.Remove("value"); return;
                }
                typing.Saved["operation"] = "manual_entry"; typing.Saved["reason"] = "existing_text_requires_review"; typing.Saved.Remove("value"); Warn("recording_final_value_unconfirmed");
            }
        }
        void StartProbe() {
            info.Text = "기록 시작 전 점검 중 · 입력 후크와 UIA 응답을 확인합니다.\n버튼 위에 마우스만 올리는 동작은 기록하지 않습니다.";
            try { InstallHooks(); hookProbe = "available"; } catch { hookProbe = "unavailable"; Failure("recording_hook_unavailable"); return; }
            probeAt = DateTime.UtcNow; Heartbeat(true);
            Thread probe = new Thread(delegate() { try { foreach (Target target in targets) { var root = AutomationElement.FromHandle(target.Hwnd); if (root == null || root.Current.ProcessId != target.Pid) return; } probeGood = true; } catch { } finally { probeDone = true; } });
            probe.IsBackground = true; probe.SetApartmentState(ApartmentState.MTA); probe.Start();
        }
        void Heartbeat(bool force) {
            if (Done || (!force && DateTime.UtcNow.Subtract(heartbeatAt).TotalMilliseconds < 250)) return;
            int manual = 0; foreach (var item in events) if (Str(item, "operation", "") == "manual_entry") manual++;
            var value = new Dictionary<string, object> { { "nonce", NonceValue }, { "helper_pid", Process.GetCurrentProcess().Id }, { "state", recordState }, { "event_count", events.Count }, { "manual_count", manual }, { "max_events", maximum }, { "warning_codes", warnings }, { "probe", new Dictionary<string, object> { { "hooks", hookProbe }, { "uia", uiaProbe } } } };
            if (startDiagnostic != null) value["start_diagnostic"] = startDiagnostic;
            if (events.Count > 0) value["last_event"] = new Dictionary<string, object> { { "operation", Str(events[events.Count - 1], "operation", "") }, { "recognition", events[events.Count - 1].ContainsKey("native_target") ? "uia_candidate" : "image_or_manual" } };
            try { Write(Response + ".progress.json", value); heartbeatAt = DateTime.UtcNow; } catch { recording = false; recordState = "failed"; Warn("recording_progress_failed"); info.Text = "기록 상태를 전달하지 못해 중지했습니다. 지금까지의 내용을 검토하세요."; start.Enabled = pause.Enabled = false; stop.Enabled = events.Count > 0; }
        }
        string ActiveText() {
            bool semanticOnly = false; foreach (Target target in targets) if (!target.ImageAllowed) semanticOnly = true;
            return "● 기록 중 · 클릭과 입력이 실제 프로그램에 적용됩니다.\n" + (semanticOnly
                ? "요소 방식으로 기록합니다. 이미지 재생 조건이 확인된 창만 이미지로 기록합니다."
                : "요소로 기록할 수 없으면 이미지로 기록합니다. 가려진 대상은 보완이 필요합니다.")
                + "\nEsc: 일시정지 · 마친 뒤 동작과 입력값을 검토하세요.";
        }
        void Pause() { recording = false; preparingTarget = null; generation++; Compact(false); FinalizePendingInput(); tracked = hovered = null; trackedEvent = null; trackedTarget = hoverTarget = null; typing = null; selectAllPreparation = null; lastPointer = null; focusedState = null; lastImage = null; lastImageTarget = null; recordState = "paused"; start.Enabled = !payloadLimited && probeSettled && hookProbe == "available" && events.Count < maximum; pause.Enabled = false; info.Text = "일시정지 · " + events.Count + "/" + maximum + "개 동작을 기록했습니다.\n다시 시작할 때 입력 대상을 클릭하거나 기록을 마치고 검토하세요."; ReleaseFrame(); ClearFrames(); Heartbeat(true); }
        void EnforcePayloadBudget() {
            if (!payloadBudget.Bound(events)) return;
            payloadLimited = true; Warn("recording_payload_limit"); Pause();
            while (authorQueue.Count > 0) {
                Pending waiting = authorQueue.Dequeue();
                if (waiting.Saved != null) RecordingPayloadBudget.RequireRepair(waiting.Saved);
                foreach (Pending dependant in waiting.ImageDependants) if (dependant.Saved != null) RecordingPayloadBudget.RequireRepair(dependant.Saved);
                waiting.ImageDependants.Clear(); waiting.AuthorCompleted = true; DisposePending(waiting);
            }
            // Finalizing a pending value cannot restore a target removed for
            // size. Keep every row in order and require repair for the suffix.
            payloadBudget.Bound(events);
            for (int index = 0; index < events.Count; index++) if (Str(events[index], "reason", "") == "recording_payload_limit")
                list.Items[index] = (index + 1).ToString() + "  저장 용량 확인 · 이 단계만 보완 필요";
            start.Enabled = pause.Enabled = false; stop.Enabled = events.Count > 0;
            info.Text = "이미지 기록 용량에 도달해 녹화를 마쳤습니다. 앞선 기록과 동작 순서는 보존했습니다.\n이미지를 보관하지 못한 단계는 ‘보완 필요’로 남깁니다. ‘기록 마치고 검토’에서 저장하세요.";
            Heartbeat(true);
        }
        void InstallHooks() { if (mouseHook != IntPtr.Zero) return; mouseProc = MouseHook; keyProc = KeyHook; IntPtr module = GetModuleHandle(null); mouseHook = SetWindowsHookEx(14, mouseProc, module, 0); keyHook = SetWindowsHookEx(13, keyProc, module, 0); if (mouseHook == IntPtr.Zero || keyHook == IntPtr.Zero) { UninstallHooks(); throw new InvalidOperationException("recording_hook_unavailable"); }
            windowProc = delegate(IntPtr hook, uint eventType, IntPtr hwnd, int objectId, int childId, uint thread, uint time) {
                if (!recording || Done || IsDisposed || popupWakePending || objectId != 0 || hwnd == IntPtr.Zero) return;
                uint pid; if (GetWindowThreadProcessId(hwnd, out pid) == 0) return;
                bool allowed = false; foreach (Target root in targets) if (root.Owner == null && root.Pid == (int)pid) { allowed = true; break; } if (!allowed) return;
                popupWakePending = true;
                try { BeginInvoke((Action)delegate { popupWakePending = false; if (recording && !Done) { IntPtr shown = GetAncestor(hwnd, 2); DiscoverPopup(shown);
                    Tick(null, EventArgs.Empty);
                } }); }
                catch (InvalidOperationException) { popupWakePending = false; }
            };
            // SHOW notifications wake capture before the usual 80ms timer.
            // They never inject input and are restricted again by exact owner
            // or ComboLBox relationship at binding, not process ID alone.
            windowHook = SetWinEventHook(0x8002, 0x8002, IntPtr.Zero, windowProc, 0, 0, 2);
        }
        void UninstallHooks() { recording = false; if (mouseHook != IntPtr.Zero) UnhookWindowsHookEx(mouseHook); if (keyHook != IntPtr.Zero) UnhookWindowsHookEx(keyHook); if (windowHook != IntPtr.Zero) UnhookWinEvent(windowHook); mouseHook = keyHook = windowHook = IntPtr.Zero; }
        Target Current() {
            // A native dropdown can hide and later reuse the same HWND. Each
            // visibility episode gets a distinct recipe binding; otherwise a
            // first selection would try to review an already hidden window.
            RetireHiddenPopups();
            POINT cursor; if (GetCursorPos(out cursor)) foreach (Target t in targets) if (t.Contains(cursor) && t.Foreground()) return t;
            IntPtr foreground = GetForegroundWindow();
            foreach (Target t in targets) if ((t.Hwnd == foreground || t.HostedHwnd != IntPtr.Zero && t.AcceptsForeground(foreground)) && t.Foreground()) return t; return null;
        }
        void RetireHiddenPopups() { foreach (Target t in targets) if (t.Owner != null && !IsWindowVisible(t.Hwnd)) t.Retired = true; }
        void DiscoverPopup() { DiscoverPopup(IntPtr.Zero); }
        void DiscoverPopup(IntPtr hint) {
            // Native metadata only, outside low-level input hooks. Never scan
            // another process's UIA tree or infer trust from PID alone.
            RetireHiddenPopups();
            foreach (Target t in targets) if (t.Owner != null && !t.Valid()) t.Retired = true;
            POINT cursor; IntPtr candidate = hint == IntPtr.Zero ? GetForegroundWindow() : hint;
            if (hint == IntPtr.Zero && GetCursorPos(out cursor)) { IntPtr at = GetAncestor(WindowFromPoint(cursor), 2); uint atPid;
                if (at != IntPtr.Zero && GetWindowThreadProcessId(at, out atPid) != 0)
                    foreach (Target root in targets) if (root.Owner == null && atPid == (uint)root.Pid && at != root.Hwnd && RelatedTo(at, root.Hwnd, root.Pid)) { candidate = at; break; }
            }
            foreach (Target known in targets) if (known.Hwnd == candidate && !known.Retired) return;
            var owners = new List<Target>();
            foreach (Target known in targets) if (known.Valid() && known.Hwnd != candidate
                    && RelatedTo(candidate, known.Hwnd, known.Pid)) owners.Add(known);
            // Bind to the closest known related window, not always the initial
            // root. The recipe can then return to the actual business dialog.
            var ownerSnapshot = new List<Target>(owners);
            owners.RemoveAll(delegate(Target possible) { return ownerSnapshot.Exists(delegate(Target other) {
                return other != possible && other.Pid == possible.Pid && RelatedTo(other.Hwnd, possible.Hwnd, possible.Pid);
            }); });
            if (owners.Count > 1) { Warn("recording_window_ambiguous"); return; }
            foreach (Target root in owners) {
                if (!root.Valid() || candidate == root.Hwnd || !IsWindowVisible(candidate) || !RelatedTo(candidate, root.Hwnd, root.Pid)) continue;
                string title = WindowText(candidate), cls = WindowClass(candidate); if (cls.Length == 0 || cls == "IME" || cls == "MSCTFIME UI" || cls == "tooltips_class32") return;
                // SHOW / hit-test already identified this exact HWND. Sibling
                // menus legitimately share an empty title and the same class.
                // Ownership above, not title uniqueness, establishes scope.
                int count = 0, active = 0; foreach (Target t in targets) if (t.Owner != null) { count++; if (!t.Retired && t.Valid() && IsWindowVisible(t.Hwnd)) active++; }
                if (!RecordingPopupCapacity(active, count)) { Warn("recording_window_limit"); return; }
                string reference = popupPrefix + (count + 1).ToString(); bool used;
                do { used = false; foreach (Target t in targets) if (t.Program == root.Program && t.Window == reference) { used = true; reference = popupPrefix + (++count + 1).ToString(); break; } } while (used);
                Target discovered = new Target(root, candidate, reference);
                targets.Add(discovered);
                // Discovery runs outside the input hook. Keep a pre-input
                // image even when a sibling menu owns the foreground.
                if (discovered.ImageAllowed) { try { CaptureFor(discovered, false); } catch { } }
                return;
            }
        }
        Dictionary<string, object> Event(Target t, string operation) { return new Dictionary<string, object> { { "operation", operation }, { "program_id", t.Program }, { "window_ref", t.Window }, { "verification", "manual_required" } }; }
        void Queue(Pending p) {
            if (events.Count + pending.Count >= maximum) { DisposePending(p); Warn("recording_event_limit"); recording = false; BeginInvoke((Action)delegate { Drain(); Pause(); info.Text = "기록 가능한 " + maximum.ToString() + "단계에 도달했습니다. 이후 동작은 기록하지 않습니다. [기록 마치고 검토]를 눌러 확인하세요."; }); return; }
            if (p.RequestedOperation == null && lastPointer != null && lastPointer.ImageFocusValid && lastPointer.Target == p.Target && ((p.Key != null || p.Keys != null) && p.Reason == null || p.Baseline != null)) p.ImageOwner = lastPointer;
            pending.Enqueue(p);
        }
        bool FocusMatches(Target target) {
            if (focusedTarget != target || focusedState == null || DateTime.UtcNow.Subtract(focusedAt).TotalMilliseconds > 1200 || !target.Foreground()) return false;
            if (Str(focusedState, "protected", "False") == "True") return false;
            if (lastPointer == null) return Map(focusedState, "native_target") != null;
            if (lastPointer.Target != target) return false;
            return new Rectangle(Num(focusedState, "x", 0), Num(focusedState, "y", 0), Num(focusedState, "width", 0), Num(focusedState, "height", 0)).Contains(lastPointer.ScreenPoint);
        }
        Pending DraftInputCandidate(Target target) {
            Pending pointer = lastPointer;
            if (pointer == null || pointer.PointWindow == IntPtr.Zero) return null;
            bool protectedInput = focusedTarget == target && focusedState != null && Str(focusedState, "protected", "False") == "True";
            uint pid;
            bool current = target.Valid() && target.AcceptsWindow(pointer.PointWindow)
                && GetWindowThreadProcessId(pointer.PointWindow, out pid) == pointer.PointThread && pointer.PointThread != 0
                && WindowClass(pointer.PointWindow) == pointer.PointClass
                && WindowFromPoint(new POINT { X = pointer.ScreenPoint.X, Y = pointer.ScreenPoint.Y }) == pointer.PointWindow;
            return RecordingDraftInputCandidate(pointer.Target == target, current, target.Foreground(),
                pointer.RequestedOperation == "click" && pointer.Reason != "drag_requires_manual_setup" && mouseDown != pointer
                    && (pointer.Saved == null || Str(pointer.Saved, "operation", "") != "double_click"),
                pointer.ImageFocusValid && pointer.DraftInputFocusValid, protectedInput, DateTime.UtcNow.Subtract(pointer.ClickAt).TotalSeconds) ? pointer : null;
        }
        void InvalidateDraftInput() {
            if (lastPointer != null) lastPointer.DraftInputFocusValid = false;
            if (typing != null && typing.DraftInputTarget) typing = null;
            if (selectAllPreparation != null && selectAllPreparation.DraftInputTarget) selectAllPreparation = null;
        }
        void ResetPointerInput(bool scrolling) {
            if (scrolling) InvalidateDraftInput();
            typing = null; selectAllPreparation = null;
        }
        static void ClearDraftInputCandidate(Pending input) {
            input.DraftInputTarget = false; input.ImageOwner = null; input.DraftPreparation = null;
            if (input.Saved != null) foreach (string key in new string[] { "input_target_candidate", "recorded_preparation", "template_png", "width", "height", "anchor", "capture_window", "source_size", "capture_diagnostic" }) input.Saved.Remove(key);
        }
        static bool CoalesceDraftPreparation(Pending input, List<Dictionary<string, object>> recorded) {
            Pending preparation = input.DraftPreparation;
            if (!input.DraftInputTarget || input.Reason != "unknown_input" || input.Baseline != null || preparation == null
                || !preparation.DraftInputTarget || preparation.Baseline != null || preparation.Reason != "focus_target_requires_selection"
                || preparation.Target != input.Target || preparation.ImageOwner == null || preparation.ImageOwner != input.ImageOwner
                || preparation.Keys == null || preparation.Keys.Length != 2 || preparation.Keys[0] != "CTRL" || preparation.Keys[1] != "A"
                || preparation.Saved == null || recorded.Count == 0 || !Object.ReferenceEquals(recorded[recorded.Count - 1], preparation.Saved)
                || Str(preparation.Saved, "operation", "") != "manual_entry" || Str(preparation.Saved, "reason", "") != "focus_target_requires_selection") return false;
            recorded.RemoveAt(recorded.Count - 1); RetireFoldedPointer(preparation); return true;
        }
        bool OwnRecorderControl(IntPtr window) {
            uint pid;
            return window != IntPtr.Zero && GetWindowThreadProcessId(window, out pid) != 0 && pid == (uint)Process.GetCurrentProcess().Id;
        }
        Target PointerTarget(POINT point) {
            // Only exact registered roots or already owner-verified popups are
            // eligible. No UIA reads, new-popup discovery, or capture in hook.
            IntPtr hit = WindowFromPoint(point); IntPtr root = GetAncestor(hit, 2); uint pid;
            if (root == IntPtr.Zero || GetWindowThreadProcessId(root, out pid) == 0) return null;
            foreach (Target target in targets) if (!target.Retired && target.Hwnd == root && target.Pid == (int)pid) {
                try {
                    if (target.AcceptsWindow(hit) && RecordingPointerMatches(root.ToInt64(), (int)pid, target.Hwnd.ToInt64(), target.Pid,
                            target.Valid(), IsWindowVisible(root), IsIconic(root), target.Bounds(), new Point(point.X, point.Y))) return target;
                } catch { }
                return null;
            }
            return null;
        }
        // Hook callbacks copy only a small cached crop. PNG encoding, UIA,
        // matching and file writes run outside the low-level input callback.
        bool UnboundPopupClick(POINT point, string operation) {
            IntPtr hit = GetAncestor(WindowFromPoint(point), 2);
            if (hit == IntPtr.Zero || !IsWindowVisible(hit)) return false;
            Target owner = null;
            foreach (Target known in targets) {
                if (known.Retired || !known.Valid() || known.Hwnd == hit || !RelatedTo(hit, known.Hwnd, known.Pid)) continue;
                if (owner == null || RelatedTo(known.Hwnd, owner.Hwnd, owner.Pid)) owner = known;
                else if (!RelatedTo(owner.Hwnd, known.Hwnd, known.Pid)) return false;
            }
            if (owner == null) return false;
            // The hook has no pre-input pixels for this newly shown HWND.
            // Preserve a repairable event, never turn post-click pixels into a
            // runnable target. Full discovery/capture remains outside the hook.
            Queue(new Pending { Target = owner, Operation = "manual_entry",
                RequestedOperation = operation, Reason = "image_capture_required",
                ScreenPoint = new Point(point.X, point.Y), ClickAt = DateTime.UtcNow,
                LastInputAt = DateTime.UtcNow, InputVersion = inputVersion + 1 });
            return true;
        }
        Pending CaptureClick(Target target, POINT point, string operation) {
            var item = new Pending { Target = target, Operation = operation, RequestedOperation = operation, ScreenPoint = new Point(point.X, point.Y), ClickAt = DateTime.UtcNow, LastInputAt = DateTime.UtcNow, ForegroundBeforeInput = target.AcceptsForeground(GetForegroundWindow()), InputVersion = inputVersion + 1 };
            IntPtr hit = WindowFromPoint(point); RECT hitBounds; uint hitPid;
            if (operation == "click" && target.AcceptsWindow(hit)) { item.PointWindow = hit; item.PointThread = GetWindowThreadProcessId(hit, out hitPid); item.PointClass = WindowClass(hit); }
            if (operation == "click" && hit != target.Hwnd && GetAncestor(hit, 2) == target.Hwnd && GetWindowRect(hit, out hitBounds)) {
                uint thread = GetWindowThreadProcessId(hit, out hitPid);
                if (thread != 0 && target.AcceptsWindow(hit)) { item.HitWindow = hit; item.HitThread = thread; item.HitEnabled = IsWindowEnabled(hit) && IsWindowEnabled(target.Hwnd); item.HitClass = WindowClass(hit); item.HitControlId = GetDlgCtrlID(hit); item.HitBounds = Rectangle.FromLTRB(hitBounds.Left, hitBounds.Top, hitBounds.Right, hitBounds.Bottom); }
                var combo = new COMBOINFO { Size = Marshal.SizeOf(typeof(COMBOINFO)) };
                if (item.HitWindow != IntPtr.Zero && GetComboBoxInfo(hit, ref combo) && combo.Combo == hit) item.ComboListWindow = combo.List;
            }
            if (operation == "click" && hovered != null && hoverTarget == target && DateTime.UtcNow.Subtract(hoverAt).TotalMilliseconds <= 600) {
                var bounds = Map(hovered.Native, "bounds");
                if (bounds != null && new Rectangle((int)Real(bounds, "x", 0), (int)Real(bounds, "y", 0), (int)Real(bounds, "width", 0), (int)Real(bounds, "height", 0)).Contains(point.X, point.Y)) { item.Semantic = hovered; item.SemanticBeforeInput = hovered.ObservedAt <= item.ClickAt; }
            }
            if (!target.ImageAllowed) { if (item.Semantic == null) item.Operation = "manual_entry"; item.Reason = "image_replay_unavailable"; return item; }
            RestoreClickFrame(target);
            DateTime clickedAt=DateTime.UtcNow;
            var frameTiming=frame==null?null:frame.Tag as CapturedFrameInfo;
            string frameIssue=RecordingFrameIssue(frame!=null,frameTarget==target,frameAt,clickedAt,inputChangedAt,true,true,frameTiming);
            if(frameIssue==null&&!CapturedFrameAfterInput(frameInputAt,inputChangedAt))frameIssue="frame_predates_input";
            if(frameIssue==null&&frameInputVersion!=inputVersion)frameIssue="frame_predates_input";
            if (frameIssue==null) frameIssue=RecordingFrameIssue(true,true,frameAt,clickedAt,inputChangedAt,
                frameBounds==target.CaptureBounds(),target.Contains(point),frameTiming);
            if (frameIssue!=null) {
                item.Operation="manual_entry"; item.Reason="image_capture_required";
                item.CaptureDiagnostic=new Dictionary<string,object> { { "status", "unavailable" }, { "capture_failure", frameIssue },
                    { "frame_age_ms", frame==null ? -1 : Math.Round(Math.Max(-1,Math.Min(60000,clickedAt.Subtract(frameAt).TotalMilliseconds))) },
                    { "last_capture_failure", lastCaptureFailure ?? "none" },
                    { "last_capture_failure_age_ms", lastCaptureFailure==null ? -1 : Math.Round(Math.Max(0,Math.Min(60000,clickedAt.Subtract(lastCaptureFailureAt).TotalMilliseconds))) } };
                AppendCaptureBackendDiagnostic(target,item.CaptureDiagnostic);
                AppendFrameEpochDiagnostic(target,item.CaptureDiagnostic,clickedAt);
                CapturePump activePump;if(recordingPumps.TryGetValue(target,out activePump))activePump.AppendLifecycle(item.CaptureDiagnostic);
                return item;
            }
            int x = point.X - frameBounds.X, y = point.Y - frameBounds.Y;
            var semanticIdentity = item.Semantic == null ? null : Map(item.Semantic.Native, "element");
            bool identified = semanticIdentity != null && (Str(semanticIdentity, "name", "").Length > 0 || Str(semanticIdentity, "automation_id", "").Length > 0);
            Rectangle r = RecordingRegion(frame.Size, new Point(x, y), item.Semantic != null, identified, false);
            List<Rectangle> currentOccluders = Occluders(target, frameExcludesLayered);
            var blocked = new List<Rectangle>();
            if (!frameRendered) foreach (List<Rectangle> rectangles in new List<Rectangle>[] { frameOccluders, currentOccluders }) foreach (Rectangle obstruction in rectangles)
                blocked.Add(new Rectangle(obstruction.X - frameBounds.X, obstruction.Y - frameBounds.Y, obstruction.Width, obstruction.Height));
            item.SourceAnchor = new Point(x, y); item.Region = r; item.Blocked = blocked; item.FrameSize = frame.Size;
            item.CaptureMode = frameExcludesLayered ? "window_surface" : frameRendered ? frameSourceMode ?? "rendered_window" : "screen";
            item.CaptureDiagnostic = new Dictionary<string, object> { { "capture_mode", item.CaptureMode }, { "before_input", true },
                { "frame_age_ms", Math.Round(DateTime.UtcNow.Subtract(frameAt).TotalMilliseconds) }, { "frame_obstruction_count", frameOccluders.Count },
                { "input_obstruction_count", currentOccluders.Count }, { "obstruction_scope", "recognition_context" },
                { "frame_occluding_windows", frameOcclusionDetails }, { "input_occluding_windows", currentOccluders.Count == 0 ? new List<Dictionary<string, object>>() : OcclusionDetails(target, frameExcludesLayered) } };
            var timing=frame.Tag as CapturedFrameInfo;
            if(timing!=null){item.CaptureDiagnostic["capture_future_lead_ms"]=Math.Round(timing.FutureLeadMilliseconds,3);item.CaptureDiagnostic["frame_received_age_ms"]=Math.Round(DateTime.UtcNow.Subtract(timing.ReceivedAt).TotalMilliseconds);
                item.CaptureDiagnostic["frame_render_age_ms"]=Math.Round(DateTime.UtcNow.Subtract(timing.At).TotalMilliseconds);
                item.CaptureDiagnostic["observation_source"]=timing.RequestAt==DateTime.MinValue?"render_time":"new_capture_request_first_frame";
                if(timing.RequestAt!=DateTime.MinValue)item.CaptureDiagnostic["frame_request_age_ms"]=Math.Round(DateTime.UtcNow.Subtract(timing.RequestAt).TotalMilliseconds);}
            foreach (Rectangle obstruction in blocked) if (obstruction.Contains(item.SourceAnchor)) {
                item.Operation = "manual_entry"; item.Reason = "image_occluded_requires_selection"; item.CaptureDiagnostic["obstruction_scope"] = "action_point"; return item;
            }
            // Retain, do not copy or encode the frame in the input hook. A
            // worker later chooses compact, unobstructed context from these
            // exact pre-input pixels while the recorder continues capturing.
            if (sharedFrame == null) sharedFrame = new SharedFrame(frame);
            item.Source = sharedFrame.Retain();
            List<CachedFrame> history;
            if (operation == "click" && hoverHistory.TryGetValue(target, out history)) foreach (CachedFrame old in history) {
                if (!RecordingPreHoverWitness(old.At, item.ClickAt, old.InputVersion, inputVersion, old.Bounds, frameBounds, old.CursorKnown,
                        old.Rendered == frameRendered && old.ExcludesLayered == frameExcludesLayered,
                        Covered(frameBounds, old.Occluders) || Covered(frameBounds, currentOccluders) || Covered(frameBounds, frameOccluders))) continue;
                old.Source.Retain(); item.BeforeHover.Add(old);
            }
            return item;
        }
        IntPtr MouseHook(int code, IntPtr message, IntPtr data) {
            try {
                if (code >= 0 && finalRead != null && (message.ToInt32() == 0x201 || message.ToInt32() == 0x204 || message.ToInt32() == 0x20A)) finalRead.Cancel();
                if (code >= 0 && recording) {
                    MOUSE mouse = (MOUSE)Marshal.PtrToStructure(data, typeof(MOUSE)); int kind = message.ToInt32();
                    if (kind == 0x201 || kind == 0x204 || kind == 0x20A) {
                        Target target = PointerTarget(mouse.point); if (target != null) {
                            if ((mouse.flags & 1) != 0) injected++; ResetPointerInput(kind == 0x20A);
                            Pending item = CaptureClick(target, mouse.point, kind == 0x204 ? "right_click" : kind == 0x20A ? "scroll" : "click");
                            item.Wheel = (short)(mouse.data >> 16); if (kind != 0x20A) { lastPointer = item; clickEvidence.Add(item); } Queue(item);
                            if (kind != 0x20A) { mouseDown = item; mouseStart = new Point(mouse.point.X, mouse.point.Y); }
                        } else { InvalidateDraftInput(); if (!OwnRecorderControl(WindowFromPoint(mouse.point))
                                && !UnboundPopupClick(mouse.point, kind == 0x204 ? "right_click" : kind == 0x20A ? "scroll" : "click"))
                                    Warn("outside_target_not_recorded"); }
                        InputChanged();
                    } else if ((kind == 0x202 || kind == 0x205) && mouseDown != null) {
                        if (Math.Abs(mouse.point.X - mouseStart.X) > SystemInformation.DragSize.Width || Math.Abs(mouse.point.Y - mouseStart.Y) > SystemInformation.DragSize.Height) {
                            mouseDown.Operation = "manual_entry"; mouseDown.Reason = "drag_requires_manual_setup";
                            if (mouseDown.Saved != null) { mouseDown.Saved["operation"] = "manual_entry"; mouseDown.Saved["reason"] = mouseDown.Reason; int at = mouseDown.Index; BeginInvoke((Action)delegate { if (at >= 0 && at < list.Items.Count) list.Items[at] = (at + 1).ToString() + "  드래그 · 직접 설정 필요"; }); }
                        } mouseDown = null;
                    }
                }
            } catch { recording = false; BeginInvoke((Action)delegate { Failure("recording_mouse_failed"); }); }
            return CallNextHookEx(mouseHook, code, message, data);
        }
        IntPtr KeyHook(int code, IntPtr message, IntPtr data) {
            try {
                if (code >= 0 && finalRead != null && (message.ToInt32() == 0x100 || message.ToInt32() == 0x104)) finalRead.Cancel();
                if (code >= 0 && recording && (message.ToInt32() == 0x100 || message.ToInt32() == 0x104)) {
                    InputChanged();
                    KEY key = (KEY)Marshal.PtrToStructure(data, typeof(KEY)); Target target = Current(); if (target != null) {
                        if ((key.flags & 16) != 0) injected++; Keys value = (Keys)key.code;
                        if (value == Keys.Escape) BeginInvoke((Action)delegate { WindowState = FormWindowState.Normal; Drain(); Pause(); });
                        else if (value != Keys.ControlKey && value != Keys.ShiftKey && value != Keys.Menu && value != Keys.LWin && value != Keys.RWin && value != Keys.LControlKey && value != Keys.RControlKey && value != Keys.LShiftKey && value != Keys.RShiftKey && value != Keys.LMenu && value != Keys.RMenu) {
                            // A final ComboBox value does not prove how its child Edit
                            // committed it. Never silently replace keyboard+commit with
                            // a different generic selection strategy.
                            if (tracked != null && trackedTarget == target && tracked.Role == "ComboBox") trackedKeyboard = true;
                            bool ctrl = (GetAsyncKeyState(0x11) & 0x8000) != 0, alt = (GetAsyncKeyState(0x12) & 0x8000) != 0, shift = (GetAsyncKeyState(0x10) & 0x8000) != 0;
                            if (typing != null && typing.DraftInputTarget && (ctrl || alt) && !(ctrl && !alt && !shift && value == Keys.V)) InvalidateDraftInput();
                            if (ctrl && !alt && !shift && (value == Keys.A || value == Keys.Z || value == Keys.Y)) {
                                typing = null; bool valid = FocusMatches(target); Pending candidate = !valid && value == Keys.A ? DraftInputCandidate(target) : null;
                                var preparation = new Pending { Target = target, Operation = valid ? "hotkey" : "manual_entry", Reason = valid ? null : "focus_target_requires_selection",
                                    Keys = new string[] { "CTRL", value.ToString().ToUpperInvariant() }, Baseline = valid ? focusedState : null, ImageOwner = candidate, DraftInputTarget = candidate != null };
                                selectAllPreparation = (valid || candidate != null) && value == Keys.A ? preparation : null; Queue(preparation);
                                if (value != Keys.A) InvalidateDraftInput();
                            }
                            else if (!ctrl && !alt && !shift && (value == Keys.Enter || value == Keys.Tab || value == Keys.Up || value == Keys.Down || value == Keys.Left || value == Keys.Right || value == Keys.Home || value == Keys.End || value == Keys.PageDown || value == Keys.PageUp)) { typing = null; selectAllPreparation = null; bool valid = FocusMatches(target); Queue(new Pending { Target = target, Operation = valid ? "press_key" : "manual_entry", Reason = valid ? null : "focus_target_requires_selection", Key = value.ToString().ToUpperInvariant() }); InvalidateDraftInput(); }
                            else {
                                if (typing == null || typing.Target != target) {
                                    Dictionary<string, object> baseline = FocusMatches(target) ? focusedState : null;
                                    bool protectedField = focusedTarget == target && focusedState != null && Str(focusedState, "protected", "False") == "True" && DateTime.UtcNow.Subtract(focusedAt).TotalMilliseconds < 1200;
                                    Pending candidate = baseline == null && !protectedField ? DraftInputCandidate(target) : null;
                                    bool paste = ctrl && !alt && !shift && value == Keys.V;
                                    string reason = protectedField ? "protected_input" : (ctrl || alt) && !(paste && (baseline != null || candidate != null)) ? "shortcut_requires_manual_setup" : "unknown_input";
                                    if (reason != "unknown_input") candidate = null;
                                    typing = new Pending { Target = target, Operation = "manual_entry", Reason = reason, Baseline = baseline, EditElement = baseline != null ? focusedEdit : null,
                                        ImageOwner = candidate, DraftInputTarget = candidate != null, DraftPreparation = candidate != null ? selectAllPreparation : null,
                                        ReplaceAll = baseline != null && selectAllPreparation != null && selectAllPreparation.Target == target && Str(selectAllPreparation.Baseline, "identity", "before") == Str(baseline, "identity", "after") }; Queue(typing);
                                    if (reason == "shortcut_requires_manual_setup" || reason == "protected_input") InvalidateDraftInput();
                                } dirtyAt = DateTime.UtcNow;
                            }
                        }
                    } else { InvalidateDraftInput(); if (!OwnRecorderControl(GetForegroundWindow())) Warn("outside_target_not_recorded"); }
                }
            } catch { recording = false; BeginInvoke((Action)delegate { Failure("recording_keyboard_failed"); }); }
            return CallNextHookEx(keyHook, code, message, data);
        }
        DateTime lastClickTime; Dictionary<string, object> lastClickEvent; int lastClickIndex; Target lastClickTarget; Point lastClickAnchor;
        static void CopyImage(Dictionary<string, object> source, Dictionary<string, object> target) {
            foreach (string key in new string[] { "template_png", "width", "height", "anchor", "capture_window", "source_size", "capture_diagnostic" }) {
                object value; if (source.TryGetValue(key, out value)) target[key] = value;
            }
        }
        static void RedirectImageDependants(Queue<Pending> queued, Pending discarded, Pending original) {
            foreach (Pending dependant in queued) if (dependant.ImageOwner == discarded) dependant.ImageOwner = original;
        }
        static void CompleteImageDependants(Pending pointer, Dictionary<string, object> result, string failure, List<Dictionary<string, object>> activeEvents) {
            foreach (Pending dependant in pointer.ImageDependants) if (dependant.Saved != null && activeEvents.Contains(dependant.Saved)) {
                if (Str(dependant.Saved, "reason", "") == "protected_input") continue;
                if (result != null) { CopyImage(result, dependant.Saved); dependant.Saved.Remove("capture_issue"); }
                else { dependant.Saved["capture_issue"] = failure; if (!dependant.Saved.ContainsKey("native_target")) { dependant.Saved["operation"] = "manual_entry"; dependant.Saved["reason"] = "focus_target_requires_selection"; } }
            }
            pointer.ImageDependants.Clear();
        }
        void Drain() {
            while (pending.Count > 0) {
                Pending p = pending.Dequeue(); var item = Event(p.Target, p.Operation); string label = p.Operation;
                if (p.DraftInputTarget) item["input_target_candidate"] = new Dictionary<string, object> {
                    { "source", "preceding_recorded_click" }, { "verified", false }, { "requires_user_confirmation", true }, { "typed_text_recorded", false } };
                if (CoalesceDraftPreparation(p, events)) { list.Items.RemoveAt(list.Items.Count - 1); item["recorded_preparation"] = "CTRL+A"; }
                // Each real pointer action needs its own captured pixels. An
                // absent new crop must never inherit a previous button image.
                if (p.RequestedOperation != null) { lastImage = null; lastImageTarget = null; }
                if (p.Source != null) { item["capture_issue"] = "image_authoring_pending"; }
                else if (p.ImageOwner != null && p.ImageOwner.Saved != null) {
                    if (p.ImageOwner.Saved.ContainsKey("template_png")) CopyImage(p.ImageOwner.Saved, item);
                    else if (!p.ImageOwner.AuthorCompleted) { p.ImageOwner.ImageDependants.Add(p); item["capture_issue"] = "image_authoring_pending"; }
                    else item["reason"] = "focus_target_requires_selection";
                } else if (p.RequestedOperation == null && (p.Key != null || p.Keys != null || p.Baseline != null) && lastImage != null && lastImageTarget == p.Target) CopyImage(lastImage, item);
                else item["reason"] = "image_capture_required";
                if (p.Reason != null) item["reason"] = p.Reason;
                if (p.CaptureDiagnostic != null) item["capture_diagnostic"] = p.CaptureDiagnostic;
                if (p.RequestedOperation != null && p.RequestedOperation != "manual_entry" && p.Reason != "drag_requires_manual_setup") {
                    item["requested_operation"] = p.RequestedOperation;
                    item["pointer_evidence"] = new Dictionary<string, object> { { "source", "native_mouse_hook" }, { "operation", p.RequestedOperation }, { "before_input", p.Source != null || item.ContainsKey("template_png") } };
                    item["foreground_before_input"] = p.ForegroundBeforeInput;
                    item["semantic_target_before_input"] = p.SemanticBeforeInput;
                    if (!item.ContainsKey("template_png") && p.Source == null) item["capture_issue"] = Str(item, "reason", "image_capture_required");
                }
                // Preserve the original action for an in-place target repair.
                if (p.Operation == "manual_entry" && p.Key == null && p.Keys == null && p.Baseline == null && p.ScreenPoint != Point.Empty)
                    item["requested_operation"] = p.Reason == "drag_requires_manual_setup" ? "manual_entry" : p.RequestedOperation;
                if (p.Semantic != null && p.RequestedOperation == "click" && PointerRole(p.Semantic.Role)
                        && (p.Operation == "click" || p.Operation == "manual_entry" && (p.Reason == "image_capture_required" || p.Reason == "image_occluded_requires_selection" || p.Reason == "image_replay_unavailable"))) {
                    item["operation"] = "click"; item["native_target"] = p.Semantic.Native;
                    item.Remove("reason"); label = "요소 클릭 · 저장 전 Driver 대조";
                }
                if (p.Reason == "focus_target_requires_selection" && !p.DraftInputTarget) foreach (string key in new string[] { "template_png", "width", "height", "anchor", "capture_window", "source_size" }) item.Remove(key);
                if (p.Operation == "scroll") { item["wheel"] = p.Wheel; item["direction"] = p.Wheel < 0 ? "down" : "up"; item["amount"] = Math.Max(1, Math.Abs(p.Wheel) / 120); label = "스크롤"; }
                if (p.Key != null) item["key"] = p.Key; if (p.Keys != null) item["keys"] = p.Keys;
                if (Str(item, "operation", "") == "click" && lastClickEvent != null && lastClickTarget == p.Target && lastClickIndex == events.Count - 1 && DateTime.UtcNow.Subtract(lastClickTime).TotalMilliseconds <= SystemInformation.DoubleClickTime && Math.Abs(p.ScreenPoint.X - lastClickAnchor.X) <= SystemInformation.DoubleClickSize.Width && Math.Abs(p.ScreenPoint.Y - lastClickAnchor.Y) <= SystemInformation.DoubleClickSize.Height) {
                    foreach (Pending original in clickEvidence) if (original.Saved == lastClickEvent) {
                        if (lastPointer == p) lastPointer = original;
                        RedirectImageDependants(pending, p, original);
                        original.ValueSuperseded = true; break;
                    }
                    lastClickEvent["operation"] = "double_click"; lastClickEvent.Remove("native_target"); list.Items[lastClickIndex] = (lastClickIndex + 1).ToString() + "  두 번 클릭"; lastClickEvent = null; DisposePending(p); continue;
                }
                if (Str(item, "operation", "") == "manual_entry") label = "직접 설정 필요 · " + Str(item, "reason", "unknown_input");
                else if (p.Operation == "click") label = "클릭"; else if (p.Operation == "right_click") label = "오른쪽 클릭"; else if (p.Operation == "press_key") label = "키 " + p.Key; else if (p.Operation == "hotkey") label = "단축키 " + String.Join("+", p.Keys);
                if (item.ContainsKey("native_target")) label = "클릭 · 요소 후보 (저장 전 확인)";
                p.Saved = item; p.Index = events.Count; events.Add(item); list.Items.Add((events.Count).ToString() + "  " + label); list.TopIndex = Math.Max(0, list.Items.Count - 1);
                if (p.Source != null) authorQueue.Enqueue(p);
                else p.AuthorCompleted = true;
                if (p.Semantic != null) foreach (Pending previous in clickEvidence) if (previous != p && previous.ClickAt <= p.ClickAt && previous.Target == p.Target && previous.Semantic != null && previous.Semantic.Identity == p.Semantic.Identity) previous.ValueSuperseded = true;
                if (p.Semantic != null && p.RequestedOperation == "click" && (p.Operation == "click" || p.Reason == "image_capture_required" || p.Reason == "image_occluded_requires_selection" || p.Reason == "image_replay_unavailable")) { tracked = p.Semantic; trackedTarget = p.Target; trackedEvent = item; trackedIndex = p.Index; trackedAt = DateTime.UtcNow; trackedKeyboard = false; }
                if (trackedKeyboard && trackedTarget == p.Target && (p.Key != null || p.Keys != null || p.Operation == "manual_entry")) {
                    item["operation"] = "manual_entry"; item["reason"] = "recording_method_unverified"; item.Remove("key"); item.Remove("keys"); item.Remove("value"); p.Reason = "recording_method_unverified";
                    list.Items[p.Index] = (p.Index + 1).ToString() + "  콤보 입력·확정 방법 · 직접 설정 필요";
                }
                if (Str(item, "operation", "") == "click") { lastClickEvent = item; lastClickIndex = events.Count - 1; lastClickTarget = p.Target; lastClickTime = DateTime.UtcNow; lastClickAnchor = p.ScreenPoint; }
                // Replaying a key begins with focusing its image target. Once
                // a key can move focus, that old image is no longer a valid
                // target for subsequent input. Require another human selection.
                if (p.Key != null || (p.Keys != null && (p.Keys.Length != 2 || p.Keys[1] != "A"))) { lastImage = null; lastImageTarget = null; focusedState = null; if (lastPointer != null) lastPointer.ImageFocusValid = false; }
            }
            EnforcePayloadBudget(); StartAuthor();
            if (events.Count >= maximum && recording) { Warn("recording_event_limit"); Pause(); info.Text = "기록 가능한 " + maximum.ToString() + "단계에 도달했습니다. 이후 동작은 기록하지 않습니다. [기록 마치고 검토]를 눌러 확인하세요."; }
        }
        void StartAuthor() {
            if (payloadLimited || authorBusy || authorQueue.Count == 0 || Done || IsDisposed) return;
            Pending pointer = authorQueue.Dequeue(); SharedFrame source = pointer.Source; pointer.Source = null; authorBusy = true;
            Thread worker = new Thread(delegate() {
                Dictionary<string, object> result = null, rejected = null; string failure = null;
                try {
                    Bitmap authorFrame = source.Image;
                    for (int i = pointer.BeforeHover.Count - 1; i >= 0; i--) {
                        CachedFrame old = pointer.BeforeHover[i]; Point priorCursor = new Point(old.Cursor.X - old.Bounds.X, old.Cursor.Y - old.Bounds.Y);
                        if (!RecordingPreHoverFrame(old.Source.Image, source.Image, pointer.SourceAnchor, priorCursor)) continue;
                        authorFrame = old.Source.Image; pointer.CaptureDiagnostic["template_frame"] = "witnessed_before_hover";
                        pointer.CaptureDiagnostic["template_frame_age_ms"] = Math.Round(pointer.ClickAt.Subtract(old.At).TotalMilliseconds); break;
                    }
                    result = AuthorImage(authorFrame, pointer.Region, pointer.SourceAnchor, pointer.Blocked, true);
                }
                catch (Exception error) { failure = error is InvalidOperationException ? error.Message : "image_authoring_failed";
                    try { Rectangle region = UncoveredContext(pointer.Region, pointer.SourceAnchor, pointer.Blocked);
                        if (region.Width >= 8 && region.Height >= 8) using (Bitmap pixels = source.Image.Clone(region, PixelFormat.Format32bppArgb))
                            rejected = new Dictionary<string, object> { { "format", "computer-rejected-capture/v1" }, { "png", Encode(pixels) }, { "width", pixels.Width }, { "height", pixels.Height }, { "before_input", true } };
                    } catch { }
                }
                finally { source.Release(); foreach (CachedFrame old in pointer.BeforeHover) old.Source.Release(); pointer.BeforeHover.Clear(); }
                if (Done || IsDisposed) return;
                try { BeginInvoke((Action)delegate {
                    authorBusy = false; if (Done || IsDisposed) return;
                    var item = pointer.Saved;
                    pointer.AuthorCompleted = true;
                    if (payloadLimited) {
                        if (item != null) RecordingPayloadBudget.RequireRepair(item);
                        foreach (Pending dependant in pointer.ImageDependants) if (dependant.Saved != null) RecordingPayloadBudget.RequireRepair(dependant.Saved);
                        pointer.ImageDependants.Clear(); result = rejected = null; failure = "recording_payload_limit";
                    }
                    if (item != null && events.Contains(item)) {
                        if (result != null) {
                            foreach (var pair in result) item[pair.Key] = pair.Value;
                            var diagnostic = Map(item, "capture_diagnostic");
                            if (diagnostic != null && pointer.CaptureDiagnostic != null) foreach (var pair in pointer.CaptureDiagnostic) diagnostic[pair.Key] = pair.Value;
                            item.Remove("capture_issue");
                            if (Str(item, "reason", "") == "image_capture_required") item.Remove("reason");
                            if (pointer == lastPointer && pointer.ImageFocusValid) { lastImage = result; lastImageTarget = pointer.Target; lastImagePoint = pointer.ScreenPoint; }
                        } else {
                            item["capture_issue"] = failure;
                            if (rejected != null) item["rejected_capture"] = rejected;
                            if (!item.ContainsKey("native_target")) { item["operation"] = "manual_entry"; item["reason"] = failure; }
                            if (pointer.CaptureDiagnostic != null) { pointer.CaptureDiagnostic["status"] = "rejected"; pointer.CaptureDiagnostic["code"] = failure; }
                        }
                        int index = events.IndexOf(item);
                        if (index >= 0 && index < list.Items.Count) list.Items[index] = (index + 1).ToString() + "  " + (Str(item, "operation", "") == "manual_entry" ? "이 단계만 보완 필요 · " + failure : item.ContainsKey("native_target") ? "요소 확인 · " + Str(item, "operation", "click") : "이미지 확인 · " + Str(item, "operation", "click"));
                    }
                    CompleteImageDependants(pointer, result, failure, events);
                    EnforcePayloadBudget(); StartAuthor(); Heartbeat(true);
                    if (completeRequested && !authorBusy && authorQueue.Count == 0) { completeRequested = false; CompleteRecording(); }
                    else if (!payloadLimited && !recording && !authorBusy && authorQueue.Count == 0 && warnings.Contains("recording_capture_backlog")) {
                        start.Enabled = probeSettled && events.Count < maximum;
                        info.Text = "기록한 화면 정리가 끝났습니다. 지금까지의 동작은 보존되었습니다.\n‘기록 시작’으로 이어가거나 ‘기록 마치고 검토’를 누르세요.";
                    }
                }); } catch (InvalidOperationException) { }
            }); worker.IsBackground = true; worker.Start();
        }
        static bool PointerRole(string role) { return role == "Button" || role == "MenuItem" || role == "ListItem" || role == "DataItem"; }
        bool PendingComboSelection(Pending opener) {
            Pending popup = lastPointer;
            if (opener == null || popup == null || opener == popup || opener.Semantic == null || opener.Semantic.Role != "ComboBox"
                    || opener.Saved == null || popup.Saved == null || opener.ComboListWindow == IntPtr.Zero
                    || popup.Target.Owner != opener.Target || popup.Target.Hwnd != opener.ComboListWindow
                    || opener.RequestedOperation != "click" || popup.RequestedOperation != "click"
                    || opener.Reason == "drag_requires_manual_setup" || popup.Reason == "drag_requires_manual_setup"
                    || opener.InputVersion + 1 != popup.InputVersion || inputVersion != popup.InputVersion) return false;
            int index = events.IndexOf(opener.Saved);
            return index >= 0 && index == events.Count - 2 && Object.ReferenceEquals(events[index + 1], popup.Saved);
        }
        void Sample(Target target) {
            textBusy = true; workerStarted = DateTime.UtcNow; samplingTarget = target; int version = generation, sample = ++sampleVersion;
            System.Threading.Interlocked.Increment(ref activeWorkers);
            lock (stateLock) { sampleWorkers[target] = 1; }
            SemanticSample observeTracked = trackedTarget == target ? tracked : null;
            var observeValues = new List<Pending>();
            foreach (Pending pointer in clickEvidence) if (!pointer.ValueConfirmed && !pointer.ValueSuperseded && pointer.Saved != null && pointer.Target == target && pointer.Semantic != null && pointer.Semantic.Property != null
                    && !EditableTextRole(pointer.Semantic.Role) && DateTime.UtcNow.Subtract(pointer.LastInputAt).TotalMilliseconds >= 350
                    && RecordingValueObservationAllowed(DateTime.UtcNow.Subtract(pointer.ClickAt).TotalSeconds, PendingComboSelection(pointer))) { observeValues.Add(pointer); if (observeValues.Count >= 8) break; }
            var observePointers = new List<Pending>();
            foreach (Pending pointer in clickEvidence) if (!pointer.ProbeRequested && pointer.Semantic == null && pointer.Saved != null && pointer.HitWindow != IntPtr.Zero && DateTime.UtcNow.Subtract(pointer.ClickAt).TotalMilliseconds <= 1200) {
                pointer.ProbeRequested = true; observePointers.Add(pointer); if (observePointers.Count >= 4) break;
            }
            Thread worker = new Thread(delegate() {
                Dictionary<string, object> state = null; AutomationElement stateElement = null;
                SemanticSample hover = null, changed = null; var clicks = new List<KeyValuePair<Pending, SemanticSample>>(); var values = new List<KeyValuePair<Pending, SemanticSample>>();
                try {
                    // A popup can take foreground before its opener is sampled.
                    // Only immutable exact-child evidence may resolve a historical
                    // Button; active Edit/value tracking still requires foreground.
                    foreach (Pending pointer in observePointers) {
                        try { bool historical = pointer != lastPointer || pointer.Target != target || !pointer.Target.Foreground();
                            SemanticSample hit = ReadClickedChild(pointer, historical); if (hit != null) clicks.Add(new KeyValuePair<Pending, SemanticSample>(pointer, hit));
                        } catch { }
                    }
                    if (target.Foreground()) {
                        foreach (Pending pointer in observeValues) { try { var point = Map(pointer.Semantic.Native, "point");
                            SemanticSample value = ReadSemantic(pointer.Semantic.Element, target, new POINT { X = (int)Real(point, "x", 0), Y = (int)Real(point, "y", 0) }, false);
                            if (value != null) values.Add(new KeyValuePair<Pending, SemanticSample>(pointer, value));
                        } catch { } }
                        POINT cursor; if (GetCursorPos(out cursor) && target.Contains(cursor)) hover = ReadSemantic(AutomationElement.FromPoint(new System.Windows.Point(cursor.X, cursor.Y)), target, cursor, true);
                        if (observeTracked != null) { var pt = Map(observeTracked.Native, "point"); changed = ReadSemantic(observeTracked.Element, target, new POINT { X = (int)Real(pt, "x", 0), Y = (int)Real(pt, "y", 0) }, false); }
                        AutomationElement e = AutomationElement.FocusedElement;
                        if (e != null && target.AcceptsSemanticPid(e.Current.ProcessId) && ProtectedOrUnowned(e, target)) state = new Dictionary<string, object> { { "protected", true } };
                        else if (e != null && target.AcceptsSemanticPid(e.Current.ProcessId) && EditableTextRole(e.Current.ControlType.ProgrammaticName.Replace("ControlType.", "")) && e.Current.IsEnabled) {
                            object raw;
                            if (e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) { ValuePattern p = (ValuePattern)raw;
                                if (!p.Current.IsReadOnly && target.Foreground()) { string value = p.Current.Value; System.Windows.Rect bounds = e.Current.BoundingRectangle;
                                    if (value.Length <= 2000 && !bounds.IsEmpty) {
                                        state = new Dictionary<string, object> { { "value", value }, { "identity", String.Join(",", Array.ConvertAll(e.GetRuntimeId(), delegate(int n) { return n.ToString(); })) }, { "x", (int)bounds.X }, { "y", (int)bounds.Y }, { "width", (int)bounds.Width }, { "height", (int)bounds.Height } };
                                        var focus = ReadSemantic(e, target, new POINT { X = (int)(bounds.X + bounds.Width / 2), Y = (int)(bounds.Y + bounds.Height / 2) }, false);
                                        if (focus != null && EditableTextRole(focus.Role)) { state["native_target"] = focus.Native; stateElement = e; }
                                    }
                                }
                            }
                        }
                    }
                } catch { }
                lock (stateLock) { sampleWorkers.Remove(target); if (sample == sampleVersion) { completedState = state; completedEdit = stateElement; completedHover = hover; completedTracked = changed; completedClicks = clicks; completedValues = values; completedTarget = target; completedGeneration = version; workerCompleted = true; } }
                System.Threading.Interlocked.Decrement(ref activeWorkers);
            }); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }
        bool ClickedChildStillMatches(Pending pointer) {
            uint pid; RECT bounds;
            return pointer.Target.Valid() && pointer.RequestedOperation == "click" && pointer.HitWindow != IntPtr.Zero && pointer.HitEnabled && pointer.Reason != "drag_requires_manual_setup"
                && DateTime.UtcNow.Subtract(pointer.ClickAt).TotalMilliseconds <= 1200
                && GetWindowThreadProcessId(pointer.HitWindow, out pid) == pointer.HitThread && pointer.Target.AcceptsWindow(pointer.HitWindow)
                && GetAncestor(pointer.HitWindow, 2) == pointer.Target.Hwnd && GetWindowRect(pointer.HitWindow, out bounds)
                && WindowClass(pointer.HitWindow) == pointer.HitClass && GetDlgCtrlID(pointer.HitWindow) == pointer.HitControlId
                && Rectangle.FromLTRB(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom) == pointer.HitBounds;
        }
        SemanticSample ReadClickedChild(Pending pointer, bool historical) {
            if (!ClickedChildStillMatches(pointer)) return null;
            AutomationElement element = AutomationElement.FromHandle(pointer.HitWindow);
            if (element == null || element.Current.NativeWindowHandle != pointer.HitWindow.ToInt64() || historical && element.Current.ControlType != ControlType.Button) return null;
            // A shared top-level HWND is intentionally excluded: reading a new
            // WPF control at an old point after a transition could change identity.
            var result = ReadSemantic(element, pointer.Target, new POINT { X = pointer.ScreenPoint.X, Y = pointer.ScreenPoint.Y }, false, historical);
            return ClickedChildStillMatches(pointer) ? result : null;
        }
        void AttachClickedSemantic(Pending pointer, SemanticSample semantic) {
            if (pointer == null || semantic == null || pointer.Saved == null || pointer.Reason == "drag_requires_manual_setup"
                || pointer.Index < 0 || pointer.Index >= events.Count || !Object.ReferenceEquals(events[pointer.Index], pointer.Saved)
                || Str(pointer.Saved, "operation", "") == "double_click" || pointer != lastPointer && semantic.Role != "Button") return;
            pointer.Semantic = semantic;
            foreach (Pending previous in clickEvidence) if (previous != pointer && previous.ClickAt <= pointer.ClickAt && previous.Target == pointer.Target && previous.Semantic != null && previous.Semantic.Identity == semantic.Identity) previous.ValueSuperseded = true;
            if (pointer == lastPointer && pointer.Target.Foreground()) { tracked = semantic; trackedTarget = pointer.Target; trackedEvent = pointer.Saved; trackedIndex = pointer.Index; trackedAt = DateTime.UtcNow; trackedKeyboard = false; }
            if (semantic.Role == "Button") { pointer.Saved["operation"] = "click"; pointer.Saved["native_target"] = semantic.Native; pointer.Saved.Remove("reason"); list.Items[pointer.Index] = (pointer.Index + 1).ToString() + "  버튼 클릭 · 저장 전 Driver 대조 필요"; }
            // An exact native child was witnessed by the hook before input.
            // A fresh UIA read may author its desired checkbox state, even if
            // the user clicked too quickly for a pre-hover value sample. This
            // does not assert a previous value or an observed state transition.
            if (RecordingDesiredCheckWitness(ClickedChildStillMatches(pointer) && semantic.NativeWindow == pointer.HitWindow,
                    pointer == lastPointer && !pointer.ValueSuperseded, pointer.Target.Foreground(), pointer.InputVersion, inputVersion,
                    pointer.ClickAt, semantic.ObservedAt, DateTime.UtcNow, semantic.Role, semantic.Property, semantic.Value)) {
                pointer.Saved["native_child_before_input"] = true;
                pointer.Saved["value_observation"] = "desired_state_after_exact_click";
                SaveSemanticValue(pointer, semantic);
            }
        }
        bool FoldNativeCombo(Pending opener, SemanticSample after) {
            Pending popup = lastPointer;
            if (opener == null || popup == null || opener == popup || opener.Semantic == null || popup.Saved == null || opener.Saved == null
                || popup.Target.Owner != opener.Target || popup.RequestedOperation != "click" || popup.Reason == "drag_requires_manual_setup"
                || opener.HitWindow == IntPtr.Zero || opener.ComboListWindow == IntPtr.Zero || after.NativeWindow != opener.HitWindow
                || !opener.Target.Valid() || !opener.Target.Foreground()) return false;
            uint childPid, popupPid; RECT bounds; var combo = new COMBOINFO { Size = Marshal.SizeOf(typeof(COMBOINFO)) };
            bool childCurrent = GetWindowThreadProcessId(opener.HitWindow, out childPid) == opener.HitThread && opener.Target.AcceptsWindow(opener.HitWindow)
                && GetAncestor(opener.HitWindow, 2) == opener.Target.Hwnd && GetDlgCtrlID(opener.HitWindow) == opener.HitControlId && WindowClass(opener.HitWindow) == opener.HitClass
                && GetWindowRect(opener.HitWindow, out bounds) && Rectangle.FromLTRB(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom) == opener.HitBounds;
            bool exactList = childCurrent && GetComboBoxInfo(opener.HitWindow, ref combo) && combo.Combo == opener.HitWindow && combo.List == opener.ComboListWindow
                && combo.List == popup.Target.Hwnd && popup.PointWindow == combo.List && popup.PointClass == "ComboLBox"
                && GetWindowThreadProcessId(combo.List, out popupPid) == popup.PointThread && popup.PointThread != 0 && popupPid == popup.Target.Pid && WindowClass(combo.List) == popup.PointClass;
            int index = events.IndexOf(opener.Saved);
            // The exact semantic proof is independent of capture availability.
            // Keeping a good pre-click image must not disable native selection
            // recording and leave an unreplayable, already-closed popup click.
            if (!RecordingNativeComboWitness(opener.Semantic.Identity == after.Identity && opener.Semantic.Role == "ComboBox" && after.Role == "ComboBox" && opener.HitEnabled,
                    exactList, index >= 0 && index == events.Count - 2 && Object.ReferenceEquals(events[index + 1], popup.Saved) && Str(popup.Saved, "operation", "") != "double_click",
                    mouseDown != popup && mouseDown != opener,
                    opener.RequestedOperation == "click" && opener.Reason != "drag_requires_manual_setup" && Str(opener.Saved, "requested_operation", "") == "click"
                        && Str(popup.Saved, "requested_operation", "") == "click" && Str(opener.Saved, "operation", "") != "double_click",
                    opener.InputVersion, popup.InputVersion, inputVersion, opener.Semantic.ObservedAt, popup.ClickAt, after.ObservedAt, opener.Semantic.Value, after.Value)) return false;
            // Both genuine clicks are represented by this one absolute value
            // action. Remove only the verified tail popup row; retaining it
            // would replay a second selection against an already closed list.
            opener.Saved["selection_method"] = "native_combobox_list_click";
            opener.Saved["recorded_pointer_actions"] = 2;
            events.RemoveAt(index + 1); list.Items.RemoveAt(index + 1); clickEvidence.Remove(popup);
            RetireFoldedPointer(popup); lastPointer = opener;
            lastClickEvent = null; lastClickIndex = -1;
            if (trackedEvent != null && !events.Contains(trackedEvent)) { tracked = null; trackedEvent = null; trackedIndex = -1; trackedTarget = null; }
            return true;
        }
        void ConfirmValue(Pending pointer, SemanticSample after) {
            if (pointer.ValueConfirmed || pointer.ValueSuperseded || pointer.Saved == null || !events.Contains(pointer.Saved) || Str(pointer.Saved, "operation", "") == "double_click" || pointer.Semantic == null || pointer.Semantic.Identity != after.Identity || after.Property == null
                    || Object.Equals(pointer.Semantic.Value, after.Value) || !pointer.Target.Foreground()) return;
            // The original opener owns this observation even after the user
            // clicked another control. A later interaction with the same
            // identity supersedes it instead of assigning the final value to
            // multiple earlier clicks.
            int index = events.IndexOf(pointer.Saved); bool popupInput = false;
            foreach (Target popup in targets) if (popup.Owner == pointer.Target) for (int i = index + 1; i < events.Count; i++) if (Str(events[i], "program_id", "") == popup.Program && Str(events[i], "window_ref", "") == popup.Window) popupInput = true;
            if (after.Role == "ComboBox") {
                pointer.Saved["observed_selection"] = after.Value;
                // A held click or unfinished popup sample may become provable
                // after mouse-up. Do not freeze it as confirmed on a failed fold.
                if (popupInput && !FoldNativeCombo(pointer, after)) return;
                // Keyboard commit semantics are deliberately not invented.
                for (int i = index + 1; i < events.Count; i++) if (Str(events[i], "program_id", "") == pointer.Target.Program && Str(events[i], "window_ref", "main") == pointer.Target.Window
                    && (events[i].ContainsKey("key") || events[i].ContainsKey("keys") || Str(events[i], "input_method", "") == "keyboard_unverified")) return;
            }
            if (after.Role != "CheckBox" && after.Role != "ComboBox") return;
            SaveSemanticValue(pointer, after);
        }
        void SaveSemanticValue(Pending pointer, SemanticSample after) {
            int index = events.IndexOf(pointer.Saved); if (index < 0) return;
            string operation = after.Role == "CheckBox" ? "set_checked" : "select_option";
            pointer.Saved["operation"] = operation; pointer.Saved["native_target"] = after.Native;
            pointer.Saved[operation == "set_checked" ? "checked" : "value"] = after.Value;
            pointer.Saved["after"] = new Dictionary<string, object> { { "property", after.Property }, { "equals", after.Value } };
            pointer.Saved["event_evidence"] = new Dictionary<string, object> { { "source", "uia_recording" }, { "identity", after.Identity }, { "after_input", true } };
            pointer.Saved.Remove("reason"); pointer.ValueConfirmed = true;
            list.Items[index] = (index + 1).ToString() + "  " + (operation == "set_checked" ? "체크 상태 확인" : "목록 선택값 확인") + " · 저장 전 Driver 대조 필요";
        }
        SemanticSample ReadSemantic(AutomationElement leaf, Target target, POINT point, bool normalizeComposite, bool historicalButton = false) {
            var chain = ReadOwnedSemanticChain(leaf, target);
            if (chain == null) return null;
            int chosen = 0;
            if (normalizeComposite) {
                // A ComboBox often exposes an Edit/Text child at the click.
                // It is its owning ComboBox's final selection we must observe.
                for (int i = 0; i < Math.Min(5, chain.Count); i++) if (chain[i].Current.ControlType == ControlType.ComboBox) { chosen = i; break; }
                if (chosen == 0 && leaf.Current.ControlType == ControlType.Text)
                    for (int i = 1; i < Math.Min(4, chain.Count); i++) if (chain[i].Current.ControlType == ControlType.Button || chain[i].Current.ControlType == ControlType.CheckBox) { chosen = i; break; }
            }
            AutomationElement e = chain[chosen]; string role = e.Current.ControlType.ProgrammaticName.Replace("ControlType.", "");
            if (!EditableTextRole(role) && role != "ComboBox" && role != "CheckBox" && role != "Button" && role != "MenuItem" && !(target.Owner != null && (role == "ListItem" || role == "DataItem"))) return null;
            System.Windows.Rect b = e.Current.BoundingRectangle;
            if (b.IsEmpty || !b.Contains(point.X, point.Y) || !e.Current.IsEnabled && !(historicalButton && role == "Button")) return null;
            var ancestors = new List<Dictionary<string, object>>();
            for (int i = chosen + 1; i < chain.Count; i++) ancestors.Add(SemanticIdentity(chain[i]));
            Rectangle window = target.Bounds();
            var native = new Dictionary<string, object> { { "status", "selected" }, { "pid", target.Pid }, { "window_id", target.Hwnd.ToInt64() }, { "element", SemanticIdentity(e) }, { "ancestors", ancestors },
                { "point", new Dictionary<string, object> { { "x", point.X }, { "y", point.Y } } },
                { "bounds", new Dictionary<string, object> { { "x", b.X }, { "y", b.Y }, { "width", b.Width }, { "height", b.Height } } },
                { "window_bounds", new Dictionary<string, object> { { "x", window.X }, { "y", window.Y }, { "width", window.Width }, { "height", window.Height } } } };
            var sample = new SemanticSample { Element = e, Native = native, Role = role, Enabled = e.Current.IsEnabled, NativeWindow = new IntPtr(e.Current.NativeWindowHandle), Identity = String.Join(",", Array.ConvertAll(e.GetRuntimeId(), delegate(int n) { return n.ToString(); })) };
            object raw;
            if (role == "CheckBox" && e.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) {
                ToggleState state = ((TogglePattern)raw).Current.ToggleState;
                if (state != ToggleState.Indeterminate) { sample.Property = "selected"; sample.Value = state == ToggleState.On; }
            } else if (role == "ComboBox") {
                if (e.TryGetCurrentPattern(SelectionPattern.Pattern, out raw)) {
                    AutomationElement[] selection = ((SelectionPattern)raw).Current.GetSelection();
                    if (selection.Length == 1 && !ProtectedOrUnowned(selection[0], target)) { string value = selection[0].Current.Name; if (!String.IsNullOrEmpty(value) && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; } }
                }
                if (sample.Property == null && e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) {
                    string value = ((ValuePattern)raw).Current.Value; if (value != null && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; }
                }
            } else if (EditableTextRole(role) && e.TryGetCurrentPattern(ValuePattern.Pattern, out raw) && !((ValuePattern)raw).Current.IsReadOnly) {
                string value = ((ValuePattern)raw).Current.Value; if (value != null && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; }
            }
            sample.ObservedAt = DateTime.UtcNow; return sample;
        }
        bool ProtectedOrUnowned(AutomationElement element, Target target) {
            return ReadOwnedSemanticChain(element, target) == null;
        }
        Dictionary<string, object> SemanticIdentity(AutomationElement element) {
            if (element.Current.IsPassword) throw new InvalidOperationException("protected_input");
            string name = element.Current.Name ?? "", id = element.Current.AutomationId ?? "";
            if (name.Length > 1000 || id.Length > 1000) throw new InvalidOperationException("identity_too_large");
            return new Dictionary<string, object> { { "role", element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") }, { "name", name }, { "automation_id", id }, { "is_password", false } };
        }
        void Tick(object sender, EventArgs args) { if (ticking || Done) return; ticking = true; try { TickCore(); } finally { ticking = false; } }
        void TickCore() {
            if (Done) return;
            if (finalRead != null) { FinishFinalRead(); return; }
            if (preparingTarget != null) { PrepareTick(); return; }
            if (!probeSettled && hookProbe == "available" && (probeDone || DateTime.UtcNow.Subtract(probeAt).TotalMilliseconds > 1500)) {
                probeSettled = true; uiaProbe = probeGood ? "available" : probeDone ? "unavailable" : "timeout";
                if (!probeGood) Warn("uia_probe_unavailable"); recordState = "ready"; start.Enabled = true;
                bool imageBlocked = false; foreach (Target target in targets) if (!target.ImageAllowed) imageBlocked = true;
                info.Text = "시작 점검 완료 · 입력 후크 사용 가능 / UIA " + (probeGood ? "응답 확인" : "응답 확인 안 됨")
                    + (imageBlocked ? "\n이미지 재생 좌표를 확인하지 못한 창은 요소 방식만 기록합니다. 이미지 동작은 ‘보완 필요’로 표시합니다." : "\n이미지 재생 좌표 점검 완료 · 요소를 찾지 못하면 이미지로 기록합니다.")
                    + "\n[기록 시작] 후 클릭과 입력은 실제 프로그램에 적용됩니다.";
            }
            Drain(); Heartbeat(false); if (!recording) return; DiscoverPopup(); Target current = Current();
            if (trackedKeyboard && trackedEvent != null) {
                trackedEvent["operation"] = "manual_entry"; trackedEvent["reason"] = "recording_method_unverified"; trackedEvent["input_method"] = "keyboard_unverified";
                trackedEvent.Remove("value"); trackedEvent.Remove("native_target"); trackedEvent.Remove("after"); Warn("recording_method_unverified");
                list.Items[trackedIndex] = (trackedIndex + 1).ToString() + "  콤보 입력·확정 방법 · 직접 설정 필요";
            }
            // A focus transition alone does not mean an action was lost. The
            // input hooks report actual out-of-scope actions; restoring or
            // clicking our recorder must not add a missing-action warning.
            if (current == null) {
                InvalidateDraftInput();
                string waitingText = OwnRecorderControl(GetForegroundWindow())
                    ? "녹화 도구 확인 중 · 지금까지 기록한 동작은 보존됩니다.\n대상 프로그램으로 돌아가면 계속 기록합니다.\n녹화 도구의 버튼은 작업에 기록되지 않습니다."
                    : "연결 범위 밖이라 기록 일시정지\n원래 프로그램이나 관련 팝업으로 돌아오면 계속합니다.\n다른 프로그램·관련 없는 창은 기록되지 않습니다.";
                if (!outside || info.Text != waitingText) { outside = true; recordState = "outside_target"; info.Text = waitingText; Heartbeat(true); }
                ReleaseFrame();
            }
            else {
                if (lastPointer != null && lastPointer.Target != current) InvalidateDraftInput();
                if (outside) { outside = false; recordState = "recording"; info.Text = ActiveText(); Heartbeat(true); }
                try {
                    if (current.ImageAllowed) CaptureFor(current, true); else ReleaseFrame();
                } catch (Exception error) { CaptureUnavailable(current,error);
                    if (error.Message == "recording_capture_backlog") {
                        Warn("recording_capture_backlog"); Pause(); WindowState = FormWindowState.Normal; Activate();
                        bool waitingForAuthor = authorBusy || authorQueue.Count > 0;
                        start.Enabled = !payloadLimited && !waitingForAuthor && probeSettled && hookProbe == "available" && events.Count < maximum;
                        info.Text = "큰 화면의 기록을 정리하느라 잠시 멈췄습니다. 지금까지의 동작은 보존되었습니다.\n" + (waitingForAuthor
                            ? "정리가 끝나면 다시 시작할 수 있습니다. 이후 입력은 아직 기록하지 않습니다."
                            : "화면 캐시 정리가 끝났습니다. ‘기록 시작’으로 이어가거나 기록을 마치고 검토하세요."); Heartbeat(true); return;
                    }
                }
            }
            lock (stateLock) {
                if (workerCompleted) {
                    workerCompleted = false; textBusy = false;
                    if (completedGeneration == generation && completedClicks != null) foreach (var click in completedClicks) AttachClickedSemantic(click.Key, click.Value);
                    if (completedGeneration == generation && completedValues != null) foreach (var value in completedValues) ConfirmValue(value.Key, value.Value);
                    if (completedGeneration == generation && completedTarget == current) {
                        hovered = completedHover; hoverTarget = current; hoverAt = DateTime.UtcNow;
                        if (tracked != null && !trackedKeyboard && trackedTarget == current && trackedEvent != null && Str(trackedEvent, "operation", "") != "double_click" && completedTracked != null && tracked.Identity == completedTracked.Identity
                            && DateTime.UtcNow.Subtract(trackedAt).TotalMilliseconds >= 350 && DateTime.UtcNow.Subtract(dirtyAt).TotalMilliseconds >= 400 && workerStarted >= dirtyAt
                            && !Object.Equals(tracked.Value, completedTracked.Value) && completedTracked.Property != null) {
                            bool popupInput = false;
                            if (completedTracked.Role == "ComboBox") for (int index = trackedIndex + 1; index < events.Count; index++)
                                if (Str(events[index], "program_id", "") == current.Program && Str(events[index], "window_ref", "main") != current.Window) popupInput = true;
                            if (popupInput) {
                                // Preserve the actual open-popup + item-click path;
                                // replacing its first click with select_option would
                                // select twice and leave a stale popup step behind.
                                trackedEvent["observed_selection"] = completedTracked.Value;
                                Pending opener = clickEvidence.Find(delegate(Pending entry) { return entry.Saved == trackedEvent; });
                                if (opener != null) ConfirmValue(opener, completedTracked);
                            } else {
                            string op = completedTracked.Role == "ComboBox" ? "select_option" : completedTracked.Property == "selected" ? "set_checked" : "set_value";
                            // Only an observed changed property becomes a semantic action.
                            // The parent independently checks this identity against Driver.
                            trackedEvent["operation"] = op; trackedEvent["native_target"] = completedTracked.Native;
                            trackedEvent["after"] = new Dictionary<string, object> { { "property", completedTracked.Property }, { "equals", completedTracked.Value } };
                            trackedEvent["event_evidence"] = new Dictionary<string, object> { { "source", "uia_recording" }, { "identity", completedTracked.Identity }, { "after_input", true } };
                            trackedEvent[op == "set_checked" ? "checked" : "value"] = completedTracked.Value; trackedEvent.Remove("reason");
                            list.Items[trackedIndex] = (trackedIndex + 1).ToString() + "  " + (op == "select_option" ? "목록 선택값 확인" : op == "set_checked" ? "체크 상태 확인" : "입력값 확인") + " · 저장 전 Driver 대조 필요";
                            // Text keys belong to the same clicked Edit/ComboBox; avoid replaying both the click-change and a duplicate text placeholder.
                            if (typing != null && typing.Saved != null && typing.Target == current && typing.Saved != trackedEvent && typing.Index == events.Count - 1
                                && typing.Reason != "protected_input" && completedState != null && Str(completedState, "identity", "") == tracked.Identity) {
                                events.RemoveAt(typing.Index); list.Items.RemoveAt(typing.Index); typing = null;
                                // CTRL+A only prepared this exact Edit replacement.
                                // Keeping it after the final-value step would select
                                // the new value a second time and add a stale image.
                                if (events.Count == trackedIndex + 2) {
                                    var preparation = events[events.Count - 1]; object rawKeys;
                                    if (Str(preparation, "operation", "") == "hotkey" && preparation.TryGetValue("keys", out rawKeys)) {
                                        string[] keys = rawKeys as string[];
                                        if (keys != null && keys.Length == 2 && keys[0] == "CTRL" && keys[1] == "A") { events.RemoveAt(events.Count - 1); list.Items.RemoveAt(list.Items.Count - 1); }
                                    }
                                }
                            }
                            }
                        }
                        focusedState = completedState; focusedEdit = completedEdit; focusedTarget = completedTarget; focusedAt = DateTime.UtcNow;
                        if (typing != null && typing.Target == current && typing.Saved != null && completedState != null && Str(completedState, "protected", "False") == "True") {
                            typing.Reason = "protected_input"; typing.Baseline = null; typing.Saved["operation"] = "manual_entry"; typing.Saved["reason"] = "protected_input"; typing.Saved.Remove("value"); list.Items[typing.Index] = (typing.Index + 1).ToString() + "  암호 입력 · 기록하지 않았습니다";
                            ClearDraftInputCandidate(typing);
                        }
                        if (typing != null && typing.Target == current && typing.Saved != null && typing.Baseline != null && completedState != null && DateTime.UtcNow.Subtract(dirtyAt).TotalMilliseconds >= 400 && workerStarted >= dirtyAt && typing.Reason != "shortcut_requires_manual_setup" && typing.Reason != "recording_method_unverified") {
                            string before = Str(typing.Baseline, "value", ""), after = Str(completedState, "value", "");
                            if (RecordedEditConfirmed(typing.Baseline, completedState, typing.ReplaceAll)) {
                                typing.Saved["operation"] = "set_value"; typing.Saved["value"] = after; typing.Saved["input_role"] = Str(Map(Map(completedState, "native_target"), "element"), "role", "Edit"); typing.Saved.Remove("reason");
                                typing.Saved["native_target"] = Map(completedState, "native_target");
                                typing.Saved["after"] = new Dictionary<string, object> { { "property", "value" }, { "equals", after } };
                                typing.Saved["event_evidence"] = new Dictionary<string, object> { { "source", "uia_recording" }, { "identity", Str(completedState, "identity", "") }, { "after_input", true } };
                                typingConfirmedAt = DateTime.UtcNow; list.Items[typing.Index] = (typing.Index + 1).ToString() + "  글자 입력 · 저장 전 Driver 대조";
                                if (typing.ReplaceAll && selectAllPreparation != null && selectAllPreparation.Saved != null && selectAllPreparation.Index == typing.Index - 1 && typing.Index == events.Count - 1) {
                                    events.RemoveAt(selectAllPreparation.Index); list.Items.RemoveAt(selectAllPreparation.Index); typing.Index--;
                                    list.Items[typing.Index] = (typing.Index + 1).ToString() + "  글자 입력 · 저장 전 Driver 대조";
                                    selectAllPreparation = null;
                                }
                            } else if (before.Length > 0) typing.Saved["reason"] = "existing_text_requires_review";
                        }
                    }
                }
            }
            if (textBusy && DateTime.UtcNow.Subtract(workerStarted).TotalMilliseconds > 1200) {
                // UIA providers may block COM indefinitely. Abandon only this
                // sample; late responses cannot overwrite the next generation.
                // Bound concurrent workers so repeated stalls cannot leak threads.
                lock (stateLock) { sampleVersion++; workerCompleted = false; textBusy = false; }
                if (samplingTarget != null) sampleRetryAt[samplingTarget] = DateTime.UtcNow.AddSeconds(2);
                hovered = null; hoverTarget = null; focusedState = null;
                Warn("uia_text_unavailable"); info.Text = "요소 응답 지연 · 이 동작은 이미지 또는 보완 필요로 기록합니다.\n요소 읽기는 응답이 돌아오면 자동으로 재개합니다.\n마친 뒤 ‘보완 필요’ 단계를 확인하세요."; Heartbeat(true);
            }
            EnforcePayloadBudget(); if (!recording) return;
            DateTime retryAt;
            bool targetWorkerFree; lock (stateLock) { targetWorkerFree = current != null && !sampleWorkers.ContainsKey(current); }
            if (!textBusy && targetWorkerFree && activeWorkers < 4 && current != null && (!sampleRetryAt.TryGetValue(current, out retryAt) || DateTime.UtcNow >= retryAt)
                    && DateTime.UtcNow.Subtract(lastSample).TotalMilliseconds >= 300) { lastSample = DateTime.UtcNow; Sample(current); }
        }
    }
}
