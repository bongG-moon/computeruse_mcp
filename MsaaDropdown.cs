// Read-only legacy accessibility inventory. Runs in an isolated, timed process.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using Accessibility;

class MsaaDropdown {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object value);
    [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible parent, int start, int count, [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=2)] object[] children, out int obtained);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out Rect rect, int size);
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int left,top,right,bottom; }
    static Dictionary<string, object> Box(int x,int y,int w,int h) { return new Dictionary<string,object>{{"x",x},{"y",y},{"width",w},{"height",h}}; }
    static JavaScriptSerializer Json = new JavaScriptSerializer {MaxJsonLength=2097152};
    static int visited;
    static void Check(IntPtr hwnd, int pid) {uint actual; if(!IsWindow(hwnd)||GetWindowThreadProcessId(hwnd,out actual)==0||actual!=pid) throw new Exception("msaa_target_changed");}
    static void Walk(IAccessible node, object child, int depth, List<object> lists) {
        if (++visited > 2048 || depth > 32) throw new Exception("msaa_scan_limit");
        int state = Convert.ToInt32(node.get_accState(child));
        if ((state & (0x8000|0x10000|0x20000000)) != 0) return; // invisible, offscreen, protected
        int role = Convert.ToInt32(node.get_accRole(child));
        if(role==0x21) { // ROLE_SYSTEM_LIST; no assumption that it owns an HWND
            int x,y,w,h; node.accLocation(out x,out y,out w,out h,child);
            if(w>0 && h>0) lists.Add(new Dictionary<string,object>{{"role","List"},{"screen_rect",Box(x,y,w,h)}});
        }
        if (!(child is int) || (int)child != 0) return;
        int count=node.accChildCount;
        if(count<0 || count>2048 || lists.Count>32) throw new Exception("msaa_scan_limit");
        if(count==0) return;
        object[] children = new object[count]; int obtained;
        int hr=AccessibleChildren(node,0,count,children,out obtained);
        if(hr<0 || obtained!=count) throw new Exception("msaa_incomplete_tree");
        foreach(object item in children) {
            IAccessible nested=item as IAccessible;
            if(nested!=null) Walk(nested,0,depth+1,lists);
            else if(item is int && (int)item>0) Walk(node,item,depth+1,lists);
            else throw new Exception("msaa_invalid_child");
        }
    }
    [STAThread] static int Main() {
        IntPtr requested = IntPtr.Zero; int requestedPid = 0;
        try {
            SetProcessDpiAwarenessContext(new IntPtr(-4));
            Console.InputEncoding=System.Text.Encoding.UTF8; Console.OutputEncoding=new System.Text.UTF8Encoding(false);
            var request=Json.Deserialize<Dictionary<string,object>>(Console.ReadLine());
            int pid=Convert.ToInt32(request["pid"]); var hwnd=new IntPtr(Convert.ToInt64(request["window_id"]));
            requested = hwnd; requestedPid = pid;
            Check(hwnd,pid);
            if(!IsWindowVisible(hwnd)) throw new Exception("msaa_target_hidden");
            Rect outer,frame; if(!GetWindowRect(hwnd,out outer)) throw new Exception("msaa_geometry_unavailable");
            if(DwmGetWindowAttribute(hwnd,9,out frame,Marshal.SizeOf(typeof(Rect)))!=0) frame=outer;
            object raw; Guid iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            int status=AccessibleObjectFromWindow(hwnd,unchecked((uint)-4),ref iid,out raw);
            var lists=new List<object>();
            if(status<0 || !(raw is IAccessible)) throw new Exception("msaa_provider_unavailable");
            Walk((IAccessible)raw,0,0,lists);
            Check(hwnd,pid); Rect after; GetWindowRect(hwnd,out after);
            if(after.left!=outer.left||after.top!=outer.top||after.right!=outer.right||after.bottom!=outer.bottom) throw new Exception("msaa_geometry_changed");
            Console.WriteLine(Json.Serialize(new {ok=true,pid=pid,window_id=hwnd.ToInt64(),lists=lists,
                frames=new object[]{Box(frame.left,frame.top,frame.right-frame.left,frame.bottom-frame.top),Box(outer.left,outer.top,outer.right-outer.left,outer.bottom-outer.top)},
                complete=true,read_only=true,visited=visited})); return 0;
        } catch(Exception error) {
            string code = error.Message.StartsWith("msaa_") ? error.Message :
                error is COMException ? "msaa_provider_error" :
                error is ArgumentException || error is IndexOutOfRangeException ? "msaa_provider_range" : "msaa_read_failed";
            if (requested != IntPtr.Zero) { try { Check(requested, requestedPid); }
                catch { code = "msaa_target_changed"; } }
            Console.WriteLine(Json.Serialize(new {ok=false,code=code,hresult=error.HResult,read_only=true})); return 1;
        }
    }
}
