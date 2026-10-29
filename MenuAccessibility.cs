// Local menu-only observation/action bridge. One bounded request per process.
// Not an authorization boundary: the Python caller supplies Guard approvals.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using Accessibility;

[ComImport, Guid("7852B78D-1CFD-41C1-A615-9C0C85960B5F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMenuAccIdentity {
    [PreserveSig] int GetIdentityString(uint child, out IntPtr identity, out uint length);
}

class MenuAccessibility {
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int x, y; public POINT(int a,int b) {x=a;y=b;} }
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd,out RECT rect);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd,StringBuilder text,int size);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x,int y);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT point);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT {public int dx,dy;public uint mouseData,dwFlags,time;public IntPtr extraInfo;}
    [StructLayout(LayoutKind.Explicit)] struct INPUTUNION {[FieldOffset(0)]public MOUSEINPUT mouse;}
    [StructLayout(LayoutKind.Sequential)] struct INPUT {public uint type;public INPUTUNION data;}
    [DllImport("user32.dll",SetLastError=true)] static extern uint SendInput(uint count,INPUT[] inputs,int size);
    [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd,uint objectId,ref Guid iid,[MarshalAs(UnmanagedType.Interface)]out object value);
    [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible parent,int start,int count,[Out,MarshalAs(UnmanagedType.LPArray,SizeParamIndex=2)]object[] children,out int obtained);
    [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(IAccessible accessible,out IntPtr hwnd);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer {MaxJsonLength=8388608,RecursionLimit=128};
    static readonly Stopwatch Timer = Stopwatch.StartNew();
    static readonly List<Dictionary<string,object>> Rows = new List<Dictionary<string,object>>();
    static readonly List<object> Errors = new List<object>();
    static readonly List<string> Limits = new List<string>();
    static readonly Dictionary<int,AutomationElement> Uia = new Dictionary<int,AutomationElement>();
    static readonly Dictionary<int,Tuple<IAccessible,object>> Msaa = new Dictionary<int,Tuple<IAccessible,object>>();
    static readonly HashSet<string> Seen = new HashSet<string>();
    static IntPtr Target;
    static int Pid, MaxDepth, MaxElements, Budget;
    static string Source="unknown", Operation="observe";
    static bool Truncated, Attempted;
    static Dictionary<string,object> Fingerprint;
    static IAccessible MsaaRoot;
    static long Started;

    static Dictionary<string,object> D(params object[] items) {
        var d=new Dictionary<string,object>(); for(int i=0;i<items.Length;i+=2)d[(string)items[i]]=items[i+1];return d;
    }
    static Dictionary<string,object> Obj(object value) {return (Dictionary<string,object>)value;}
    static long Now() {return (long)(DateTime.UtcNow-new DateTime(1970,1,1)).TotalMilliseconds;}
    static string Text(object value) {
        string text=value==null?null:Convert.ToString(value);
        if(text!=null&&text.Length>1000) throw new InvalidOperationException("property_too_large");return text;
    }
    static void Error(string code,Exception error=null) {Errors.Add(D("code",code,"hresult",error==null?null:(object)error.HResult));}
    static bool Limit(string reason) {Truncated=true;if(!Limits.Contains(reason))Limits.Add(reason);return true;}
    static bool Expired() {return Timer.ElapsedMilliseconds>=Budget&&Limit("time_budget");}
    static void Check() {
        uint actual;if(!IsWindow(Target)||GetWindowThreadProcessId(Target,out actual)==0||actual!=Pid)
            throw new InvalidOperationException("menu_accessibility_target_changed");
    }
    static bool Scope(IntPtr hwnd) {
        uint actual;return hwnd!=IntPtr.Zero&&IsWindow(hwnd)&&GetWindowThreadProcessId(hwnd,out actual)!=0&&actual==Pid&&(hwnd==Target||IsChild(Target,hwnd));
    }
    static Dictionary<string,object> Box(double x,double y,double w,double h) {
        foreach(double n in new[]{x,y,w,h}) if(Double.IsNaN(n)||Double.IsInfinity(n))throw new InvalidOperationException("geometry_unavailable");
        return D("x",x,"y",y,"width",w,"height",h,"coordinate_space","desktop_physical");
    }
    static bool Positive(Dictionary<string,object> bounds) {return Convert.ToDouble(bounds["width"])>0&&Convert.ToDouble(bounds["height"])>0;}
    static Dictionary<string,object> NativeFingerprint() {
        Check();uint pid;uint thread=GetWindowThreadProcessId(Target,out pid);RECT box;
        if(!GetWindowRect(Target,out box))throw new InvalidOperationException("geometry_unavailable");
        var name=new StringBuilder(512);if(GetClassName(Target,name,name.Capacity)==0)throw new InvalidOperationException("class_unavailable");
        long ticks;using(var process=Process.GetProcessById(Pid))ticks=process.StartTime.ToUniversalTime().Ticks;
        return D("pid",Pid,"window_id",Target.ToInt64(),"thread_id",thread,"class_name",name.ToString(),
            "process_start_ticks",ticks.ToString(),"bounds",Box(box.left,box.top,box.right-box.left,box.bottom-box.top));
    }
    static bool Equal(object a,object b) {
        if(a==null||b==null)return a==b;
        var da=a as IDictionary;var db=b as IDictionary;
        if(da!=null||db!=null) {
            if(da==null||db==null||da.Count!=db.Count)return false;
            foreach(object key in da.Keys)if(!db.Contains(key)||!Equal(da[key],db[key]))return false;
            return true;
        }
        var aa=a as IList;var ab=b as IList;
        if(aa!=null||ab!=null) {
            if(aa==null||ab==null||aa.Count!=ab.Count)return false;
            for(int i=0;i<aa.Count;i++)if(!Equal(aa[i],ab[i]))return false;return true;
        }
        if(a is string||b is string||a is bool||b is bool)return a.Equals(b);
        return Convert.ToDecimal(a)==Convert.ToDecimal(b);
    }
    static string RuntimeIdentity(AutomationElement element) {
        var ids=element.GetRuntimeId();if(ids==null||ids.Length==0)throw new InvalidOperationException("identity_unavailable");
        return String.Join(",",Array.ConvertAll(ids,n=>n.ToString()));
    }
    static string ComIdentity(IAccessible accessible,object child) {
        IntPtr identity=Marshal.GetIUnknownForObject(accessible);
        try{return identity.ToInt64()+":"+Convert.ToString(child);}finally{Marshal.Release(identity);}
    }
    static string PersistentMsaaIdentity(IAccessible accessible,object child) {
        // IUnknown addresses are request-local and NEVER cross the helper pipe.
        // Without optional IAccIdentity, direct default-action execution fails
        // closed; the observed menu can still be hit-tested for guarded hover.
        var provider=accessible as IMenuAccIdentity;if(provider==null||!(child is int))return null;
        IntPtr bytes=IntPtr.Zero;uint count=0;
        try {
            int hr=provider.GetIdentityString((uint)(int)child,out bytes,out count);
            if(hr<0||bytes==IntPtr.Zero||count<1||count>4096)return null;
            var data=new byte[(int)count];Marshal.Copy(bytes,data,0,(int)count);return Convert.ToBase64String(data);
        } catch(COMException){return null;} finally {if(bytes!=IntPtr.Zero)Marshal.FreeCoTaskMem(bytes);}
    }
    static List<int> ChildPath(List<int> path,int child) {var result=new List<int>(path);result.Add(child);return result;}
    static List<object> PathSignatures(int parent,Dictionary<string,object> signature) {
        var result=parent<0?new List<object>():new List<object>((List<object>)Obj(Rows[parent]["expected"])["path"]);
        result.Add(signature);return result;
    }
    static Dictionary<string,object> Row(int parent,int depth,List<int> path,Dictionary<string,object> signature,List<string> actions) {
        var row=new Dictionary<string,object>(signature);
        row["element_index"]=Rows.Count;row["index"]=Rows.Count;row["parent_index"]=parent;row["depth"]=depth;
        row["source"]=Source;row["verification_only"]=true;row["authorizes_input"]=false;
        row["identity_lifetime"]="current_menu_observation_only";row["supported_actions"]=actions;
        row["locator"]=D("source",Source,"path",path,"root_object_id",-4);
        row["expected"]=D("target",Fingerprint,"path",PathSignatures(parent,signature),"observed_at_ms",Started);
        row["evidence"]=row["expected"];
        Rows.Add(row);return row;
    }
    static bool ParentMenu(Dictionary<string,object> row) {
        return Convert.ToString(row["role"])=="MenuItem"&&
            (Equal(row["has_popup"],true)||Equal(row["has_children"],true)||Convert.ToString(row["expansion_state"])=="Collapsed"||
             Convert.ToString(row["expansion_state"])=="Expanded"||Convert.ToString(row["expansion_state"])=="PartiallyExpanded");
    }
    static Dictionary<string,object> UiaSignature(AutomationElement element) {
        var c=element.Current;
        if(c.ProcessId!=Pid||(c.NativeWindowHandle!=0&&!Scope(new IntPtr(c.NativeWindowHandle))))
            throw new InvalidOperationException("outside_window_scope");
        var role=c.ControlType.ProgrammaticName.Replace("ControlType.","");
        if(c.IsPassword)throw new InvalidOperationException("protected_subtree");
        object pattern;string expansion=null;bool canExpand=element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern,out pattern);
        if(canExpand)expansion=((ExpandCollapsePattern)pattern).Current.ExpandCollapseState.ToString();
        bool invoke=element.TryGetCurrentPattern(InvokePattern.Pattern,out pattern);
        object legacyState=null;var legacyProperty=AutomationProperty.LookupById(30096);
        if(legacyProperty!=null) {
            var value=element.GetCurrentPropertyValue(legacyProperty,true);
            if(value!=AutomationElement.NotSupported&&value!=null)legacyState=Convert.ToInt32(value);
        }
        object popup=canExpand?(object)(expansion!="LeafNode"):legacyState==null?null:(object)((Convert.ToInt32(legacyState)&0x40000000)!=0);
        var b=c.BoundingRectangle;
        return D("name",Text(c.Name),"role",role,"enabled",c.IsEnabled,"visible",!c.IsOffscreen,
            "bounds",Box(b.X,b.Y,b.Width,b.Height),"native_window_id",c.NativeWindowHandle,
            "identity",RuntimeIdentity(element),"identity_kind","uia_runtime_id",
            "expansion_state",expansion,"has_popup",popup,"legacy_state",legacyState,
            "default_action",null,"invoke_supported",invoke,"expand_supported",canExpand,
            "has_children",TreeWalker.RawViewWalker.GetFirstChild(element)!=null);
    }
    static List<string> Actions(Dictionary<string,object> signature) {
        var actions=new List<string>();
        if(Convert.ToString(signature["role"])!="MenuItem"||!Equal(signature["enabled"],true)||!Equal(signature["visible"],true)
            ||!Positive(Obj(signature["bounds"])))return actions;
        bool parent=ParentMenu(signature);
        if(parent){actions.Add("hover");actions.Add("click_parent");}
        else if(Equal(signature["has_popup"],false)&&Equal(signature["has_children"],false))actions.Add("click_leaf");
        if(Source=="uia_raw") {
            if(parent&&Equal(signature["expand_supported"],true)&&Convert.ToString(signature["expansion_state"])!="LeafNode")actions.Add("expand");
            if(!parent&&Equal(signature["has_popup"],false)&&!Equal(signature["has_children"],true)&&Equal(signature["invoke_supported"],true))actions.Add("invoke");
        } else if(signature["identity"]!=null&&!String.IsNullOrEmpty(Convert.ToString(signature["default_action"]))) {
            if(parent&&Equal(signature["expand_supported"],true))actions.Add("expand");
            else if(!parent&&Equal(signature["has_children"],false))actions.Add("invoke");
        }
        return actions;
    }
    static void WalkUia(AutomationElement element,int parent,int depth,List<int> path) {
        if(Expired())return;if(Rows.Count>=MaxElements){Limit("max_elements");return;}
        Dictionary<string,object> signature;
        try{signature=UiaSignature(element);}catch(Exception error){Error("uia_element_read_failed",error);return;}
        if(!Seen.Add((string)signature["identity"])){Error("uia_provider_cycle");return;}
        int index=Rows.Count;var row=Row(parent,depth,path,signature,Actions(signature));Uia[index]=element;
        try {
            var child=TreeWalker.RawViewWalker.GetFirstChild(element);
            if(child!=null&&depth>=MaxDepth){Limit("max_depth");row["children_status"]="depth_limit";return;}
            row["children_status"]=child==null?"provider_returned_zero":"enumerated";
            for(int childIndex=0;child!=null;childIndex++) {
                if(Expired())return;if(Rows.Count>=MaxElements){Limit("max_elements");return;}
                WalkUia(child,index,depth+1,ChildPath(path,childIndex));
                child=TreeWalker.RawViewWalker.GetNextSibling(child);
            }
        } catch(Exception error){row["children_status"]="provider_error";Error("uia_children_read_failed",error);}
    }
    static bool MsaaScope(IAccessible accessible,object child,int parent,out long native) {
        IntPtr hwnd;int hr=WindowFromAccessibleObject(accessible,out hwnd);native=hwnd.ToInt64();
        if(hr<0||hwnd==IntPtr.Zero)return false;if(Scope(hwnd))return true;
        // ToolStrip popup items can report the launching menubar HWND. Accept
        // only an actual direct child edge from this popup, same native thread,
        // menu roles and positive physical bounds contained in this popup.
        if(parent!=0||MsaaRoot==null||Convert.ToInt32(MsaaRoot.get_accRole(0))!=11||Convert.ToInt32(accessible.get_accRole(child))!=12)return false;
        uint actual,rootPid;uint thread=GetWindowThreadProcessId(hwnd,out actual),rootThread=GetWindowThreadProcessId(Target,out rootPid);
        if(!IsWindow(hwnd)||thread==0||thread!=rootThread||actual!=Pid||rootPid!=Pid)return false;
        int x,y,w,h;accessible.accLocation(out x,out y,out w,out h,child);RECT frame;
        return GetWindowRect(Target,out frame)&&w>0&&h>0&&x>=frame.left&&y>=frame.top&&(long)x+w<=frame.right&&(long)y+h<=frame.bottom;
    }
    static string MsaaRole(int role) {
        switch(role){case 2:return "MenuBar";case 9:return "Window";case 10:return "Pane";case 11:return "Menu";case 12:return "MenuItem";case 21:return "Separator";default:return "MsaaRole"+role;}
    }
    static Dictionary<string,object> MsaaSignature(IAccessible accessible,object child,int parent) {
        long hwnd;if(!MsaaScope(accessible,child,parent,out hwnd))throw new InvalidOperationException("outside_window_scope");
        int state=Convert.ToInt32(accessible.get_accState(child)),role=Convert.ToInt32(accessible.get_accRole(child));
        if((state&0x20000000)!=0)throw new InvalidOperationException("protected_subtree");
        int x,y,w,h;accessible.accLocation(out x,out y,out w,out h,child);
        string action=null;
        try{action=Text(accessible.get_accDefaultAction(child));}
        catch(COMException error){if(error.HResult!=unchecked((int)0x80020003)&&error.HResult!=unchecked((int)0x80004001))throw;}
        string identity=PersistentMsaaIdentity(accessible,child);
        return D("name",Text(accessible.get_accName(child)),"role",MsaaRole(role),"role_id",role,
            "enabled",(state&1)==0,"visible",(state&0x18000)==0,"state",state,
            "bounds",Box(x,y,w,h),"native_window_id",hwnd,"child_id",child,
            "identity",identity,"identity_kind",identity==null?"path_and_current_properties_only":"iacc_identity",
            "expansion_state",(state&0x200)!=0?"Expanded":(state&0x400)!=0?"Collapsed":null,
            "has_popup",(state&0x40000000)!=0,"default_action",action,
            "invoke_supported",identity!=null&&!String.IsNullOrEmpty(action)&&(state&0x40000000)==0,
            "expand_supported",identity!=null&&!String.IsNullOrEmpty(action)&&(state&0x40000000)!=0,
            "has_children",child is int&&(int)child==0?accessible.accChildCount>0:false);
    }
    static void WalkMsaa(IAccessible accessible,object child,int parent,int depth,List<int> path) {
        if(Expired())return;if(Rows.Count>=MaxElements){Limit("max_elements");return;}
        Dictionary<string,object> signature;
        try{signature=MsaaSignature(accessible,child,parent);}catch(Exception error){Error("msaa_element_read_failed",error);return;}
        if(!Seen.Add(ComIdentity(accessible,child))){Error("msaa_provider_cycle");return;}
        int index=Rows.Count;var row=Row(parent,depth,path,signature,Actions(signature));Msaa[index]=Tuple.Create(accessible,child);
        if(!(child is int)||(int)child!=0){row["children_status"]="simple_child";return;}
        try {
            int count=accessible.accChildCount;if(count<0)throw new InvalidOperationException("invalid_child_count");
            row["reported_child_count"]=count;row["children_status"]=count==0?"provider_returned_zero":"enumerated";
            if(count==0)return;if(depth>=MaxDepth){Limit("max_depth");row["children_status"]="depth_limit";return;}
            int take=Math.Min(count,MaxElements-Rows.Count);if(take<count)Limit("max_elements");
            if(take==0)return;var children=new object[take];int obtained;
            int hr=AccessibleChildren(accessible,0,take,children,out obtained);if(hr<0)Marshal.ThrowExceptionForHR(hr);
            if(obtained!=take){Error("msaa_incomplete_children");}
            for(int i=0;i<obtained&&i<take;i++) {
                if(Expired())return;var nested=children[i] as IAccessible;
                if(nested!=null)WalkMsaa(nested,0,index,depth+1,ChildPath(path,i));
                else if(children[i] is int&&(int)children[i]>0)WalkMsaa(accessible,children[i],index,depth+1,ChildPath(path,i));
                else Error("msaa_invalid_child");
            }
        } catch(Exception error){row["children_status"]="provider_error";Error("msaa_children_read_failed",error);}
    }
    static void Observe() {
        Fingerprint=NativeFingerprint();Started=Now();
        if(Source=="uia_raw")WalkUia(AutomationElement.FromHandle(Target),-1,0,new List<int>());
        else {
            object raw;var iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            int hr=AccessibleObjectFromWindow(Target,unchecked((uint)-4),ref iid,out raw);if(hr<0)Marshal.ThrowExceptionForHR(hr);
            MsaaRoot=raw as IAccessible;if(MsaaRoot==null)throw new InvalidOperationException("msaa_provider_unavailable");
            WalkMsaa(MsaaRoot,0,-1,0,new List<int>());
        }
        NormalizeMenuChildren();
        if(!Equal(Fingerprint,NativeFingerprint()))throw new InvalidOperationException("menu_accessibility_target_changed");
    }
    static void NormalizeMenuChildren() {
        // A raw UIA text/label child is not a submenu. Preserve raw child
        // presence in exact evidence, but use actual menu descendants and
        // explicit provider popup/leaf state for action eligibility.
        var menuParents=new HashSet<int>();
        foreach(var row in Rows) {
            string role=Convert.ToString(row["role"]);
            if(role!="MenuItem"&&role!="Menu")continue;
            int at=Convert.ToInt32(row["parent_index"]);
            while(at>=0) {
                if(Convert.ToString(Rows[at]["role"])=="MenuItem"){menuParents.Add(at);break;}
                at=Convert.ToInt32(Rows[at]["parent_index"]);
            }
        }
        foreach(var row in Rows) {
            row["provider_has_children"]=row["has_children"];
            if(Convert.ToString(row["role"])=="MenuItem") {
                bool knownParent=menuParents.Contains(Convert.ToInt32(row["element_index"]))||Equal(row["has_popup"],true);
                row["has_children"]=knownParent?(object)true:Equal(row["has_popup"],false)?(object)false:null;
            }
            row["supported_actions"]=Actions(row);
        }
    }
    static Dictionary<string,object> Base() {return D("pid",Pid,"window_id",Target.ToInt64(),"source",Source,"authorizes_input",false);}
    static Dictionary<string,object> Observation() {
        Observe();var result=Base();bool complete=!Truncated&&Errors.Count==0;int menus=0;
        foreach(var row in Rows)if(Convert.ToString(row["role"])=="MenuItem")menus++;
        result["status"]=!complete?"partial":menus==0?"empty":"ok";result["complete"]=complete;
        result["truncated"]=Truncated;result["limit_reasons"]=Limits;result["provider_errors"]=Errors;
        result["elements"]=Rows;result["menu_item_count"]=menus;result["read_only"]=true;return result;
    }
    static void Fresh(Dictionary<string,object> expected,Dictionary<string,object> row) {
        long age=Now()-Convert.ToInt64(expected["observed_at_ms"]);
        if(age < -500||age>10000)throw new InvalidOperationException("menu_accessibility_stale_evidence");
        if(!Equal(expected["target"],Fingerprint)||!Equal(expected["path"],Obj(row["expected"])["path"]))
            throw new InvalidOperationException("menu_accessibility_evidence_changed");
        if(!IsWindowVisible(Target)||!IsWindowEnabled(Target)||!Equal(row["enabled"],true)||!Equal(row["visible"],true))
            throw new InvalidOperationException("menu_accessibility_not_actionable");
    }
    static Dictionary<string,object> Action(Dictionary<string,object> request) {
        var locator=Obj(request["locator"]);var expected=Obj(request["expected"]);
        if(Convert.ToString(locator["source"])!=Source||Convert.ToInt32(locator["root_object_id"])!=-4)
            throw new InvalidOperationException("menu_accessibility_invalid_locator");
        // Resolve a fresh complete provider tree and its real parent edges.
        Observe();if(Truncated||Errors.Count>0)throw new InvalidOperationException("menu_accessibility_incomplete_reobservation");
        Dictionary<string,object> chosen=null;
        foreach(var row in Rows)if(Equal(Obj(row["locator"])["path"],locator["path"])) {
            if(chosen!=null)throw new InvalidOperationException("menu_accessibility_ambiguous");chosen=row;
        }
        if(chosen==null)throw new InvalidOperationException("menu_accessibility_not_found");
        Fresh(expected,chosen);
        if(!((List<string>)chosen["supported_actions"]).Contains(Operation))throw new InvalidOperationException("menu_accessibility_action_unsupported");
        int index=Convert.ToInt32(chosen["element_index"]);
        // Re-read the target and every ancestor immediately before mutation.
        for(int at=index;at>=0;at=Convert.ToInt32(Rows[at]["parent_index"])) {
            var row=Rows[at];var signature=Source=="uia_raw"?UiaSignature(Uia[at]):MsaaSignature(Msaa[at].Item1,Msaa[at].Item2,Convert.ToInt32(row["parent_index"]));
            var path=(List<object>)Obj(row["expected"])["path"];
            if(!Equal(signature,path[path.Count-1]))throw new InvalidOperationException("menu_accessibility_evidence_changed");
        }
        if(!Equal(Fingerprint,NativeFingerprint())||Expired())throw new InvalidOperationException("menu_accessibility_target_changed");
        Fresh(expected,chosen);
        if(Operation=="expand"&&Convert.ToString(chosen["expansion_state"])=="Expanded")return ActionResult("not_needed",false);
        if(Operation=="hover"||Operation=="click_parent"||Operation=="click_leaf") {
            var bounds=Obj(chosen["bounds"]);double x=Convert.ToDouble(bounds["x"])+Convert.ToDouble(bounds["width"])/2;
            double y=Convert.ToDouble(bounds["y"])+Convert.ToDouble(bounds["height"])/2;
            if(x<Int32.MinValue||x>Int32.MaxValue||y<Int32.MinValue||y>Int32.MaxValue)throw new InvalidOperationException("menu_accessibility_geometry_invalid");
            var point=new POINT((int)x,(int)y);
            int screenX=GetSystemMetrics(76),screenY=GetSystemMetrics(77),screenW=GetSystemMetrics(78),screenH=GetSystemMetrics(79);
            if(screenW<=0||screenH<=0||point.x<screenX||point.y<screenY||(long)point.x>=((long)screenX+screenW)||(long)point.y>=((long)screenY+screenH))
                throw new InvalidOperationException("menu_accessibility_offscreen");
            if(!Scope(WindowFromPoint(point)))throw new InvalidOperationException("menu_accessibility_occluded");
            if(Source=="uia_raw") {
                var hit=AutomationElement.FromPoint(new System.Windows.Point(point.x,point.y));
                if(hit==null||RuntimeIdentity(hit)!=Convert.ToString(chosen["identity"]))throw new InvalidOperationException("menu_accessibility_hit_changed");
            } else {
                var item=Msaa[index];object hit=MsaaRoot.accHitTest(point.x,point.y);var hitObject=hit as IAccessible;
                string actual=hitObject==null?ComIdentity(MsaaRoot,hit):ComIdentity(hitObject,0);
                if(actual!=ComIdentity(item.Item1,item.Item2))throw new InvalidOperationException("menu_accessibility_hit_changed");
            }
            Attempted=true;if(!SetCursorPos(point.x,point.y))throw new InvalidOperationException("menu_accessibility_hover_failed");
            POINT after;if(!GetCursorPos(out after)||after.x!=point.x||after.y!=point.y)throw new InvalidOperationException("menu_accessibility_hover_unconfirmed");
            if(Operation!="hover") {
                // Moving the pointer may itself expand a menu. Never click a
                // replacement element after that transition; stop on any change.
                var current=Source=="uia_raw"?UiaSignature(Uia[index]):MsaaSignature(Msaa[index].Item1,Msaa[index].Item2,Convert.ToInt32(chosen["parent_index"]));
                var expectedPath=(List<object>)Obj(chosen["expected"])["path"];
                if(!Equal(current,expectedPath[expectedPath.Count-1])||!Equal(Fingerprint,NativeFingerprint())||Expired()||
                    !GetCursorPos(out after)||after.x!=point.x||after.y!=point.y||!Scope(WindowFromPoint(point)))
                    throw new InvalidOperationException("menu_accessibility_click_target_changed");
                var inputs=new[]{new INPUT{type=0,data=new INPUTUNION{mouse=new MOUSEINPUT{dwFlags=0x0002}}},
                    new INPUT{type=0,data=new INPUTUNION{mouse=new MOUSEINPUT{dwFlags=0x0004}}}};
                uint delivered=SendInput(2,inputs,Marshal.SizeOf(typeof(INPUT)));
                if(delivered!=2) {
                    // If only mouse-down was accepted, release it once as
                    // cleanup. This never reissues the menu command's down.
                    if(delivered==1)SendInput(1,new[]{inputs[1]},Marshal.SizeOf(typeof(INPUT)));
                    throw new InvalidOperationException("menu_accessibility_click_unconfirmed");
                }
            }
        } else if(Source=="uia_raw") {
            object pattern;
            if(!Uia[index].TryGetCurrentPattern(Operation=="expand"?ExpandCollapsePattern.Pattern:InvokePattern.Pattern,out pattern))
                throw new InvalidOperationException("menu_accessibility_action_unsupported");
            Attempted=true;
            if(Operation=="expand")((ExpandCollapsePattern)pattern).Expand();else ((InvokePattern)pattern).Invoke();
        } else {
            if(chosen["identity"]==null)throw new InvalidOperationException("menu_accessibility_identity_unavailable");
            Attempted=true;Msaa[index].Item1.accDoDefaultAction(Msaa[index].Item2);
        }
        return ActionResult("sent",true);
    }
    static Dictionary<string,object> ActionResult(string delivery,object dispatched) {
        var result=Base();result["status"]="ok";result["operation"]=Operation;result["delivery"]=delivery;
        result["input_dispatched"]=dispatched;result["business_result_verified"]=false;result["read_only"]=false;
        result["revalidated"]=true;result["accepted"]=true;result["task_verified"]=false;return result;
    }
    [STAThread] static int Main() {
        Console.InputEncoding=Encoding.UTF8;Console.OutputEncoding=new UTF8Encoding(false);
        try {
            try{SetProcessDpiAwarenessContext(new IntPtr(-4));}catch(EntryPointNotFoundException){}
            string line=Console.ReadLine();if(line==null||line.Length>524288)throw new InvalidOperationException("request_too_large");
            var request=Json.Deserialize<Dictionary<string,object>>(line);
            Pid=Convert.ToInt32(request["pid"]);Target=new IntPtr(Convert.ToInt64(request["window_id"]));
            Source=Convert.ToString(request["source"]);Operation=Convert.ToString(request["operation"]);
            MaxDepth=Convert.ToInt32(request["max_depth"]);MaxElements=Convert.ToInt32(request["max_elements"]);Budget=Convert.ToInt32(request["time_budget_ms"]);
            if(Pid<1||Target==IntPtr.Zero||(Source!="uia_raw"&&Source!="direct_msaa")||MaxDepth<1||MaxDepth>32||MaxElements<1||MaxElements>2000||Budget<1||Budget>30000)
                throw new InvalidOperationException("menu_accessibility_invalid_request");
            if(Operation!="observe"&&Operation!="expand"&&Operation!="invoke"&&Operation!="hover"&&Operation!="click_parent"&&Operation!="click_leaf")throw new InvalidOperationException("menu_accessibility_invalid_operation");
            Timer.Restart();var response=Operation=="observe"?Observation():Action(request);Console.WriteLine(Json.Serialize(response));return 0;
        } catch(Exception error) {
            var result=Base();result["status"]="error";result["complete"]=false;result["truncated"]=Truncated;
            result["elements"]=new object[0];result["read_only"]=Operation=="observe";result["operation"]=Operation;
            result["diagnostic"]=D("code",error.Message.StartsWith("menu_accessibility_")?error.Message:"menu_accessibility_provider_error","hresult",error.HResult);
            result["delivery"]=Attempted?"unknown":"not_sent";result["input_dispatched"]=Attempted?null:(object)false;result["business_result_verified"]=false;
            result["accepted"]=Attempted?null:(object)false;result["task_verified"]=false;
            Console.WriteLine(Json.Serialize(result));return 1;
        }
    }
}
