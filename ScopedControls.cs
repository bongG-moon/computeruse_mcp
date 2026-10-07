// Read-only UIA property queries. No input injection and no reusable Driver handles.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Automation;

class ScopedControls {
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    static JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 2097152, RecursionLimit = 32 };
    static Dictionary<string, object> Obj(object value) { return (Dictionary<string, object>)value; }
    static string Text(Dictionary<string, object> value, string key) { return value.ContainsKey(key) ? Convert.ToString(value[key]) : null; }
    static void CheckWindow(IntPtr hwnd, int pid) {
        uint actual; if (!IsWindow(hwnd) || GetWindowThreadProcessId(hwnd, out actual) == 0 || actual != pid) throw new InvalidOperationException("target_mismatch");
    }
    static Condition Selector(Dictionary<string, object> selector) {
        List<Condition> conditions = new List<Condition>();
        foreach (string key in selector.Keys) {
            if (key == "within") continue;
            AutomationProperty property;
            object value = selector[key];
            if (key == "name") property = AutomationElement.NameProperty;
            else if (key == "automation_id") property = AutomationElement.AutomationIdProperty;
            else if (key == "role") {
                property = AutomationElement.ControlTypeProperty;
                var field = typeof(ControlType).GetField(Convert.ToString(value));
                if (field == null) throw new InvalidOperationException("unsupported_role");
                value = field.GetValue(null);
            } else throw new InvalidOperationException("invalid_selector");
            conditions.Add(new PropertyCondition(property, value));
        }
        if (conditions.Count == 0) throw new InvalidOperationException("invalid_selector");
        return conditions.Count == 1 ? conditions[0] : new AndCondition(conditions.ToArray());
    }
    static int LookupVisited;
    static AutomationElement Metadata(AutomationElement parent) {
        var cache = new CacheRequest(); cache.TreeScope = TreeScope.Subtree;
        cache.TreeFilter = Automation.RawViewCondition;
        cache.Add(AutomationElement.NameProperty); cache.Add(AutomationElement.AutomationIdProperty);
        cache.Add(AutomationElement.ControlTypeProperty); cache.AutomationElementMode = AutomationElementMode.Full;
        return parent.GetUpdatedCache(cache);
    }
    static AutomationElement Unique(AutomationElement parent, Dictionary<string, object> selector, bool includeRoot, bool metadataCached = false) {
        // One bulk metadata cache avoids a cross-process property query per
        // candidate. Current values and patterns are read only in the selected
        // region; this cache is request-local and never authorizes an input.
        Selector(selector);
        var cachedRoot = metadataCached ? parent : Metadata(parent);
        AutomationElement found = null;
        var pending = new Queue<AutomationElement>(); pending.Enqueue(cachedRoot);
        int visited = 0; LookupVisited = 0;
        while (pending.Count > 0) {
            var item = pending.Dequeue();
            LookupVisited = ++visited;
            if (visited > 20000) throw new InvalidOperationException("scope_lookup_limit");
            var children = item.CachedChildren;
            if (children != null) foreach (AutomationElement child in children) pending.Enqueue(child);
            if (!includeRoot && item == cachedRoot) continue;
            var value = item.Cached;
            if (selector.ContainsKey("name") && value.Name != Convert.ToString(selector["name"])) continue;
            if (selector.ContainsKey("automation_id") && value.AutomationId != Convert.ToString(selector["automation_id"])) continue;
            if (selector.ContainsKey("role") && value.ControlType.ProgrammaticName != "ControlType." + Convert.ToString(selector["role"])) continue;
            if (found != null) throw new InvalidOperationException("ambiguous_selector");
            found = item;
        }
        return found;
    }
    static string Identity(AutomationElement element) {
        int[] identity = element.GetRuntimeId();
        if (identity == null || identity.Length == 0) throw new InvalidOperationException("scope_identity_unavailable");
        return String.Join(",", Array.ConvertAll(identity, v => v.ToString()));
    }
    // The same element may satisfy multiple within selectors. Its parent must
    // follow the real tree, never whichever selector happened to be queried last.
    // Continue to the approved root after finding a selected ancestor: a detached
    // or reparented provider cannot claim a complete in-window scope.
    static int NearestSelectedAncestor(string identity, string root, Dictionary<string, int> selected, Func<string, string> parentOf) {
        var visited = new HashSet<string>(); int nearest = -1;
        for (int depth = 0; depth <= 128; depth++) {
            if (identity == root) return nearest;
            if (depth == 128) throw new InvalidOperationException("scope_ancestry_limit");
            if (!visited.Add(identity)) throw new InvalidOperationException("scope_ancestry_cycle");
            string parent = parentOf(identity);
            if (parent == null) throw new InvalidOperationException("scope_outside_target");
            int index;
            if (nearest < 0 && selected.TryGetValue(parent, out index)) nearest = index;
            identity = parent;
        }
        throw new InvalidOperationException("scope_ancestry_limit");
    }
    static void ConnectParents(AutomationElement root, List<Dictionary<string, object>> rows,
                               Dictionary<string, int> selected, Dictionary<string, AutomationElement> elements) {
        string rootIdentity = Identity(root);
        elements[rootIdentity] = root;
        var parents = new Dictionary<string, string>();
        Func<string, string> parentOf = delegate(string identity) {
            string known;
            if (parents.TryGetValue(identity, out known)) return known;
            var parent = TreeWalker.RawViewWalker.GetParent(elements[identity]);
            if (parent == null) return null;
            string parentIdentity = Identity(parent);
            parents[identity] = parentIdentity;
            if (!elements.ContainsKey(parentIdentity)) elements[parentIdentity] = parent;
            return parentIdentity;
        };
        foreach (var item in selected)
            rows[item.Value]["parent_index"] = NearestSelectedAncestor(item.Key, rootIdentity, selected, parentOf);
    }
    static Dictionary<string, object> Read(AutomationElement e, int index, int parent, int pid, bool inspect = false) {
        var current = e.Current;
        if (current.ProcessId != pid) throw new InvalidOperationException("target_mismatch");
        var row = new Dictionary<string, object> { { "element_index", index }, { "parent_index", parent },
            { "name", current.Name }, { "automation_id", current.AutomationId },
            { "role", current.ControlType.ProgrammaticName.Replace("ControlType.", "") },
            { "enabled", current.IsEnabled }, { "verification_only", true } };
        if (current.IsPassword) { row["protected"] = true; return row; }
        object raw;
        if (inspect) {
            var patterns = new List<string>();
            if (e.TryGetCurrentPattern(InvokePattern.Pattern, out raw)) patterns.Add("invoke");
            if (e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) patterns.Add("set_value");
            if (e.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) patterns.Add("toggle");
            if (e.TryGetCurrentPattern(SelectionItemPattern.Pattern, out raw)) patterns.Add("select");
            if (e.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out raw)) patterns.Add("expand");
            row["observed_patterns"] = patterns;
        }
        if (e.TryGetCurrentPattern(ValuePattern.Pattern, out raw)) {
            var pattern = (ValuePattern)raw;
            row["value"] = pattern.Current.Value; row["read_only"] = pattern.Current.IsReadOnly;
        }
        if (e.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) {
            var state = ((TogglePattern)raw).Current.ToggleState;
            if (state != ToggleState.Indeterminate) row["selected"] = state == ToggleState.On;
        } else if (e.TryGetCurrentPattern(SelectionItemPattern.Pattern, out raw)) row["selected"] = ((SelectionItemPattern)raw).Current.IsSelected;
        if (current.ControlType == ControlType.ComboBox && e.TryGetCurrentPattern(SelectionPattern.Pattern, out raw)) {
            var selected = ((SelectionPattern)raw).Current.GetSelection();
            row.Remove("value");
            row["selection_committed"] = selected.Length == 1;
            if (selected.Length == 1 && selected[0].Current.ProcessId == pid && !selected[0].Current.IsPassword) {
                row["selected_value"] = selected[0].Current.Name;
                // ValuePattern text alone may be an uncommitted search string.
                row["value"] = selected[0].Current.Name;
            }
        }
        foreach (string key in new string[] { "name", "automation_id", "value", "selected_value" })
            if (row.ContainsKey(key) && Convert.ToString(row[key]).Length > 16000) throw new InvalidOperationException("property_too_large");
        return row;
    }
    // A bounded raw-tree walk within an already unique parent. The root lookup
    // can still traverse provider metadata, but sibling subtree properties are
    // never read. Python terminates this helper when a provider call hangs.
    static object Inspect(Dictionary<string, object> request) {
        int pid = Convert.ToInt32(request["pid"]); var hwnd = new IntPtr(Convert.ToInt64(request["window_id"]));
        int maxDepth = Convert.ToInt32(request["max_depth"]), maxElements = Convert.ToInt32(request["max_elements"]);
        if (maxDepth < 1 || maxDepth > 32 || maxElements < 1 || maxElements > 5000) throw new InvalidOperationException("invalid_inspection_limits");
        var within = Obj(request["within"]);
        if (within.ContainsKey("within")) throw new InvalidOperationException("invalid_selector");
        CheckWindow(hwnd, pid); var root = AutomationElement.FromHandle(hwnd); string rootIdentity = Identity(root);
        var scope = Unique(root, within, true); int lookupCount = LookupVisited;
        if (scope == null) throw new InvalidOperationException("selector_not_found");
        var rows = new List<Dictionary<string, object>>();
        var seen = new Dictionary<string, int>();
        var elements = new Dictionary<string, AutomationElement>();
        string scopeIdentity = Identity(scope);
        rows.Add(Read(scope, 0, -1, pid, true)); rows[0]["depth"] = 0;
        seen[scopeIdentity] = 0; elements[scopeIdentity] = scope;
        // Prove the selected region is still beneath this approved HWND.
        ConnectParents(root, rows, seen, elements);
        var queue = new Queue<Tuple<AutomationElement, int, int>>();
        queue.Enqueue(Tuple.Create(scope, 0, 0));
        bool truncated = false;
        while (queue.Count > 0) {
            var node = queue.Dequeue();
            var child = TreeWalker.RawViewWalker.GetFirstChild(node.Item1);
            if (node.Item3 >= maxDepth) { if (child != null) truncated = true; continue; }
            while (child != null) {
                if (rows.Count >= maxElements) { truncated = true; break; }
                string key = Identity(child);
                if (seen.ContainsKey(key)) throw new InvalidOperationException("scope_ancestry_cycle");
                int index = rows.Count;
                var row = Read(child, index, node.Item2, pid, true); row["depth"] = node.Item3 + 1;
                rows.Add(row); seen[key] = index; elements[key] = child;
                queue.Enqueue(Tuple.Create(child, index, node.Item3 + 1));
                child = TreeWalker.RawViewWalker.GetNextSibling(child);
            }
            if (rows.Count >= maxElements && (child != null || queue.Count > 0)) { truncated = true; break; }
        }
        // Read-only selector recommendations must still be resolved freshly
        // before input. Recheck the selected node's identity and current
        // ancestry without a second full-window metadata enumeration.
        if (Identity(scope) != scopeIdentity) throw new InvalidOperationException("scope_identity_changed");
        var currentScope = scope.Current;
        if (within.ContainsKey("name") && currentScope.Name != Convert.ToString(within["name"]) ||
            within.ContainsKey("automation_id") && currentScope.AutomationId != Convert.ToString(within["automation_id"]) ||
            within.ContainsKey("role") && currentScope.ControlType.ProgrammaticName != "ControlType." + Convert.ToString(within["role"]))
            throw new InvalidOperationException("scope_identity_changed");
        ConnectParents(root, rows, seen, elements);
        CheckWindow(hwnd, pid);
        if (Identity(AutomationElement.FromHandle(hwnd)) != rootIdentity) throw new InvalidOperationException("target_mismatch");
        return new Dictionary<string, object> { { "pid", pid }, { "window_id", hwnd.ToInt64() }, { "elements", rows },
            { "read_only", true }, { "scoped_observation", true }, { "scoped_inspection", true }, { "within", within },
            { "scope_complete", true }, { "truncated", truncated }, { "visited_controls", rows.Count },
            { "lookup_metadata_count", lookupCount }, { "scope_lookup", "whole_window_identity_metadata" } };
    }
    static object Query(Dictionary<string, object> request) {
        if (Text(request, "operation") == "inspect") return Inspect(request);
        int pid = Convert.ToInt32(request["pid"]); var hwnd = new IntPtr(Convert.ToInt64(request["window_id"]));
        CheckWindow(hwnd, pid); var root = AutomationElement.FromHandle(hwnd);
        var selectors = (ArrayList)request["selectors"];
        if (selectors.Count < 1 || selectors.Count > 40) throw new InvalidOperationException("invalid_selector_count");
        var rows = new List<Dictionary<string, object>>();
        var seen = new Dictionary<string, int>();
        var elements = new Dictionary<string, AutomationElement>();
        // Share only identity metadata within this one response. Every next
        // request reads a new tree and every selected property's current value.
        var metadataRoot = Metadata(root);
        foreach (object item in selectors) {
            var selector = Obj(item); var scope = metadataRoot;
            if (selector.ContainsKey("within")) {
                scope = Unique(metadataRoot, Obj(selector["within"]), true, true);
                if (scope == null) continue;
                string scopeKey = Identity(scope);
                if (!seen.ContainsKey(scopeKey)) { int index = rows.Count; rows.Add(Read(scope, index, -1, pid)); seen[scopeKey] = index; elements[scopeKey] = scope; }
            }
            var element = Unique(scope, selector, !selector.ContainsKey("within"), true);
            if (element == null) continue;
            string identity = Identity(element);
            if (!seen.ContainsKey(identity)) { int index = rows.Count; rows.Add(Read(element, index, -1, pid)); seen[identity] = index; elements[identity] = element; }
        }
        ConnectParents(root, rows, seen, elements);
        CheckWindow(hwnd, pid);
        return new Dictionary<string, object> { { "pid", pid }, { "window_id", hwnd.ToInt64() }, { "elements", rows },
            { "read_only", true }, { "scoped_observation", true }, { "scope_complete", true } };
    }
    [MTAThread] static void Main() {
        Console.InputEncoding = new UTF8Encoding(false); Console.OutputEncoding = new UTF8Encoding(false);
        string line;
        while ((line = Console.ReadLine()) != null) {
            string id = null; object response;
            try {
                if (line.Length > 262144) throw new InvalidOperationException("request_too_large");
                var request = Json.Deserialize<Dictionary<string, object>>(line); id = Text(request, "id");
                if (id == null || id.Length != 32) throw new InvalidOperationException("invalid_request");
                response = new Dictionary<string, object> { { "id", id }, { "ok", true }, { "data", Query(request) } };
            } catch (Exception error) {
                string code = error is InvalidOperationException ? error.Message : "uia_property_read_failed";
                response = new Dictionary<string, object> { { "id", id }, { "ok", false }, { "code", code } };
            }
            Console.WriteLine(Json.Serialize(response)); Console.Out.Flush();
        }
    }
}
