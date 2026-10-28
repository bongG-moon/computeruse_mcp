// Opt-in accessibility inventory. No focus, input, pattern invocation, or screenshots.
// One source per process isolates blocked providers; the caller enforces its deadline.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using Accessibility;

class AccessibilityDiagnostics {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, System.Text.StringBuilder name, int size);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object value);
    [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible parent, int start, int count, [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=2)] object[] children, out int obtained);
    [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(IAccessible accessible, out IntPtr hwnd);
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int left, top, right, bottom; }
    static JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16777216, RecursionLimit = 128 };
    static IntPtr target;
    static int pid, maxDepth, maxElements, outside, suppressed, textTruncated;
    static bool truncated, providerErrors;
    static bool timeLimit, nodeLimit, depthLimit;
    static int timeBudget=28000;
    static System.Diagnostics.Stopwatch elapsed=System.Diagnostics.Stopwatch.StartNew();
    static IAccessible msaaRoot;
    static List<object> nodes = new List<object>();
    static List<object> errors = new List<object>();
    static List<object> scopeExclusions = new List<object>();
    static HashSet<string> visited = new HashSet<string>();
    static HashSet<string> msaaVisited = new HashSet<string>();
    static HashSet<string> directMenuChildren = new HashSet<string>();

    static Dictionary<string,object> D(params object[] values) {
        var result=new Dictionary<string,object>();
        for(int i=0;i<values.Length;i+=2) result[(string)values[i]]=values[i+1];
        return result;
    }
    static string Text(object value) { string s=value as string; if(s==null)return null;if(s.Length<=1000)return s;textTruncated++;return s.Substring(0,1000); }
    static Dictionary<string,object> Failure(Exception error, string reason) {
        providerErrors=true;
        return D("status","unknown","reason",reason,"hresult",error.HResult);
    }
    static object Read(Func<object> read) {
        if(Expired()) return D("status","unknown","reason","time_budget");
        try { object value=read(); return value==AutomationElement.NotSupported?
            D("status","unsupported"):D("status","observed","value",value); }
        catch(Exception error) {
            if(error.HResult==unchecked((int)0x80020003)||error.HResult==unchecked((int)0x80004001))
                return D("status","unsupported","hresult",error.HResult);
            return Failure(error,"provider_read_failed");
        }
    }
    static bool Expired() {
        if(elapsed.ElapsedMilliseconds<timeBudget) return false;
        truncated=true;timeLimit=true;return true;
    }
    static Dictionary<string,object> Box(double x,double y,double w,double h) {
        if(Double.IsNaN(x)||Double.IsNaN(y)||Double.IsNaN(w)||Double.IsNaN(h)||
           Double.IsInfinity(x)||Double.IsInfinity(y)||Double.IsInfinity(w)||Double.IsInfinity(h))
            return D("status","unknown","reason","non_finite_geometry");
        return D("x",x,"y",y,"width",w,"height",h,"coordinate_space","desktop_physical");
    }
    static void Check() {uint actual; if(!IsWindow(target)||GetWindowThreadProcessId(target,out actual)==0||actual!=pid) throw new InvalidOperationException("target_changed");}
    static bool InScope(IntPtr hwnd) {
        uint actual; return hwnd!=IntPtr.Zero && IsWindow(hwnd) && GetWindowThreadProcessId(hwnd,out actual)!=0 && actual==pid && (hwnd==target||IsChild(target,hwnd));
    }
    static bool NativePassword(IntPtr hwnd) {
        // MSAA's outer ROLE_SYSTEM_WINDOW may expose its caption/value while
        // only the client object sets STATE_SYSTEM_PROTECTED. Redact the whole
        // native password edit subtree, including its non-client objects.
        var name=new System.Text.StringBuilder(512);
        if(hwnd==IntPtr.Zero||GetClassName(hwnd,name,name.Capacity)==0) return false;
        string cls=name.ToString();
        bool edit=String.Equals(cls,"Edit",StringComparison.OrdinalIgnoreCase)||
            cls.IndexOf(".EDIT.",StringComparison.OrdinalIgnoreCase)>=0||
            cls.StartsWith("RichEdit",StringComparison.OrdinalIgnoreCase);
        return edit&&(GetWindowLong(hwnd,-16)&0x20)!=0;
    }
    static object Property(AutomationElement element, AutomationProperty property) {
        if(property==null) return D("status","unknown","reason","managed_uia_property_unavailable");
        return Read(delegate {object value=element.GetCurrentPropertyValue(property,true); return value is string?Text(value):value;});
    }
    static object PropertyId(AutomationElement element, int id) {
        return Property(element,AutomationProperty.LookupById(id));
    }
    static object Pattern(AutomationElement element, AutomationPattern pattern, Func<object,object> read) {
        if(Expired()) return D("status","unknown","reason","time_budget");
        if(pattern==null) return D("status","unknown","reason","managed_uia_pattern_unavailable");
        try {
            object instance;
            if(!element.TryGetCurrentPattern(pattern,out instance)) return D("status","unsupported");
            var result=D("status","supported","read_only",true);
            if(read!=null) result["properties"]=Read(delegate{return read(instance);});
            return result;
        } catch(Exception error) {return Failure(error,"pattern_read_failed");}
    }
    static object ElementRef(AutomationElement element) {
        if(element==null) return null;
        AutomationElement ancestor=element;
        bool proven=false;
        for(int i=0;i<32&&ancestor!=null;i++) {
            if(ancestor.Current.ProcessId!=pid) return D("status","excluded","reason","different_process");
            int handle=ancestor.Current.NativeWindowHandle;
            if(handle!=0) {
                if(!InScope(new IntPtr(handle))) return D("status","excluded","reason","outside_window_scope");
                proven=true;break;
            }
            ancestor=TreeWalker.RawViewWalker.GetParent(ancestor);
        }
        if(!proven) return D("status","unknown","reason","relation_scope_unproven");
        return D("runtime_id",element.GetRuntimeId(),"session_only",true);
    }
    static object ElementRefs(AutomationElement[] elements) {
        var result=new List<object>();
        for(int i=0;i<elements.Length&&i<100;i++) result.Add(ElementRef(elements[i]));
        return D("items",result,"truncated",elements.Length>100);
    }
    static object Patterns(AutomationElement e, bool safe) {
        var p=D(
            "Invoke",Pattern(e,InvokePattern.Pattern,null),
            "ExpandCollapse",Pattern(e,ExpandCollapsePattern.Pattern,delegate(object v){return ((ExpandCollapsePattern)v).Current.ExpandCollapseState.ToString();}),
            "Selection",Pattern(e,SelectionPattern.Pattern,delegate(object v){var c=((SelectionPattern)v).Current;return D("can_select_multiple",c.CanSelectMultiple,"selection_required",c.IsSelectionRequired,"selection",ElementRefs(c.GetSelection()));}),
            "SelectionItem",Pattern(e,SelectionItemPattern.Pattern,delegate(object v){var c=((SelectionItemPattern)v).Current;return D("selected",c.IsSelected,"container",ElementRef(c.SelectionContainer));}),
            "Grid",Pattern(e,GridPattern.Pattern,delegate(object v){var c=((GridPattern)v).Current;return D("row_count",c.RowCount,"column_count",c.ColumnCount);}),
            "GridItem",Pattern(e,GridItemPattern.Pattern,delegate(object v){var c=((GridItemPattern)v).Current;return D("row",c.Row,"column",c.Column,"row_span",c.RowSpan,"column_span",c.ColumnSpan,"grid",ElementRef(c.ContainingGrid));}),
            "Table",Pattern(e,TablePattern.Pattern,delegate(object v){var c=((TablePattern)v).Current;return D("row_headers",ElementRefs(c.GetRowHeaders()),"column_headers",ElementRefs(c.GetColumnHeaders()));}),
            "TableItem",Pattern(e,TableItemPattern.Pattern,delegate(object v){var c=((TableItemPattern)v).Current;return D("row_headers",ElementRefs(c.GetRowHeaderItems()),"column_headers",ElementRefs(c.GetColumnHeaderItems()));}),
            "ItemContainer",Pattern(e,ItemContainerPattern.Pattern,null),
            "VirtualizedItem",Pattern(e,VirtualizedItemPattern.Pattern,null),
            "ScrollItem",Pattern(e,ScrollItemPattern.Pattern,null),
            "Scroll",Pattern(e,ScrollPattern.Pattern,null),
            "Toggle",Pattern(e,TogglePattern.Pattern,delegate(object v){return ((TogglePattern)v).Current.ToggleState.ToString();}),
            "Text",Pattern(e,TextPattern.Pattern,null),
            "Value",Pattern(e,ValuePattern.Pattern,safe?(Func<object,object>)delegate(object v){var c=((ValuePattern)v).Current;return D("value",Text(c.Value),"read_only",c.IsReadOnly);}:null));
        // .NET Framework's managed adapter does not expose every native pattern.
        // Unknown is deliberate: it is not evidence that the provider lacks MSAA.
        p["LegacyIAccessible"]=Pattern(e,AutomationPattern.LookupById(10018),null);
        return p;
    }
    static bool AddUia(AutomationElement e, int parent, int depth, TreeWalker walker) {
        if(Expired()) return false;
        if(nodes.Count>=maxElements) {truncated=true;nodeLimit=true;return false;}
        int actual, hwnd;
        try {actual=e.Current.ProcessId;hwnd=e.Current.NativeWindowHandle;}
        catch(Exception error) {errors.Add(Failure(error,"element_identity_unreadable"));return true;}
        if(actual!=pid||(hwnd!=0&&!InScope(new IntPtr(hwnd)))) {outside++;return true;}
        string key;
        int[] runtime;
        try {runtime=e.GetRuntimeId();key=String.Join(",",runtime);} catch(Exception error) {errors.Add(Failure(error,"runtime_id_unreadable"));return true;}
        if(!visited.Add(key)) {errors.Add(D("status","unknown","reason","provider_cycle"));providerErrors=true;return false;}
        bool safe=false;object protection;
        try {
            bool value=e.Current.IsPassword||NativePassword(new IntPtr(hwnd));
            AutomationProperty legacyState=AutomationProperty.LookupById(30096);
            if(legacyState!=null) {
                object legacyValue=e.GetCurrentPropertyValue(legacyState,true);
                if(legacyValue!=AutomationElement.NotSupported&&legacyValue!=null)
                    value=value||(Convert.ToInt32(legacyValue)&0x20000000)!=0;
            }
            safe=!value;protection=D("status","observed","value",value);
        }
        catch(Exception error) {protection=Failure(error,"protection_unknown");}
        var properties=D("framework_id",Property(e,AutomationElement.FrameworkIdProperty),
            "class_name",Property(e,AutomationElement.ClassNameProperty),
            "provider_description",PropertyId(e,30107),
            "native_window_handle",hwnd,"runtime_id",runtime,"process_id",actual,
            "is_control_element",Property(e,AutomationElement.IsControlElementProperty),
            "is_content_element",Property(e,AutomationElement.IsContentElementProperty),
            "is_offscreen",Property(e,AutomationElement.IsOffscreenProperty),
            "enabled",Property(e,AutomationElement.IsEnabledProperty),"is_password",protection,
            "control_type",Read(delegate{return e.Current.ControlType.ProgrammaticName;}),
            "bounds",Read(delegate{var b=e.Current.BoundingRectangle;return Box(b.X,b.Y,b.Width,b.Height);}));
        if(safe) {
            properties["name"]=Property(e,AutomationElement.NameProperty);
            properties["automation_id"]=Property(e,AutomationElement.AutomationIdProperty);
            properties["labeled_by"]=Read(delegate{return ElementRef(e.Current.LabeledBy);});
        } else {properties["content_redacted"]=true;suppressed++;}
        var legacy=D("source","uia_legacy_properties","status","unknown","read_only",true,
            "reason","native_legacy_fields_may_be_unavailable_in_managed_adapter");
        if(safe) foreach(var pair in new Dictionary<string,int>{{"child_id",30091},{"name",30092},{"value",30093},{"role",30095},{"state",30096},{"default_action",30100}})
            legacy[pair.Key]=PropertyId(e,pair.Value);
        int index=nodes.Count;
        var row=D("node_id",index,"parent_id",parent<0?null:(object)parent,"depth",depth,
            "properties",properties,"patterns",Patterns(e,safe),"legacy_iaccessible",legacy,
            "read_only",true,"authorizes_input",false,"identity_lifetime","current_diagnostic_only");
        nodes.Add(row);
        if(!safe) {row["children_status"]="redacted";return true;}
        if(Expired()) {row["children_status"]="time_budget";return false;}
        try {
            AutomationElement child=walker.GetFirstChild(e);
            if(depth>=maxDepth&&child!=null) {truncated=true;depthLimit=true;row["children_status"]="depth_limit";return true;}
            row["children_status"]=child==null?"provider_returned_zero":"enumerated";
            while(child!=null) {
                if(!AddUia(child,index,depth+1,walker)) return false;
                if(Expired()) {row["children_status"]="time_budget";return false;}
                child=walker.GetNextSibling(child);
            }
        } catch(Exception error) {row["children_status"]="provider_error";errors.Add(Failure(error,"children_read_failed"));}
        return true;
    }
    static void AddMsaa(IAccessible e, object child, int parent, int depth, int objectId) {
        if(Expired()) return;
        if(nodes.Count>=maxElements) {truncated=true;nodeLimit=true;return;}
        IntPtr hwnd;int hr=WindowFromAccessibleObject(e,out hwnd);
        bool virtualMenu=false;
        if(hr>=0&&hwnd!=IntPtr.Zero&&!InScope(hwnd)&&parent>=0) virtualMenu=VirtualMenuScope(e,child,hwnd);
        if(!virtualMenu&&!AcceptMsaaWindow(hr,hwnd)) {
            if(hr>=0&&hwnd!=IntPtr.Zero) scopeExclusions.Add(D("source","direct_msaa",
                "parent_node_id",parent,"reported_window_id",hwnd.ToInt64(),"reason","outside_native_window_scope"));
            return;
        }
        string identityKey=AccessibleIdentity(e,child);
        if(!msaaVisited.Add(identityKey)) {
            providerErrors=true;errors.Add(D("status","unknown","reason","msaa_provider_cycle"));return;
        }
        int state=0;bool safe=false;
        object protection;
        try {state=Convert.ToInt32(e.get_accState(child));bool value=(state&0x20000000)!=0||NativePassword(hwnd);safe=!value;protection=D("status","observed","value",value);}
        catch(Exception error) {protection=Failure(error,"msaa_protection_unknown");}
        int index=nodes.Count;
        // AccessibleChildren does not disclose an OBJID for returned COM
        // objects. Keep the known root request separate instead of inventing
        // OBJID_CLIENT for every nested accessible object.
        var properties=D("object_id",depth==0?(object)objectId:null,"root_object_id",objectId,
            "child_id",child is int?child:null,"native_window_handle",hwnd.ToInt64(),
            "window_binding",virtualMenu?"direct_accessible_children_edge_and_target_bounds":"direct_or_descendant",
            "is_password",protection,"state",Read(delegate{return e.get_accState(child);}),
            "role",Read(delegate{return e.get_accRole(child);}),
            "location",Read(delegate{int x,y,w,h;e.accLocation(out x,out y,out w,out h,child);return Box(x,y,w,h);}));
        if(safe) {
            properties["name"]=Read(delegate{return Text(e.get_accName(child));});
            properties["value"]=Read(delegate{return Text(e.get_accValue(child));});
            properties["default_action"]=Read(delegate{return Text(e.get_accDefaultAction(child));});
        } else {properties["content_redacted"]=true;suppressed++;}
        var row=D("node_id",index,"parent_id",parent<0?null:(object)parent,"depth",depth,
            "properties",properties,"source","direct_msaa","read_only",true,"authorizes_input",false,
            "identity_lifetime","current_diagnostic_only");
        nodes.Add(row);
        if(!safe) {row["children_status"]="redacted";return;}
        if(Expired()) {row["children_status"]="time_budget";return;}
        if(!(child is int)||(int)child!=0) {row["children_status"]="simple_child";return;}
        try {
            int count=e.accChildCount;
            if(count<0) throw new ArgumentOutOfRangeException("child_count");
            row["reported_child_count"]=count;
            if(count==0) {row["children_status"]="provider_returned_zero";return;}
            if(depth>=maxDepth) {truncated=true;depthLimit=true;row["children_status"]="depth_limit";return;}
            int take=Math.Min(count,maxElements-nodes.Count);
            if(take<count) {truncated=true;nodeLimit=true;}
            object[] children=new object[take];int obtained;
            hr=AccessibleChildren(e,0,take,children,out obtained);
            if(hr<0) Marshal.ThrowExceptionForHR(hr);
            if(obtained!=take) {providerErrors=true;errors.Add(D("status","unknown","reason","msaa_incomplete_children","hresult",hr));}
            row["children_status"]="enumerated";
            for(int i=0;i<obtained&&i<take;i++) {
                if(Expired()) {row["children_status"]="time_budget";return;}
                IAccessible nested=children[i] as IAccessible;
                if(nested!=null) {
                    if(index==0) directMenuChildren.Add(AccessibleIdentity(nested,0));
                    AddMsaa(nested,0,index,depth+1,objectId);
                }
                else if(children[i] is int&&(int)children[i]>0) {
                    if(index==0) directMenuChildren.Add(AccessibleIdentity(e,children[i]));
                    AddMsaa(e,children[i],index,depth+1,objectId);
                }
                else {providerErrors=true;errors.Add(D("status","unknown","reason","msaa_invalid_child"));}
            }
        } catch(Exception error) {row["children_status"]="provider_error";errors.Add(Failure(error,"msaa_children_read_failed"));}
    }
    static string AccessibleIdentity(IAccessible accessible,object child) {
        IntPtr identity=Marshal.GetIUnknownForObject(accessible);
        try {return identity.ToInt64().ToString()+":"+Convert.ToString(child);}
        finally {Marshal.Release(identity);}
    }
    static bool VirtualMenuScope(IAccessible e,object child,IntPtr reported) {
        // ToolStrip submenu items can report the top menu HWND rather than
        // their popup; accParent can also point to the launching menu item.
        // Permit only COM identities JUST returned by this approved root's
        // AccessibleChildren, with popup/menu-item roles, a live same-thread
        // HWND and bounds wholly inside the approved popup. This proves a
        // read-only provider edge, not a native input HWND or durable selector.
        if(msaaRoot==null||Expired()) return false;
        if(!directMenuChildren.Contains(AccessibleIdentity(e,child))) return false;
        uint actual,rootPid;
        uint thread=GetWindowThreadProcessId(reported,out actual),rootThread=GetWindowThreadProcessId(target,out rootPid);
        if(!IsWindow(reported)||actual!=pid||rootPid!=pid||thread==0||thread!=rootThread) return false;
        try {
            if(Convert.ToInt32(msaaRoot.get_accRole(0))!=11||Convert.ToInt32(e.get_accRole(child))!=12) return false;
            int x,y,w,h;e.accLocation(out x,out y,out w,out h,child);
            Rect bounds;if(!GetWindowRect(target,out bounds)) return false;
            return BoundsWithin(x,y,w,h,bounds);
        } catch(Exception error) {errors.Add(Failure(error,"msaa_menu_scope_unproven"));return false;}
    }
    static bool BoundsWithin(int x,int y,int w,int h,Rect bounds) {
        return w>0&&h>0&&x>=bounds.left&&y>=bounds.top&&
            (long)x+w<=bounds.right&&(long)y+h<=bounds.bottom;
    }
    static bool AcceptMsaaWindow(int hr,IntPtr hwnd) {
        if(hr<0||hwnd==IntPtr.Zero) {
            providerErrors=true;
            errors.Add(D("status","unknown","reason","msaa_window_binding_failed","hresult",hr));
            return false;
        }
        if(!InScope(hwnd)) {outside++;return false;}
        return true;
    }
    [STAThread] static int Main() {
        string source="unknown";
        try {
            Console.InputEncoding=System.Text.Encoding.UTF8;Console.OutputEncoding=new System.Text.UTF8Encoding(false);
            try {SetProcessDpiAwarenessContext(new IntPtr(-4));} catch(EntryPointNotFoundException) {}
            var request=Json.Deserialize<Dictionary<string,object>>(Console.ReadLine());
            pid=Convert.ToInt32(request["pid"]);target=new IntPtr(Convert.ToInt64(request["window_id"]));
            source=(string)request["source"];maxDepth=Convert.ToInt32(request["max_depth"]);maxElements=Convert.ToInt32(request["max_elements"]);
            if(request.ContainsKey("time_budget_ms")) timeBudget=Convert.ToInt32(request["time_budget_ms"]);
            if(timeBudget<1||timeBudget>30000) throw new ArgumentException("invalid_time_budget");
            elapsed.Restart();
            if(pid<1||maxDepth<1||maxDepth>32||maxElements<1||maxElements>2000) throw new ArgumentException("invalid_request");
            Check();Rect before;if(!GetWindowRect(target,out before)) throw new InvalidOperationException("geometry_unavailable");
            if(source=="direct_msaa") {
                object raw;Guid iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
                int hr=AccessibleObjectFromWindow(target,unchecked((uint)-4),ref iid,out raw);
                if(hr<0) Marshal.ThrowExceptionForHR(hr);
                IAccessible accessible=raw as IAccessible;
                if(accessible==null) throw new InvalidOperationException("msaa_provider_unavailable");
                msaaRoot=accessible;
                AddMsaa(accessible,0,-1,0,-4);
            } else {
                TreeWalker walker=source=="uia_raw"?TreeWalker.RawViewWalker:source=="uia_control"?TreeWalker.ControlViewWalker:source=="uia_content"?TreeWalker.ContentViewWalker:null;
                if(walker==null) throw new ArgumentException("invalid_source");
                AddUia(AutomationElement.FromHandle(target),-1,0,walker);
            }
            Check();Rect after;if(!GetWindowRect(target,out after)||before.left!=after.left||before.top!=after.top||before.right!=after.right||before.bottom!=after.bottom)
                throw new InvalidOperationException("geometry_changed");
            bool complete=!truncated&&!providerErrors&&textTruncated==0&&outside==0;
            var limits=new List<string>();if(timeLimit)limits.Add("time_budget");if(nodeLimit)limits.Add("max_elements");if(depthLimit)limits.Add("max_depth");
            Console.WriteLine(Json.Serialize(D("pid",pid,"window_id",target.ToInt64(),"source",source,
                "status",!complete?"partial":nodes.Count==0?"empty":"ok","complete",complete,
                "truncated",truncated,"limit_reasons",limits,"text_truncated_count",textTruncated,"provider_errors",errors,"outside_scope_count",outside,
                "scope_exclusions",scopeExclusions,"redacted_count",suppressed,
                "observed_count",nodes.Count,"elements",nodes,"read_only",true,"authorizes_input",false,
                "frame",Box(before.left,before.top,before.right-before.left,before.bottom-before.top))));
            return 0;
        } catch(Exception error) {
            Console.WriteLine(Json.Serialize(D("pid",pid,"window_id",target.ToInt64(),"source",source,
                "status","error","complete",false,"elements",new object[0],"observed_count",null,
                "read_only",true,"authorizes_input",false,"diagnostic",D("code",error is COMException?"provider_error":
                    error.Message=="target_changed"?"target_changed":error.Message=="geometry_changed"?"geometry_changed":"native_read_failed","hresult",error.HResult))));
            return 1;
        }
    }
}
