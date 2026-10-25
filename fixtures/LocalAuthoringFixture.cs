// Synthetic, offline acceptance app for local authoring and real file output.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Security;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;
class LocalAuthoringFixture : Form {
    readonly TextBox code = new TextBox();
    readonly ComboBox family = new ComboBox();
    readonly CheckBox query = new CheckBox();
    readonly Label result = new Label();
    readonly string folder;
    int exports;
    LocalAuthoringFixture(string folder) {
        this.folder=folder; Directory.CreateDirectory(folder);
        Text="Local Authoring Acceptance"; ClientSize=new Size(560,300);
        StartPosition=FormStartPosition.Manual; Location=new Point(80,80);
        var label=new Label {Text="업무 코드",Bounds=new Rectangle(25,30,120,26)}; Controls.Add(label);
        code.Name="BusinessCode"; code.AccessibleName="업무 코드"; code.Bounds=new Rectangle(155,25,350,32); Controls.Add(code);
        Controls.Add(new Label {Text="Family",Bounds=new Rectangle(25,85,120,26)});
        family.Name="Family"; family.AccessibleName="Family"; family.Bounds=new Rectangle(155,80,180,32);
        family.DropDownStyle=ComboBoxStyle.DropDownList; family.Items.AddRange(new object[]{"N","P"}); family.SelectedIndex=0; Controls.Add(family);
        query.Name="ExportResult"; query.AccessibleName="조회하고 Excel 저장"; query.Text="조회하고 Excel 저장";
        query.Bounds=new Rectangle(155,135,330,40); Controls.Add(query);
        result.Name="Result"; result.AccessibleName="생성 결과"; result.Bounds=new Rectangle(25,200,510,75); Controls.Add(result);
        query.CheckedChanged+=(s,e)=>{if(query.Checked){ Export(); } Audit();};
        code.TextChanged+=(s,e)=>Audit(); family.SelectedIndexChanged+=(s,e)=>Audit();
        Shown+=(s,e)=>Audit();
    }
    void Audit() {
        File.WriteAllText(Path.Combine(folder,"state.json"),new JavaScriptSerializer().Serialize(new {
            pid=Process.GetCurrentProcess().Id,window_id=Handle.ToInt64(),code=code.Text,
            family=family.Text,query=query.Checked,exports=exports
        }),new UTF8Encoding(false));
    }
    static string Escape(string s) { return SecurityElement.Escape(s); }
    void Export() {
        string file=Path.Combine(folder,"result-"+DateTime.UtcNow.Ticks+".xlsx");
        using(var z=ZipFile.Open(file,ZipArchiveMode.Create)) {
            Action<string,string> entry=(name,text)=>{using(var w=new StreamWriter(z.CreateEntry(name).Open(),new UTF8Encoding(false)))w.Write(text);};
            entry("[Content_Types].xml","<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/><Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/></Types>");
            entry("_rels/.rels","<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>");
            entry("xl/workbook.xml","<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets><sheet name=\"Result\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>");
            entry("xl/_rels/workbook.xml.rels","<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/></Relationships>");
            entry("xl/worksheets/sheet1.xml","<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData><row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>Code</t></is></c><c r=\"B1\" t=\"inlineStr\"><is><t>Family</t></is></c></row><row r=\"2\"><c r=\"A2\" t=\"inlineStr\"><is><t>"+Escape(code.Text)+"</t></is></c><c r=\"B2\" t=\"inlineStr\"><is><t>"+Escape(family.Text)+"</t></is></c></row></sheetData></worksheet>");
        }
        exports++; result.Text="Excel 생성 완료: "+code.Text+" / "+family.Text;
    }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new LocalAuthoringFixture(args.Length>0?args[0]:Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"fixture-output")));
    }
}
