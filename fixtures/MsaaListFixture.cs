// Local, short-lived compatibility fixture. No business data or network.
using System;
using System.Drawing;
using System.Windows.Forms;

class LegacyListSurface : Control {
    protected override AccessibleObject CreateAccessibilityInstance() { return new SurfaceAccess(this); }
    protected override void OnPaint(PaintEventArgs e) {
        e.Graphics.Clear(Color.White);
        e.Graphics.DrawRectangle(Pens.Black,10,10,180,95);
        e.Graphics.DrawString("W\nN\nP",Font,Brushes.Black,20,20);
    }
    class SurfaceAccess : ControlAccessibleObject {
        Control control;
        public SurfaceAccess(Control c):base(c){control=c;}
        public override AccessibleRole Role {get{return AccessibleRole.Client;}}
        public override int GetChildCount(){return 1;}
        public override AccessibleObject GetChild(int index){return index==0?new ListAccess(control,this):null;}
    }
    class ListAccess : AccessibleObject {
        Control control; AccessibleObject parent;
        public ListAccess(Control c,AccessibleObject p){control=c;parent=p;}
        public override AccessibleRole Role {get{return AccessibleRole.List;}}
        public override AccessibleStates State {get{return AccessibleStates.Focusable;}}
        public override Rectangle Bounds {get{return control.RectangleToScreen(new Rectangle(10,10,180,95));}}
        public override AccessibleObject Parent {get{return parent;}}
        public override string Name {get{return "Legacy dropdown";}}
        public override int GetChildCount(){return 0;}
    }
}
class MsaaListFixture {
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [STAThread] static void Main() {
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();
        var form=new Form {Text="MSAA compatibility test",Width=260,Height=190,StartPosition=FormStartPosition.CenterScreen};
        form.Controls.Add(new LegacyListSurface {Dock=DockStyle.Fill});
        var timer=new Timer {Interval=12000}; timer.Tick+=(s,e)=>form.Close();
        form.Shown+=(s,e)=>{Console.WriteLine(form.Handle.ToInt64()); Console.Out.Flush(); timer.Start();};
        Application.Run(form);
    }
}
