// Read-only FlaUI fallback. No input methods, Driver handles or global hooks.
// Each request is scoped to an existing HWND/PID and bounded by the Python owner.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;

class FlaUIProbe {
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    static JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 2097152, RecursionLimit = 48 };
    static Dictionary<string, object> Obj(object value) { return (Dictionary<string, object>)value; }
    static int Number(Dictionary<string, object> value, string key, int fallback) { return value.ContainsKey(key) ? Convert.ToInt32(value[key]) : fallback; }
    static string Text(Dictionary<string, object> value, string key, string fallback) { return value.ContainsKey(key) ? Convert.ToString(value[key]) : fallback; }
    static void CheckWindow(IntPtr hwnd, int pid) {
        uint actual;
        if (pid < 1 || !IsWindow(hwnd) || GetWindowThreadProcessId(hwnd, out actual) == 0 || actual != pid)
            throw new InvalidOperationException("target_mismatch");
    }
    static string Identity(AutomationElement e) {
        var id = e.Properties.RuntimeId.Value;
        if (id == null || id.Length == 0) throw new InvalidOperationException("scope_identity_unavailable");
        return String.Join(",", Array.ConvertAll(id, v => v.ToString()));
    }
    static void ProveWithin(AutomationElement e, AutomationElement root, ITreeWalker walker, int pid) {
        string rootId = Identity(root); var seen = new HashSet<string>();
        for (int depth = 0; e != null && depth < 128; depth++, e = walker.GetParent(e)) {
            if (e.Properties.ProcessId.Value != pid) throw new InvalidOperationException("target_mismatch");
            string id = Identity(e);
            if (id == rootId) return;
            if (!seen.Add(id)) throw new InvalidOperationException("scope_ancestry_cycle");
        }
        throw new InvalidOperationException("scope_outside_target");
    }
    static bool Matches(AutomationElement e, Dictionary<string, object> selector) {
        if (selector.Count == 0) throw new InvalidOperationException("invalid_selector");
        foreach (string key in selector.Keys) {
            if (key == "within") continue;
            string actual;
            if (key == "name") actual = e.Properties.Name.ValueOrDefault ?? "";
            else if (key == "automation_id") actual = e.Properties.AutomationId.ValueOrDefault ?? "";
            else if (key == "role") {
                ControlType role;
                if(!e.Properties.ControlType.TryGetValue(out role)) return false;
                actual=role.ToString();
            }
            else throw new InvalidOperationException("invalid_selector");
            if (!String.Equals(actual, Convert.ToString(selector[key]), StringComparison.Ordinal)) return false;
        }
        return true;
    }
    static AutomationElement Unique(AutomationElement root, Dictionary<string, object> selector, ITreeWalker walker, int pid, Stopwatch watch, int timeout) {
        AutomationElement found = null;
        var pending = new Queue<AutomationElement>(); pending.Enqueue(root); var seen = new HashSet<string>();
        while (pending.Count > 0) {
            if (watch.ElapsedMilliseconds >= timeout) throw new InvalidOperationException("scoped_observation_timeout");
            var node = pending.Dequeue();
            if (seen.Count >= 2000) throw new InvalidOperationException("scope_lookup_limit");
            if (!seen.Add(Identity(node))) throw new InvalidOperationException("scope_ancestry_cycle");
            if (node.Properties.ProcessId.Value != pid) throw new InvalidOperationException("target_mismatch");
            if (Matches(node, selector)) {
                if (found != null) throw new InvalidOperationException("ambiguous_selector");
                found = node;
            }
            for (var child = walker.GetFirstChild(node); child != null; child = walker.GetNextSibling(child)) {
                if (pending.Count + seen.Count >= 2000) throw new InvalidOperationException("scope_lookup_limit");
                pending.Enqueue(child);
            }
        }
        if (found == null) throw new InvalidOperationException("selector_not_found");
        return found;
    }
    static Dictionary<string, object> Read(AutomationElement e, int index, int parent, int pid) {
        if (e.Properties.ProcessId.Value != pid) throw new InvalidOperationException("target_mismatch");
        ControlType controlType; bool hasRole=e.Properties.ControlType.TryGetValue(out controlType);
        var row = new Dictionary<string, object> {
            {"element_index", index}, {"parent_index", parent}, {"name", e.Properties.Name.ValueOrDefault ?? ""}, {"automation_id", e.Properties.AutomationId.ValueOrDefault ?? ""},
            {"role", hasRole?controlType.ToString():"Unknown"},
            {"verification_only", true}
        };
        bool enabled, offscreen;
        if(e.Properties.IsEnabled.TryGetValue(out enabled)) row["enabled"]=enabled;
        if(e.Properties.IsOffscreen.TryGetValue(out offscreen)) row["offscreen"]=offscreen;
        if (e.Properties.IsPassword.ValueOrDefault) { row["protected"] = true; row["name"] = ""; return row; }
        Rectangle rect;
        if(e.Properties.BoundingRectangle.TryGetValue(out rect))
            row["bounds"] = new Dictionary<string, object> {{"x",rect.X},{"y",rect.Y},{"width",rect.Width},{"height",rect.Height}};
        var patterns = new List<string>();
        if (e.Patterns.Invoke.IsSupported) patterns.Add("invoke");
        if (e.Patterns.Value.IsSupported) {
            patterns.Add("set_value"); var value = e.Patterns.Value.Pattern;
            row["value"] = value.Value.Value; row["read_only"] = value.IsReadOnly.Value;
        }
        if (e.Patterns.Toggle.IsSupported) {
            patterns.Add("toggle"); var state = e.Patterns.Toggle.Pattern.ToggleState.Value;
            if (state != ToggleState.Indeterminate) row["selected"] = state == ToggleState.On;
        } else if (e.Patterns.SelectionItem.IsSupported) {
            patterns.Add("select"); row["selected"] = e.Patterns.SelectionItem.Pattern.IsSelected.Value;
        }
        if (e.Patterns.ExpandCollapse.IsSupported) patterns.Add("expand");
        if (hasRole && controlType == ControlType.ComboBox && e.Patterns.Selection.IsSupported) {
            var selected = e.Patterns.Selection.Pattern.Selection.Value;
            row.Remove("value"); row["selection_committed"] = selected.Length == 1;
            if (selected.Length == 1 && selected[0].Properties.ProcessId.Value == pid && !selected[0].Properties.IsPassword.ValueOrDefault) {
                row["value"] = selected[0].Name; row["selected_value"] = selected[0].Name;
            }
        }
        row["observed_patterns"] = patterns;
        foreach (string key in new string[]{"name","automation_id","value","selected_value"})
            if (row.ContainsKey(key) && Convert.ToString(row[key]).Length > 16000) throw new InvalidOperationException("property_too_large");
        return row;
    }
    static object Query(Dictionary<string, object> request, AutomationBase automation, string backend) {
        int pid = Number(request,"pid",0); IntPtr hwnd = new IntPtr(Convert.ToInt64(request["window_id"]));
        int timeout = Number(request,"timeout_ms",3000), maxDepth = Number(request,"max_depth",12), maxElements = Number(request,"max_elements",600);
        if (timeout < 1 || timeout > 120000 || maxDepth < 1 || maxDepth > 32 || maxElements < 1 || maxElements > 5000)
            throw new InvalidOperationException("invalid_inspection_limits");
        var watch = Stopwatch.StartNew(); CheckWindow(hwnd,pid); var root = automation.FromHandle(hwnd);
        string rootId = Identity(root); var walker = automation.TreeWalkerFactory.GetRawViewWalker();
        string operation = Text(request,"operation","observe"); bool truncated = false;
        var rows = new List<Dictionary<string, object>>();
        if (operation == "point") {
            var point = new Point(Number(request,"x",0),Number(request,"y",0));
            if (!root.BoundingRectangle.Contains(point)) throw new InvalidOperationException("point_outside_target");
            var element = automation.FromPoint(point); ProveWithin(element,root,walker,pid);
            rows.Add(Read(element,0,-1,pid));
        } else if (operation == "observe") {
            var selectors = (ArrayList)request["selectors"];
            if (selectors.Count < 1 || selectors.Count > 40) throw new InvalidOperationException("invalid_selector");
            var selected = new Dictionary<string,int>(); var elements = new List<AutomationElement>();
            foreach (object raw in selectors) {
                var selector = Obj(raw); var scope = root;
                if (selector.ContainsKey("within")) scope = Unique(root,Obj(selector["within"]),walker,pid,watch,timeout);
                var e = Unique(scope,selector,walker,pid,watch,timeout); ProveWithin(e,root,walker,pid);
                string identity = Identity(e);
                if (!selected.ContainsKey(identity)) { selected[identity] = rows.Count; rows.Add(Read(e,rows.Count,-1,pid)); elements.Add(e); }
            }
            // Preserve selector-within semantics without inventing input authority.
            for (int i=0; i<elements.Count; i++) {
                var e=walker.GetParent(elements[i]); int depth=0;
                while(e!=null && depth++<128) {
                    string id=Identity(e); int parent;
                    if(selected.TryGetValue(id,out parent)) { rows[i]["parent_index"]=parent; break; }
                    if(id==rootId) break; e=walker.GetParent(e);
                }
            }
        } else if (operation == "inspect" || operation == "dump") {
            var scope = operation == "dump" ? root : Unique(root,Obj(request["within"]),walker,pid,watch,timeout);
            ProveWithin(scope,root,walker,pid);
            var pending = new Queue<Tuple<AutomationElement,int,int>>(); pending.Enqueue(Tuple.Create(scope,-1,0));
            var seen = new HashSet<string>();
            while(pending.Count>0) {
                if(watch.ElapsedMilliseconds>=timeout) throw new InvalidOperationException("scoped_observation_timeout");
                var item=pending.Dequeue(); string id=Identity(item.Item1);
                if(!seen.Add(id)) throw new InvalidOperationException("scope_ancestry_cycle");
                int index=rows.Count; var row=Read(item.Item1,index,item.Item2,pid); row["depth"]=item.Item3; rows.Add(row);
                var child=walker.GetFirstChild(item.Item1);
                if(item.Item3>=maxDepth) { if(child!=null) truncated=true; continue; }
                while(child!=null) {
                    if(rows.Count+pending.Count>=maxElements) { truncated=true; break; }
                    pending.Enqueue(Tuple.Create(child,index,item.Item3+1)); child=walker.GetNextSibling(child);
                }
                if(rows.Count>=maxElements) { if(pending.Count>0) truncated=true; break; }
            }
            ProveWithin(scope,root,walker,pid);
        } else throw new InvalidOperationException("unsupported_operation");
        CheckWindow(hwnd,pid); if(Identity(root)!=rootId) throw new InvalidOperationException("scope_identity_changed");
        var result = new Dictionary<string,object> {{"pid",pid},{"window_id",hwnd.ToInt64()},{"elements",rows},
            {"read_only",true},{"verification_only",true},{"scope_complete",true},{"scoped_observation",true},
            {"backend",backend},{"truncated",truncated},{"native_elapsed_ms",watch.Elapsed.TotalMilliseconds}};
        if(operation=="inspect") { result["within"]=request["within"]; result["scoped_inspection"]=true; }
        return result;
    }
    [STAThread] static int Main(string[] args) {
        Console.InputEncoding=Encoding.UTF8; Console.OutputEncoding=new UTF8Encoding(false);
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch(EntryPointNotFoundException) {}
        string backend=args.Length==1?args[0]:"uia3";
        if(backend!="uia2" && backend!="uia3") return 2;
        using(AutomationBase automation=backend=="uia2"?(AutomationBase)new FlaUI.UIA2.UIA2Automation():new FlaUI.UIA3.UIA3Automation()) {
            if(backend=="uia3") { automation.ConnectionTimeout=TimeSpan.FromMilliseconds(1000); automation.TransactionTimeout=TimeSpan.FromMilliseconds(1000); }
            string line;
            while((line=Console.ReadLine())!=null) {
                if(line.Length>1048576) return 2;
                string id="";
                try {
                    var request=Json.Deserialize<Dictionary<string,object>>(line); id=Text(request,"id","");
                    var data=Query(request,automation,"flaui_"+backend);
                    Console.WriteLine(Json.Serialize(new{ id=id, ok=true, data=data }));
                } catch(Exception ex) {
                    string code=ex is InvalidOperationException?ex.Message:"scoped_read_failed";
                    var unsupported=ex as FlaUI.Core.Exceptions.PropertyNotSupportedException;
                    Console.WriteLine(Json.Serialize(new{ id=id, ok=false, code=code, exception_type=ex.GetType().Name,
                        unsupported_property=unsupported==null?null:unsupported.Property.ToString() }));
                }
            }
        }
        return 0;
    }
}
