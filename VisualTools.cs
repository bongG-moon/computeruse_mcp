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
    [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
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
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool MoveFileEx(string a, string b, uint flags);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] static extern int DwmGetWindowRectangle(IntPtr hwnd, int attribute, out RECT value, int size);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
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
        if (args.Length != 3 || (args[0] != "--pick" && args[0] != "--match" && args[0] != "--record")) return 2;
        string nonce = null, response = null;
        try {
            if (!Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            string request = Path.GetFullPath(args[1]); response = Path.GetFullPath(args[2]);
            if (!String.Equals(Path.GetDirectoryName(request), Path.GetDirectoryName(response), StringComparison.OrdinalIgnoreCase)) return 2;
            Dictionary<string, object> data = Json.Deserialize<Dictionary<string, object>>(Read(request));
            string token = Str(data, "nonce", ""); if (!Nonce(token)) return 2; nonce = token;
            if (args[0] == "--match") { Write(response, Match(data)); return 0; }
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
        internal Target(Dictionary<string, object> d) {
            Pid = Num(d, "pid", 0); Hwnd = new IntPtr(Convert.ToInt64(Str(d, "window_id", "0")));
            Program = Str(d, "program_id", ""); Window = Str(d, "window_ref", "main"); Label = Str(d, "label", "선택한 프로그램");
            ImageAllowed = !d.ContainsKey("image_allowed") || Object.Equals(d["image_allowed"], true); PreflightMessage = Str(d, "preflight_message", "");
            if (Pid <= 0 || Hwnd == IntPtr.Zero || Label.Length > 200 || !Valid()) throw new InvalidOperationException("target_unavailable");
            Lifetime = OpenProcess(0x100000 | 0x1000, false, (uint)Pid); if (Lifetime == IntPtr.Zero) throw new InvalidOperationException("process_identity_unavailable"); OwnLifetime = true;
            uint ignored; ThreadId = GetWindowThreadProcessId(Hwnd, out ignored); Title = WindowText(Hwnd); ClassName = WindowClass(Hwnd);
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
            return Owner == null || (Owner.Valid() && RelatedTo(Hwnd, Owner.Hwnd, Pid));
        } catch { return false; } }
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
        internal void EnsureCaptureReady() {
            if (!Valid()) throw new InvalidOperationException("target_unavailable");
            if (IsIconic(Hwnd)) throw new InvalidOperationException("target_minimized");
            if (!IsWindowVisible(Hwnd)) throw new InvalidOperationException("target_hidden");
            IntPtr foreground = GetForegroundWindow(); if (foreground != Hwnd && (Owner == null || (foreground != Owner.Hwnd && !RelatedTo(Hwnd, foreground, Pid)))) throw new InvalidOperationException("target_requires_foreground");
            int cloaked; if (DwmGetWindowAttribute(Hwnd, 14, out cloaked, 4) == 0 && cloaked != 0) throw new InvalidOperationException("target_cloaked");
            CaptureBounds();
        }
        internal bool Foreground() { try { EnsureCaptureReady(); return true; } catch { return false; } }
        internal bool Contains(POINT p) { return GetAncestor(WindowFromPoint(p), 2) == Hwnd; }
        internal void Release() { if (OwnLifetime && Lifetime != IntPtr.Zero) { CloseHandle(Lifetime); Lifetime = IntPtr.Zero; Retired = true; } }
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
        var seen = new HashSet<IntPtr>(); IntPtr current = hwnd;
        for (int count = 0; count < 32 && current != IntPtr.Zero && seen.Add(current); count++) {
            current = GetWindow(current, 4); if (current == IntPtr.Zero) return false;
            uint found; if (GetWindowThreadProcessId(current, out found) == 0 || found != (uint)pid) return false;
            current = GetAncestor(current, 2); if (current == owner) return true;
        } return false;
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
    static List<Rectangle> Occluders(Target target)
    {
        var found = new List<Rectangle>(); IntPtr window = GetWindow(target.Hwnd, 3); int count = 0;
        while (window != IntPtr.Zero) {
            if (++count > 512) throw new InvalidOperationException("window_order_unavailable");
            if (IsWindowVisible(window) && !IsIconic(window)) {
                int cloaked; RECT r;
                if ((DwmGetWindowAttribute(window, 14, out cloaked, 4) != 0 || cloaked == 0) &&
                    (DwmGetWindowRectangle(window, 9, out r, Marshal.SizeOf(typeof(RECT))) == 0 || GetWindowRect(window, out r))) {
                    Rectangle bounds = Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom); if (bounds.Width > 0 && bounds.Height > 0) found.Add(bounds);
                }
            }
            window = GetWindow(window, 3);
        }
        return found;
    }
    static bool Covered(Rectangle region, List<Rectangle> rectangles) { foreach (Rectangle r in rectangles) if (r.IntersectsWith(region)) return true; return false; }
    sealed class RenderJob {
        internal DateTime Started; internal Rectangle Raw, Visible; internal Bitmap Image; internal Exception Error;
        internal readonly ManualResetEvent Completed = new ManualResetEvent(false);
    }
    static readonly object renderLock = new object();
    static readonly Dictionary<IntPtr, RenderJob> renderJobs = new Dictionary<IntPtr, RenderJob>();
    // PrintWindow can hang inside an application's renderer. Never call it on
    // the input-hook/UI thread; keep at most four outstanding target workers.
    // We render the target's pixels rather than ignoring overlay rectangles.
    static Bitmap RenderWindow(Target target, Rectangle visible) {
        RenderJob job; Rectangle raw = target.Bounds();
        if (raw.Width < 8 || raw.Height < 8 || raw.Width > 4128 || raw.Height > 4128 || !raw.Contains(visible)) throw new InvalidOperationException("target_capture_size_invalid");
        lock (renderLock) {
            if (renderJobs.TryGetValue(target.Hwnd, out job) && job.Completed.WaitOne(0)
                && (DateTime.UtcNow.Subtract(job.Started).TotalMilliseconds > 250 || job.Raw != raw || job.Visible != visible)) {
                if (job.Image != null) job.Image.Dispose(); job.Completed.Dispose(); renderJobs.Remove(target.Hwnd); job = null;
            }
            if (job == null) {
                // Reclaim completed captures belonging to other targets, but
                // never create another worker for an outstanding same HWND.
                var expired = new List<IntPtr>(); foreach (var pair in renderJobs) if (pair.Value.Completed.WaitOne(0)) expired.Add(pair.Key);
                foreach (IntPtr key in expired) { var previous = renderJobs[key]; if (previous.Image != null) previous.Image.Dispose(); previous.Completed.Dispose(); renderJobs.Remove(key); }
                if (renderJobs.Count >= 4) throw new InvalidOperationException("target_capture_busy");
                job = new RenderJob { Started = DateTime.UtcNow, Raw = raw, Visible = visible }; renderJobs[target.Hwnd] = job; RenderJob owned = job;
                Thread worker = new Thread(delegate() {
                    try {
                        using (Bitmap full = new Bitmap(raw.Width, raw.Height, PixelFormat.Format32bppRgb)) {
                            using (Graphics graphics = Graphics.FromImage(full)) { graphics.Clear(Color.Magenta); IntPtr dc = graphics.GetHdc(); bool rendered;
                                try { rendered = PrintWindow(target.Hwnd, dc, 2); } finally { graphics.ReleaseHdc(dc); }
                                if (!rendered) throw new InvalidOperationException("target_render_unavailable");
                            }
                            if (!target.Valid() || target.Bounds() != raw || target.CaptureBounds() != visible) throw new InvalidOperationException("target_changed");
                            owned.Image = ValidateRenderedFrame(full, raw, visible);
                        }
                    } catch (Exception error) { owned.Error = error; }
                    finally { owned.Completed.Set(); }
                }); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
            }
        }
        if (!job.Completed.WaitOne(120)) throw new InvalidOperationException("target_render_waiting");
        lock (renderLock) {
            if (job.Error != null) throw new InvalidOperationException(job.Error is InvalidOperationException ? job.Error.Message : "target_render_unavailable");
            if (job.Image == null || DateTime.UtcNow.Subtract(job.Started).TotalMilliseconds > 350 || target.Bounds() != raw || target.CaptureBounds() != visible) throw new InvalidOperationException("target_changed");
            return (Bitmap)job.Image.Clone();
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
    static Bitmap Capture(Target target, bool allowOcclusion) { bool rendered; return Capture(target, allowOcclusion, out rendered); }
    static Bitmap Capture(Target target, bool allowOcclusion, out bool rendered) {
        target.EnsureCaptureReady(); Rectangle r = target.CaptureBounds(); rendered = Covered(r, Occluders(target));
        if (rendered) { Bitmap isolated = RenderWindow(target, r); if (!target.Foreground() || target.CaptureBounds() != r) { isolated.Dispose(); throw new InvalidOperationException("target_changed"); } return isolated; }
        Bitmap b = new Bitmap(r.Width, r.Height, PixelFormat.Format32bppArgb);
        try { using (Graphics g = Graphics.FromImage(b)) g.CopyFromScreen(r.Location, Point.Empty, r.Size, CopyPixelOperation.SourceCopy);
            if (!target.Foreground() || target.CaptureBounds() != r) throw new InvalidOperationException("target_changed");
            if (Covered(r, Occluders(target))) throw new InvalidOperationException("target_occluded"); return b;
        } catch { b.Dispose(); throw; }
    }
    static string Encode(Bitmap b) { using (MemoryStream s = new MemoryStream()) { b.Save(s, ImageFormat.Png); if (s.Length > 524288) throw new InvalidOperationException("template_too_large"); return Convert.ToBase64String(s.ToArray()); } }
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
    static bool Detailed(Pixels p)
    {
        double sum = 0, sq = 0, edge = 0; int n = 0, edges = 0, step = Math.Max(1, Math.Min(p.W, p.H) / 64);
        for (int y = 0; y < p.H; y += step) for (int x = 0; x < p.W; x += step) {
            int i = y * p.Stride + x * 4; double v = (p.Data[i] + p.Data[i + 1] + p.Data[i + 2]) / 3.0;
            sum += v; sq += v * v; n++;
            if (x + step < p.W) { int j = i + step * 4; edge += Math.Abs(v - (p.Data[j] + p.Data[j + 1] + p.Data[j + 2]) / 3.0); edges++; }
        }
        return n > 0 && sq / n - (sum / n) * (sum / n) >= 36 && edges > 0 && edge / edges >= 1.2;
    }
    sealed class Candidate { internal int X, Y, W, H; internal double Score, Scale; }
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
            HashSet<long> sampledSizes = new HashSet<long>();
            // A different capture border can change the window ratio without
            // changing the control's pixels. Always preserve the original
            // source size; only equal raster dimensions are duplicate scales.
            foreach (double baseScale in new double[] { 1, ratio, .75, .85, 1.15, 1.25, 1.5, ratio * .9, ratio * 1.1 }) {
                double scale = baseScale * sourceScale;
                if (scale < .2 || scale > 16) continue;
                int width = (int)Math.Round(original.Width * scale), height = (int)Math.Round(original.Height * scale);
                if (width < 8 || height < 8 || width > image.W || height > image.H) continue;
                if (sampledSizes.Add(((long)width << 32) | (uint)height)) scales.Add(scale);
            }
            List<Candidate> best = new List<Candidate>(); Stopwatch elapsed = Stopwatch.StartNew();
            foreach (double scale in scales) {
                int w = (int)Math.Round(original.Width * scale), h = (int)Math.Round(original.Height * scale);
                if (w < 8 || h < 8 || w > image.W || h > image.H) continue;
                using (Bitmap resized = new Bitmap(w, h)) {
                    using (Graphics g = Graphics.FromImage(resized)) using (ImageAttributes attributes = new ImageAttributes()) { attributes.SetWrapMode(System.Drawing.Drawing2D.WrapMode.TileFlipXY); g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic; if (w == original.Width && h == original.Height) g.DrawImageUnscaled(original, 0, 0); else g.DrawImage(original, new Rectangle(0, 0, w, h), 0, 0, original.Width, original.Height, GraphicsUnit.Pixel, attributes); }
                    Pixels pattern = new Pixels(resized); List<Feature> features = Features(pattern); if (features.Count == 0) continue;
                    List<Candidate> coarse = new List<Candidate>();
                    // Check every possible origin: even a one-pixel offset can
                    // change the sparse text in a wide, mostly blank field.
                    for (int y = 0; y <= image.H - h; y++) {
                        if (elapsed.ElapsedMilliseconds > 15000) throw new InvalidOperationException("match_timeout");
                        for (int x = 0; x <= image.W - w; x++) {
                            double score = FeatureScore(image, pattern, features, x, y, minimum - .12);
                            if (score >= minimum - .12) AddBest(coarse, new Candidate { X = x, Y = y, W = w, H = h, Score = score, Scale = scale }, 128, true);
                        }
                    }
                    foreach (Candidate c in coarse) {
                        c.Score = Score(image, pattern, c.X, c.Y, false);
                        if (c.Score >= minimum - margin) AddBest(best, c, 8, true);
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
        Bitmap frame, crop; Rectangle selection, display; Point start, anchor; bool dragging, anchorChosen; DateTime captureAt, captureDeadline;
        internal Picker(Dictionary<string, object> data, string response) : base(data, response, 180) {
            target = new Target(data); Style(this, "이미지로 요소 선택", new Size(1050, 760)); MinimumSize = new Size(780, 580);
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 3, Padding = new Padding(20) };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 76)); layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 24));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 86)); layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 100));
            info.Name = info.AccessibleName = "CaptureStatus";
            info.Text = target.Label + "\n화면을 캡처한 다음, 찾을 버튼이나 그림의 테두리를 마우스로 드래그하세요.\n프로그램에는 클릭이나 입력을 보내지 않습니다."; info.Dock = DockStyle.Fill; layout.Controls.Add(info, 0, 0); layout.SetColumnSpan(info, 2);
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
                if (captureAt == DateTime.MinValue || DateTime.UtcNow < captureAt) return;
                try {
                    frame = Capture(target);
                    info.Text = "찾을 부분을 드래그한 뒤 오른쪽 미리보기에서 실제 클릭할 위치를 지정하세요.\n같은 버튼이 여러 개면 주변 이름까지 포함하면 구별하기 쉽습니다.";
                } catch (Exception e) {
                    // Only re-observe while Windows finishes restore/activation.
                    // The activation request is issued once; no application input.
                    bool settling = e.Message == "target_requires_foreground" || e.Message == "target_minimized" || e.Message == "target_changed" || e.Message == "target_occluded";
                    if (settling && DateTime.UtcNow < captureDeadline) { captureAt = DateTime.UtcNow.AddMilliseconds(80); return; }
                    info.Text = CaptureMessage(e.Message);
                }
                captureAt = DateTime.MinValue; capture.Enabled = true; Show(); Activate(); picture.Invalidate();
            };
            again.Click += delegate { selection = Rectangle.Empty; confirm.Enabled = false; preview.Image = null; picture.Invalidate(); };
            cancel.Click += delegate { Finish("cancelled", null); };
            confirm.Click += delegate {
                try { if (frame == null || !target.Valid() || target.CaptureBounds().Size != frame.Size) throw new InvalidOperationException("target_changed");
                    if (!anchorChosen) throw new InvalidOperationException("anchor_required");
                    var result = Template(frame, selection, anchor);
                    result["human_confirmed"] = true; result["pid"] = target.Pid; result["window_id"] = target.Hwnd.ToInt64();
                    result["selection"] = new Dictionary<string, object> { { "x", selection.X }, { "y", selection.Y }, { "width", selection.Width }, { "height", selection.Height } };
                    Finish("selected", result);
                } catch (Exception e) { info.Text = e.Message == "template_low_detail" ? "구별할 수 있는 글자나 아이콘을 조금 더 포함해 선택하세요." : e.Message == "selection_too_thin" ? "영역이 너무 가늘게 선택되었습니다. 위아래 또는 좌우 여백을 조금 더 포함해 주세요." : "선택한 크기 또는 창 상태가 바뀌었습니다. 다시 캡처해 주세요."; }
            };
            FormClosed += delegate { if (frame != null) frame.Dispose(); if (crop != null) crop.Dispose(); target.Release(); };
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
        void End(object s, MouseEventArgs e) { if (!dragging) return; DragSelection(s, e); dragging = false; picture.Capture = false; anchorChosen = false; confirm.Enabled = false; again.Enabled = true; preview.Image = null; if (crop != null) { crop.Dispose(); crop = null; } if (selection.Width >= 8 && selection.Height >= 8) { crop = frame.Clone(selection, PixelFormat.Format32bppArgb); preview.Image = crop; info.Text = "이제 오른쪽 미리보기에서 실제 클릭할 위치를 한 번 눌러주세요.\n같은 버튼이 여러 개면 주변 이름까지 포함해 선택하고 클릭 위치를 지정하세요."; } }
    }
    sealed class Recorder : SessionForm
    {
        sealed class Pending {
            internal Target Target; internal string Operation, RequestedOperation, Reason; internal Bitmap Crop; internal Point Anchor, ScreenPoint; internal Size FrameSize;
            internal int Wheel; internal string Key; internal string[] Keys; internal Dictionary<string, object> Baseline;
            internal Dictionary<string, object> Saved; internal int Index = -1;
            internal SemanticSample Semantic; internal IntPtr HitWindow; internal Rectangle HitBounds; internal uint HitThread; internal int HitControlId; internal string HitClass; internal DateTime ClickAt; internal bool HitEnabled, ProbeRequested;
        }
        sealed class SemanticSample { internal AutomationElement Element; internal Dictionary<string, object> Native; internal string Identity, Role, Property; internal object Value; internal bool Enabled; }
        readonly List<Target> targets = new List<Target>(); readonly List<Dictionary<string, object>> events = new List<Dictionary<string, object>>();
        readonly Queue<Pending> pending = new Queue<Pending>(); readonly List<string> warnings = new List<string>(); readonly int maximum;
        readonly string popupPrefix = "popup_" + Guid.NewGuid().ToString("N").Substring(0, 8) + "_";
        readonly Label info = new Label(); readonly ListBox list = new ListBox(); readonly Button start = Button("기록 시작"), pause = Button("일시정지"), stop = Button("기록 마치고 검토");
        Hook mouseProc, keyProc; IntPtr mouseHook, keyHook; bool recording, textBusy, outside; int generation, injected, sampleVersion, activeWorkers;
        readonly Dictionary<Target, DateTime> sampleRetryAt = new Dictionary<Target, DateTime>();
        readonly Dictionary<Target, int> sampleWorkers = new Dictionary<Target, int>(); Target samplingTarget;
        bool compact; TableLayoutPanel recorderLayout;
        string recordState = "checking", hookProbe = "checking", uiaProbe = "checking"; DateTime heartbeatAt, probeAt;
        volatile bool probeDone, probeGood; bool probeSettled;
        DateTime frameAt, dirtyAt, lastSample, workerStarted, typingConfirmedAt; Target frameTarget; Rectangle frameBounds; Bitmap frame; bool frameRendered; List<Rectangle> frameOccluders = new List<Rectangle>();
        Dictionary<string, object> lastImage, focusedState; Target lastImageTarget, focusedTarget; DateTime focusedAt; Point lastImagePoint;
        Pending mouseDown, typing, lastPointer; readonly List<Pending> clickEvidence = new List<Pending>(); List<KeyValuePair<Pending, SemanticSample>> completedClicks; Point mouseStart; readonly object stateLock = new object(); Dictionary<string, object> completedState; Target completedTarget; int completedGeneration; bool workerCompleted;
        SemanticSample hovered, completedHover, tracked, completedTracked; Target hoverTarget, trackedTarget; DateTime hoverAt, trackedAt; Dictionary<string, object> trackedEvent; int trackedIndex; bool trackedKeyboard;
        internal Recorder(Dictionary<string, object> data, string response) : base(data, response, 1800) {
            maximum = Num(data, "max_events", 15); if (maximum < 1 || maximum > 15) throw new InvalidOperationException("invalid_event_limit");
            object values; if (!data.TryGetValue("targets", out values) || !(values is IEnumerable)) throw new InvalidOperationException("targets_required");
            foreach (object item in (IEnumerable)values) { if (targets.Count >= 10) throw new InvalidOperationException("too_many_targets"); targets.Add(new Target(item as Dictionary<string, object>)); } if (targets.Count == 0) throw new InvalidOperationException("targets_required");
            Style(this, "동작 기록 — 선택한 프로그램만 기록", new Size(650, 390)); MinimumSize = new Size(540, 210); TopMost = true;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, Padding = new Padding(16) };
            recorderLayout = layout;
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 118)); layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 70));
            info.Dock = DockStyle.Fill; info.Text = "기록 중 클릭과 입력은 실제 프로그램에 적용됩니다.\n같은 프로그램이 연 팝업도 함께 기록합니다.\n암호와 알 수 없는 입력칸의 글자는 저장하지 않습니다.\n관련 없는 창에서는 자동으로 멈춥니다. Esc: 기록 일시정지";
            list.Dock = DockStyle.Fill; var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill }; Button cancel = Button("취소"), tuck = Button("접어두기");
            start.Width = pause.Width = cancel.Width = tuck.Width = 92; stop.Width = 174;
            tuck.Click += delegate { WindowState = FormWindowState.Minimized; };
            buttons.Controls.Add(start); buttons.Controls.Add(pause); buttons.Controls.Add(stop); buttons.Controls.Add(tuck); buttons.Controls.Add(cancel);
            layout.Controls.Add(info, 0, 0); layout.Controls.Add(list, 0, 1); layout.Controls.Add(buttons, 0, 2); Controls.Add(layout);
            start.Enabled = pause.Enabled = stop.Enabled = false;
            start.Click += delegate { try { InstallHooks(); recording = true; generation++; outside = false; recordState = "recording"; start.Enabled = false; pause.Enabled = true; stop.Enabled = true; Compact(true); info.Text = ActiveText(); Heartbeat(true); } catch { Failure("recording_hook_unavailable"); } };
            pause.Click += delegate { Drain(); Pause(); };
            stop.Click += delegate { CompleteRecording(); };
            cancel.Click += delegate { Finish("cancelled", null); };
            Clock.Tick += Tick; FormClosed += delegate { UninstallHooks(); if (frame != null) frame.Dispose(); while (pending.Count > 0) { Pending p = pending.Dequeue(); if (p.Crop != null) p.Crop.Dispose(); } foreach (Target t in targets) t.Release(); focusedState = null; };
            Shown += delegate {
                PlaceToolbar();
                StartProbe();
            };
        }
        void Compact(bool value) {
            if (compact == value) return; compact = value; list.Visible = !value;
            recorderLayout.RowStyles[0].Height = value ? 86 : 118;
            recorderLayout.RowStyles[1].SizeType = value ? SizeType.Absolute : SizeType.Percent;
            recorderLayout.RowStyles[1].Height = value ? 0 : 100;
            float dpi; using (Graphics graphics = CreateGraphics()) dpi = graphics.DpiY / 96f;
            ClientSize = new Size(ClientSize.Width, (int)Math.Round((value ? 205 : 390) * dpi)); PlaceToolbar();
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
            Drain(); Pause(); recordState = "review"; Heartbeat(true);
            var windows = new List<Dictionary<string, object>>();
            foreach (Target t in targets) if (t.Owner != null) windows.Add(new Dictionary<string, object> {
                { "program_id", t.Program }, { "window_ref", t.Window }, { "owner_ref", t.Owner.Window }, { "pid", t.Pid }, { "window_id", t.Hwnd.ToInt64() },
                { "owner_window_id", t.Owner.Hwnd.ToInt64() }, { "title", t.Title }, { "class_name", t.ClassName }, { "owner_verified", true },
                { "closed", !t.Valid() || !IsWindowVisible(t.Hwnd) } });
            Finish("recorded", new Dictionary<string, object> { { "events", events }, { "windows", windows }, { "human_confirmed", true }, { "review_required", true }, { "warnings", warnings }, { "partial", warnings.Count > 0 }, { "injected_events", injected } });
        }
        void FinalizePendingInput() {
            if (typing != null && typing.Saved != null && dirtyAt > typingConfirmedAt && typing.Reason != "protected_input" && typing.Reason != "recording_method_unverified") {
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
            if (events.Count > 0) value["last_event"] = new Dictionary<string, object> { { "operation", Str(events[events.Count - 1], "operation", "") }, { "recognition", events[events.Count - 1].ContainsKey("native_target") ? "uia_candidate" : "image_or_manual" } };
            try { Write(Response + ".progress.json", value); heartbeatAt = DateTime.UtcNow; } catch { recording = false; recordState = "failed"; Warn("recording_progress_failed"); info.Text = "기록 상태를 전달하지 못해 중지했습니다. 지금까지의 내용을 검토하세요."; start.Enabled = pause.Enabled = false; stop.Enabled = events.Count > 0; }
        }
        string ActiveText() { return "● 기록 중 · 클릭과 입력이 실제 프로그램에 적용됩니다.\n요소로 기록할 수 없으면 이미지로 기록합니다. 가려진 대상은 보완이 필요합니다.\nEsc: 일시정지 · 마친 뒤 동작과 입력값을 검토하세요."; }
        void Pause() { recording = false; generation++; Compact(false); FinalizePendingInput(); tracked = hovered = null; trackedEvent = null; trackedTarget = hoverTarget = null; typing = null; lastPointer = null; focusedState = null; lastImage = null; lastImageTarget = null; recordState = "paused"; start.Enabled = probeSettled && hookProbe == "available" && events.Count < maximum; pause.Enabled = false; info.Text = "일시정지 · " + events.Count + "/" + maximum + "개 동작을 기록했습니다.\n다시 시작할 때 입력 대상을 클릭하거나 기록을 마치고 검토하세요."; if (frame != null) { frame.Dispose(); frame = null; } Heartbeat(true); }
        void InstallHooks() { if (mouseHook != IntPtr.Zero) return; mouseProc = MouseHook; keyProc = KeyHook; IntPtr module = GetModuleHandle(null); mouseHook = SetWindowsHookEx(14, mouseProc, module, 0); keyHook = SetWindowsHookEx(13, keyProc, module, 0); if (mouseHook == IntPtr.Zero || keyHook == IntPtr.Zero) { UninstallHooks(); throw new InvalidOperationException("recording_hook_unavailable"); } }
        void UninstallHooks() { recording = false; if (mouseHook != IntPtr.Zero) UnhookWindowsHookEx(mouseHook); if (keyHook != IntPtr.Zero) UnhookWindowsHookEx(keyHook); mouseHook = keyHook = IntPtr.Zero; }
        Target Current() {
            // A native dropdown can hide and later reuse the same HWND. Each
            // visibility episode gets a distinct recipe binding; otherwise a
            // first selection would try to review an already hidden window.
            RetireHiddenPopups();
            POINT cursor; if (GetCursorPos(out cursor)) foreach (Target t in targets) if (t.Contains(cursor) && t.Foreground()) return t;
            foreach (Target t in targets) if (t.Hwnd == GetForegroundWindow() && t.Foreground()) return t; return null;
        }
        void RetireHiddenPopups() { foreach (Target t in targets) if (t.Owner != null && !IsWindowVisible(t.Hwnd)) t.Retired = true; }
        void DiscoverPopup() {
            // Native metadata only, outside low-level input hooks. Never scan
            // another process's UIA tree or infer trust from PID alone.
            RetireHiddenPopups();
            foreach (Target t in targets) if (t.Owner != null && !t.Valid()) t.Retired = true;
            POINT cursor; IntPtr candidate = GetForegroundWindow();
            if (GetCursorPos(out cursor)) { IntPtr at = GetAncestor(WindowFromPoint(cursor), 2); uint atPid;
                if (at != IntPtr.Zero && GetWindowThreadProcessId(at, out atPid) != 0)
                    foreach (Target root in targets) if (root.Owner == null && atPid == (uint)root.Pid && at != root.Hwnd && RelatedTo(at, root.Hwnd, root.Pid)) { candidate = at; break; }
            }
            foreach (Target known in targets) if (known.Hwnd == candidate && !known.Retired) return;
            foreach (Target root in targets.ToArray()) {
                if (root.Owner != null || !root.Valid() || candidate == root.Hwnd || !IsWindowVisible(candidate) || !RelatedTo(candidate, root.Hwnd, root.Pid)) continue;
                string title = WindowText(candidate), cls = WindowClass(candidate); if (cls.Length == 0 || cls == "IME" || cls == "MSCTFIME UI") return;
                int matches = 0; EnumWindows(delegate(IntPtr hwnd, IntPtr ignored) {
                    if (IsWindowVisible(hwnd) && WindowText(hwnd) == title && WindowClass(hwnd) == cls && RelatedTo(hwnd, root.Hwnd, root.Pid)) matches++;
                    return true;
                }, IntPtr.Zero);
                if (matches != 1) { Warn("recording_window_ambiguous"); return; }
                int count = 0; foreach (Target t in targets) if (t.Owner != null) count++;
                if (count >= 5) { Warn("recording_window_limit"); return; }
                string reference = popupPrefix + (count + 1).ToString(); bool used;
                do { used = false; foreach (Target t in targets) if (t.Program == root.Program && t.Window == reference) { used = true; reference = popupPrefix + (++count + 1).ToString(); break; } } while (used);
                targets.Add(new Target(root, candidate, reference)); return;
            }
        }
        Dictionary<string, object> Event(Target t, string operation) { return new Dictionary<string, object> { { "operation", operation }, { "program_id", t.Program }, { "window_ref", t.Window }, { "verification", "manual_required" } }; }
        void Queue(Pending p) {
            if (events.Count + pending.Count >= maximum) { if (p.Crop != null) p.Crop.Dispose(); Warn("recording_event_limit"); recording = false; BeginInvoke((Action)delegate { Drain(); Pause(); info.Text = "기록 가능한 " + maximum.ToString() + "단계에 도달했습니다. 이후 동작은 기록하지 않습니다. [기록 마치고 검토]를 눌러 확인하세요."; }); return; }
            pending.Enqueue(p);
        }
        bool FocusMatches(Target target) {
            if (lastPointer == null || lastPointer.Target != target || focusedTarget != target || focusedState == null || DateTime.UtcNow.Subtract(focusedAt).TotalMilliseconds > 1200) return false;
            if (Str(focusedState, "protected", "False") == "True") return false;
            return new Rectangle(Num(focusedState, "x", 0), Num(focusedState, "y", 0), Num(focusedState, "width", 0), Num(focusedState, "height", 0)).Contains(lastPointer.ScreenPoint);
        }
        // Hook callbacks copy only a small cached crop. PNG encoding, UIA,
        // matching and file writes run outside the low-level input callback.
        Pending CaptureClick(Target target, POINT point, string operation) {
            var item = new Pending { Target = target, Operation = operation, RequestedOperation = operation, ScreenPoint = new Point(point.X, point.Y), ClickAt = DateTime.UtcNow };
            IntPtr hit = WindowFromPoint(point); RECT hitBounds; uint hitPid;
            if (operation == "click" && hit != target.Hwnd && GetAncestor(hit, 2) == target.Hwnd && GetWindowRect(hit, out hitBounds)) {
                uint thread = GetWindowThreadProcessId(hit, out hitPid);
                if (thread != 0 && hitPid == (uint)target.Pid) { item.HitWindow = hit; item.HitThread = thread; item.HitEnabled = IsWindowEnabled(hit) && IsWindowEnabled(target.Hwnd); item.HitClass = WindowClass(hit); item.HitControlId = GetDlgCtrlID(hit); item.HitBounds = Rectangle.FromLTRB(hitBounds.Left, hitBounds.Top, hitBounds.Right, hitBounds.Bottom); }
            }
            if (operation == "click" && hovered != null && hoverTarget == target && DateTime.UtcNow.Subtract(hoverAt).TotalMilliseconds <= 600) {
                var bounds = Map(hovered.Native, "bounds");
                if (bounds != null && new Rectangle((int)Real(bounds, "x", 0), (int)Real(bounds, "y", 0), (int)Real(bounds, "width", 0), (int)Real(bounds, "height", 0)).Contains(point.X, point.Y)) item.Semantic = hovered;
            }
            if (!target.ImageAllowed) { if (item.Semantic == null) item.Operation = "manual_entry"; item.Reason = "image_replay_unavailable"; return item; }
            if (frame == null || frameTarget != target || DateTime.UtcNow.Subtract(frameAt).TotalMilliseconds > 250 || frameBounds != target.CaptureBounds() || !target.Contains(point)) { item.Operation = "manual_entry"; item.Reason = "image_capture_required"; return item; }
            int x = point.X - frameBounds.X, y = point.Y - frameBounds.Y, w = Math.Min(160, frame.Width), h = Math.Min(96, frame.Height);
            Rectangle r = new Rectangle(Math.Max(0, Math.Min(frame.Width - w, x - w / 2)), Math.Max(0, Math.Min(frame.Height - h, y - h / 2)), w, h);
            Rectangle desktopRegion = new Rectangle(frameBounds.X + r.X, frameBounds.Y + r.Y, r.Width, r.Height);
            List<Rectangle> currentOccluders = Occluders(target);
            if (!frameRendered && (Covered(desktopRegion, frameOccluders) || Covered(desktopRegion, currentOccluders))) {
                item.Operation = "manual_entry"; item.Reason = "image_occluded_requires_selection"; return item;
            }
            item.Crop = frame.Clone(r, PixelFormat.Format32bppArgb); item.Anchor = new Point(x - r.X, y - r.Y); item.FrameSize = frame.Size; return item;
        }
        IntPtr MouseHook(int code, IntPtr message, IntPtr data) {
            try {
                if (code >= 0 && recording) {
                    MOUSE mouse = (MOUSE)Marshal.PtrToStructure(data, typeof(MOUSE)); int kind = message.ToInt32();
                    if (kind == 0x201 || kind == 0x204 || kind == 0x20A) {
                        Target target = Current(); if (target != null && target.Contains(mouse.point)) {
                            if ((mouse.flags & 1) != 0) injected++; typing = null;
                            Pending item = CaptureClick(target, mouse.point, kind == 0x204 ? "right_click" : kind == 0x20A ? "scroll" : "click");
                            item.Wheel = (short)(mouse.data >> 16); if (kind != 0x20A) { lastPointer = item; clickEvidence.Add(item); } Queue(item);
                            if (kind != 0x20A) { mouseDown = item; mouseStart = new Point(mouse.point.X, mouse.point.Y); }
                        }
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
                if (code >= 0 && recording && (message.ToInt32() == 0x100 || message.ToInt32() == 0x104)) {
                    KEY key = (KEY)Marshal.PtrToStructure(data, typeof(KEY)); Target target = Current(); if (target != null) {
                        if ((key.flags & 16) != 0) injected++; Keys value = (Keys)key.code;
                        if (value == Keys.Escape) BeginInvoke((Action)delegate { WindowState = FormWindowState.Normal; Drain(); Pause(); });
                        else if (value != Keys.ControlKey && value != Keys.ShiftKey && value != Keys.Menu && value != Keys.LWin && value != Keys.RWin && value != Keys.LControlKey && value != Keys.RControlKey && value != Keys.LShiftKey && value != Keys.RShiftKey && value != Keys.LMenu && value != Keys.RMenu) {
                            // A final ComboBox value does not prove how its child Edit
                            // committed it. Never silently replace keyboard+commit with
                            // a different generic selection strategy.
                            if (tracked != null && trackedTarget == target && tracked.Role == "ComboBox") trackedKeyboard = true;
                            bool ctrl = (GetAsyncKeyState(0x11) & 0x8000) != 0, alt = (GetAsyncKeyState(0x12) & 0x8000) != 0, shift = (GetAsyncKeyState(0x10) & 0x8000) != 0;
                            if (ctrl && !alt && !shift && (value == Keys.A || value == Keys.Z || value == Keys.Y)) { typing = null; bool valid = FocusMatches(target); Queue(new Pending { Target = target, Operation = valid ? "hotkey" : "manual_entry", Reason = valid ? null : "focus_target_requires_selection", Keys = new string[] { "CTRL", value.ToString().ToUpperInvariant() } }); }
                            else if (!ctrl && !alt && !shift && (value == Keys.Enter || value == Keys.Tab || value == Keys.Up || value == Keys.Down || value == Keys.Left || value == Keys.Right || value == Keys.Home || value == Keys.End || value == Keys.PageDown || value == Keys.PageUp)) { typing = null; bool valid = FocusMatches(target); Queue(new Pending { Target = target, Operation = valid ? "press_key" : "manual_entry", Reason = valid ? null : "focus_target_requires_selection", Key = value.ToString().ToUpperInvariant() }); }
                            else {
                                if (typing == null || typing.Target != target) {
                                    Dictionary<string, object> baseline = FocusMatches(target) ? focusedState : null;
                                    bool protectedField = focusedTarget == target && focusedState != null && Str(focusedState, "protected", "False") == "True" && DateTime.UtcNow.Subtract(focusedAt).TotalMilliseconds < 1200;
                                    typing = new Pending { Target = target, Operation = "manual_entry", Reason = protectedField ? "protected_input" : (ctrl || alt) && !(ctrl && !alt && !shift && value == Keys.V && baseline != null) ? "shortcut_requires_manual_setup" : "unknown_input", Baseline = baseline }; Queue(typing);
                                } dirtyAt = DateTime.UtcNow;
                            }
                        }
                    }
                }
            } catch { recording = false; BeginInvoke((Action)delegate { Failure("recording_keyboard_failed"); }); }
            return CallNextHookEx(keyHook, code, message, data);
        }
        DateTime lastClickTime; Dictionary<string, object> lastClickEvent; int lastClickIndex; Target lastClickTarget; Point lastClickAnchor;
        void Drain() {
            while (pending.Count > 0) {
                Pending p = pending.Dequeue(); var item = Event(p.Target, p.Operation); string label = p.Operation;
                // Each real pointer action needs its own captured pixels. An
                // absent new crop must never inherit a previous button image.
                if (p.RequestedOperation != null) { lastImage = null; lastImageTarget = null; }
                if (p.Crop != null) {
                    using (Bitmap image = p.Crop) {
                        if (Detailed(new Pixels(image))) {
                            lastImage = new Dictionary<string, object> { { "template_png", Encode(image) }, { "width", image.Width }, { "height", image.Height },
                                { "anchor", new Dictionary<string, object> { { "x", (double)p.Anchor.X / image.Width }, { "y", (double)p.Anchor.Y / image.Height } } },
                                { "capture_window", new Dictionary<string, object> { { "width", p.FrameSize.Width }, { "height", p.FrameSize.Height } } } }; lastImageTarget = p.Target; lastImagePoint = p.ScreenPoint;
                            foreach (var pair in lastImage) item[pair.Key] = pair.Value;
                        } else { item["operation"] = "manual_entry"; item["reason"] = "template_low_detail"; }
                    } p.Crop = null;
                } else if (p.RequestedOperation == null && (p.Key != null || p.Keys != null || p.Baseline != null) && lastImage != null && lastImageTarget == p.Target) foreach (var pair in lastImage) item[pair.Key] = pair.Value;
                else item["reason"] = "image_capture_required";
                if (p.Reason != null) item["reason"] = p.Reason;
                // Preserve the original action for an in-place target repair.
                if (p.Operation == "manual_entry" && p.Key == null && p.Keys == null && p.Baseline == null && p.ScreenPoint != Point.Empty)
                    item["requested_operation"] = p.Reason == "drag_requires_manual_setup" ? "manual_entry" : p.RequestedOperation;
                if (p.Semantic != null && p.RequestedOperation == "click" && p.Semantic.Role == "Button"
                        && (p.Operation == "click" || p.Operation == "manual_entry" && (p.Reason == "image_capture_required" || p.Reason == "image_occluded_requires_selection"))) {
                    item["operation"] = "click"; item["native_target"] = p.Semantic.Native;
                    item.Remove("reason"); label = "요소 클릭 · 저장 전 Driver 대조";
                }
                if (p.Reason == "focus_target_requires_selection") foreach (string key in new string[] { "template_png", "width", "height", "anchor", "capture_window", "source_size" }) item.Remove(key);
                if (p.Operation == "scroll") { item["wheel"] = p.Wheel; item["direction"] = p.Wheel < 0 ? "down" : "up"; item["amount"] = Math.Max(1, Math.Abs(p.Wheel) / 120); label = "스크롤"; }
                if (p.Key != null) item["key"] = p.Key; if (p.Keys != null) item["keys"] = p.Keys;
                if (Str(item, "operation", "") == "click" && lastClickEvent != null && lastClickTarget == p.Target && lastClickIndex == events.Count - 1 && DateTime.UtcNow.Subtract(lastClickTime).TotalMilliseconds <= SystemInformation.DoubleClickTime && Math.Abs(p.ScreenPoint.X - lastClickAnchor.X) <= SystemInformation.DoubleClickSize.Width && Math.Abs(p.ScreenPoint.Y - lastClickAnchor.Y) <= SystemInformation.DoubleClickSize.Height) {
                    lastClickEvent["operation"] = "double_click"; lastClickEvent.Remove("native_target"); list.Items[lastClickIndex] = (lastClickIndex + 1).ToString() + "  두 번 클릭"; lastClickEvent = null; continue;
                }
                if (Str(item, "operation", "") == "manual_entry") label = "직접 설정 필요 · " + Str(item, "reason", "unknown_input");
                else if (p.Operation == "click") label = "클릭"; else if (p.Operation == "right_click") label = "오른쪽 클릭"; else if (p.Operation == "press_key") label = "키 " + p.Key; else if (p.Operation == "hotkey") label = "단축키 " + String.Join("+", p.Keys);
                if (item.ContainsKey("native_target")) label = "클릭 · 요소 후보 (저장 전 확인)";
                p.Saved = item; p.Index = events.Count; events.Add(item); list.Items.Add((events.Count).ToString() + "  " + label); list.TopIndex = Math.Max(0, list.Items.Count - 1);
                if (p.Semantic != null && p.RequestedOperation == "click" && (p.Operation == "click" || p.Reason == "image_capture_required" || p.Reason == "image_occluded_requires_selection")) { tracked = p.Semantic; trackedTarget = p.Target; trackedEvent = item; trackedIndex = p.Index; trackedAt = DateTime.UtcNow; trackedKeyboard = false; }
                if (trackedKeyboard && trackedTarget == p.Target && (p.Key != null || p.Keys != null || p.Operation == "manual_entry")) {
                    item["operation"] = "manual_entry"; item["reason"] = "recording_method_unverified"; item.Remove("key"); item.Remove("keys"); item.Remove("value"); p.Reason = "recording_method_unverified";
                    list.Items[p.Index] = (p.Index + 1).ToString() + "  콤보 입력·확정 방법 · 직접 설정 필요";
                }
                if (Str(item, "operation", "") == "click") { lastClickEvent = item; lastClickIndex = events.Count - 1; lastClickTarget = p.Target; lastClickTime = DateTime.UtcNow; lastClickAnchor = p.ScreenPoint; }
                // Replaying a key begins with focusing its image target. Once
                // a key can move focus, that old image is no longer a valid
                // target for subsequent input. Require another human selection.
                if (p.Key != null || (p.Keys != null && (p.Keys.Length != 2 || p.Keys[1] != "A"))) { lastImage = null; lastImageTarget = null; focusedState = null; }
            }
            if (events.Count >= maximum && recording) { Warn("recording_event_limit"); Pause(); info.Text = "기록 가능한 " + maximum.ToString() + "단계에 도달했습니다. 이후 동작은 기록하지 않습니다. [기록 마치고 검토]를 눌러 확인하세요."; }
        }
        void Sample(Target target) {
            textBusy = true; workerStarted = DateTime.UtcNow; samplingTarget = target; int version = generation, sample = ++sampleVersion;
            System.Threading.Interlocked.Increment(ref activeWorkers);
            lock (stateLock) { sampleWorkers[target] = 1; }
            SemanticSample observeTracked = trackedTarget == target ? tracked : null;
            var observePointers = new List<Pending>();
            foreach (Pending pointer in clickEvidence) if (!pointer.ProbeRequested && pointer.Semantic == null && pointer.Saved != null && pointer.HitWindow != IntPtr.Zero && DateTime.UtcNow.Subtract(pointer.ClickAt).TotalMilliseconds <= 1200) {
                pointer.ProbeRequested = true; observePointers.Add(pointer); if (observePointers.Count >= 4) break;
            }
            Thread worker = new Thread(delegate() {
                Dictionary<string, object> state = null;
                SemanticSample hover = null, changed = null; var clicks = new List<KeyValuePair<Pending, SemanticSample>>();
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
                        POINT cursor; if (GetCursorPos(out cursor) && target.Contains(cursor)) hover = ReadSemantic(AutomationElement.FromPoint(new System.Windows.Point(cursor.X, cursor.Y)), target, cursor, true);
                        if (observeTracked != null) { var pt = Map(observeTracked.Native, "point"); changed = ReadSemantic(observeTracked.Element, target, new POINT { X = (int)Real(pt, "x", 0), Y = (int)Real(pt, "y", 0) }, false); }
                        AutomationElement e = AutomationElement.FocusedElement;
                        if (e != null && e.Current.ProcessId == target.Pid && ProtectedOrUnowned(e, target)) state = new Dictionary<string, object> { { "protected", true } };
                        else if (e != null && e.Current.ProcessId == target.Pid && e.Current.ControlType == ControlType.Edit && e.Current.IsEnabled) {
                            object raw;
                            if (e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) { ValuePattern p = (ValuePattern)raw;
                                if (!p.Current.IsReadOnly && target.Foreground()) { string value = p.Current.Value; System.Windows.Rect bounds = e.Current.BoundingRectangle; if (value.Length <= 2000 && !bounds.IsEmpty) state = new Dictionary<string, object> { { "value", value }, { "identity", String.Join(",", Array.ConvertAll(e.GetRuntimeId(), delegate(int n) { return n.ToString(); })) }, { "x", (int)bounds.X }, { "y", (int)bounds.Y }, { "width", (int)bounds.Width }, { "height", (int)bounds.Height } }; }
                            }
                        }
                    }
                } catch { }
                lock (stateLock) { sampleWorkers.Remove(target); if (sample == sampleVersion) { completedState = state; completedHover = hover; completedTracked = changed; completedClicks = clicks; completedTarget = target; completedGeneration = version; workerCompleted = true; } }
                System.Threading.Interlocked.Decrement(ref activeWorkers);
            }); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }
        bool ClickedChildStillMatches(Pending pointer) {
            uint pid; RECT bounds;
            return pointer.Target.Valid() && pointer.RequestedOperation == "click" && pointer.HitWindow != IntPtr.Zero && pointer.HitEnabled && pointer.Reason != "drag_requires_manual_setup"
                && DateTime.UtcNow.Subtract(pointer.ClickAt).TotalMilliseconds <= 1200
                && GetWindowThreadProcessId(pointer.HitWindow, out pid) == pointer.HitThread && pid == (uint)pointer.Target.Pid
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
                || pointer != lastPointer && semantic.Role != "Button") return;
            pointer.Semantic = semantic;
            if (pointer == lastPointer && pointer.Target.Foreground()) { tracked = semantic; trackedTarget = pointer.Target; trackedEvent = pointer.Saved; trackedIndex = pointer.Index; trackedAt = DateTime.UtcNow; trackedKeyboard = false; }
            if (semantic.Role == "Button") { pointer.Saved["operation"] = "click"; pointer.Saved["native_target"] = semantic.Native; pointer.Saved.Remove("reason"); list.Items[pointer.Index] = (pointer.Index + 1).ToString() + "  버튼 클릭 · 저장 전 Driver 대조 필요"; }
        }
        SemanticSample ReadSemantic(AutomationElement leaf, Target target, POINT point, bool normalizeComposite, bool historicalButton = false) {
            if (leaf == null || leaf.Current.ProcessId != target.Pid || leaf.Current.IsPassword) return null;
            var chain = new List<AutomationElement>(); AutomationElement next = leaf; bool owned = false;
            for (int i = 0; i < 17 && next != null; i++) {
                if (next.Current.ProcessId != target.Pid || next.Current.IsPassword) return null;
                chain.Add(next);
                if (next.Current.NativeWindowHandle == target.Hwnd.ToInt64()) { owned = true; break; }
                next = TreeWalker.ControlViewWalker.GetParent(next);
            }
            if (!owned) return null;
            int chosen = 0;
            if (normalizeComposite) {
                // A ComboBox often exposes an Edit/Text child at the click.
                // It is its owning ComboBox's final selection we must observe.
                for (int i = 0; i < Math.Min(5, chain.Count); i++) if (chain[i].Current.ControlType == ControlType.ComboBox) { chosen = i; break; }
                if (chosen == 0 && leaf.Current.ControlType == ControlType.Text)
                    for (int i = 1; i < Math.Min(4, chain.Count); i++) if (chain[i].Current.ControlType == ControlType.Button || chain[i].Current.ControlType == ControlType.CheckBox) { chosen = i; break; }
            }
            AutomationElement e = chain[chosen]; string role = e.Current.ControlType.ProgrammaticName.Replace("ControlType.", "");
            if (role != "Edit" && role != "ComboBox" && role != "CheckBox" && role != "Button") return null;
            System.Windows.Rect b = e.Current.BoundingRectangle;
            if (b.IsEmpty || !b.Contains(point.X, point.Y) || !e.Current.IsEnabled && !(historicalButton && role == "Button")) return null;
            var ancestors = new List<Dictionary<string, object>>();
            for (int i = chosen + 1; i < chain.Count; i++) ancestors.Add(SemanticIdentity(chain[i]));
            Rectangle window = target.Bounds();
            var native = new Dictionary<string, object> { { "status", "selected" }, { "pid", target.Pid }, { "window_id", target.Hwnd.ToInt64() }, { "element", SemanticIdentity(e) }, { "ancestors", ancestors },
                { "point", new Dictionary<string, object> { { "x", point.X }, { "y", point.Y } } },
                { "bounds", new Dictionary<string, object> { { "x", b.X }, { "y", b.Y }, { "width", b.Width }, { "height", b.Height } } },
                { "window_bounds", new Dictionary<string, object> { { "x", window.X }, { "y", window.Y }, { "width", window.Width }, { "height", window.Height } } } };
            var sample = new SemanticSample { Element = e, Native = native, Role = role, Enabled = e.Current.IsEnabled, Identity = String.Join(",", Array.ConvertAll(e.GetRuntimeId(), delegate(int n) { return n.ToString(); })) };
            object raw;
            if (e.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) {
                ToggleState state = ((TogglePattern)raw).Current.ToggleState;
                if (state != ToggleState.Indeterminate) { sample.Property = "selected"; sample.Value = state == ToggleState.On; }
            } else if (role == "ComboBox") {
                if (e.TryGetCurrentPattern(SelectionPattern.Pattern, out raw)) {
                    AutomationElement[] selection = ((SelectionPattern)raw).Current.GetSelection();
                    if (selection.Length == 1 && selection[0].Current.ProcessId == target.Pid && !selection[0].Current.IsPassword) { string value = selection[0].Current.Name; if (!String.IsNullOrEmpty(value) && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; } }
                }
                if (sample.Property == null && e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) {
                    string value = ((ValuePattern)raw).Current.Value; if (value != null && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; }
                }
            } else if (role == "Edit" && e.TryGetCurrentPattern(ValuePattern.Pattern, out raw) && !((ValuePattern)raw).Current.IsReadOnly) {
                string value = ((ValuePattern)raw).Current.Value; if (value != null && value.Length <= 2000) { sample.Property = "value"; sample.Value = value; }
            }
            return sample;
        }
        bool ProtectedOrUnowned(AutomationElement element, Target target) {
            for (int level = 0; level < 17 && element != null; level++) {
                if (element.Current.ProcessId != target.Pid || element.Current.IsPassword) return true;
                if (element.Current.NativeWindowHandle == target.Hwnd.ToInt64()) return false;
                element = TreeWalker.ControlViewWalker.GetParent(element);
            }
            return true;
        }
        Dictionary<string, object> SemanticIdentity(AutomationElement element) {
            if (element.Current.IsPassword) throw new InvalidOperationException("protected_input");
            string name = element.Current.Name ?? "", id = element.Current.AutomationId ?? "";
            if (name.Length > 1000 || id.Length > 1000) throw new InvalidOperationException("identity_too_large");
            return new Dictionary<string, object> { { "role", element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") }, { "name", name }, { "automation_id", id }, { "is_password", false } };
        }
        void Tick(object sender, EventArgs args) {
            if (Done) return;
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
            if (current == null) { if (GetForegroundWindow() != Handle) Warn("outside_target_not_recorded"); if (!outside) { outside = true; recordState = "outside_target"; info.Text = "연결 범위 밖이라 기록 일시정지\n원래 프로그램이나 관련 팝업으로 돌아오면 계속합니다.\n다른 프로그램·관련 없는 창은 기록되지 않습니다."; Heartbeat(true); } if (frame != null) { frame.Dispose(); frame = null; } }
            else {
                if (outside) { outside = false; recordState = "recording"; info.Text = ActiveText(); Heartbeat(true); }
                try { List<Rectangle> occluders = Occluders(current); Rectangle capturedBounds = current.CaptureBounds(); bool rendered; Bitmap next = Capture(current, true, out rendered); occluders.AddRange(Occluders(current)); if (current.CaptureBounds() != capturedBounds) { next.Dispose(); throw new InvalidOperationException("target_changed"); } if (frame != null) frame.Dispose(); frame = next; frameRendered = rendered; frameBounds = capturedBounds; frameTarget = current; frameOccluders = occluders; frameAt = DateTime.UtcNow; } catch { if (frame != null) { frame.Dispose(); frame = null; } }
            }
            lock (stateLock) {
                if (workerCompleted) {
                    workerCompleted = false; textBusy = false;
                    if (completedGeneration == generation && completedClicks != null) foreach (var click in completedClicks) AttachClickedSemantic(click.Key, click.Value);
                    if (completedGeneration == generation && completedTarget == current) {
                        hovered = completedHover; hoverTarget = current; hoverAt = DateTime.UtcNow;
                        if (tracked != null && !trackedKeyboard && trackedTarget == current && trackedEvent != null && completedTracked != null && tracked.Identity == completedTracked.Identity
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
                        focusedState = completedState; focusedTarget = completedTarget; focusedAt = DateTime.UtcNow;
                        if (typing != null && typing.Target == current && typing.Saved != null && completedState != null && Str(completedState, "protected", "False") == "True") {
                            typing.Reason = "protected_input"; typing.Baseline = null; typing.Saved["operation"] = "manual_entry"; typing.Saved["reason"] = "protected_input"; typing.Saved.Remove("value"); list.Items[typing.Index] = (typing.Index + 1).ToString() + "  암호 입력 · 기록하지 않았습니다";
                        }
                        if (typing != null && typing.Target == current && typing.Saved != null && typing.Baseline != null && completedState != null && DateTime.UtcNow.Subtract(dirtyAt).TotalMilliseconds >= 400 && workerStarted >= dirtyAt && typing.Reason != "shortcut_requires_manual_setup" && typing.Reason != "recording_method_unverified") {
                            string before = Str(typing.Baseline, "value", ""), after = Str(completedState, "value", "");
                            if (Str(typing.Baseline, "identity", "before") == Str(completedState, "identity", "after") && before.Length == 0 && after != before) {
                                typing.Saved["operation"] = "set_value"; typing.Saved["value"] = after; typing.Saved["input_role"] = "Edit"; typing.Saved.Remove("reason"); typingConfirmedAt = DateTime.UtcNow; list.Items[typing.Index] = (typing.Index + 1).ToString() + "  글자 입력 · 저장 전 내용을 확인하세요";
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
            DateTime retryAt;
            bool targetWorkerFree; lock (stateLock) { targetWorkerFree = current != null && !sampleWorkers.ContainsKey(current); }
            if (!textBusy && targetWorkerFree && activeWorkers < 4 && current != null && (!sampleRetryAt.TryGetValue(current, out retryAt) || DateTime.UtcNow >= retryAt)
                    && DateTime.UtcNow.Subtract(lastSample).TotalMilliseconds >= 300) { lastSample = DateTime.UtcNow; Sample(current); }
        }
    }
}
