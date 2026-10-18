// Benchmark entry point using the product's unchanged ElementPicker.Observe.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
class FlaUINativePointBaseline {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [STAThread] static void Main() {
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch(EntryPointNotFoundException) {}
        Console.InputEncoding=Encoding.UTF8; Console.OutputEncoding=new UTF8Encoding(false);
        var json=new JavaScriptSerializer(); string line;
        while((line=Console.ReadLine())!=null) {
            string id="";
            try {
                var r=json.Deserialize<Dictionary<string,object>>(line); id=Convert.ToString(r["id"]);
                int pid=Convert.ToInt32(r["pid"]); long hwnd=Convert.ToInt64(r["window_id"]);
                var point=new ElementPicker.POINT { X=Convert.ToInt32(r["x"]),Y=Convert.ToInt32(r["y"]) };
                var candidates=ElementPicker.Observe(point,pid,new IntPtr(hwnd));
                var rows=new List<Dictionary<string,object>>();
                if(candidates.Count>0) {
                    var c=candidates[0]; var row=new Dictionary<string,object>(c.Identity);
                    row["observed_patterns"]=Convert.ToBoolean(c.Identity["has_control_patterns"])?new string[]{"control_pattern"}:new string[0];
                    row["bounds"]=new Dictionary<string,object>{{"x",c.Bounds.X},{"y",c.Bounds.Y},{"width",c.Bounds.Width},{"height",c.Bounds.Height}};
                    rows.Add(row);
                }
                Console.WriteLine(json.Serialize(new{id=id,ok=true,data=new{elements=rows,backend="native_picker"}}));
            } catch(Exception ex) { Console.WriteLine(json.Serialize(new{id=id,ok=false,code=ex is InvalidOperationException?ex.Message:"read_failed",exception_type=ex.GetType().Name})); }
        }
    }
}
