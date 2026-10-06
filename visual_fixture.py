"""Developer-only painted-window fixture; never launches a user application."""
from pathlib import Path
import subprocess

SOURCE = r'''
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Forms;
class VisualFixture : Form {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
 readonly int[] clicks = new int[3]; readonly string output;
 readonly TextBox input = new TextBox(), password = new TextBox();
 readonly JavaScriptSerializer json = new JavaScriptSerializer();
 readonly Label result = new Label(); int wheels;
 VisualFixture(string title, string path) {
  output=path; Text=title; StartPosition=FormStartPosition.CenterScreen; ClientSize=new Size(720,490);
  AutoScaleMode=AutoScaleMode.None; DoubleBuffered=true; BackColor=Color.FromArgb(244,247,252);
  input.Name="safe_input"; input.AccessibleName="녹화 시험 입력"; input.SetBounds(30,370,310,30);
  password.Name="protected_input"; password.AccessibleName="보호된 시험 입력"; password.UseSystemPasswordChar=true;
  password.SetBounds(390,370,280,30); Controls.Add(input); Controls.Add(password);
  result.SetBounds(30,420,650,45); result.Text="화면에 그려진 반복 버튼 시험"; Controls.Add(result);
  input.TextChanged += delegate { Save(); }; Shown += delegate { Save(); }; FormClosed += delegate { Save(); };
 }
 Rectangle Button(int row) { return new Rectangle(590,47+100*row,40,40); }
 protected override void OnPaint(PaintEventArgs e) {
  base.OnPaint(e); e.Graphics.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
  for(int i=0;i<3;i++) {
   Rectangle row=new Rectangle(30,30+100*i,650,75);
   using(Brush b=new SolidBrush(Color.White)) e.Graphics.FillRectangle(b,row);
   using(Brush b=new SolidBrush(Color.FromArgb(25+50*i,85,155))) e.Graphics.FillRectangle(b,30,row.Y,8,row.Height);
   using(Font f=new Font("Segoe UI",17,FontStyle.Bold)) e.Graphics.DrawString("WORK ITEM "+(char)('A'+i),f,Brushes.Navy,65,row.Y+23);
   Rectangle button=Button(i); using(Brush b=new SolidBrush(Color.FromArgb(80,90,110)))e.Graphics.FillEllipse(b,button);
   e.Graphics.FillPolygon(Brushes.White,new Point[]{new Point(button.X+15,button.Y+10),new Point(button.X+15,button.Y+30),new Point(button.X+29,button.Y+20)});
  }
  using(Font f=new Font("Segoe UI",10)) { e.Graphics.DrawString("Editable field",f,Brushes.Gray,30,343); e.Graphics.DrawString("Protected field",f,Brushes.Gray,390,343); }
 }
 protected override void OnMouseDown(MouseEventArgs e) {
  base.OnMouseDown(e); for(int i=0;i<3;i++)if(Button(i).Contains(e.Location)) {clicks[i]++; result.Text="RESULT "+(char)('A'+i)+" / "+clicks[i]; Save();}
 }
 protected override void OnMouseWheel(MouseEventArgs e) {base.OnMouseWheel(e); wheels+=e.Delta; Save();}
 void Save() {
  string temporary=output+".tmp";
  File.WriteAllText(temporary,json.Serialize(new Dictionary<string,object>{{"synthetic_fixture",true},{"clicks",clicks},{"input",input.Text},{"wheel",wheels}}));
  if(File.Exists(output))File.Delete(output); File.Move(temporary,output);
 }
 [STAThread] static void Main(string[] args) {
  if(args.Length!=2)return; SetProcessDpiAwarenessContext(new IntPtr(-4)); Application.EnableVisualStyles();
  Application.Run(new VisualFixture(args[0],args[1]));
 }
}
'''


def compile_fixture(folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / 'VisualFixture.cs'
    exe = folder / 'VisualFixture.exe'
    source.write_text(SOURCE, encoding='utf-8')
    compiler = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/codepage:65001',
        '/reference:System.dll', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll',
        '/reference:System.Web.Extensions.dll', '/out:'+str(exe), str(source)], check=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return exe
