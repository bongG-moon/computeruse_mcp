// Local human screenshot review only. No capture, input injection, network,
// application launch or workflow acknowledgement.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class CheckpointReview
{
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 32768 };
    [STAThread] static int Main(string[] args)
    {
        try {
            if (args.Length != 1 || !Path.IsPathRooted(args[0]) || new FileInfo(args[0]).Length > 32768) return 2;
            string folder = Path.GetDirectoryName(Path.GetFullPath(args[0]));
            var request = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[0], Encoding.UTF8));
            string nonce = Convert.ToString(request["nonce"]), id = Convert.ToString(request["review_id"]);
            if (nonce.Length != 64 || id.Length != 32) return 2;
            foreach (char ch in nonce + id) if (!Uri.IsHexDigit(ch)) return 2;
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            using (var window = new ReviewWindow(folder, request, nonce, id)) Application.Run(window);
            return 0;
        } catch { return 3; }
    }
    sealed class ReviewWindow : Form
    {
        readonly string folder, nonce, id; bool finished; readonly Timer timer = new Timer(); readonly Image picture;
        readonly DateTime expires = DateTime.UtcNow.AddMinutes(10);
        internal ReviewWindow(string dir, Dictionary<string, object> request, string token, string identity)
        {
            folder = dir; nonce = token; id = identity;
            Text = "작업 결과 확인 · Computer Use MCP"; BackColor = Color.FromArgb(244,247,252);
            AutoScaleMode = AutoScaleMode.None;
            Font = new Font("Malgun Gothic", 10);
            float scale; using (Graphics graphics = CreateGraphics()) scale = graphics.DpiX / 96f;
            Func<int,int> px = delegate(int value) { return (int)Math.Round(value * scale); };
            Rectangle working = Screen.FromHandle(Handle).WorkingArea;
            ClientSize = new Size(Math.Min(px(1060), Math.Max(420, working.Width - px(40))),
                                  Math.Min(px(740), Math.Max(340, working.Height - px(64))));
            MinimumSize = new Size(Math.Min(px(640), Width), Math.Min(px(500), Height));
            StartPosition = FormStartPosition.CenterScreen;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(px(22)), RowCount = 4, ColumnCount = 1 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            // Point-sized fonts grow with Windows DPI. Let the text determine
            // its row height instead of clipping it inside fixed pixel rows.
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); Controls.Add(layout);
            layout.Controls.Add(new Label { Name = "ReviewHeading", Text = "이 단계가 의도대로 끝났나요?", Font = new Font(Font.FontFamily, 18, FontStyle.Bold), ForeColor = Color.FromArgb(22,52,86), AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0,0,0,px(8)) },0,0);
            layout.Controls.Add(new Label { Name = "ReviewDescription", Text = Convert.ToString(request["message"]) + "\r\n아래는 이 단계에서 캡처한 화면입니다. 확인을 눌러도 다음 동작은 자동 실행되지 않습니다.", AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0,0,0,px(14)) },0,1);
            using (var source = Image.FromFile(Path.Combine(folder,"screen.png"))) picture = new Bitmap(source);
            layout.Controls.Add(new PictureBox { Name = "ReviewImage", Image = picture, SizeMode = PictureBoxSizeMode.Zoom, Dock = DockStyle.Fill, BackColor = Color.White, BorderStyle = BorderStyle.FixedSingle, Margin = Padding.Empty },0,2);
            var buttons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(0,px(14),0,0), Margin = Padding.Empty };
            buttons.Controls.Add(Button("확인 완료", "confirmed", Color.FromArgb(37,99,235), Color.White, px));
            buttons.Controls.Add(Button("결과가 다름", "rejected", Color.White, Color.FromArgb(22,52,86), px));
            layout.Controls.Add(buttons,0,3);
            Shown += delegate { Write("ready.json", new Dictionary<string,object> { {"visible",true} }); };
            FormClosing += delegate { Finish("cancelled"); };
            timer.Interval = 200; timer.Tick += delegate {
                if (DateTime.UtcNow >= expires || File.Exists(Path.Combine(folder,"cancel.flag")) ||
                    File.Exists(Path.Combine(Path.GetDirectoryName(folder),"stop.flag"))) Close();
            }; timer.Start();
        }
        Button Button(string title, string state, Color back, Color fore, Func<int,int> px)
        {
            var button = new Button { Text = title, Width = px(160), Height = px(42), FlatStyle = FlatStyle.Flat, BackColor = back, ForeColor = fore, Margin = new Padding(px(10),0,0,0) };
            button.Click += delegate { Finish(state); Close(); }; return button;
        }
        void Write(string name, Dictionary<string,object> value)
        {
            value["nonce"] = nonce; value["review_id"] = id;
            string path = Path.Combine(folder,name), temporary = path + ".tmp";
            File.WriteAllText(temporary,Json.Serialize(value),new UTF8Encoding(false)); File.Move(temporary,path);
        }
        void Finish(string state) { if (finished) return; finished = true; try { Write("result.json",new Dictionary<string,object> { {"status",state} }); } catch { } }
        protected override void Dispose(bool disposing) { if (disposing) { timer.Dispose(); picture.Dispose(); } base.Dispose(disposing); }
    }
}
