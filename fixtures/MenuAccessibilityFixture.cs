// Disposable public-accessibility fixture. Hidden offscreen, no activation,
// no application reflection, business data, network or global input injection.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Windows.Automation;
using System.Windows.Automation.Provider;

[ComVisible(true),Guid("7852B78D-1CFD-41C1-A615-9C0C85960B5F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFixtureAccIdentity {
    [PreserveSig] int GetIdentityString(uint child,out IntPtr identity,out uint length);
}

[ComVisible(true)]
public class MenuFixtureSurface : Control {
    public bool Expanded, Disabled;
    public int Commands;
    public string ResultPath;
    public string LeafName="Active sheet";
    public readonly string Generation=Guid.NewGuid().ToString();
    MenuProvider provider;
    public MenuFixtureSurface(){SetStyle(ControlStyles.Selectable,false);}
    public void RunCommand(bool active){
        Commands++;
        if(!active||String.IsNullOrEmpty(ResultPath))return;
        using(var file=new FileStream(ResultPath,FileMode.CreateNew,FileAccess.ReadWrite))
        using(var archive=new ZipArchive(file,ZipArchiveMode.Create)){
            Entry(archive,"[Content_Types].xml","<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/><Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/></Types>");
            Entry(archive,"_rels/.rels","<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>");
            Entry(archive,"xl/workbook.xml","<?xml version=\"1.0\"?><workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets><sheet name=\"Fixture result\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>");
            Entry(archive,"xl/_rels/workbook.xml.rels","<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/></Relationships>");
            Entry(archive,"xl/worksheets/sheet1.xml","<?xml version=\"1.0\"?><worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData><row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>SYNTHETIC-MENU-RESULT</t></is></c></row></sheetData></worksheet>");
        }
    }
    static void Entry(ZipArchive archive,string path,string xml){using(var writer=new StreamWriter(archive.CreateEntry(path).Open(),new UTF8Encoding(false)))writer.Write(xml);}
    protected override AccessibleObject CreateAccessibilityInstance(){return new MenuRoot(this);}
    protected override void OnPaint(PaintEventArgs e){e.Graphics.Clear(Color.White);e.Graphics.DrawString("Synthetic menu fixture",Font,Brushes.Black,8,8);}
    [DllImport("UIAutomationCore.dll")] static extern IntPtr UiaReturnRawElementProvider(IntPtr hwnd,IntPtr wparam,IntPtr lparam,IRawElementProviderSimple provider);
    protected override void WndProc(ref Message message){
        if(message.Msg==0x003D&&message.LParam.ToInt64()==-25){
            if(provider==null)provider=new MenuProvider(this,null,-1);
            message.Result=UiaReturnRawElementProvider(Handle,message.WParam,message.LParam,provider);return;
        }
        base.WndProc(ref message);
    }
    // A real public UIA provider, independently served from WM_GETOBJECT -25.
    // MSAA uses the separate AccessibleObject tree returned by WM_GETOBJECT -4.
    [ComVisible(true)]
    public class MenuProvider : IRawElementProviderFragmentRoot, IExpandCollapseProvider, IInvokeProvider {
        readonly MenuFixtureSurface owner;readonly MenuProvider parent;readonly int ordinal;
        readonly MenuProvider[] children;
        public MenuProvider(MenuFixtureSurface owner,MenuProvider parent,int ordinal){
            this.owner=owner;this.parent=parent;this.ordinal=ordinal;
            if(ordinal==-1)children=new[]{new MenuProvider(owner,this,0),new MenuProvider(owner,this,1)};
            else if(ordinal==0)children=new[]{new MenuProvider(owner,this,2),new MenuProvider(owner,this,3)};
            else if(ordinal==2)children=new[]{new MenuProvider(owner,this,4)};
            else children=new MenuProvider[0];
        }
        public ProviderOptions ProviderOptions{get{return ProviderOptions.ServerSideProvider;}}
        public IRawElementProviderSimple HostRawElementProvider{get{return ordinal==-1?AutomationInteropProvider.HostProviderFromHandle(owner.Handle):null;}}
        public object GetPatternProvider(int pattern){
            if(pattern==ExpandCollapsePatternIdentifiers.Pattern.Id&&ordinal>=0&&ordinal<4)return this;
            if(pattern==InvokePatternIdentifiers.Pattern.Id&&ordinal>=1&&ordinal<4)return this;return null;
        }
        public object GetPropertyValue(int property){
            if(property==AutomationElementIdentifiers.ControlTypeProperty.Id)return ordinal==-1?ControlType.Menu.Id:ordinal==4?ControlType.Text.Id:ControlType.MenuItem.Id;
            if(property==AutomationElementIdentifiers.NameProperty.Id)return ordinal==-1?"Fixture menu":ordinal==0?"Export to excel":ordinal==1?"Disabled command":ordinal==2?owner.LeafName:ordinal==3?"All sheets":"Active sheet label";
            if(property==AutomationElementIdentifiers.IsEnabledProperty.Id)return ordinal!=1&&!owner.Disabled;
            if(property==AutomationElementIdentifiers.IsOffscreenProperty.Id)return ordinal>=2&&!owner.Expanded;
            if(property==AutomationElementIdentifiers.IsControlElementProperty.Id||property==AutomationElementIdentifiers.IsContentElementProperty.Id)return true;
            if(property==AutomationElementIdentifiers.IsPasswordProperty.Id)return false;
            if(property==AutomationElementIdentifiers.ProcessIdProperty.Id)return Process.GetCurrentProcess().Id;
            if(property==AutomationElementIdentifiers.NativeWindowHandleProperty.Id)return ordinal==-1?owner.Handle.ToInt32():0;
            return null;
        }
        public IRawElementProviderFragment Navigate(NavigateDirection direction){
            if(direction==NavigateDirection.Parent)return parent;
            if(direction==NavigateDirection.FirstChild)return children.Length>0&&(ordinal==-1||owner.Expanded)?children[0]:null;
            if(direction==NavigateDirection.LastChild)return children.Length>0&&(ordinal==-1||owner.Expanded)?children[children.Length-1]:null;
            if(parent==null)return null;
            int index=Array.IndexOf(parent.children,this);
            if(direction==NavigateDirection.NextSibling&&index+1<parent.children.Length)return parent.children[index+1];
            if(direction==NavigateDirection.PreviousSibling&&index>0)return parent.children[index-1];return null;
        }
        public int[] GetRuntimeId(){return ordinal==-1?null:new[]{AutomationInteropProvider.AppendRuntimeId,ordinal+100};}
        public System.Windows.Rect BoundingRectangle{get{
            var b=ordinal==-1?owner.RectangleToScreen(owner.ClientRectangle):owner.RectangleToScreen(new Rectangle(8,32+ordinal*30,220,25));
            return new System.Windows.Rect(b.X,b.Y,b.Width,b.Height);
        }}
        public IRawElementProviderSimple[] GetEmbeddedFragmentRoots(){return null;}
        public void SetFocus(){throw new NotSupportedException();}
        public IRawElementProviderFragmentRoot FragmentRoot{get{return parent==null?this:parent.FragmentRoot;}}
        public IRawElementProviderFragment ElementProviderFromPoint(double x,double y){
            foreach(var child in children){if(child.BoundingRectangle.Contains(x,y))return child;}return BoundingRectangle.Contains(x,y)?this:null;
        }
        public IRawElementProviderFragment GetFocus(){return null;}
        public ExpandCollapseState ExpandCollapseState{get{return ordinal!=0?ExpandCollapseState.LeafNode:owner.Expanded?ExpandCollapseState.Expanded:ExpandCollapseState.Collapsed;}}
        public void Expand(){if(ordinal!=0||owner.Disabled)throw new InvalidOperationException();owner.Expanded=true;}
        public void Collapse(){if(ordinal!=0||owner.Disabled)throw new InvalidOperationException();owner.Expanded=false;}
        public void Invoke(){if(ordinal<2||owner.Disabled)throw new InvalidOperationException();owner.RunCommand(ordinal==2);}
    }

    [ComVisible(true)]
    public class MenuRoot : ControlAccessibleObject, IFixtureAccIdentity {
        readonly MenuFixtureSurface owner;
        readonly MenuNode export, disabled;
        public MenuRoot(MenuFixtureSurface owner):base(owner){this.owner=owner;export=new MenuNode(owner,this,"Export to excel",0,true);disabled=new MenuNode(owner,this,"Disabled command",1,false);}
        public override AccessibleRole Role{get{return AccessibleRole.MenuPopup;}}
        public override string Name{get{return "Fixture menu";}}
        public override AccessibleStates State{get{return AccessibleStates.None;}}
        public override int GetChildCount(){return 2;}
        public override AccessibleObject GetChild(int index){return index==0?export:index==1?disabled:null;}
        public override AccessibleObject HitTest(int x,int y){for(int i=0;i<2;i++){var item=GetChild(i);if(item.Bounds.Contains(x,y))return item;}return Bounds.Contains(x,y)?this:null;}
        public int GetIdentityString(uint child,out IntPtr identity,out uint length){return Identity(owner.Generation+":root:"+child,out identity,out length);}
    }

    [ComVisible(true)]
    public class MenuNode : AccessibleObject, IFixtureAccIdentity {
        readonly MenuFixtureSurface owner;
        readonly AccessibleObject parent;
        readonly string label;
        readonly int ordinal;
        readonly bool popup;
        readonly MenuNode active, all;
        public MenuNode(MenuFixtureSurface owner,AccessibleObject parent,string label,int ordinal,bool popup){
            this.owner=owner;this.parent=parent;this.label=label;this.ordinal=ordinal;this.popup=popup;
            if(popup){active=new MenuNode(owner,this,"Active sheet",2,false);all=new MenuNode(owner,this,"All sheets",3,false);}
        }
        public override AccessibleObject Parent{get{return parent;}}
        public override AccessibleRole Role{get{return AccessibleRole.MenuItem;}}
        public override string Name{get{return ordinal==2?owner.LeafName:label;}}
        public override string DefaultAction{get{return popup?"Open":"Execute";}}
        public override AccessibleStates State{get{
            var value=AccessibleStates.Focusable;
            if(ordinal==1||owner.Disabled)value|=AccessibleStates.Unavailable;
            if(popup)value|=AccessibleStates.HasPopup|(owner.Expanded?AccessibleStates.Expanded:AccessibleStates.Collapsed);
            if(ordinal>=2&&!owner.Expanded)value|=AccessibleStates.Invisible;
            return value;
        }}
        public override Rectangle Bounds{get{return owner.RectangleToScreen(new Rectangle(8,32+ordinal*30,220,25));}}
        public override int GetChildCount(){return popup&&owner.Expanded?2:0;}
        public override AccessibleObject GetChild(int index){return popup&&owner.Expanded?(index==0?active:index==1?all:null):null;}
        public override void DoDefaultAction(){
            if(ordinal==1||owner.Disabled)return;
            if(popup)owner.Expanded=true;else owner.RunCommand(ordinal==2);
            owner.Invalidate();
        }
        public override AccessibleObject HitTest(int x,int y){
            if(popup&&owner.Expanded){if(active.Bounds.Contains(x,y))return active;if(all.Bounds.Contains(x,y))return all;}
            return Bounds.Contains(x,y)?this:null;
        }
        public int GetIdentityString(uint child,out IntPtr identity,out uint length){return Identity(owner.Generation+":node:"+ordinal+":"+child,out identity,out length);}
    }
    public static int Identity(string value,out IntPtr identity,out uint length){
        var bytes=Encoding.UTF8.GetBytes(value);length=(uint)bytes.Length;identity=Marshal.AllocCoTaskMem(bytes.Length);Marshal.Copy(bytes,0,identity,bytes.Length);return 0;
    }
}

class MenuFixtureWindow : Form {
    protected override bool ShowWithoutActivation{get{return true;}}
    protected override CreateParams CreateParams{get{var p=base.CreateParams;p.ExStyle|=0x08000000;return p;}}
}

class MenuAccessibilityFixture {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [STAThread] static void Main(string[] args){
        Console.InputEncoding=Encoding.UTF8;Console.OutputEncoding=new UTF8Encoding(false);
        try{SetProcessDpiAwarenessContext(new IntPtr(-4));}catch(EntryPointNotFoundException){}
        var json=new JavaScriptSerializer();
        var form=new MenuFixtureWindow{Text="CUA disposable menu fixture",ShowInTaskbar=false,
            StartPosition=FormStartPosition.Manual,Location=new Point(-15000,-15000),Size=new Size(420,260)};
        var surface=new MenuFixtureSurface{Dock=DockStyle.Fill,ResultPath=args.Length==1?Path.GetFullPath(args[0]):null};form.Controls.Add(surface);
        Action emit=delegate{Console.WriteLine(json.Serialize(new{pid=Process.GetCurrentProcess().Id,window_id=surface.Handle.ToInt64(),
            expanded=surface.Expanded,commands=surface.Commands,disabled=surface.Disabled,visible=surface.Visible}));Console.Out.Flush();};
        var timer=new System.Windows.Forms.Timer{Interval=45000};timer.Tick+=delegate{form.Close();};
        form.Shown+=delegate{emit();timer.Start();};
        var input=new Thread(delegate(){
            string line;
            while((line=Console.ReadLine())!=null){
                string command=line;
                try{form.BeginInvoke((MethodInvoker)delegate{
                    if(command=="quit"){form.Close();return;}
                    if(command=="expand")surface.Expanded=true;
                    else if(command=="collapse")surface.Expanded=false;
                    else if(command=="disable")surface.Disabled=true;
                    else if(command=="rename")surface.LeafName="Changed sheet";
                    else if(command=="hide")surface.Visible=false;
                    emit();
                });}catch{return;}
            }
        });input.IsBackground=true;input.Start();Application.Run(form);timer.Dispose();form.Dispose();
    }
}
