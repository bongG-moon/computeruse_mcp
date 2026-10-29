// Offscreen HWND lifetime/ownership and recorder evidence tests. No mouse,
// keyboard, foreground changes, application data, or live UIA are synthesized.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class MenuExpansionRecorderTests {
    const BindingFlags F = BindingFlags.Instance | BindingFlags.NonPublic;
    static readonly Type Recorder = typeof(VisualTools).GetNestedType("Recorder", BindingFlags.NonPublic);
    static readonly Type Pending = Recorder.GetNestedType("Pending", BindingFlags.NonPublic);
    static readonly Type Semantic = Recorder.GetNestedType("SemanticSample", BindingFlags.NonPublic);
    static readonly Type Shared = Recorder.GetNestedType("SharedFrame", BindingFlags.NonPublic);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    sealed class OffscreenForm : Form {
        internal OffscreenForm() { StartPosition = FormStartPosition.Manual; Location = new Point(-24000, -24000); Size = new Size(320, 160); ShowInTaskbar = false; }
        protected override bool ShowWithoutActivation { get { return true; } }
        protected override CreateParams CreateParams { get { var p = base.CreateParams; p.ExStyle |= 0x08000000; return p; } }
    }
    static void Set(object item, string name, object value) { item.GetType().GetField(name, F).SetValue(item, value); }
    static object Get(object item, string name) { return item.GetType().GetField(name, F).GetValue(item); }
    static object Call(object item, string name, params object[] args) { return item.GetType().GetMethod(name, F).Invoke(item, args); }
    static Dictionary<string, object> Bounds(Rectangle b) { return new Dictionary<string, object> { {"x", b.X}, {"y", b.Y}, {"width", b.Width}, {"height", b.Height} }; }
    static object Sample(VisualTools.Target target, bool expanded) {
        object sample = Activator.CreateInstance(Semantic, true);
        Set(sample, "Identity", "synthetic-parent-runtime-id"); Set(sample, "Role", "MenuItem"); Set(sample, "Enabled", true);
        Set(sample, "ObservedAt", DateTime.UtcNow.AddMilliseconds(expanded ? -10 : -300));
        Set(sample, "MenuState", expanded ? "expanded" : "collapsed"); Set(sample, "MenuStateSource", "fixture_injected_semantics"); Set(sample, "MenuComplete", true);
        Set(sample, "Native", new Dictionary<string, object> { { "element", new Dictionary<string, object> { {"name", "Export test"}, {"role", "MenuItem"} } }, { "window_bounds", Bounds(target.Bounds()) } });
        if (expanded) ((IList)Get(sample, "MenuItems")).Add(new Dictionary<string, object> { {"name", "Active sheet test"}, {"role", "MenuItem"}, {"enabled", true} });
        return sample;
    }
    static object BlankRecorder() {
        object recorder = FormatterServices.GetUninitializedObject(Recorder);
        foreach (string field in new[] { "targets", "events", "pending", "warnings" }) {
            var type = Recorder.GetField(field, F).FieldType; Set(recorder, field, Activator.CreateInstance(type));
        }
        Set(recorder, "maximum", 100); Set(recorder, "inputVersion", 7); Set(recorder, "stateLock", new object());
        return recorder;
    }
    static object Candidate(VisualTools.Target parent, object before) {
        object p = Activator.CreateInstance(Pending, true);
        Set(p, "Target", parent); Set(p, "Semantic", before); Set(p, "InputVersion", 7);
        Set(p, "Operation", "expand_menu"); Set(p, "RequestedOperation", "expand_menu");
        Set(p, "ClickAt", DateTime.UtcNow.AddMilliseconds(-320));
        var pixels = new Bitmap(96, 32); pixels.SetPixel(3, 4, Color.Red);
        Set(p, "Source", Activator.CreateInstance(Shared, F, null, new object[] {pixels}, null));
        return p;
    }
    static Dictionary<string, object> Run(string mode) {
        IntPtr foreground = GetForegroundWindow();
        var result = new Dictionary<string, object>();
        using (var main = new OffscreenForm()) using (var menu = new OffscreenForm()) using (var child = new OffscreenForm()) using (var second = new OffscreenForm()) {
            main.Show(); menu.Owner = main; menu.Show(); child.Owner = main; child.Show(); second.Owner = main; second.Show();
            if (mode == "left") child.Left = menu.Left - child.Width;
            using (var unrelated = new OffscreenForm()) {
                unrelated.Show();
                var root = new VisualTools.Target(new Dictionary<string, object> { {"pid", Process.GetCurrentProcess().Id}, {"window_id", main.Handle.ToInt64()}, {"program_id", "fixture"} });
                var otherRoot = new VisualTools.Target(new Dictionary<string, object> { {"pid", Process.GetCurrentProcess().Id}, {"window_id", unrelated.Handle.ToInt64()}, {"program_id", "fixture"} });
                try {
                    var parent = new VisualTools.Target(root, menu.Handle, "menu_parent");
                    var popup = new VisualTools.Target(mode == "unrelated" ? otherRoot : root, child.Handle, "menu_child_episode_1");
                    var extra = new VisualTools.Target(root, second.Handle, "menu_child_episode_2");
                    object recorder = BlankRecorder(), before = Sample(parent, false), after = Sample(parent, true), pending = Candidate(parent, before);
                    Set(recorder, "menuCandidate", pending);
                    var targets = (IList)Get(recorder, "targets"); targets.Add(root); targets.Add(parent); targets.Add(popup); targets.Add(extra);
                    ((HashSet<IntPtr>)Get(after, "MenuWindows")).Add(mode == "logical" ? menu.Handle : child.Handle);
                    if (mode == "duplicate") targets.Add(new VisualTools.Target(root, child.Handle, "duplicate_episode"));
                    if (mode == "two_children") ((HashSet<IntPtr>)Get(after, "MenuWindows")).Add(second.Handle);
                    if (mode == "retired") popup.Retired = true;
                    if (mode == "hidden") child.Hide();
                    if (mode == "epoch") Set(recorder, "inputVersion", 8);
                    if (mode == "moved") ((Dictionary<string, object>)Get(after, "Native"))["window_bounds"] = Bounds(new Rectangle(-24001, -24000, 320, 160));
                    if (mode == "identity") Set(after, "Identity", "different-runtime-id");
                    if (mode == "incomplete") Set(after, "MenuComplete", false);
                    if (mode == "hover_only") { Set(after, "MenuState", "collapsed"); ((IList)Get(after, "MenuItems")).Clear(); }
                    if (mode == "repair") {
                        Call(recorder, "PreserveMenuExpansionRepair", pending, popup);
                        object click = Activator.CreateInstance(Pending, true); Set(click, "Target", popup); Set(click, "Operation", "click");
                        Call(recorder, "Queue", click);
                    } else Call(recorder, "CompleteMenuCandidate", pending, after, 7);
                    var queue = (ICollection)Get(recorder, "pending"); result["queued"] = queue.Count;
                    if (queue.Count > 0) {
                        object[] entries = (object[])queue.GetType().GetMethod("ToArray").Invoke(queue, new object[0]);
                        result["operation"] = Get(entries[0], "Operation"); result["reason"] = Get(entries[0], "Reason"); result["evidence"] = Get(entries[0], "MenuEvidence");
                        object source = Get(entries[0], "Source"); result["retained_original_frame"] = source != null && ((Bitmap)Get(source, "Image")).GetPixel(3, 4).ToArgb() == Color.Red.ToArgb();
                        if (entries.Length == 2) result["next_operation"] = Get(entries[1], "Operation");
                        foreach (object entry in entries) Recorder.GetMethod("DisposePending", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] {entry});
                    }
                    Call(recorder, "ClearMenuCandidate");
                    result["retained_bytes_after_disposal"] = Shared.GetField("RetainedBytes", BindingFlags.Static | BindingFlags.NonPublic).GetValue(null);
                    result["input_version"] = Get(recorder, "inputVersion");
                } finally { root.Release(); otherRoot.Release(); }
            }
        }
        result["foreground_unchanged"] = foreground == GetForegroundWindow();
        result["scope"] = "offscreen_hwnd_and_injected_semantic_evidence_not_live_recording";
        return result;
    }
    [STAThread] static int Main(string[] args) {
        try {
            if (args[0] == "witness") {
                var result = new Dictionary<string, object>();
                foreach (string kind in new[] { "valid", "empty", "identity", "closed", "expanded_before", "disabled", "incomplete", "lifetime", "input", "no_child", "overflow", "stale", "future" })
                    result[kind] = VisualTools.RecordingMenuExpansionWitness(kind == "empty" ? "" : "parent", kind == "identity" ? "other" : "parent",
                        kind == "expanded_before" ? "expanded" : "collapsed", kind == "closed" ? "collapsed" : "expanded",
                        kind != "disabled", kind != "incomplete", kind != "lifetime", 7, kind == "input" ? 8 : 7,
                        kind == "no_child" ? 0 : kind == "overflow" ? 33 : 2, kind == "stale" ? 2501 : kind == "future" ? -1 : 300);
                Console.WriteLine(new JavaScriptSerializer().Serialize(result));
            } else Console.WriteLine(new JavaScriptSerializer().Serialize(Run(args[0])));
            return 0;
        } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
