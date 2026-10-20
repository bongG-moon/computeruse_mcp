// Local synthetic fixture: no corporate URLs, credentials, data or network.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Runtime.InteropServices;

class PopupExportFixture : Form {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr c);
    readonly string folder;
    string mes = "All", family = "All";
    int queries, exports;
    readonly List<MenuWindow> menus = new List<MenuWindow>();
    readonly Timer timer = new Timer();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    public PopupExportFixture(string folder) {
        this.folder=folder; Directory.CreateDirectory(folder);
        Text="CUA Popup Export Fixture"; ClientSize=new Size(680,380);
        StartPosition=FormStartPosition.Manual; Location=new Point(80,100);
        DoubleBuffered=true;
        timer.Interval=1000; timer.Tick+=(s,e)=>Invalidate(new Rectangle(440,340,220,30)); timer.Start();
        Shown+=(s,e)=>Audit("ready",null);
    }
    void Audit(string action,string file) {
        File.WriteAllText(Path.Combine(folder,"fixture-state.json"),json.Serialize(new {
            fixture=true,pid=Process.GetCurrentProcess().Id,window_id=Handle.ToInt64(),
            mes=mes,family=family,queries=queries,exports=exports,action=action,file=file
        }),new UTF8Encoding(false));
    }
    void CloseMenus() { foreach(var m in menus.ToArray()) if(!m.IsDisposed)m.Close(); menus.Clear(); }
    void Menu(string[] entries, Point location, Action<int> selected, bool keep=false) {
        if(!keep)CloseMenus();
        var menu=new MenuWindow(entries,selected);
        menus.Add(menu); menu.Location=PointToScreen(location); menu.Show(this);
        Audit("popup",null);
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e); var g=e.Graphics; g.Clear(Color.White);
        Action<string,Rectangle> box=(text,r)=>{g.FillRectangle(Brushes.WhiteSmoke,r);g.DrawRectangle(Pens.Gray,r);g.DrawString(text,Font,Brushes.Black,r.X+8,r.Y+13);};
        box("MES Setup: "+mes,new Rectangle(20,40,180,44));
        box("Family: "+family,new Rectangle(220,40,210,44));
        box("Query",new Rectangle(450,40,100,44));
        box("Reset",new Rectangle(570,40,90,44));
        g.DrawString("Device        MES Setup        Family",Font,Brushes.Black,25,122);
        for(int i=0;i<5;i++) {
            var r=new Rectangle(20,150+i*30,640,30);
            g.FillRectangle(i%2==0?Brushes.AliceBlue:Brushes.WhiteSmoke,r);
            if(queries>0)g.DrawString("DEV-"+(i+1)+"           "+mes+"                    "+family,Font,Brushes.Black,r.X+6,r.Y+8);
        }
        g.DrawString("Right-click result row to export",Font,Brushes.Black,20,345);
        g.DrawString(DateTime.Now.ToString("HH:mm:ss"),Font,Brushes.Black,540,345);
    }
    protected override void OnMouseUp(MouseEventArgs e) {
        base.OnMouseUp(e);
        if(e.Button==MouseButtons.Right&&e.Y>=150&&e.Y<300&&queries>0) {
            Menu(new[]{"Export to excel","Other action"},new Point(260,150),i=>{
                if(i==0)Menu(new[]{"Active sheet","Formula","Grouping"},new Point(430,190),j=>{
                    if(j==0){Export();CloseMenus();}
                },true);
            }); return;
        }
        if(e.Button!=MouseButtons.Left||e.Y<40||e.Y>84)return;
        if(e.X>=20&&e.X<200)Menu(new[]{"N","Y"},new Point(20,85),i=>{mes=i==0?"N":"Y";CloseMenus();Invalidate();Audit("MES",null);});
        else if(e.X>=220&&e.X<430)Menu(new[]{"Hybrid","Standard"},new Point(220,85),i=>{family=i==0?"Hybrid":"Standard";CloseMenus();Invalidate();Audit("Family",null);});
        else if(e.X>=450&&e.X<550){queries++;CloseMenus();Invalidate();Audit("query",null);}
        else if(e.X>=570){CloseMenus();mes=family="All";queries=0;Invalidate();Audit("reset",null);}
    }
    void Export() {
        string file=Path.Combine(folder,"export-"+DateTime.UtcNow.Ticks+".xlsx");
        using(var z=ZipFile.Open(file,ZipArchiveMode.Create)) {
            Action<string,string> add=(name,body)=>{using(var w=new StreamWriter(z.CreateEntry(name).Open(),new UTF8Encoding(false)))w.Write(body);};
            add("[Content_Types].xml","<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/><Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/></Types>");
            add("_rels/.rels","<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>");
            add("xl/workbook.xml","<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets><sheet name=\"Active sheet\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>");
            add("xl/_rels/workbook.xml.rels","<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/></Relationships>");
            string rows="<row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>Device</t></is></c><c r=\"B1\" t=\"inlineStr\"><is><t>MES</t></is></c><c r=\"C1\" t=\"inlineStr\"><is><t>Family</t></is></c></row>";
            for(int i=0;i<5;i++){int r=i+2;rows+="<row r=\""+r+"\"><c r=\"A"+r+"\" t=\"inlineStr\"><is><t>DEV-"+(i+1)+"</t></is></c><c r=\"B"+r+"\" t=\"inlineStr\"><is><t>"+mes+"</t></is></c><c r=\"C"+r+"\" t=\"inlineStr\"><is><t>"+family+"</t></is></c></row>";}
            add("xl/worksheets/sheet1.xml","<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData>"+rows+"</sheetData></worksheet>");
        }
        exports++;Audit("export",file);
    }
    [STAThread] static void Main(string[] args) {
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();Application.Run(new PopupExportFixture(args[0]));
    }
    sealed class MenuWindow : Form {
        readonly string[] entries; readonly Action<int> selected; int hover=-1;
        public MenuWindow(string[] entries,Action<int> selected) {
            this.entries=entries;this.selected=selected;Text="";FormBorderStyle=FormBorderStyle.None;
            ShowInTaskbar=false;StartPosition=FormStartPosition.Manual;ClientSize=new Size(220,entries.Length*42);
            DoubleBuffered=true;
        }
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.Clear(Color.White);
            for(int i=0;i<entries.Length;i++){
                var r=new Rectangle(0,i*42,219,41);
                if(i==hover)e.Graphics.FillRectangle(new SolidBrush(Color.FromArgb(200,220,240)),r);
                e.Graphics.DrawRectangle(Pens.Gray,r);
                e.Graphics.DrawString(entries[i],Font,Brushes.Black,10,i*42+13);
            }
        }
        protected override void OnMouseMove(MouseEventArgs e){base.OnMouseMove(e);int h=e.Y/42;if(h!=hover){hover=h;Invalidate();}}
        protected override void OnMouseUp(MouseEventArgs e){base.OnMouseUp(e);int i=e.Y/42;if(e.Button==MouseButtons.Left&&i>=0&&i<entries.Length)selected(i);}
    }
}
