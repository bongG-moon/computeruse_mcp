// Human-authored process editor. Local file IPC only; never injects input,
// reads screen values, launches applications, or runs arbitrary code.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class ProcessEditor
{
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] internal static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool MoveFileEx(string existing, string replacement, uint flags);
    internal const int MaximumSteps = 1000;
    internal const int MaximumTargets = MaximumSteps + 100;
    internal const int MaximumMessageBytes = 2 * 1024 * 1024;
    internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = MaximumMessageBytes };
    internal static string Str(Dictionary<string, object> data, string key, string fallback)
    { object value; return data != null && data.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : fallback; }
    internal static int Num(Dictionary<string, object> data, string key, int fallback)
    { int result; return Int32.TryParse(Str(data, key, ""), out result) ? result : fallback; }
    internal static long Long(Dictionary<string, object> data, string key, long fallback)
    { long result; return Int64.TryParse(Str(data, key, ""), out result) ? result : fallback; }
    internal static Dictionary<string, object> Map(Dictionary<string, object> data, string key)
    { object value; return data != null && data.TryGetValue(key, out value) ? value as Dictionary<string, object> : null; }
    internal static bool PreviewAnchorPoint(Dictionary<string, object> anchor, Size image, Size viewport, out PointF point)
    {
        point = PointF.Empty;
        if (anchor == null || anchor.Count != 2 || !anchor.ContainsKey("x") || !anchor.ContainsKey("y")
                || image.Width <= 0 || image.Height <= 0 || viewport.Width <= 0 || viewport.Height <= 0) return false;
        double x, y;
        if (!PreviewUnit(anchor["x"], out x) || !PreviewUnit(anchor["y"], out y)) return false;
        // Match PictureBox Zoom's integer destination rectangle, including
        // letterboxing. The recorded anchor addresses a source pixel.
        float ratio = Math.Min((float)viewport.Width / image.Width, (float)viewport.Height / image.Height);
        int width = (int)(image.Width * ratio), height = (int)(image.Height * ratio);
        if (width < 1 || height < 1) return false;
        int pixelX = Math.Min(image.Width - 1, (int)(x * image.Width));
        int pixelY = Math.Min(image.Height - 1, (int)(y * image.Height));
        point = new PointF((viewport.Width - width) / 2 + (pixelX + .5f) * width / image.Width,
                           (viewport.Height - height) / 2 + (pixelY + .5f) * height / image.Height);
        return true;
    }
    static bool PreviewUnit(object value, out double number)
    {
        number = 0;
        if (!(value is int || value is long || value is float || value is double || value is decimal)) return false;
        number = Convert.ToDouble(value);
        return !Double.IsNaN(number) && !Double.IsInfinity(number) && number >= 0 && number <= 1;
    }
    internal static void PaintPreviewAnchor(Graphics graphics, PointF point, float scale)
    {
        var previous = graphics.SmoothingMode;
        graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        float radius = 7 * scale, arm = 12 * scale;
        using (var outline = new Pen(Color.White, 4 * scale))
        using (var marker = new Pen(Color.FromArgb(0, 118, 230), 2 * scale)) {
            foreach (Pen pen in new[] { outline, marker }) {
                graphics.DrawEllipse(pen, point.X - radius, point.Y - radius, radius * 2, radius * 2);
                graphics.DrawLine(pen, point.X - arm, point.Y, point.X + arm, point.Y);
                graphics.DrawLine(pen, point.X, point.Y - arm, point.X, point.Y + arm);
            }
        }
        graphics.SmoothingMode = previous;
    }
    internal static string TrialMessage(Dictionary<string, object> tested)
    {
        if (Object.Equals(tested != null && tested.ContainsKey("task_verified") ? tested["task_verified"] : null, true))
            return "시험 실행을 완료하고 결과를 확인했습니다. 필요하면 저장하세요.";
        string reason = Str(Map(tested, "continuation"), "reason", "");
        if (reason == "workflow_chunk_complete") return "여기까지 " + Num(tested, "completed_steps", 0).ToString() + "단계를 확인했습니다. 채팅에서 이어서 실행하면 다음 단계부터 계속합니다.";
        if (reason == "session_budget_exhausted") return "이 연결의 실행 범위가 끝났습니다. 초안은 유지했습니다. 처음부터 다시 시험하지 말고 채팅의 안내를 확인하세요.";
        var diagnostic = Map(tested, "diagnostic"); string code = Str(diagnostic, "code", "");
        if (code == "start_screen_not_ready" || code == "image_not_found" || code == "image_ambiguous")
            return (Num(tested, "repair_step_index", 0) + 1).ToString() + "단계 대상 확인이 필요합니다. " + Str(diagnostic, "message", "")
                + " 실행 순서에서 해당 단계를 두 번 클릭하고 ‘이 단계만 다시 지정’을 선택하세요. 다른 단계는 유지됩니다.";
        return "시험 실행 상태: " + Str(tested, "status", "확인 필요") + ". 채팅의 결과를 확인하세요.";
    }
    internal static IEnumerable List(Dictionary<string, object> data, string key)
    { object value; return data != null && data.TryGetValue(key, out value) && !(value is string) ? value as IEnumerable : null; }
    internal static bool ValidNonce(string text)
    { if (text == null || text.Length != 64) return false; foreach (char ch in text) if (!Uri.IsHexDigit(ch)) return false; return true; }
    static bool TransientSharingFailure(Exception error)
    {
        int code = error.HResult & 65535;
        return (error is IOException || error is UnauthorizedAccessException) && (code == 5 || code == 32 || code == 33);
    }
    static void RetrySharedFile(Action action, bool reading)
    {
        Stopwatch elapsed = Stopwatch.StartNew();
        for (;;) {
            try { action(); return; }
            catch (Exception error) {
                long remaining = 500 - elapsed.ElapsedMilliseconds;
                // ReplaceFile can briefly hide the destination name during
                // replacement. Only a reader may retry that missing snapshot.
                bool replaceGap = reading && error is FileNotFoundException;
                if ((!TransientSharingFailure(error) && !replaceGap) || remaining <= 0) throw;
                System.Threading.Thread.Sleep((int)Math.Min(20, remaining));
            }
        }
    }
    internal static string Read(string path, int maximumBytes)
    {
        string result = null;
        RetrySharedFile(delegate {
            // Atomic replacement is allowed while this handle reads its own
            // complete file snapshot. Never deny deletion to the IPC writer.
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
                if (stream.Length > maximumBytes) throw new InvalidDataException("message_too_large");
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8, true)) result = reader.ReadToEnd();
            }
        }, true);
        return result;
    }
    internal static void Write(string path, Dictionary<string, object> data)
    {
        string text = Json.Serialize(data);
        if (Encoding.UTF8.GetByteCount(text) > MaximumMessageBytes) throw new InvalidOperationException("message_too_large");
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, text, new UTF8Encoding(false));
        try {
            // Retry only publishing these same bytes, not a command or app
            // input. A persistent access failure still ends after 0.5 seconds.
            RetrySharedFile(delegate {
                // Same-directory rename matches Python os.replace. ReplaceFile
                // also merges metadata and can expose a transient name gap.
                if (!MoveFileEx(temporary, path, 1)) {
                    int code = Marshal.GetLastWin32Error();
                    throw new IOException("ipc_replace_failed_" + code.ToString(), unchecked((int)(0x80070000u | (uint)code)));
                }
            }, false);
        } finally { try { if (File.Exists(temporary)) File.Delete(temporary); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
    }
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 3 || (args[0] != "--edit" && args[0] != "--inputs" && args[0] != "--result")) return 2;
        string nonce = null, response = null;
        try {
            if (!Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            string request = Path.GetFullPath(args[1]), result = Path.GetFullPath(args[2]);
            if (!String.Equals(Path.GetDirectoryName(request), Path.GetDirectoryName(result), StringComparison.OrdinalIgnoreCase)) return 2;
            var data = Json.Deserialize<Dictionary<string, object>>(Read(request, MaximumMessageBytes));
            string token = Str(data, "nonce", ""); if (!ValidNonce(token)) return 2;
            nonce = token; response = result;
            if (!Environment.UserInteractive) throw new InvalidOperationException("interactive_desktop_unavailable");
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            if (args[0] == "--result") {
                using (var form = new LocalResultForm(data)) {
                    form.Shown += delegate { Write(result + ".ready.json", new Dictionary<string, object> { { "nonce", token }, { "status", "ready" }, { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", form.Handle.ToInt64() } }); };
                    form.ShowDialog();
                    Write(result, new Dictionary<string,object>{{"nonce",token},{"status","closed"}});
                }
            } else if (args[0] == "--inputs") {
                using (var form = new LocalInputForm(data)) {
                    form.Shown += delegate { Write(result + ".ready.json", new Dictionary<string, object> { { "nonce", token }, { "status", "ready" }, { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", form.Handle.ToInt64() } }); };
                    DialogResult outcome = form.ShowDialog();
                    var answer = new Dictionary<string, object> { { "nonce", token }, { "status", outcome == DialogResult.OK ? "confirmed" : "cancelled" } };
                    if (outcome == DialogResult.OK) answer["inputs"] = form.Values;
                    Write(result, answer);
                }
            } else using (EditorForm editor = new EditorForm(data, result)) Application.Run(editor);
            return File.Exists(result) ? 0 : 3;
        } catch (Exception error) {
            if (nonce != null && response != null && !File.Exists(response)) {
                try { Write(response, new Dictionary<string, object> { { "nonce", nonce },
                    { "status", File.Exists(response + ".ready.json") ? "runtime_failed" : "startup_failed" },
                    { "code", "process_editor_failed" }, { "error_type", error.GetType().Name } }); } catch { }
            }
            return 2;
        }
    }
    internal static string InputText(object value)
    {
        if (value == null) return "";
        if (value is bool) return (bool)value ? "true" : "false";
        var entries = value as IEnumerable;
        if (entries != null && !(value is string) && !(value is IDictionary)) {
            var lines = new List<string>(); foreach (object item in entries) lines.Add(InputText(item)); return String.Join("\r\n", lines);
        }
        return Convert.ToString(value, CultureInfo.InvariantCulture);
    }
    internal static object ParseInputText(Dictionary<string, object> spec, string text)
    {
        string kind = Str(spec, "type", "text");
        if (kind == "list") {
            var items = Map(spec, "items"); if (items == null || Str(items, "type", "") == "list") throw new ArgumentException("목록의 항목 형식을 확인하세요.");
            var result = new List<object>();
            foreach (string line in text.Replace("\r\n", "\n").Split('\n')) result.Add(ParseInputText(items, line));
            if (text.Length == 0 || result.Count > Num(spec, "max_items", 1000)) throw new ArgumentException("목록에 항목을 한 줄씩 입력하고 허용 개수를 확인하세요.");
            return result;
        }
        if (kind == "boolean") {
            if (text == "true" || text == "예") return true;
            if (text == "false" || text == "아니요") return false;
            throw new ArgumentException("예 또는 아니요를 선택하세요.");
        }
        if (kind == "number" || kind == "integer") {
            double number; long integer;
            if (!Double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number) || Double.IsNaN(number) || Double.IsInfinity(number) || Math.Abs(number) > 1e15)
                throw new ArgumentException("올바른 숫자를 입력하세요. 소수점은 .으로 표시합니다.");
            if (kind == "integer" && !Int64.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out integer)) throw new ArgumentException("정수를 입력하세요.");
            double boundary;
            if (spec.ContainsKey("minimum") && Double.TryParse(InputText(spec["minimum"]), NumberStyles.Float, CultureInfo.InvariantCulture, out boundary) && number < boundary ||
                spec.ContainsKey("maximum") && Double.TryParse(InputText(spec["maximum"]), NumberStyles.Float, CultureInfo.InvariantCulture, out boundary) && number > boundary)
                throw new ArgumentException("지정된 숫자 범위 안의 값을 입력하세요.");
            return kind == "integer" ? (object)Int64.Parse(text, CultureInfo.InvariantCulture) : number;
        }
        if (kind != "text" && kind != "date" && kind != "enum") throw new ArgumentException("지원하지 않는 입력 형식입니다.");
        if (text.Length > 4000 || text.IndexOf('\0') >= 0) throw new ArgumentException("입력값은 4,000자 이내여야 합니다.");
        if (kind == "date") {
            DateTime date;
            if (!DateTime.TryParseExact(text, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out date)) throw new ArgumentException("날짜를 YYYY-MM-DD 형식으로 입력하세요.");
        }
        if (kind == "enum") {
            bool found = false; var options = List(spec, "values");
            if (options != null) foreach (object option in options) if (Object.Equals(option, text)) found = true;
            if (!found) throw new ArgumentException("목록에 있는 값 중 하나를 선택하세요.");
        }
        return text;
    }
    internal static void FinishLocalDialog(Form form)
    {
        // All local forms are authored in 96-DPI units. Without this explicit
        // baseline WinForms can size the form in raw pixels while rendering
        // its font at the monitor DPI, clipping both content and footer buttons.
        form.ResumeLayout(true);
        form.PerformAutoScale();
        FitLocalDialog(form, Screen.FromControl(form).WorkingArea);
    }
    internal static void FitLocalDialog(Form form, Rectangle working)
    {
        int width = Math.Max(1, working.Width), height = Math.Max(1, working.Height);
        form.MinimumSize = new Size(Math.Min(form.MinimumSize.Width, width), Math.Min(form.MinimumSize.Height, height));
        form.Size = new Size(Math.Min(form.Width, width), Math.Min(form.Height, height));
        form.StartPosition = FormStartPosition.Manual;
        form.Location = new Point(working.Left + (width - form.Width) / 2, working.Top + (height - form.Height) / 2);
        form.PerformLayout();
    }
    internal sealed class LocalResultForm : Form
    {
        readonly Timer lifetime = new Timer();
        internal LocalResultForm(Dictionary<string,object> request)
        {
            SuspendLayout(); AutoScaleDimensions=new SizeF(96F,96F); AutoScaleMode=AutoScaleMode.Dpi;
            var result=Map(request,"result"); if(result==null)throw new ArgumentException("missing_local_result");
            Text="작업 실행 결과 · 이 PC에서만 표시";Font=new Font("맑은 고딕",10);BackColor=Color.FromArgb(245,247,251);
            ClientSize=new Size(650,430);MinimumSize=new Size(500,340);
            bool verified=Object.Equals(result.ContainsKey("task_verified")?result["task_verified"]:null,true);
            string state=Str(result,"status","unknown");
            string title=verified?"작업을 완료하고 결과를 확인했습니다.":state=="cancelled"?"작업이 취소되었습니다.":"작업을 끝내려면 확인이 필요합니다.";
            var heading=new Label {Text=title,Font=new Font(Font.FontFamily,14,FontStyle.Bold),ForeColor=verified?Color.FromArgb(26,100,74):Color.FromArgb(24,43,72),Dock=DockStyle.Top,Height=90,Padding=new Padding(20),Name="LocalResultHeading",AccessibleName="작업 결과"};Controls.Add(heading);
            var bottom=new FlowLayoutPanel {Dock=DockStyle.Bottom,Height=70,Padding=new Padding(12),FlowDirection=FlowDirection.RightToLeft};Controls.Add(bottom);
            var close=new Button {Text="닫기",Width=110,Height=34,DialogResult=DialogResult.OK,Name="CloseLocalResult",AccessibleName="닫기"};bottom.Controls.Add(close);AcceptButton=CancelButton=close;
            var lines=new List<string>();
            lines.Add("진행: "+Num(result,"completed_steps",0)+" / "+Num(result,"total_steps",0)+" 단계");
            lines.Add("상태: "+state);
            string summary=Str(result,"summary","");if(summary.Length>0)lines.Add(summary);
            var diagnostic=Map(result,"diagnostic");string code=Str(diagnostic,"code","");string message=Str(diagnostic,"message","");
            if(code.Length>0)lines.Add("확인 코드: "+code);
            if(message.Length>0)lines.Add(message);
            if(!verified)lines.Add("이미 전달된 입력은 자동으로 반복하지 않았습니다. 원인을 확인한 뒤 기존 실행을 이어가세요.");
            var body=new TextBox {ReadOnly=true,Multiline=true,ScrollBars=ScrollBars.Vertical,WordWrap=true,BackColor=Color.White,BorderStyle=BorderStyle.FixedSingle,Dock=DockStyle.Fill,Text=String.Join("\r\n\r\n",lines),Name="LocalResultDetails",AccessibleName="실행 결과 상세"};
            var panel=new Panel {Dock=DockStyle.Fill,Padding=new Padding(20,0,20,0)};panel.Controls.Add(body);Controls.Add(panel);panel.BringToFront();
            int seconds=Num(request,"timeout_seconds",600);if(seconds<30||seconds>1800)throw new ArgumentException("invalid_timeout");DateTime deadline=DateTime.UtcNow.AddSeconds(seconds);
            lifetime.Interval=500;lifetime.Tick+=delegate {if(DateTime.UtcNow>=deadline)Close();};lifetime.Start();
            FinishLocalDialog(this);
        }
        protected override void Dispose(bool disposing) {if(disposing)lifetime.Dispose();base.Dispose(disposing);}
    }
    internal sealed class LocalInputForm : Form
    {
        internal Dictionary<string, object> Values = new Dictionary<string, object>();
        readonly Timer lifetime = new Timer();
        readonly List<Dictionary<string, object>> definitions = new List<Dictionary<string, object>>();
        readonly Dictionary<string, Control> editors = new Dictionary<string, Control>();
        internal LocalInputForm(Dictionary<string, object> request)
        {
            SuspendLayout(); AutoScaleDimensions=new SizeF(96F,96F); AutoScaleMode=AutoScaleMode.Dpi;
            Text = Str(request,"title","실행할 입력값 · 이 PC에서만 사용"); Font = new Font("맑은 고딕", 10); BackColor = Color.FromArgb(245,247,251);
            ClientSize = new Size(620, 530); MinimumSize = new Size(500, 400);
            var intro = new Label { Text = Str(request,"intro","필요한 값만 입력하세요. 입력값은 로컬 실행에 전달하며 채팅이나 스킬 파일에 넣지 않습니다."), Dock = DockStyle.Top, Height = 60, Padding = new Padding(16) }; Controls.Add(intro);
            var bottom = new Panel { Dock = DockStyle.Bottom, Height = 110, Padding = new Padding(16) }; Controls.Add(bottom);
            var error = new Label { ForeColor = Color.Firebrick, AutoEllipsis = true, Dock = DockStyle.Top, Height = 26 }; bottom.Controls.Add(error);
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 42, FlowDirection = FlowDirection.RightToLeft };
            var cancel = new Button { Text = "취소", Width = 100, Height = 32, DialogResult = DialogResult.Cancel, Name = "CancelLocalInputs", AccessibleName = "취소" };
            string confirmLabel=Str(request,"confirm_label","이 값으로 실행");
            var start = new Button { Text = confirmLabel, Width = 160, Height = 32, Name = "ConfirmLocalInputs", AccessibleName = confirmLabel };
            buttons.Controls.Add(cancel); buttons.Controls.Add(start); bottom.Controls.Add(buttons); CancelButton = cancel;
            var fields = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new Padding(18) }; Controls.Add(fields); fields.BringToFront();
            var items = List(request, "inputs"); if (items == null) throw new ArgumentException("missing_input_definitions");
            foreach (object raw in items) {
                var entry = raw as Dictionary<string, object>; var spec = Map(entry, "spec"); string key = Str(entry, "name", "");
                if (key.Length == 0 || spec == null || editors.ContainsKey(key) || definitions.Count >= 30) throw new ArgumentException("invalid_input_definitions");
                definitions.Add(entry);
                string kind = Str(spec, "type", "text"), label = Str(spec, "description", key); if (label.Length == 0) label = key;
                fields.Controls.Add(new Label { Text = label + (spec.ContainsKey("default") ? " · 기본값 변경 가능" : " · 매번 입력"), Width = 540, Height = 28, AutoEllipsis = true });
                Control control;
                var itemSpec=kind=="list"?Map(spec,"items"):null;
                // A normal foreach list is ordered and may contain repeats.
                // Checklists are only for explicit set-like local choosers.
                bool multipleChoice=Object.Equals(request.ContainsKey("multiple_choice_lists")?request["multiple_choice_lists"]:null,true)
                    &&itemSpec!=null&&Str(itemSpec,"type","")=="enum";
                if(multipleChoice) {
                    var checklist=new CheckedListBox {Width=530,Height=150,CheckOnClick=true,IntegralHeight=false,HorizontalScrollbar=true};
                    var defaults=new HashSet<string>(StringComparer.Ordinal);var defaultEntries=List(spec,"default");
                    if(defaultEntries!=null)foreach(object item in defaultEntries)defaults.Add(Convert.ToString(item));
                    var options=List(itemSpec,"values");if(options!=null)foreach(object option in options){string code=Convert.ToString(option);checklist.Items.Add(new Choice {Code=code,Label=code},defaults.Contains(code));}
                    control=checklist;
                } else if (kind == "enum" || kind == "boolean") {
                    var combo = new ComboBox { Width = 530, Height = 32, DropDownStyle = ComboBoxStyle.DropDownList };
                    if (kind == "boolean") { combo.Items.Add(new Choice { Code = "true", Label = "예" }); combo.Items.Add(new Choice { Code = "false", Label = "아니요" }); }
                    else { var options = List(spec, "values"); if (options != null) foreach (object option in options) combo.Items.Add(new Choice { Code = Convert.ToString(option), Label = Convert.ToString(option) }); }
                    if (spec.ContainsKey("default")) foreach (Choice option in combo.Items) if (option.Code == InputText(spec["default"])) combo.SelectedItem = option;
                    control = combo;
                } else {
                    control = new TextBox { Width = 530, Height = kind == "list" ? 100 : 34, Multiline = kind == "list", ScrollBars = kind == "list" ? ScrollBars.Vertical : ScrollBars.None,
                        MaxLength = kind == "list" ? 200000 : 4000, Text = spec.ContainsKey("default") ? InputText(spec["default"]) : "" };
                }
                control.Name = "LocalInput_" + key; control.AccessibleName = label; control.Margin = new Padding(0,0,0,12); editors.Add(key,control); fields.Controls.Add(control);
                if (kind == "date" || kind == "list") fields.Controls.Add(new Label { Text = kind == "date" ? "예: 2026-10-25" : multipleChoice ? "사용할 항목을 하나 이상 체크하세요." : "한 줄에 항목 하나. 반복 실행은 저장된 작업의 반복 구간을 사용합니다.", ForeColor = Color.DimGray, Width = 540, Height = 30 });
            }
            start.Click += delegate {
                try {
                    var values = new Dictionary<string, object>();
                    foreach (var entry in definitions) {
                        string key = Str(entry,"name","");
                        values.Add(key, ReadControlValue(Map(entry,"spec"),editors[key]));
                    }
                    Values = values; DialogResult = DialogResult.OK; Close();
                } catch (ArgumentException failure) { error.Text = failure.Message; }
            };
            int seconds = Num(request, "timeout_seconds", 600); if (seconds < 30 || seconds > 1800) throw new ArgumentException("invalid_timeout");
            DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
            lifetime.Interval = 500; lifetime.Tick += delegate { if (DateTime.UtcNow >= deadline) { DialogResult = DialogResult.Cancel; Close(); } }; lifetime.Start();
            fields.ClientSizeChanged += delegate {
                int available = Math.Max(1, fields.ClientSize.Width-fields.Padding.Horizontal-SystemInformation.VerticalScrollBarWidth);
                foreach(Control control in fields.Controls) control.Width=Math.Max(1,available-control.Margin.Horizontal);
            };
            FinishLocalDialog(this);
        }
        internal static object ReadControlValue(Dictionary<string,object> spec,Control control)
        {
            var multiple=control as CheckedListBox;
            if(multiple!=null) {
                var item=Map(spec,"items");if(Str(spec,"type","")!="list"||item==null||Str(item,"type","")!="enum")throw new ArgumentException("선택 목록의 형식을 확인하세요.");
                var values=new List<object>();foreach(object raw in multiple.CheckedItems){var selected=raw as Choice;if(selected==null)throw new ArgumentException("선택 항목을 확인하세요.");values.Add(ParseInputText(item,selected.Code));}
                if(values.Count<1||values.Count>Num(spec,"max_items",1000))throw new ArgumentException("항목을 하나 이상 선택하고 허용 개수를 확인하세요.");return values;
            }
            var combo=control as ComboBox;string text=combo==null?control.Text:combo.SelectedItem==null?"":((Choice)combo.SelectedItem).Code;
            return ParseInputText(spec,text);
        }
        protected override void Dispose(bool disposing) { if (disposing) lifetime.Dispose(); base.Dispose(disposing); }
    }
    internal sealed class Choice
    {
        internal string Code, Label;
        internal Dictionary<string, object> Data;
        public override string ToString() { return Label; }
    }
    internal sealed class EditorForm : Form
    {
        readonly string nonce, response;
        readonly DateTime deadline;
        readonly Timer timer = new Timer();
        readonly Color ink = Color.FromArgb(24, 43, 72), blue = Color.FromArgb(37, 99, 235), muted = Color.FromArgb(92, 108, 132);
        readonly ComboBox program = new ComboBox(), action = new ComboBox(), expectProperty = new ComboBox(), expectBoolean = new ComboBox(), imageCompletion = new ComboBox(), checkedValue = new ComboBox();
        readonly ComboBox saveMode = new ComboBox();
        bool editingExisting;
        readonly Label selected = new Label(), expectSelected = new Label(), status = new Label(), valueCaption = new Label(), timeCaption = new Label(), timeUnit = new Label(), actionHelp = new Label();
        readonly TextBox value = new TextBox(), optionOrder = new TextBox(), expected = new TextBox(), processName = new TextBox(), description = new TextBox();
        readonly NumericUpDown seconds = new NumericUpDown();
        readonly ListView steps = new ListView();
        readonly TabControl authoringTabs = new TabControl();
        readonly ListView inputList = new ListView();
        readonly TextBox skillText = new TextBox();
        readonly Label inputHelp = new Label(), skillHelp = new Label();
        readonly Button diagnose = new Button(), newInput = new Button(), changeInput = new Button(), removeInput = new Button(), bindInput = new Button(), unbindInput = new Button(), previewSkill = new Button(), exportSkill = new Button(), undo = new Button(), redo = new Button();
        readonly List<Dictionary<string, object>> inputDefinitions = new List<Dictionary<string, object>>();
        Dictionary<string, object> savedTask, skillPreview;
        bool draftDirty, canUndo, canRedo, loadingDraft;
        string savedName = "", savedDescription = "";
        readonly FlowLayoutPanel fields = new FlowLayoutPanel();
        readonly Panel valueSection = new Panel(), optionSection = new Panel(), timeSection = new Panel(), expectSection = new Panel(), completionSection = new Panel();
        readonly Button pick = new Button(), pickImage = new Button(), record = new Button(), reanalyzeRecording = new Button(), reviewRecording = new Button(), editInput = new Button(), pickExpect = new Button(), add = new Button(), save = new Button(), trial = new Button(), up = new Button(), down = new Button(), remove = new Button();
        readonly PictureBox thumbnail = new PictureBox();
        readonly List<Choice> allActions = new List<Choice>();
        Dictionary<string, object> selection, expectation, recordingReview;
        int seq, pendingSeq, stepCount, externalTrialSequence, maximumSteps;
        string pendingAction, pendingPurpose;
        bool busy, done, hiddenForPicker, loadingActions, pendingInputs, pendingRecordingReview;
        float scale;
        int Px(int number) { return (int)Math.Round(number * scale); }
        Rectangle Box(int x, int y, int w, int h) { return new Rectangle(Px(x), Px(y), Px(w), Px(h)); }
        void Identify(Control control, string id, string name) { control.Name = id; control.AccessibleName = name; }
        Label Caption(string text, int y, Control parent)
        { var label = new Label { Text = text, ForeColor = muted, Bounds = Box(0, y, 398, 25), Font = new Font(Font.FontFamily, 9, FontStyle.Bold) }; parent.Controls.Add(label); return label; }
        void Input(Control control, string id, Rectangle bounds, Control parent)
        { Identify(control, id, id); control.Bounds = bounds; control.Font = new Font(Font.FontFamily, 10); parent.Controls.Add(control); }
        void ButtonStyle(Button control, string id, string text, Rectangle bounds, bool primary, Control parent)
        {
            Identify(control, id, text); control.Text = text; control.Bounds = bounds; control.FlatStyle = FlatStyle.Flat;
            control.UseVisualStyleBackColor = false; control.BackColor = primary ? blue : Color.White;
            control.ForeColor = primary ? Color.White : ink; control.FlatAppearance.BorderColor = primary ? blue : Color.FromArgb(212, 222, 237);
            control.FlatAppearance.BorderSize = primary ? 0 : 1; control.Font = new Font(Font.FontFamily, 10, FontStyle.Bold);
            control.Cursor = Cursors.Hand; parent.Controls.Add(control);
            control.EnabledChanged += delegate { control.BackColor = primary && control.Enabled ? blue : primary ? Color.FromArgb(229, 234, 242) : Color.White; };
        }
        Panel Section(int height)
        { var panel = new Panel(); AddSection(panel, height); return panel; }
        void AddSection(Panel panel, int height)
        { panel.Width = Px(398); panel.Height = Px(height); panel.Margin = new Padding(0, 0, 0, Px(10)); fields.Controls.Add(panel); }
        void ConfigureCombo(ComboBox control) { control.DropDownStyle = ComboBoxStyle.DropDownList; control.FlatStyle = FlatStyle.Flat; }
        string ActionCode { get { var choice = action.SelectedItem as Choice; return choice == null ? "" : choice.Code; } }
        bool IsImage { get { return Str(selection, "recognition", "") == "image"; } }
        bool ImageMutation { get { return IsImage && ActionCode != "wait_for_element" && ActionCode != "delay" && ActionCode != "checkpoint" && ActionCode != "wait_for_state"; } }
        bool ElementClick { get { return !IsImage && (ActionCode == "click" || ActionCode == "double_click" || ActionCode == "right_click"); } }
        bool AutomaticImage { get { var choice = imageCompletion.SelectedItem as Choice; return (ImageMutation || ElementClick) && choice != null && choice.Code == "automatic"; } }
        string CompletionMode { get { var choice = imageCompletion.SelectedItem as Choice; return choice == null ? "human" : choice.Code; } }
        bool NeedsExpect { get { string code = ActionCode; return AutomaticImage || code == "wait_for_state" || !IsImage && (code == "press_key" || code == "hotkey"); } }
        bool NeedsElement { get { return ActionCode != "delay" && ActionCode != "checkpoint" && ActionCode != "wait_for_state"; } }

        internal EditorForm(Dictionary<string, object> request, string response)
        {
            this.response = response; nonce = Str(request, "nonce", "");
            int lifetime = Num(request, "timeout_seconds", 600); if (lifetime < 30 || lifetime > 1800) throw new ArgumentException("invalid_timeout");
            maximumSteps = Num(request, "max_steps", MaximumSteps); if (maximumSteps < 1 || maximumSteps > MaximumSteps) throw new ArgumentException("invalid_step_limit");
            deadline = DateTime.UtcNow.AddSeconds(lifetime);
            var programs = List(request, "programs"); if (programs == null) throw new ArgumentException("missing_programs");
            foreach (object raw in programs) {
                var data = raw as Dictionary<string, object>; if (data == null) throw new ArgumentException("invalid_program");
                string id = Str(data, "program_id", "");
                if (id.Length == 0 || Long(data, "window_id", 0) < 0 || Num(data, "pid", 0) <= 0 ||
                    Long(data, "window_id", 0) == 0 && Str(data, "window_ref", "main") == "main") throw new ArgumentException("invalid_program_identity");
                program.Items.Add(new Choice { Code = id, Label = Str(data, "label", id), Data = data });
                if (program.Items.Count > MaximumTargets) throw new ArgumentException("too_many_programs");
            }
            if (program.Items.Count == 0) throw new ArgumentException("empty_programs");
            var first = (Choice)program.Items[0];
            Text = "화면 자동화 프로세스 만들기"; BackColor = Color.FromArgb(245, 247, 251); ForeColor = ink; Font = new Font("맑은 고딕", 10);
            AutoScaleMode = AutoScaleMode.None; StartPosition = FormStartPosition.Manual;
            Rectangle screen = Screen.FromHandle(new IntPtr(Long(first.Data, "window_id", 0))).WorkingArea;
            Location = new Point(screen.Left + 20, screen.Top + 20);
            uint dpi = 0; try { dpi = GetDpiForWindow(Handle); } catch (EntryPointNotFoundException) { }
            if (dpi == 0) { using (Graphics graphics = Graphics.FromHwnd(Handle)) dpi = (uint)graphics.DpiX; }
            scale = Math.Max(1F, dpi / 96F);
            ClientSize = new Size(Px(1060), Px(750)); FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = true;
            if (Height > screen.Height) { AutoScroll = true; AutoScrollMinSize = ClientSize; Height = screen.Height; }
            if (Width > screen.Width) { AutoScroll = true; AutoScrollMinSize = new Size(Px(1060), Px(750)); Width = screen.Width; }
            Location = new Point(Math.Max(screen.Left, screen.Left + (screen.Width - Width) / 2), Math.Max(screen.Top, screen.Top + (screen.Height - Height) / 2));
            Controls.Add(new Label { Text = "반복할 업무를 순서대로 알려주세요", Font = new Font(Font.FontFamily, 19, FontStyle.Bold), Bounds = Box(24, 20, 1008, 40) });
            Controls.Add(new Label { Text = "요소·이미지로 단계를 만들거나, 직접 하는 동작을 녹화하세요. 녹화 후 목록을 검토하고 저장합니다.", ForeColor = muted, Bounds = Box(26, 66, 1006, 30) });
            var left = new Panel { BackColor = Color.White, Bounds = Box(24, 110, 444, 562) }; Controls.Add(left);
            fields.Bounds = Box(20, 18, 420, 476); fields.AutoScroll = true; fields.FlowDirection = FlowDirection.TopDown; fields.WrapContents = false; fields.BackColor = Color.White; left.Controls.Add(fields);
            var targetSection = Section(230); Caption("대상 프로그램과 창", 0, targetSection); ConfigureCombo(program); Input(program, "TargetProgram", Box(0, 29, 394, 34), targetSection);
            ButtonStyle(record, "RecordActions", "● 동작 녹화", Box(0, 72, 194, 34), false, targetSection);
            ButtonStyle(reanalyzeRecording, "ReanalyzeRecording", "최근 녹화 다시 분석", Box(204, 72, 190, 34), false, targetSection); reanalyzeRecording.Visible = false;
            ButtonStyle(reviewRecording, "ReviewRecordingWarnings", "누락 경고 확인", Box(0, 112, 394, 32), false, targetSection); reviewRecording.Visible = false;
            targetSection.Controls.Add(new Label { Text = "시작 버튼을 누른 뒤 실제 클릭·입력을 기록합니다.\n마우스만 올려 모은 요소 목록은 동작 기록이 아닙니다.", ForeColor = muted, Font = new Font(Font.FontFamily, 8), Bounds = Box(0, 190, 394, 34) });
            ButtonStyle(diagnose, "DiagnoseElements", "요소 진단 · 이 PC에만 저장", Box(0, 150, 394, 32), false, targetSection);
            diagnose.Click += delegate { Send("diagnose", Target(), null); };
            var elementSection = Section(184); Caption("1  동작할 요소", 0, elementSection);
            selected.Text = "아직 선택한 요소가 없습니다"; selected.AutoEllipsis = true; selected.Bounds = Box(0, 28, 394, 28); elementSection.Controls.Add(selected); Identify(selected, "SelectedElement", "현재 선택한 요소");
            ButtonStyle(pick, "PickElement", "화면에서 선택", Box(0, 63, 394, 34), false, elementSection);
            pickImage.Visible = false;
            thumbnail.Bounds = Box(0, 104, 394, 70); thumbnail.SizeMode = PictureBoxSizeMode.Zoom; thumbnail.BackColor = Color.FromArgb(245, 247, 251); thumbnail.Visible = false; Identify(thumbnail, "TargetThumbnail", "선택한 대상 이미지"); elementSection.Controls.Add(thumbnail);
            var operationSection = Section(92); Caption("2  어떤 동작을 할까요?", 0, operationSection); ConfigureCombo(action); Input(action, "StepAction", Box(0, 29, 394, 34), operationSection);
            actionHelp.ForeColor = muted; actionHelp.Font = new Font(Font.FontFamily, 8); actionHelp.Bounds = Box(0, 65, 394, 25); operationSection.Controls.Add(actionHelp);
            AddSection(valueSection, 95); valueCaption.Bounds = Box(0, 0, 394, 25); valueCaption.ForeColor = muted; valueCaption.Font = new Font(Font.FontFamily, 9, FontStyle.Bold); valueSection.Controls.Add(valueCaption);
            value.Multiline = true; value.MaxLength = 16000; value.ScrollBars = ScrollBars.Vertical; Input(value, "StepValue", Box(0, 29, 394, 58), valueSection);
            ConfigureCombo(checkedValue); checkedValue.Items.AddRange(new object[] { "체크함", "체크 해제" }); Input(checkedValue, "CheckedValue", Box(0, 29, 394, 34), valueSection); checkedValue.SelectedIndex = 0; checkedValue.Visible = false;
            AddSection(optionSection, 124); Caption("선택 항목의 실제 순서 (한 줄에 하나, 선택 사항)", 0, optionSection);
            optionOrder.Multiline = true; optionOrder.ScrollBars = ScrollBars.Vertical; optionOrder.MaxLength = 30000; Input(optionOrder, "OptionOrder", Box(0, 29, 394, 82), optionSection);
            AddSection(timeSection, 74); timeCaption.Bounds = Box(0, 0, 394, 25); timeCaption.ForeColor = muted; timeCaption.Font = new Font(Font.FontFamily, 9, FontStyle.Bold); timeSection.Controls.Add(timeCaption);
            seconds.DecimalPlaces = 0; seconds.Minimum = 0; seconds.Maximum = 60; seconds.Value = 20; Input(seconds, "StepSeconds", Box(0, 29, 130, 34), timeSection);
            timeUnit.Text = "초"; timeUnit.Bounds = Box(142, 32, 80, 28); timeUnit.ForeColor = muted; timeSection.Controls.Add(timeUnit);
            AddSection(completionSection, 118); Caption("동작의 완료 확인", 0, completionSection);
            ConfigureCombo(imageCompletion);
            if (Object.Equals(request.ContainsKey("visual_review_enabled") ? request["visual_review_enabled"] : null, true)) imageCompletion.Items.Add(new Choice { Code = "visual", Label = "채팅에서 화면 확인 (모델)" });
            imageCompletion.Items.AddRange(new object[] { new Choice { Code = "human", Label = "화면을 보고 직접 확인" }, new Choice { Code = "automatic", Label = "선택한 요소의 변화로 자동 확인" } });
            Input(imageCompletion, "ImageVerificationMode", Box(0, 29, 394, 34), completionSection); imageCompletion.SelectedIndex = 0;
            completionSection.Controls.Add(new Label { Text = "자동 확인은 아래 요소의 값이 동작 전과 달라지고\n예상 내용과 일치하면 멈추지 않고 이어갑니다.", ForeColor = muted, Font = new Font(Font.FontFamily, 8), Bounds = Box(0, 74, 394, 36) });
            AddSection(expectSection, 244); Caption("3  동작 뒤 무엇으로 완료를 확인할까요?", 0, expectSection);
            expectSelected.Text = "확인할 요소를 선택하세요"; expectSelected.AutoEllipsis = true; expectSelected.Bounds = Box(0, 29, 394, 28); expectSection.Controls.Add(expectSelected); Identify(expectSelected, "ExpectedElement", "완료 확인 요소");
            ButtonStyle(pickExpect, "PickExpectedElement", "확인할 요소 선택", Box(0, 63, 394, 34), false, expectSection);
            ConfigureCombo(expectProperty); expectProperty.Items.AddRange(new object[] { new Choice { Code = "value", Label = "입력값 / 선택값이 다음과 같음" }, new Choice { Code = "name", Label = "요소 이름이 다음과 같음" }, new Choice { Code = "selected", Label = "선택 여부가 다음과 같음" }, new Choice { Code = "enabled", Label = "사용 가능 여부가 다음과 같음" } });
            Input(expectProperty, "ExpectedProperty", Box(0, 111, 394, 34), expectSection); expectProperty.SelectedIndex = 0;
            expected.MaxLength = 16000; Input(expected, "ExpectedValue", Box(0, 157, 394, 34), expectSection);
            ConfigureCombo(expectBoolean); expectBoolean.Items.AddRange(new object[] { new Choice { Code = "true", Label = "예" }, new Choice { Code = "false", Label = "아니요" } });
            Input(expectBoolean, "ExpectedBoolean", Box(0, 157, 394, 34), expectSection); expectBoolean.SelectedIndex = 0;
            expectSection.Controls.Add(new Label { Text = "동작 뒤 확인할 요소를 고르고 예상 내용을 입력하세요.\n현재 화면에서 요소만 골라 둡니다.", ForeColor = muted, Font = new Font(Font.FontFamily, 8), Bounds = Box(0, 199, 394, 40) });
            ButtonStyle(add, "AddStep", "이 동작을 다음 단계로 추가", Box(20, 507, 404, 38), true, left);
            var right = new Panel { BackColor = Color.White, Bounds = Box(488, 110, 548, 562) }; Controls.Add(right);
            right.Controls.Add(new Label { Text = "프로세스 이름", Bounds = Box(20, 18, 508, 25), ForeColor = muted, Font = new Font(Font.FontFamily, 9, FontStyle.Bold) });
            processName.MaxLength = 100; Input(processName, "ProcessName", Box(20, 46, 290, 34), right);
            ConfigureCombo(saveMode); Input(saveMode, "ProcessSaveMode", Box(320, 46, 208, 34), right);
            saveMode.Items.Add(new Choice { Code = "update", Label = "기존 작업 수정" });
            saveMode.Items.Add(new Choice { Code = "copy", Label = "새 작업으로 저장" });
            saveMode.SelectedIndex = 1;
            right.Controls.Add(new Label { Text = "업무 설명 (선택 사항)", Bounds = Box(20, 93, 508, 25), ForeColor = muted, Font = new Font(Font.FontFamily, 9, FontStyle.Bold) });
            description.Multiline = true; description.MaxLength = 4000; description.ScrollBars = ScrollBars.Vertical; Input(description, "ProcessDescription", Box(20, 122, 508, 56), right);
            authoringTabs.Bounds = Box(16, 192, 516, 358); Identify(authoringTabs, "AuthoringTabs", "단계 · 입력값 · 스킬 보기"); right.Controls.Add(authoringTabs);
            var stepsPage = new TabPage("단계") { BackColor = Color.White };
            var inputsPage = new TabPage("입력값") { BackColor = Color.White };
            var skillPage = new TabPage("스킬 보기") { BackColor = Color.White };
            authoringTabs.TabPages.Add(stepsPage); authoringTabs.TabPages.Add(inputsPage); authoringTabs.TabPages.Add(skillPage);
            stepsPage.Controls.Add(new Label { Text = "실행 순서 · 두 번 클릭 / Enter로 대상 확인", Bounds = Box(8, 6, 488, 25), ForeColor = blue });
            steps.Bounds = Box(8, 36, 488, 225); steps.View = View.Details; steps.FullRowSelect = true; steps.MultiSelect = false; steps.HideSelection = false;
            steps.HeaderStyle = ColumnHeaderStyle.Nonclickable; steps.GridLines = false; steps.BorderStyle = BorderStyle.FixedSingle;
            steps.Columns.Add("순서", Px(40)); steps.Columns.Add("프로그램", Px(96)); steps.Columns.Add("동작 / 대상", Px(164)); steps.Columns.Add("내용", Px(170)); Identify(steps, "ProcessSteps", "프로세스 실행 순서"); stepsPage.Controls.Add(steps);
            ButtonStyle(up, "MoveStepUp", "위로", Box(8, 272, 58, 34), false, stepsPage); ButtonStyle(down, "MoveStepDown", "아래로", Box(72, 272, 58, 34), false, stepsPage);
            ButtonStyle(editInput, "ResolveInput", "값·조건", Box(136, 272, 100, 34), false, stepsPage); ButtonStyle(remove, "RemoveStep", "삭제", Box(242, 272, 64, 34), false, stepsPage);
            ButtonStyle(undo, "UndoEdit", "되돌리기", Box(320,272,78,34),false,stepsPage); ButtonStyle(redo,"RedoEdit","다시 적용",Box(404,272,78,34),false,stepsPage);
            inputHelp.Text = "실행마다 바꿀 값을 모아서 관리합니다. 값은 이 PC에만 저장합니다."; inputHelp.ForeColor = muted; inputHelp.Bounds = Box(8,6,488,42); inputsPage.Controls.Add(inputHelp);
            inputList.Bounds = Box(8,52,488,198); inputList.View = View.Details; inputList.FullRowSelect = true; inputList.MultiSelect = false; inputList.HideSelection = false;
            inputList.Columns.Add("입력 이름",Px(160)); inputList.Columns.Add("형식",Px(72)); inputList.Columns.Add("기본값",Px(130)); inputList.Columns.Add("사용 단계",Px(100)); Identify(inputList,"InputDefinitions","실행할 입력값"); inputsPage.Controls.Add(inputList);
            ButtonStyle(newInput,"NewInput","추가",Box(8,266,56,36),false,inputsPage); ButtonStyle(changeInput,"EditInputDefinition","수정",Box(70,266,56,36),false,inputsPage);
            ButtonStyle(removeInput,"RemoveInputDefinition","삭제",Box(132,266,56,36),false,inputsPage); ButtonStyle(bindInput,"BindInputToStep","단계에 연결",Box(196,266,138,36),false,inputsPage);
            ButtonStyle(unbindInput,"UnbindInputFromStep","고정값으로",Box(342,266,146,36),false,inputsPage);
            skillHelp.Text = "저장한 작업의 호출 안내를 미리 봅니다. 업무 값과 화면은 넣지 않습니다."; skillHelp.ForeColor = muted; skillHelp.Bounds = Box(8,6,488,44); skillPage.Controls.Add(skillHelp);
            skillText.Multiline = true; skillText.ReadOnly = true; skillText.ScrollBars = ScrollBars.Both; skillText.WordWrap = false; skillText.Font = new Font("Consolas",9); skillText.Bounds = Box(8,56,488,196); Identify(skillText,"SkillPreviewText","스킬 파일 전체 미리보기"); skillPage.Controls.Add(skillText);
            ButtonStyle(previewSkill,"PreviewSkill","미리보기 갱신",Box(8,266,206,36),false,skillPage); ButtonStyle(exportSkill,"ExportSkill","보이는 내용 내보내기",Box(226,266,262,36),true,skillPage);
            inputList.SelectedIndexChanged += delegate { UpdateButtons(); };
            newInput.Click += delegate { EditInputDefinition(null); };
            changeInput.Click += delegate { if (inputList.SelectedItems.Count==1) EditInputDefinition(inputList.SelectedItems[0].Tag as Dictionary<string,object>); };
            removeInput.Click += delegate { if(inputList.SelectedItems.Count==1) { var item=inputList.SelectedItems[0].Tag as Dictionary<string,object>; Send("input_edit",new Dictionary<string,object>{{"edit",new Dictionary<string,object>{{"action","remove"},{"name",Str(item,"name","")}}}},null); } };
            bindInput.Click += delegate { BindSelectedInput(); };
            unbindInput.Click += delegate { UnbindSelectedInput(); };
            undo.Click += delegate { Send("undo",new Dictionary<string,object>(),null); };
            redo.Click += delegate { Send("redo",new Dictionary<string,object>(),null); };
            previewSkill.Click += delegate { Send("skill_preview",new Dictionary<string,object>(),null); };
            exportSkill.Click += delegate { if(skillPreview!=null) Send("skill_export",new Dictionary<string,object>{{"preview_id",Str(skillPreview,"preview_id","")}},null); };
            status.Text = "화면에서 대상을 선택하거나 녹화를 시작하세요."; status.ForeColor = muted; status.Bounds = Box(26, 688, 532, 44); Identify(status, "ProcessEditorStatus", "프로세스 작성 상태"); Controls.Add(status);
            ButtonStyle(trial, "TestProcess", "▷ 시험 실행", Box(574, 692, 156, 38), false, this);
            ButtonStyle(save, "SaveProcess", "프로세스 저장", Box(748, 692, 176, 38), true, this);
            var cancel = new Button(); ButtonStyle(cancel, "CancelProcess", "닫기", Box(940, 692, 96, 38), false, this);
            allActions.AddRange(new Choice[] {
                new Choice { Code = "click", Label = "클릭" }, new Choice { Code = "double_click", Label = "두 번 클릭" }, new Choice { Code = "right_click", Label = "오른쪽 클릭" },
                new Choice { Code = "set_value", Label = "글자 입력" }, new Choice { Code = "select_option", Label = "선택 상자에서 항목 선택" },
                new Choice { Code = "set_checked", Label = "체크 / 체크 해제" },
                new Choice { Code = "press_key", Label = "키 누르기 (Enter, Tab 등)" }, new Choice { Code = "hotkey", Label = "단축키 (Ctrl+S 등)" }, new Choice { Code = "scroll", Label = "스크롤 (이미지)" },
                new Choice { Code = "wait_for_element", Label = "요소가 나타날 때까지 대기" }, new Choice { Code = "wait_for_state", Label = "완료 조건이 될 때까지 대기" }, new Choice { Code = "delay", Label = "정해진 시간 대기" }, new Choice { Code = "checkpoint", Label = "화면 캡처 후 확인할 때까지 일시정지" }
            });
            program.SelectedIndexChanged += delegate { if (!busy) { selection = expectation = null; ShowThumbnail(null); selected.Text = "아직 선택한 요소가 없습니다"; expectSelected.Text = "확인할 요소를 선택하세요"; PopulateActions(); } };
            action.SelectedIndexChanged += delegate { if (!loadingActions) UpdateActionFields(); };
            expectProperty.SelectedIndexChanged += delegate { UpdateExpectFields(); };
            imageCompletion.SelectedIndexChanged += delegate { UpdateActionFields(); };
            pick.Click += delegate { Pick("action", false); }; pickImage.Click += delegate { Pick("action", true); }; pickExpect.Click += delegate { Pick("expect", false); }; add.Click += delegate { AddStep(); };
            record.Click += delegate { StartRecording(); };
            reanalyzeRecording.Click += delegate { Send("reimport_recording", new Dictionary<string, object>(), null); };
            reviewRecording.Click += delegate { ShowRecordingReview(); };
            editInput.Click += delegate { if (steps.SelectedIndices.Count == 1) Send("preview_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, "edit"); };
            trial.Click += delegate { StartTrial(); };
            save.Click += delegate { Send("save", new Dictionary<string, object> { { "name", processName.Text.Trim() }, { "description", description.Text }, { "save_mode", ((Choice)saveMode.SelectedItem).Code } }, null); };
            cancel.Click += delegate { CloseEditor(); };
            up.Click += delegate { MoveStep(-1); }; down.Click += delegate { MoveStep(1); };
            remove.Click += delegate { if (steps.SelectedIndices.Count == 1) Send("remove_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null); };
            steps.SelectedIndexChanged += delegate { UpdateButtons(); }; processName.TextChanged += delegate { InvalidateSkill(); UpdateButtons(); }; description.TextChanged += delegate { InvalidateSkill(); UpdateButtons(); };
            steps.DoubleClick += delegate { if (!busy && steps.SelectedIndices.Count == 1) Send("preview_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null); };
            steps.KeyDown += delegate(object sender, KeyEventArgs e) {
                if (e.KeyCode == Keys.Enter && !busy && steps.SelectedIndices.Count == 1) {
                    e.Handled = true; e.SuppressKeyPress = true;
                    Send("preview_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null);
                }
            };
            program.SelectedIndex = 0; PopulateActions(); UpdateExpectFields();
            loadingDraft = true; var draft = Map(request, "draft"); processName.Text = Str(draft, "name", ""); description.Text = Str(draft, "description", ""); UpdateSteps(List(draft, "steps")); SetRecordingReview(Map(draft, "recording_review")); UpdateAuthoring(draft); loadingDraft = false; UpdateButtons();
            editingExisting = Str(draft, "task_id", "").Length > 0;
            saveMode.SelectedIndex = editingExisting ? 0 : 1;
            saveMode.Enabled = editingExisting;
            saveMode.SelectedIndexChanged += delegate { UpdateButtons(); };
            UpdateButtons();
            FormClosing += delegate(object sender, FormClosingEventArgs args) { if (!done) { args.Cancel = true; CloseEditor(); } };
            Shown += delegate { BeginInvoke(new Action(delegate {
                if (!Visible || !IsWindowVisible(Handle)) { Finish("startup_failed", new Dictionary<string, object> { { "code", "editor_not_visible" } }); return; }
                Write(response + ".ready.json", new Dictionary<string, object> { { "nonce", nonce }, { "status", "ready" }, { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", Handle.ToInt64() } });
                timer.Interval = 100; timer.Tick += Tick; timer.Start();
            })); };
        }

        bool UnsavedChanges { get { return draftDirty || savedTask != null && (processName.Text.Trim() != savedName || description.Text != savedDescription); } }
        void InvalidateSkill()
        {
            if (loadingDraft) return;
            skillPreview = null; skillText.Clear();
            skillHelp.Text = "변경 내용을 저장한 뒤 미리보기를 갱신하세요. 저장 전 내용을 내보내지 않습니다.";
        }
        internal void DisplaySkillPreview(Dictionary<string,object> preview)
        {
            // WinForms multiline edit controls require CRLF for visible line
            // breaks. Normalize only this presentation string: the reviewed
            // preview ID, original Markdown, hash and export bytes stay intact.
            skillPreview = preview;
            string original = Str(preview,"skill_markdown","");
            skillText.Text = original.Replace("\r\n","\n").Replace("\r","\n").Replace("\n","\r\n");
            skillHelp.Text=preview==null?"미리보기를 만들지 못했습니다.":"저장본 v"+Num(preview,"revision",1)+" · 화면에 보이는 파일 그대로 내보냅니다. 자동 설치하지 않습니다.";
        }
        void CloseEditor()
        {
            // Preserve cancellation while a local request is outstanding.
            // Closing never claims that already dispatched app input was undone.
            if (busy) { Finish("cancelled",null); return; }
            if (UnsavedChanges && MessageBox.Show(this,"아직 저장하지 않은 편집 내용이 있습니다. 저장하지 않고 닫을까요?", "편집 내용 확인",MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes) return;
            if (savedTask != null && !UnsavedChanges) Finish("saved",new Dictionary<string,object>{{"saved_task",savedTask}});
            else Finish("cancelled",null);
        }
        void UpdateAuthoring(Dictionary<string,object> result)
        {
            if (result == null) return;
            if (result.ContainsKey("dirty")) draftDirty = Object.Equals(result["dirty"],true);
            if (result.ContainsKey("can_undo")) canUndo=Object.Equals(result["can_undo"],true);
            if (result.ContainsKey("can_redo")) canRedo=Object.Equals(result["can_redo"],true);
            if (result.ContainsKey("saved_task")) {
                var saved=Map(result,"saved_task");
                if(saved!=null) {
                    bool changed=savedTask==null || Str(savedTask,"id",Str(savedTask,"task_id",""))!=Str(saved,"id",Str(saved,"task_id","")) || Num(savedTask,"revision",0)!=Num(saved,"revision",0);
                    savedTask=saved;if(changed){savedName=processName.Text.Trim();savedDescription=description.Text;}editingExisting=true;
                }
            } else if (Str(result,"task_id","").Length>0 && savedTask==null) {
                savedTask=new Dictionary<string,object>{{"id",Str(result,"task_id","")},{"revision",Num(result,"revision",1)}};
                savedName=processName.Text.Trim(); savedDescription=description.Text;
            }
            if(result.ContainsKey("inputs")) {
                string chosen=inputList.SelectedItems.Count==1?Str(inputList.SelectedItems[0].Tag as Dictionary<string,object>,"name",""):"";
                inputDefinitions.Clear(); inputList.Items.Clear(); var entries=List(result,"inputs");
                if(entries!=null) foreach(object raw in entries) {
                    var entry=raw as Dictionary<string,object>; var spec=Map(entry,"spec"); if(entry==null || spec==null) continue;
                    inputDefinitions.Add(entry); string key=Str(entry,"name",""); string label=Str(spec,"description",key); if(label.Length==0) label=key;
                    var uses=new List<string>(); var used=List(entry,"uses");
                    if(used!=null) foreach(object rawUse in used) { var use=rawUse as Dictionary<string,object>; string n=(Num(use,"index",-1)+1).ToString(); if(n!="0"&&!uses.Contains(n)) uses.Add(n); }
                    var row=new ListViewItem(label); row.SubItems.Add(TypeLabel(Str(spec,"type","text"))); row.SubItems.Add(spec.ContainsKey("default")?InputText(spec["default"]).Replace("\r\n"," / "):"매번 입력"); row.SubItems.Add(uses.Count==0?"연결 없음":String.Join(", ",uses)); row.Tag=entry; inputList.Items.Add(row); if(key==chosen) row.Selected=true;
                }
            }
            if(UnsavedChanges) InvalidateSkill();
            else if(skillPreview==null) skillHelp.Text=savedTask==null?"프로세스를 먼저 저장하면 실제 저장본의 스킬을 볼 수 있습니다.":"저장본 v"+Num(savedTask,"revision",1)+" · 미리보기에는 업무 값과 화면을 넣지 않습니다.";
        }
        static string TypeLabel(string type)
        {
            switch(type) { case "date": return "날짜"; case "enum":return "선택 목록"; case "number":return "숫자"; case "integer":return "정수";case "boolean":return "예/아니요";case "list":return "반복 목록";default:return "글자"; }
        }
        void StartTrial()
        {
            var inputs=new Dictionary<string,object>();
            if(inputDefinitions.Count>0) using(var dialog=new LocalInputForm(new Dictionary<string,object>{{"inputs",inputDefinitions},{"timeout_seconds",600}})) {
                if(dialog.ShowDialog(this)!=DialogResult.OK) return;
                inputs=dialog.Values;
            }
            Send("test_run",new Dictionary<string,object>{{"name",processName.Text.Trim()},{"description",description.Text},{"inputs",inputs}},null);
            if(busy) { hiddenForPicker=true; Hide(); }
        }
        void BindSelectedInput()
        {
            if(inputList.SelectedItems.Count!=1 || steps.SelectedIndices.Count!=1) return;
            var chosen=inputList.SelectedItems[0].Tag as Dictionary<string,object>; int index=steps.SelectedIndices[0];
            var step=steps.Items[index].Tag as Dictionary<string,object>; string operation=Str(step,"action","");
            if(operation!="set_value" && operation!="select_option" && operation!="set_checked" && operation!="image_type_text") {
                ShowError("실제 입력값을 바꾸는 단계만 연결할 수 있습니다. 이미지 클릭은 선택할 글자만 바꿔 재생할 수 없습니다."); return;
            }
            Send("input_edit",new Dictionary<string,object>{{"edit",new Dictionary<string,object>{{"action","bind"},{"index",index},{"name",Str(chosen,"name","")},{"field",operation=="set_checked"?"checked":"value"}}}},null);
        }
        void UnbindSelectedInput()
        {
            if(inputList.SelectedItems.Count!=1 || steps.SelectedIndices.Count!=1)return;
            var chosen=inputList.SelectedItems[0].Tag as Dictionary<string,object>;int index=steps.SelectedIndices[0];string field=null;
            var uses=List(chosen,"uses");if(uses!=null)foreach(object raw in uses){var use=raw as Dictionary<string,object>;if(Num(use,"index",-1)!=index)continue;var path=List(use,"path");if(path!=null){var names=new List<string>();foreach(object part in path)names.Add(Convert.ToString(part));if(names.Count==1&&(names[0]=="value"||names[0]=="checked"))field=names[0];}else {string value=Str(use,"path","");if(value=="value"||value=="checked")field=value;}}
            if(field==null){ShowError("선택한 단계에 이 입력값이 직접 연결되어 있지 않습니다. ‘단계’ 탭에서 사용 중인 입력 단계를 선택하세요.");return;}
            using(var dialog=new LocalInputForm(new Dictionary<string,object>{{"inputs",new object[]{chosen}},{"timeout_seconds",600}})){
                dialog.Text="선택 단계의 고정값 지정";
                if(dialog.ShowDialog(this)!=DialogResult.OK)return;
                string name=Str(chosen,"name","");Send("input_edit",new Dictionary<string,object>{{"edit",new Dictionary<string,object>{{"action","unbind"},{"index",index},{"field",field},{"value",dialog.Values[name]}}}},null);
            }
        }
        void EditInputDefinition(Dictionary<string,object> entry)
        {
            string name=Str(entry,"name",""); var original=Map(entry,"spec")??new Dictionary<string,object>{{"type","text"}};
            if(name.Length==0) { var keys=new HashSet<string>(); foreach(var item in inputDefinitions) keys.Add(Str(item,"name","")); int n=1; while(keys.Contains("input_"+n)) n++; name="input_"+n; }
            using(var dialog=new Form()) {
                dialog.Text=entry==null?"실행할 입력값 추가":"실행할 입력값 수정"; dialog.Font=Font; dialog.BackColor=BackColor; dialog.StartPosition=FormStartPosition.CenterParent;
                dialog.FormBorderStyle=FormBorderStyle.FixedDialog; dialog.MaximizeBox=dialog.MinimizeBox=false; dialog.ClientSize=new Size(Px(580),Px(570));
                var area=new FlowLayoutPanel {Bounds=Box(22,18,536,470),AutoScroll=true,FlowDirection=FlowDirection.TopDown,WrapContents=false}; dialog.Controls.Add(area);
                Action<string> heading=delegate(string text) {area.Controls.Add(new Label {Text=text,Width=Px(500),Height=Px(28),ForeColor=muted});};
                heading("입력 이름 · 실행할 때 보이는 설명"); var label=new TextBox {Text=Str(original,"description","입력할 값"),Width=Px(500),MaxLength=200,Name="InputLabel",AccessibleName="입력 이름"}; area.Controls.Add(label);
                heading("값의 형식"); var kind=new ComboBox {DropDownStyle=ComboBoxStyle.DropDownList,Width=Px(500),Name="InputType",AccessibleName="입력 형식"};
                foreach(string type in new[]{"text","date","enum","number","integer","boolean","list"}) kind.Items.Add(new Choice {Code=type,Label=TypeLabel(type)}); area.Controls.Add(kind);
                var each=new CheckBox {Text="기본값 없이 실행할 때마다 입력",Checked=!original.ContainsKey("default"),Width=Px(500),Height=Px(34),Name="InputEveryRun",AccessibleName="매번 입력"}; area.Controls.Add(each);
                var optionsCaption=new Label {Text="선택 가능한 값 · 한 줄에 하나",Width=Px(500),Height=Px(28),ForeColor=muted}; area.Controls.Add(optionsCaption);
                var options=new TextBox {Multiline=true,ScrollBars=ScrollBars.Vertical,Width=Px(500),Height=Px(76),MaxLength=200000,Name="InputOptions",AccessibleName="선택 목록"}; area.Controls.Add(options);
                var itemType=new ComboBox {DropDownStyle=ComboBoxStyle.DropDownList,Width=Px(500),Name="ListItemType",AccessibleName="반복 목록의 항목 형식"};
                foreach(string type in new[]{"text","date","enum","number","integer","boolean"}) itemType.Items.Add(new Choice {Code=type,Label="목록 항목: "+TypeLabel(type)}); area.Controls.Add(itemType);
                var boundsCaption=new Label {Text="숫자 최소 / 최대 (비워 두면 제한 없음)",Width=Px(500),Height=Px(28),ForeColor=muted}; area.Controls.Add(boundsCaption);
                var limits=new FlowLayoutPanel {Width=Px(500),Height=Px(36),FlowDirection=FlowDirection.LeftToRight,WrapContents=false};
                var minimum=new TextBox {Width=Px(236),Name="InputMinimum",AccessibleName="최소값"}; var maximum=new TextBox {Width=Px(236),Name="InputMaximum",AccessibleName="최대값"}; limits.Controls.Add(minimum);limits.Controls.Add(maximum);area.Controls.Add(limits);
                heading("기본값 · 날짜는 YYYY-MM-DD, 예/아니요는 true 또는 false");
                var defaultValue=new TextBox {Multiline=true,ScrollBars=ScrollBars.Vertical,Width=Px(500),Height=Px(74),MaxLength=200000,Text=original.ContainsKey("default")?InputText(original["default"]):"",Name="InputDefault",AccessibleName="기본값"}; area.Controls.Add(defaultValue);
                var error=new Label {Bounds=Box(22,492,536,30),ForeColor=Color.Firebrick};dialog.Controls.Add(error);
                var confirm=new Button();ButtonStyle(confirm,"ApplyInputDefinition","변경 반영",Box(314,530,142,34),true,dialog);
                var cancel=new Button();ButtonStyle(cancel,"CancelInputDefinition","취소",Box(468,530,90,34),false,dialog);cancel.DialogResult=DialogResult.Cancel;dialog.CancelButton=cancel;
                Action refresh=delegate {string type=kind.SelectedItem==null?"text":((Choice)kind.SelectedItem).Code;string item=itemType.SelectedItem==null?"text":((Choice)itemType.SelectedItem).Code;bool enumeration=type=="enum" || type=="list"&&item=="enum";options.Visible=optionsCaption.Visible=enumeration;itemType.Visible=type=="list";string scalar=type=="list"?item:type;limits.Visible=boundsCaption.Visible=scalar=="number"||scalar=="integer";defaultValue.Enabled=!each.Checked;};
                kind.SelectedIndexChanged+=delegate {refresh();};itemType.SelectedIndexChanged+=delegate{refresh();};each.CheckedChanged+=delegate{refresh();};
                kind.SelectedIndex=0; foreach(Choice choice in kind.Items) if(choice.Code==Str(original,"type","text"))kind.SelectedItem=choice;
                var scalarSpec=Str(original,"type","")=="list"?Map(original,"items")??new Dictionary<string,object>():original;
                itemType.SelectedIndex=0;foreach(Choice choice in itemType.Items)if(choice.Code==Str(scalarSpec,"type","text"))itemType.SelectedItem=choice;
                options.Text=scalarSpec.ContainsKey("values")?InputText(scalarSpec["values"]):"";minimum.Text=scalarSpec.ContainsKey("minimum")?InputText(scalarSpec["minimum"]):"";maximum.Text=scalarSpec.ContainsKey("maximum")?InputText(scalarSpec["maximum"]):"";refresh();
                Dictionary<string,object> accepted=null;
                confirm.Click+=delegate {
                    try {
                        string type=((Choice)kind.SelectedItem).Code,scalar=type=="list"?((Choice)itemType.SelectedItem).Code:type;
                        var definition=new Dictionary<string,object>{{"type",scalar}};
                        if(scalar=="enum") {var values=new List<string>();foreach(string line in options.Text.Replace("\r\n","\n").Split('\n')) {if(line.Length==0||values.Contains(line))throw new ArgumentException("선택 목록은 비어 있거나 중복된 값 없이 작성하세요.");values.Add(line);}if(values.Count>100)throw new ArgumentException("선택 목록은 100개 이내입니다.");definition["values"]=values;}
                        if(scalar=="number"||scalar=="integer") {if(minimum.Text.Trim().Length>0)definition["minimum"]=ParseInputText(new Dictionary<string,object>{{"type",scalar}},minimum.Text);if(maximum.Text.Trim().Length>0)definition["maximum"]=ParseInputText(new Dictionary<string,object>{{"type",scalar}},maximum.Text);if(definition.ContainsKey("minimum")&&definition.ContainsKey("maximum")&&Convert.ToDouble(definition["minimum"])>Convert.ToDouble(definition["maximum"]))throw new ArgumentException("최소값은 최대값보다 클 수 없습니다.");}
                        if(type=="list") definition=new Dictionary<string,object>{{"type","list"},{"items",definition},{"max_items",Num(original,"max_items",1000)}};
                        definition["description"]=label.Text;
                        if(!each.Checked)definition["default"]=ParseInputText(definition,defaultValue.Text);
                        accepted=definition;dialog.DialogResult=DialogResult.OK;dialog.Close();
                    }catch(ArgumentException failure){error.Text=failure.Message;}
                };
                Rectangle working=Screen.FromControl(this).WorkingArea;if(dialog.Height>working.Height){dialog.AutoScroll=true;dialog.AutoScrollMinSize=dialog.ClientSize;dialog.Height=working.Height;}
                if(dialog.ShowDialog(this)==DialogResult.OK&&accepted!=null)Send("input_edit",new Dictionary<string,object>{{"edit",new Dictionary<string,object>{{"action","upsert"},{"name",name},{"spec",accepted}}}},null);
            }
        }
        void PopulateActions()
        {
            loadingActions = true; string previous = ActionCode; action.Items.Clear();
            var allowed = new HashSet<string>(StringComparer.Ordinal); var raw = List(selection, "actions");
            if (raw != null) foreach (object item in raw) allowed.Add(Convert.ToString(item));
            if (allowed.Contains("click")) { allowed.Add("double_click"); allowed.Add("right_click"); }
            foreach (Choice choice in allActions) {
                bool independent = choice.Code == "delay" || choice.Code == "checkpoint" || choice.Code == "wait_for_state";
                bool generic = choice.Code == "wait_for_element" || choice.Code == "press_key" || choice.Code == "hotkey";
                if (choice.Code == "scroll" && !IsImage) continue;
                if (selection == null || independent || generic || allowed.Contains(choice.Code)) action.Items.Add(choice);
            }
            if (action.Items.Count > 0) { action.SelectedIndex = 0; foreach (Choice choice in action.Items) if (choice.Code == previous) { action.SelectedItem = choice; break; } }
            loadingActions = false; UpdateActionFields();
        }
        void UpdateActionFields()
        {
            string code = ActionCode;
            valueSection.Visible = code == "set_value" || code == "set_checked" || code == "select_option" || code == "press_key" || code == "hotkey" || code == "checkpoint" || code == "scroll";
            checkedValue.Visible = code == "set_checked"; value.Visible = code != "set_checked";
            optionSection.Visible = code == "select_option"; timeSection.Visible = code == "delay" || code == "wait_for_element" || code == "wait_for_state" || code == "scroll"; completionSection.Visible = ImageMutation || ElementClick; expectSection.Visible = NeedsExpect;
            valueCaption.Text = code == "set_value" ? (IsImage ? "입력칸 전체를 바꿀 내용 (Ctrl+A 후 입력)" : "입력할 내용") : code == "select_option" ? "선택할 항목의 정확한 이름" : code == "press_key" ? "누를 키 이름 (예: Enter, Tab)" : code == "hotkey" ? "동시에 누를 키 (예: Ctrl+S)" : code == "scroll" ? "스크롤 방향 (up / down / left / right)" : "확인 지점 설명";
            if (code == "set_checked") valueCaption.Text = "실행 후 체크 상태";
            timeCaption.Text = code == "delay" ? "기다릴 시간 (초)" : code == "scroll" ? "스크롤 양 (1~20)" : "요소가 나타나기를 기다릴 최대 시간 (초)";
            timeUnit.Text = code == "scroll" ? "칸" : "초"; seconds.Minimum = code == "scroll" ? 1 : 0; seconds.Maximum = code == "scroll" ? 20 : 60;
            if (code == "scroll" && value.Text != "up" && value.Text != "down" && value.Text != "left" && value.Text != "right") value.Text = "down";
            actionHelp.Text = ImageMutation ? (AutomaticImage ? "이미지로 실행한 뒤 선택한 요소의 변화를 자동 확인합니다." : "현재 이미지 위치를 찾고 실행한 뒤 화면 확인에서 멈춥니다.") : code == "set_value" || code == "select_option" ? "입력·선택한 값이 실제로 반영됐는지 자동 확인합니다." : code == "checkpoint" ? "실행 시 화면을 캡처하고 사람이 확인할 때까지 멈춥니다." : code == "wait_for_element" ? "대상이 나타나는지 읽기만 하며 버튼을 누르지 않습니다." : code == "delay" ? "요소 선택 없이 추가할 수 있습니다." : "선택한 요소에 대해 한 번 실행하고 결과를 확인합니다.";
            if ((ImageMutation || ElementClick) && CompletionMode == "visual") actionHelp.Text = "별도 확인창 없이 채팅의 모델이 새 화면을 보고 결과를 확인합니다.";
            if (code == "wait_for_state") { timeCaption.Text = "완료 조건을 기다릴 최대 시간 (초)"; actionHelp.Text = "확인할 요소의 값·체크·활성 상태가 지정한 조건이 될 때까지 읽습니다."; }
            UpdateButtons();
        }
        void UpdateExpectFields()
        {
            var item = expectProperty.SelectedItem as Choice; bool boolean = item != null && (item.Code == "selected" || item.Code == "enabled");
            expected.Visible = !boolean; expectBoolean.Visible = boolean;
        }
        Dictionary<string, object> Target()
        {
            var choice = program.SelectedItem as Choice; if (choice == null) throw new InvalidOperationException("missing_program");
            return new Dictionary<string, object> { { "program_id", choice.Code }, { "pid", Num(choice.Data, "pid", 0) },
                { "window_id", Long(choice.Data, "window_id", 0) }, { "window_ref", Str(choice.Data, "window_ref", "main") } };
        }
        void UpdatePrograms(IEnumerable programs)
        {
            if (programs == null) return;
            var previous = program.SelectedItem as Choice;
            string priorId = previous == null ? "" : previous.Code, priorRef = previous == null ? "" : Str(previous.Data, "window_ref", "main");
            long priorWindow = previous == null ? 0 : Long(previous.Data, "window_id", 0);
            var replacements = new List<Choice>();
            foreach (object raw in programs) {
                var data = raw as Dictionary<string, object>; if (data == null) return;
                string id = Str(data, "program_id", "");
                if (id.Length == 0 || Long(data, "window_id", 0) < 0 || Num(data, "pid", 0) <= 0) return;
                replacements.Add(new Choice { Code = id, Label = Str(data, "label", id), Data = data });
                if (replacements.Count > MaximumTargets) return;
            }
            if (replacements.Count == 0) return;
            bool wasBusy = busy; busy = true;
            program.Items.Clear(); foreach (Choice replacement in replacements) program.Items.Add(replacement);
            program.SelectedIndex = 0;
            foreach (Choice replacement in replacements) if (replacement.Code == priorId && Str(replacement.Data, "window_ref", "main") == priorRef) { program.SelectedItem = replacement; break; }
            busy = wasBusy;
            var current = program.SelectedItem as Choice;
            if (current == null || current.Code != priorId || Str(current.Data, "window_ref", "main") != priorRef || Long(current.Data, "window_id", 0) != priorWindow) {
                selection = expectation = null; ShowThumbnail(null); selected.Text = "아직 선택한 요소가 없습니다"; expectSelected.Text = "확인할 요소를 선택하세요";
            }
            PopulateActions();
        }
        void StartRecording()
        {
            string reviewMode = null; bool autoPopup = true;
            using (var dialog = new Form()) {
                dialog.Text = "녹화 후 결과 확인 방법"; dialog.ClientSize = new Size(Px(570), Px(315));
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog;
                dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.BackColor = Color.FromArgb(245,247,251); dialog.Font = Font;
                bool vision = false;
                foreach (object value in imageCompletion.Items) { var choice = value as Choice; if (choice != null && choice.Code == "visual") vision = true; }
                dialog.Controls.Add(new Label { Text = vision ? "이미지 전달: 지원 · 모델이 화면을 판정할 수 있습니다." : "로컬 확인: 이미지 전송 없이 이 PC에서 직접 확인합니다.", Bounds = Box(20,15,530,45) });
                var human = new Button(); ButtonStyle(human,"ReviewHuman","사람 확인 · 로컬 창에서 직접 판정",Box(20,70,530,40),false,dialog);
                human.Click += delegate { reviewMode="human"; dialog.DialogResult=DialogResult.OK; };
                var visual = new Button(); ButtonStyle(visual,"ReviewVisual","모델 화면 판정 · 연결된 모델에 이미지 전달",Box(20,120,530,40),true,dialog);
                visual.Enabled = vision; visual.Click += delegate { reviewMode="visual"; dialog.DialogResult=DialogResult.OK; };
                dialog.Controls.Add(new Label { Text = "명시적 자동 조건: 녹화 후 편집에서 요소 값/상태 조건을 지정하세요. 조건 없는 확인은 자동 통과하지 않습니다. 기존 필수 사람 확인은 유지됩니다.", Bounds = Box(20,180,530,75) });
                var autoCheck = new CheckBox { Text = "팝업 열림은 저장 이미지로 자동 확인 (업무 결과 확인은 유지)", Checked = true, Bounds = Box(20,265,530,35) }; dialog.Controls.Add(autoCheck);
                if (dialog.ShowDialog(this) != DialogResult.OK || reviewMode == null) return;
                autoPopup = autoCheck.Checked;
            }
            string mode = "append";
            int index = steps.SelectedIndices.Count == 1 ? steps.SelectedIndices[0] : -1;
            if (stepCount > 0) {
                using (var dialog = new Form()) {
                    dialog.Text = "녹화를 어디에 반영할까요?"; dialog.ClientSize = new Size(Px(530), Px(248));
                    dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog;
                    dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.BackColor = Color.FromArgb(245,247,251);
                    dialog.Font = Font;
                    dialog.Controls.Add(new Label { Text = "기존 " + stepCount + "단계는 녹화를 마칠 때까지 유지됩니다.\n취소하거나 가져오기에 실패하면 바꾸지 않습니다.", Bounds = Box(22,18,486,50), ForeColor = ink });
                    string[] codes = { "append", "replace_selected", "replace_all" };
                    string[] titles = { "기존 단계 뒤에 추가", "선택한 단계 교체 · 연결된 확인 단계 포함", "전체 단계를 새 녹화로 교체" };
                    string chosen = null;
                    for (int i=0; i<codes.Length; i++) {
                        string code = codes[i]; var button = new Button();
                        ButtonStyle(button, "RecordMode_"+code, titles[i], Box(22,80+i*48,486,38), i==0, dialog);
                        button.Enabled = code != "replace_selected" || index >= 0;
                        button.Click += delegate { chosen=code; dialog.DialogResult=DialogResult.OK; dialog.Close(); };
                    }
                    if (dialog.ShowDialog(this) != DialogResult.OK || chosen == null) return;
                    mode = chosen;
                }
            }
            if(mode=="replace_selected" || mode=="replace_all") {
                var entry=index>=0?steps.Items[index].Tag as Dictionary<string,object>:null;
                int count=mode=="replace_all"?stepCount:Num(entry,"replacement_step_count",0);
                string impact=count>0?count.ToString()+"단계": "선택한 동작과 연결된 확인 단계";
                if(MessageBox.Show(this,impact+"를 새 녹화로 교체합니다. 기존 녹화 원본과 저장 이력은 보존하며, 녹화 취소 시 바꾸지 않습니다. 계속할까요?","녹화 교체 범위",MessageBoxButtons.OKCancel,MessageBoxIcon.Information)!=DialogResult.OK)return;
            }
            var payload = new Dictionary<string, object> { { "review_mode", reviewMode }, { "auto_popup", autoPopup }, { "mode", mode } };
            if (mode == "replace_selected") payload["index"] = index;
            Send("record", payload, null);
            if (busy) { hiddenForPicker=true; Hide(); }
        }
        void Pick(string purpose, bool image)
        {
            var payload = Target(); payload["purpose"] = purpose; Send("pick_target", payload, purpose);
            if (busy) { hiddenForPicker = true; Hide(); }
        }
        void ShowThumbnail(Dictionary<string, object> picked)
        {
            Image prior = thumbnail.Image; thumbnail.Image = null; if (prior != null) prior.Dispose(); thumbnail.Visible = false;
            string encoded = Str(picked, "thumbnail_png", ""); if (encoded.Length == 0 || encoded.Length > 1500000) return;
            try { using (var stream = new MemoryStream(Convert.FromBase64String(encoded))) using (Image image = Image.FromStream(stream)) thumbnail.Image = new Bitmap(image); thumbnail.Visible = true; }
            catch (ArgumentException) { ShowError("선택한 이미지의 미리보기를 표시하지 못했습니다."); }
            catch (FormatException) { ShowError("선택한 이미지 형식을 확인하지 못했습니다."); }
        }
        void ResolveInput()
        {
            if (steps.SelectedIndices.Count != 1) return;
            int index = steps.SelectedIndices[0]; var row = steps.Items[index].Tag as Dictionary<string, object>;
            if (row == null || !Object.Equals(row.ContainsKey("editable_input") ? row["editable_input"] : null, true)) return;
            using (Form dialog = new Form()) {
                dialog.Text = "녹화한 입력 내용 지정"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251);
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false;
                dialog.ClientSize = new Size(Px(500), Px(280));
                dialog.Controls.Add(new Label { Text = "다시 실행할 때 입력칸 전체를 바꿀 내용을 지정하세요.\n대상을 클릭하고 Ctrl+A 후 이 내용을 입력합니다.", Bounds = Box(24, 20, 452, 55), ForeColor = ink });
                var input = new TextBox { Multiline = true, MaxLength = 16000, ScrollBars = ScrollBars.Vertical, Bounds = Box(24, 88, 452, 118) }; dialog.Controls.Add(input);
                var confirm = new Button(); ButtonStyle(confirm, "ConfirmRecordedInput", "내용 반영", Box(264, 225, 112, 34), true, dialog); confirm.DialogResult = DialogResult.OK;
                var cancel = new Button(); ButtonStyle(cancel, "CancelRecordedInput", "취소", Box(384, 225, 92, 34), false, dialog); cancel.DialogResult = DialogResult.Cancel; dialog.CancelButton = cancel;
                if (dialog.ShowDialog(this) == DialogResult.OK) Send("resolve_input", new Dictionary<string, object> { { "index", index }, { "value", input.Text } }, null);
            }
        }
        void ShowStepEdit(Dictionary<string, object> item)
        {
            if (IsInputCandidate(item)) { ShowInputCandidate(item); return; }
            var editable = Map(item, "editable") ?? new Dictionary<string, object>();
            string operation = Str(item, "action", "");
            if (operation == "manual_entry") { ResolveInput(); return; }
            var changes = new Dictionary<string, object> { { "index", Num(item, "index", -1) } };
            using (var dialog = new Form()) {
                dialog.Text = "선택한 단계 수정"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251);
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false;
                dialog.ClientSize = new Size(Px(570), Px(610));
                var area = new FlowLayoutPanel { Bounds = Box(24, 20, 522, 510), AutoScroll = true, FlowDirection = FlowDirection.TopDown, WrapContents = false };
                dialog.Controls.Add(area);
                Action<string> heading = delegate(string caption) { area.Controls.Add(new Label { Text = caption, AutoSize = false, Width = Px(494), Height = Px(34), ForeColor = muted }); };
                Func<string, bool, TextBox> input = delegate(string contents, bool multiline) {
                    var box = new TextBox { Text = contents, Multiline = multiline, MaxLength = 16000, Width = Px(494), Height = Px(multiline ? 94 : 34), Margin = new Padding(0, 0, 0, Px(14)) };
                    if (multiline) box.ScrollBars = ScrollBars.Vertical; area.Controls.Add(box); return box;
                };
                heading(Str(item, "action_label", "단계") + " · " + Str(item, "label", "현재 창"));
                TextBox content = null, keys = null, condition = null, variableLabel = null;
                NumericUpDown timing = null; CheckBox parameter = null; ComboBox conditionBoolean = null, editedCheck = null;
                string keyField = null;
                ComboBox reviewChoice = null;
                if (operation == "checkpoint") {
                    heading("확인 방법 · 저장 시 새 revision에 반영됩니다");
                    reviewChoice = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = Px(494), Height = Px(34) };
                    reviewChoice.Items.Add(new Choice { Code="keep", Label="기존 확인 방식 유지 (" + Str(item,"review_mode","human") + ")" });
                    reviewChoice.Items.Add(new Choice { Code="human", Label="사람이 직접 확인" });
                    foreach (object value in imageCompletion.Items) { var c=value as Choice; if(c!=null && c.Code=="visual") reviewChoice.Items.Add(new Choice {Code="visual",Label="모델 화면 판정으로 변경"}); }
                    reviewChoice.SelectedIndex=0; area.Controls.Add(reviewChoice);
                    heading("필수 사람 확인은 유지하세요. 실행 중 승인 기록은 바뀌지 않습니다.");
                }
                if (editable.ContainsKey("value") || editable.ContainsKey("message")) {
                    heading("입력할 내용"); var variableSpec = Map(editable, "variable_spec");
                    if(variableSpec!=null && Str(variableSpec,"type","text")!="text") {
                        heading("형식이 지정된 입력값입니다. ‘입력값’ 탭에서 기본값과 형식을 수정하세요.");
                    } else {
                    content = input(variableSpec == null ? Str(editable, editable.ContainsKey("message") ? "message" : "value", "") : Str(variableSpec, "default", ""), true);
                    content.Name = content.AccessibleName = "EditedStepValue";
                    if (editable.ContainsKey("value")) {
                        parameter = new CheckBox { Text = "실행할 때 바꿀 값으로 사용", Checked = variableSpec != null, Width = Px(494), Height = Px(34), ForeColor = ink };
                        parameter.Name = parameter.AccessibleName = "UseInputVariable"; area.Controls.Add(parameter);
                        heading("입력 이름 (예: 조회 조건, 입력할 문구)");
                        variableLabel = input(variableSpec == null ? "입력할 값" : Str(variableSpec, "description", "입력할 값"), false);
                        variableLabel.Name = variableLabel.AccessibleName = "InputVariableLabel";
                    }
                    }
                }
                if (editable.ContainsKey("key") || editable.ContainsKey("keys")) {
                    keyField = editable.ContainsKey("key") ? "key" : "keys"; heading(keyField == "key" ? "누를 키 (예: Enter, Tab)" : "단축키 (예: Ctrl+S)");
                    var names = new List<string>(); if (keyField == "keys") foreach (object key in List(editable, "keys")) names.Add(Convert.ToString(key));
                    keys = input(keyField == "key" ? Str(editable, "key", "") : String.Join("+", names), false);
                }
                if (editable.ContainsKey("checked") && editable["checked"] is bool) {
                    heading("실행 후 체크 상태"); editedCheck = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = Px(230), Height = Px(34) }; editedCheck.Items.AddRange(new object[] { "체크함", "체크 해제" }); editedCheck.SelectedIndex = (bool)editable["checked"] ? 0 : 1; area.Controls.Add(editedCheck);
                }
                if (editable.ContainsKey("duration_ms") || editable.ContainsKey("timeout_ms")) {
                    heading("기다릴 시간 (초)"); timing = new NumericUpDown { DecimalPlaces = 2, Minimum = 0, Maximum = 60, Width = Px(180), Height = Px(34), Value = Math.Min(60, Math.Max(0, Num(editable, editable.ContainsKey("duration_ms") ? "duration_ms" : "timeout_ms", 0) / 1000M)) }; area.Controls.Add(timing);
                }
                if (editable.ContainsKey("expected")) {
                    heading("완료 확인 값");
                    if (editable["expected"] is bool) { conditionBoolean = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = Px(180), Height = Px(34) }; conditionBoolean.Items.AddRange(new object[] { "예", "아니요" }); conditionBoolean.SelectedIndex = (bool)editable["expected"] ? 0 : 1; area.Controls.Add(conditionBoolean); }
                    else { condition = input(Str(editable, "expected", ""), false); condition.Name = condition.AccessibleName = "EditedExpectedValue"; }
                }
                heading("대상과 다른 단계, 저장 이미지는 유지됩니다.");
                var accept = new Button(); ButtonStyle(accept, "ApplyStepChanges", "변경 반영", Box(310, 551, 130, 38), true, dialog);
                var cancel = new Button(); ButtonStyle(cancel, "CancelStepChanges", "취소", Box(452, 551, 94, 38), false, dialog); cancel.DialogResult = DialogResult.Cancel; dialog.CancelButton = cancel;
                accept.Click += delegate {
                    if (reviewChoice != null && ((Choice)reviewChoice.SelectedItem).Code != "keep") changes["review_mode"] = ((Choice)reviewChoice.SelectedItem).Code;
                    if (content != null) changes["value"] = content.Text;
                    if (keys != null) { if (keyField == "key") changes["key"] = keys.Text.Trim(); else { var names = new List<string>(); foreach (string key in keys.Text.Split('+')) if (key.Trim().Length > 0) names.Add(key.Trim()); changes["keys"] = names; } }
                    if (timing != null) changes["seconds"] = (double)timing.Value;
                    if (editedCheck != null) changes["checked"] = editedCheck.SelectedIndex == 0;
                    // An unchanged self-condition follows the edited input in
                    // Python; explicitly changed completion text stays explicit.
                    if (condition != null && condition.Text != Str(editable, "expected", "")) changes["expected"] = condition.Text;
                    if (conditionBoolean != null && (conditionBoolean.SelectedIndex == 0) != (bool)editable["expected"]) changes["expected"] = conditionBoolean.SelectedIndex == 0;
                    if (parameter != null) { changes["variable"] = parameter.Checked ? Str(editable, "variable", Str(editable, "suggested_variable", "input_1")) : ""; changes["variable_label"] = variableLabel.Text; }
                    dialog.DialogResult = DialogResult.OK; dialog.Close();
                };
                Rectangle working = Screen.FromControl(this).WorkingArea;
                if (dialog.Height > working.Height) { dialog.AutoScroll = true; dialog.AutoScrollMinSize = dialog.ClientSize; dialog.Height = working.Height; }
                if (dialog.ShowDialog(this) == DialogResult.OK) Send("edit_step", changes, null);
            }
        }
        static bool IsInputCandidate(Dictionary<string, object> item)
        {
            var proof = Map(item, "input_target_candidate");
            return Str(item, "action", "") == "manual_entry" && Str(item, "recognition", "") == "image_candidate"
                && proof != null && proof.Count == 4 && Str(proof, "source", "") == "preceding_recorded_click"
                && proof.ContainsKey("verified") && Object.Equals(proof["verified"], false)
                && proof.ContainsKey("requires_user_confirmation") && Object.Equals(proof["requires_user_confirmation"], true)
                && proof.ContainsKey("typed_text_recorded") && Object.Equals(proof["typed_text_recorded"], false)
                && Str(item, "thumbnail_png", "").Length > 0;
        }
        void ShowInputCandidate(Dictionary<string, object> item)
        {
            bool accepted = false, retarget = false;
            string chosenValue = "";
            using (var dialog = new Form()) {
                dialog.Text = "입력 위치와 내용 확인"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251);
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog;
                dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.ClientSize = new Size(Px(540), Px(520));
                dialog.Controls.Add(new Label { Text = "입력 위치 후보 · 값 확인 필요", Bounds = Box(24, 18, 492, 38), ForeColor = ink });
                dialog.Controls.Add(new Label { Text = "직전에 클릭한 위치의 이미지입니다. 입력칸으로 확인된 것은 아닙니다.\n아래 이미지와 입력할 내용을 확인해 주세요. 녹화 중 입력한 글자는 저장하지 않았습니다.", Bounds = Box(24, 60, 492, 66), ForeColor = muted });
                using (var picture = new PictureBox { Bounds = Box(24, 132, 492, 150), SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.White,
                    Name = "InputCandidatePreview", AccessibleName = "입력 위치 후보 이미지" }) {
                    dialog.Controls.Add(picture);
                    var previewAnchor = Map(item, "preview_anchor");
                    picture.Paint += delegate(object sender, PaintEventArgs args) {
                        PointF point;
                        if (picture.Image != null && PreviewAnchorPoint(previewAnchor, picture.Image.Size, picture.ClientSize, out point))
                            PaintPreviewAnchor(args.Graphics, point, scale);
                    };
                    string encoded = Str(item, "thumbnail_png", "");
                    try { if (encoded.Length <= 1500000) using (var stream = new MemoryStream(Convert.FromBase64String(encoded))) using (Image source = Image.FromStream(stream)) picture.Image = new Bitmap(source); }
                    catch (ArgumentException) { } catch (FormatException) { }
                    if (picture.Image == null) dialog.Controls.Add(new Label { Text = "이미지를 표시하지 못했습니다. 다른 위치 선택으로 대상을 지정하세요.", Bounds = Box(40, 174, 460, 70), BackColor = Color.White, ForeColor = muted });
                    dialog.Controls.Add(new Label { Text = "다시 실행할 때 입력할 내용", Bounds = Box(24, 294, 492, 30), ForeColor = ink });
                    var input = new TextBox { Multiline = true, MaxLength = 16000, ScrollBars = ScrollBars.Vertical, Bounds = Box(24, 328, 492, 76),
                        Name = "CandidateInputValue", AccessibleName = "입력할 값" }; dialog.Controls.Add(input);
                    dialog.Controls.Add(new Label { Text = "실행할 때 기존 내용을 전체 바꿉니다. 값을 비워두면 기존 내용이 지워집니다.\n지금 확인해도 프로그램에 입력하거나 재생하지 않습니다.", Bounds = Box(24, 410, 492, 46), ForeColor = muted });
                    var choose = new Button(); ButtonStyle(choose, "RetargetInputCandidate", "다른 위치 선택", Box(24, 472, 160, 34), false, dialog);
                    choose.Click += delegate { chosenValue = input.Text; retarget = true; dialog.DialogResult = DialogResult.OK; dialog.Close(); };
                    var confirm = new Button(); ButtonStyle(confirm, "ConfirmInputCandidate", "이 위치와 값 사용", Box(196, 472, 200, 34), true, dialog); confirm.Enabled = picture.Image != null;
                    confirm.Click += delegate { if (picture.Image == null) return; chosenValue = input.Text; accepted = true; dialog.DialogResult = DialogResult.OK; dialog.Close(); };
                    var cancel = new Button(); ButtonStyle(cancel, "CancelInputCandidate", "취소", Box(408, 472, 108, 34), false, dialog); cancel.DialogResult = DialogResult.Cancel;
                    dialog.CancelButton = cancel; dialog.AcceptButton = confirm;
                    Rectangle working = Screen.FromControl(this).WorkingArea;
                    if (dialog.Height > working.Height) { dialog.AutoScroll = true; dialog.AutoScrollMinSize = dialog.ClientSize; dialog.Height = working.Height; }
                    dialog.ShowDialog(this); if (picture.Image != null) picture.Image.Dispose();
                }
            }
            int index = Num(item, "index", -1);
            if (accepted) Send("resolve_input", new Dictionary<string, object> { { "index", index }, { "value", chosenValue } }, null);
            if (retarget) {
                Send("retarget_step", new Dictionary<string, object> { { "index", index }, { "action", "set_value" }, { "value", chosenValue } }, null);
                if (busy) { hiddenForPicker = true; Hide(); }
            }
        }
        void ShowStepPreview(Dictionary<string, object> item)
        {
            if (IsInputCandidate(item)) { ShowInputCandidate(item); return; }
            bool replace = false, completionRequested = false;
            string repairAction = Str(item, "repair_action", ""), repairValue = "", repairMethod = "auto";
            bool manual = Str(item, "action", "") == "manual_entry";
            using (Form dialog = new Form()) {
                dialog.Text = "기록한 동작과 대상 확인"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251);
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.ClientSize = new Size(Px(540), Px(360));
                dialog.Controls.Add(new Label { Text = Str(item, "program", "") + " · " + Str(item, "action_label", "") + "\n" + Str(item, "detail", ""), Bounds = Box(24, 18, 492, 80), ForeColor = ink });
                using (var picture = new PictureBox { Bounds = Box(24, 106, 492, 182), SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.White }) {
                    dialog.Controls.Add(picture); string encoded = Str(item, "thumbnail_png", "");
                    try { if (encoded.Length > 0 && encoded.Length <= 1500000) using (var stream = new MemoryStream(Convert.FromBase64String(encoded))) using (Image source = Image.FromStream(stream)) picture.Image = new Bitmap(source); }
                    catch (ArgumentException) { } catch (FormatException) { }
                    if (picture.Image == null && !manual) dialog.Controls.Add(new Label { Text = Str(item, "label", "현재 창") + "\n인식 방식: " + Str(item, "recognition", "uia"), Bounds = Box(40, 142, 460, 100), ForeColor = muted, BackColor = Color.White });
                    var close = new Button(); ButtonStyle(close, "CloseStepPreview", "확인", Box(404, 307, 112, 34), true, dialog); close.DialogResult = DialogResult.OK; dialog.AcceptButton = close; dialog.CancelButton = close;
                    if (Object.Equals(item.ContainsKey("repairable") ? item["repairable"] : null, true)) {
                        ComboBox repairKind = null; TextBox repairText = null;
                        if (manual) {
                            picture.Visible = false;
                            dialog.Controls.Add(new Label { Text = "이 단계에서 실행할 동작을 고르고 대상을 다시 지정하세요.\n다른 단계와 프로그램·팝업 연결은 유지합니다.", Bounds = Box(24, 110, 492, 54), ForeColor = ink });
                            repairKind = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Bounds = Box(24, 170, 260, 30), DisplayMember = "Label" };
                            repairKind.Items.Add(new Choice { Code = "click", Label = "클릭" }); repairKind.Items.Add(new Choice { Code = "double_click", Label = "두 번 클릭" }); repairKind.Items.Add(new Choice { Code = "right_click", Label = "오른쪽 클릭" }); repairKind.Items.Add(new Choice { Code = "set_value", Label = "글자 입력 (전체 바꾸기)" });
                            for (int index = 0; index < repairKind.Items.Count; index++) if (((Choice)repairKind.Items[index]).Code == repairAction) repairKind.SelectedIndex = index;
                            dialog.Controls.Add(repairKind);
                            repairText = new TextBox { Multiline = true, MaxLength = 16000, Bounds = Box(24, 210, 492, 72), Visible = false }; dialog.Controls.Add(repairText);
                            repairKind.SelectedIndexChanged += delegate { repairText.Visible = repairKind.SelectedItem != null && ((Choice)repairKind.SelectedItem).Code == "set_value"; };
                        }
                        Action<string> confirmRepair = delegate(string method) {
                            if (manual && repairKind.SelectedItem == null) { repairKind.Focus(); return; }
                            if (manual) { repairAction = ((Choice)repairKind.SelectedItem).Code; repairValue = repairText.Text; }
                            repairMethod = method; replace = true; dialog.DialogResult = DialogResult.OK; dialog.Close();
                        };
                        var choose = new Button(); ButtonStyle(choose, "RetargetStep", "이 단계만 다시 지정", Box(24, 307, 184, 34), false, dialog);
                        choose.Click += delegate { confirmRepair("auto"); };
                        if (!manual) { var completion = new Button(); ButtonStyle(completion, "SetStepCompletion", "완료 기준 지정", Box(216, 307, 180, 34), false, dialog); completion.Click += delegate { completionRequested = true; dialog.DialogResult = DialogResult.OK; dialog.Close(); }; }
                    }
                    dialog.ShowDialog(this); if (picture.Image != null) picture.Image.Dispose();
                }
            }
            if (replace) {
                var repair = new Dictionary<string, object> { { "index", Num(item, "index", -1) } };
                if (repairMethod == "image") repair["method"] = "image";
                if (manual) { repair["action"] = repairAction; if (repairAction == "set_value") repair["value"] = repairValue; }
                Send("retarget_step", repair, null); if (busy) { hiddenForPicker = true; Hide(); }
            }
            if (completionRequested) { Send("pick_completion", new Dictionary<string, object> { { "index", Num(item, "index", -1) } }, null); if (busy) { hiddenForPicker = true; Hide(); } }
        }
        void ShowCompletionValue(int index, Dictionary<string, object> chosen)
        {
            using (var dialog = new Form()) {
                dialog.Text = "자동 완료 기준"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251); dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.ClientSize = new Size(Px(540), Px(330));
                dialog.Controls.Add(new Label { Text = "확인 대상: " + Str(chosen, "label", "선택한 요소") + "\n동작 후 어떤 값이 되면 완료인가요?", Bounds = Box(24, 20, 492, 58), ForeColor = ink });
                var property = new ComboBox(); ConfigureCombo(property); property.Items.AddRange(new object[] { new Choice { Code = "value", Label = "입력값 / 선택값" }, new Choice { Code = "name", Label = "표시된 글자 / 이름" }, new Choice { Code = "selected", Label = "체크 / 선택 여부" }, new Choice { Code = "enabled", Label = "사용 가능 여부" } });
                Input(property, "CompletionProperty", Box(24, 90, 492, 34), dialog);
                var text = new TextBox(); Input(text, "CompletionExpectedValue", Box(24, 139, 492, 34), dialog);
                var boolean = new ComboBox(); ConfigureCombo(boolean); boolean.Items.AddRange(new object[] { "예", "아니요" }); Input(boolean, "CompletionBoolean", Box(24, 139, 492, 34), dialog); boolean.SelectedIndex = 0;
                property.SelectedIndexChanged += delegate { bool isBoolean = ((Choice)property.SelectedItem).Code == "selected" || ((Choice)property.SelectedItem).Code == "enabled"; boolean.Visible = isBoolean; text.Visible = !isBoolean; };
                property.SelectedIndex = Str(chosen, "role", "") == "Text" ? 1 : 0;
                dialog.Controls.Add(new Label { Text = "조건을 실제로 확인하면 확인창 없이 이어갑니다.\n이미지 동작은 값이 동작 전과 달라졌는지도 확인합니다.", Bounds = Box(24, 194, 492, 52), ForeColor = muted });
                var accept = new Button(); ButtonStyle(accept, "ApplyCompletion", "완료 기준 적용", Box(264, 270, 156, 36), true, dialog); accept.DialogResult = DialogResult.OK;
                var cancel = new Button(); ButtonStyle(cancel, "CancelCompletion", "취소", Box(432, 270, 84, 36), false, dialog); cancel.DialogResult = DialogResult.Cancel; dialog.CancelButton = cancel;
                if (dialog.ShowDialog(this) == DialogResult.OK) Send("set_completion", new Dictionary<string, object> { { "index", index }, { "selection_id", Str(chosen, "selection_id", "") }, { "property", ((Choice)property.SelectedItem).Code }, { "equals", boolean.Visible ? (object)(boolean.SelectedIndex == 0) : text.Text } }, null);
            }
        }
        void SetRecordingReview(Dictionary<string, object> value) {
            recordingReview = value;
            bool partial = value != null && Object.Equals(value.ContainsKey("partial") ? value["partial"] : null, true);
            pendingRecordingReview = partial && !Object.Equals(value.ContainsKey("acknowledged") ? value["acknowledged"] : null, true);
            reviewRecording.Visible = partial;
            reviewRecording.Text = pendingRecordingReview ? "녹화 경고 확인 · 실행 전" : "확인한 녹화 경고 다시 보기";
        }
        void ShowRecordingReview() {
            if (busy || recordingReview == null) return;
            using (var dialog = new Form()) {
                dialog.Text = "부분 기록과 누락 내용 확인"; dialog.StartPosition = FormStartPosition.CenterParent; dialog.Font = Font;
                dialog.BackColor = Color.FromArgb(246, 248, 252); dialog.ClientSize = new Size(Px(600), Px(330)); dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false;
                dialog.Controls.Add(new Label { Text = "모든 동작이 기록되었다는 뜻은 아닙니다", Font = new Font(Font.FontFamily, 14, FontStyle.Bold), ForeColor = ink, Bounds = Box(24, 20, 552, 40) });
                var messages = new List<string>(); foreach (object item in List(recordingReview, "messages")) messages.Add("• " + Convert.ToString(item));
                var text = new TextBox { ReadOnly = true, Multiline = true, ScrollBars = ScrollBars.Vertical, Text = String.Join("\r\n\r\n", messages), Bounds = Box(24, 72, 552, 168), BackColor = Color.White, BorderStyle = BorderStyle.FixedSingle };
                dialog.Controls.Add(text);
                var acknowledge = new Button(); ButtonStyle(acknowledge, "AcknowledgeRecordingWarnings", "누락 내용을 확인했습니다", Box(24, 270, 316, 36), true, dialog);
                acknowledge.Click += delegate { dialog.DialogResult = DialogResult.OK; dialog.Close(); };
                var close = new Button(); ButtonStyle(close, "CloseRecordingWarnings", "돌아가기", Box(356, 270, 220, 36), false, dialog); close.DialogResult = DialogResult.Cancel; dialog.CancelButton = close;
                if (dialog.ShowDialog(this) == DialogResult.OK) Send("ack_recording_warnings", new Dictionary<string, object> { { "acknowledged", true } }, null);
            }
        }
        void AddStep()
        {
            if (NeedsElement && selection == null) { ShowError("먼저 동작할 요소를 직접 선택해 주세요."); return; }
            if (NeedsExpect && expectation == null) { ShowError("동작 뒤 확인할 요소를 선택해 주세요."); return; }
            var payload = Target(); string code = ActionCode; payload["action"] = code;
            if (ImageMutation || ElementClick) payload["completion_mode"] = CompletionMode;
            if (NeedsElement) payload["selection_id"] = Str(selection, "selection_id", "");
            if (code == "set_value" || code == "select_option" || code == "checkpoint") payload["value"] = value.Text;
            if (code == "set_checked") payload["checked"] = checkedValue.SelectedIndex == 0;
            if (code == "press_key") payload["key"] = value.Text.Trim();
            if (code == "hotkey") { var keys = new List<string>(); foreach (string item in value.Text.Split('+')) if (item.Trim().Length > 0) keys.Add(item.Trim()); payload["keys"] = keys; }
            if (code == "delay") payload["seconds"] = (int)seconds.Value;
            if (code == "wait_for_element" || code == "wait_for_state") payload["timeout_seconds"] = (int)seconds.Value;
            if (code == "scroll") { payload["direction"] = value.Text.Trim().ToLowerInvariant(); payload["amount"] = (int)seconds.Value; }
            if (code == "select_option" && optionOrder.Text.Trim().Length > 0) { var order = new List<string>(); foreach (string line in optionOrder.Lines) if (line.Trim().Length > 0) order.Add(line.Trim()); payload["option_order"] = order; }
            if (NeedsExpect) {
                string property = ((Choice)expectProperty.SelectedItem).Code;
                object equals = property == "selected" || property == "enabled" ? (object)(((Choice)expectBoolean.SelectedItem).Code == "true") : expected.Text;
                payload["expect"] = new Dictionary<string, object> { { "selection_id", Str(expectation, "selection_id", "") }, { "property", property }, { "equals", equals } };
            }
            Send("add_step", payload, null);
        }
        void MoveStep(int direction)
        { if (steps.SelectedIndices.Count == 1) Send("move_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] }, { "direction", direction } }, null); }
        void Send(string command, Dictionary<string, object> payload, string purpose)
        {
            if (done || busy) return; pendingSeq = ++seq; pendingAction = command; pendingPurpose = purpose; busy = true;
            try { Write(response + ".command.json", new Dictionary<string, object> { { "nonce", nonce }, { "seq", pendingSeq }, { "action", command }, { "payload", payload } }); }
            catch { busy = false; Finish("runtime_failed", new Dictionary<string, object> { { "code", "command_write_failed" } }); return; }
            if (busy) { status.Text = command == "pick_element" || command == "pick_image" || command == "pick_target" ? "화면에서 대상을 고르고 확인해 주세요." : command == "record" ? "녹화 창에서 시작한 뒤 연결한 프로그램을 직접 조작하세요." : command == "test_run" ? "현재 초안을 실제 프로그램에서 시험하고 있습니다…" : "요청한 내용을 확인하고 있습니다…"; status.ForeColor = muted; }
            UpdateButtons();
        }
        void Tick(object sender, EventArgs args)
        {
            if (done) return; if (DateTime.UtcNow >= deadline) { Finish("timeout", null); return; }
            if (!busy) {
                string trialPath = response + ".trial.json";
                if (File.Exists(trialPath)) try {
                    var update = Json.Deserialize<Dictionary<string, object>>(Read(trialPath, 262144));
                    int trialSeq = Num(update, "seq", 0);
                    if (Str(update, "nonce", "") != nonce) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "trial_nonce_mismatch" } }); return; }
                    if (trialSeq > externalTrialSequence) {
                        externalTrialSequence = trialSeq;
                        if (Str(update, "state", "") == "running") {
                            Enabled = false; Hide();
                            Write(response + ".trial.ready.json", new Dictionary<string, object> { { "nonce", nonce }, { "seq", trialSeq }, { "hidden", !Visible }, { "helper_pid", Process.GetCurrentProcess().Id } });
                        } else if (Str(update, "state", "") == "finished") {
                            Enabled = true; Show(); Activate(); var tested = Map(update, "result");
                            status.Text = TrialMessage(tested);
                            status.ForeColor = muted; UpdateButtons();
                        }
                    }
                } catch (IOException) { return; } catch (ArgumentException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "trial_invalid_response" } }); return; }
                return;
            }
            string path = response + ".event.json"; if (!File.Exists(path)) return;
            Dictionary<string, object> result;
            try {
                result = Json.Deserialize<Dictionary<string, object>>(Read(path, MaximumMessageBytes));
            } catch (InvalidDataException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_too_large" } }); return;
            } catch (IOException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_read_failed" } }); return;
            } catch (UnauthorizedAccessException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_read_failed" } }); return;
            } catch (ArgumentException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "invalid_event" } }); return; }
            if (Str(result, "nonce", "") != nonce) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_nonce_mismatch" } }); return; }
            int acknowledged = Num(result, "seq", -1); if (acknowledged < pendingSeq) return;
            if (acknowledged != pendingSeq) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_sequence_mismatch" } }); return; }
            busy = false; if (hiddenForPicker) { hiddenForPicker = false; Show(); Activate(); }
            if (result.ContainsKey("recording_receipt_available")) reanalyzeRecording.Visible = Object.Equals(result["recording_receipt_available"], true);
            if (Str(result, "status", "") != "ok") { ShowError(Str(result, "message", "요청을 처리하지 못했습니다. 입력 내용을 확인해 주세요.")); UpdateButtons(); return; }
            if (result.ContainsKey("programs")) UpdatePrograms(List(result, "programs"));
            if (pendingAction == "pick_element" || pendingAction == "pick_image" || pendingAction == "pick_target") {
                var picked = Map(result, "selection");
                if (picked == null || Str(picked, "selection_id", "").Length == 0) { ShowError("선택 결과를 받지 못했습니다. 요소를 다시 선택해 주세요."); UpdateButtons(); return; }
                string purpose = Str(result, "purpose", pendingPurpose ?? "action"); string caption = Str(picked, "label", "이름 없는 요소") + "  ·  " + (Str(picked, "recognition", "") == "image" ? "이미지 인식" : Str(picked, "role", "") + " / 요소 인식");
                if (purpose == "expect") { expectation = picked; expectSelected.Text = caption; } else { selection = picked; selected.Text = caption; ShowThumbnail(picked); PopulateActions(); }
            }
            if (pendingAction == "diagnose") ShowDiagnostic(result);
            if (pendingAction == "pick_completion") { var picked = Map(result, "selection"); if (picked != null) ShowCompletionValue(Num(result, "completion_index", -1), picked); }
            if (result.ContainsKey("steps")) UpdateSteps(List(result, "steps"));
            if (result.ContainsKey("recording_review")) SetRecordingReview(Map(result, "recording_review"));
            UpdateAuthoring(result);
            if(pendingAction=="skill_preview") {
                DisplaySkillPreview(Map(result,"skill_preview"));
            }
            if(pendingAction=="skill_export") {
                var exported=Map(result,"skill_export");
                result["message"]="스킬 파일을 이 PC에 저장했습니다. "+Str(exported,"path","");
            }
            if (pendingAction == "preview_step") { var preview = Map(result, "preview"); if (preview != null) { if (pendingPurpose == "edit") ShowStepEdit(preview); else ShowStepPreview(preview); } }
            if (pendingAction == "test_run") {
                var tested = Map(result, "test_result");
                result["message"] = TrialMessage(tested);
            }
            if (pendingAction == "save") {
                var saved = Map(result, "saved_task");
                if (saved == null || Str(saved, "id", Str(saved,"task_id"," ")).Trim().Length == 0) { ShowError("저장 완료를 확인하지 못했습니다. 다시 저장하기 전에 연결 상태를 확인해 주세요."); UpdateButtons(); return; }
                savedTask=saved;draftDirty=false;savedName=processName.Text.Trim();savedDescription=description.Text;editingExisting=true;
                saveMode.Enabled=true;saveMode.SelectedIndex=0;InvalidateSkill();
                skillHelp.Text="저장본 v"+Num(savedTask,"revision",1)+" · 스킬 보기에서 미리보기를 열 수 있습니다.";
                result["message"]="프로세스를 저장했습니다. 입력값을 시험하거나 스킬 보기에서 내보낼 수 있습니다.";
            }
            status.ForeColor = muted; status.Text = Str(result, "message", pendingAction == "add_step" ? "단계를 추가했습니다. 다음 요소와 동작을 이어서 알려주세요." : "내용을 반영했습니다."); UpdateButtons();
        }
        void UpdateSteps(IEnumerable entries)
        {
            int selectedIndex=steps.SelectedIndices.Count==1?steps.SelectedIndices[0]:-1;
            steps.BeginUpdate(); steps.Items.Clear(); stepCount = 0; pendingInputs = false;
            if (entries != null) foreach (object raw in entries) {
                var entry = raw as Dictionary<string, object>; if (entry == null) continue;
                string code = Str(entry, "action", ""), label = Str(entry, "action_label", code); foreach (Choice choice in allActions) if (choice.Code == code) { label = choice.Label; break; }
                string target = Str(entry, "label", Str(entry, "program", ""));
                var row = new ListViewItem((++stepCount).ToString()); row.SubItems.Add(Str(entry, "program", "")); row.SubItems.Add(label + (target.Length == 0 ? "" : " · " + target));
                row.SubItems.Add(Str(entry, "detail", Str(entry, "summary", ""))); row.ToolTipText = Str(entry, "program", "") + " · " + label + " · " + target + "\n" + Str(entry, "detail", ""); row.Tag = entry;
                if (Object.Equals(entry.ContainsKey("requires_input") ? entry["requires_input"] : null, true)) { pendingInputs = true; row.ForeColor = Color.FromArgb(173, 48, 38); }
                steps.Items.Add(row);
            }
            if(selectedIndex>=0 && selectedIndex<steps.Items.Count)steps.Items[selectedIndex].Selected=true;
            steps.ShowItemToolTips = true; steps.EndUpdate();
        }
        void ShowDiagnostic(Dictionary<string, object> result)
        {
            using (var dialog = new Form()) {
                dialog.Text = "요소 진단 · 읽기 전용 · 이 PC에만 저장"; dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.Font = Font; dialog.ClientSize = new Size(Px(760), Px(560)); dialog.MinimumSize = new Size(Px(540), Px(360));
                var heading = new Label { Text = "원본 Provider / Driver / MCP를 각각 확인합니다. 진단 정보는 실행 대상이 아닙니다.", Dock = DockStyle.Top, Height = Px(54), Padding = new Padding(Px(12)) };
                var report = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Dock = DockStyle.Fill, Font = new Font("Consolas", 9) };
                report.Text = Str(result, "diagnostic_preview", "진단 파일을 확인하세요.");
                var footer = new TextBox { Text = "JSON 저장 위치: " + Str(result, "report_path", "") + "  (종료된 실행의 진단은 7일 뒤 자동 정리)", ReadOnly = true, Multiline = true, Dock = DockStyle.Bottom, Height = Px(60) };
                dialog.Controls.Add(report); dialog.Controls.Add(heading); dialog.Controls.Add(footer);
                dialog.ShowDialog(this);
            }
        }

        void UpdateButtons()
        {
            program.Enabled = action.Enabled = value.Enabled = checkedValue.Enabled = optionOrder.Enabled = seconds.Enabled = expectProperty.Enabled = expected.Enabled = expectBoolean.Enabled = imageCompletion.Enabled = processName.Enabled = description.Enabled = !busy;
            var targetProgram = program.SelectedItem as Choice;
            bool connected = targetProgram != null && Long(targetProgram.Data, "window_id", 0) > 0;
            pick.Enabled = pickImage.Enabled = pickExpect.Enabled = record.Enabled = !busy && connected;
            reanalyzeRecording.Enabled = diagnose.Enabled = !busy && connected;
            add.Enabled = !busy && stepCount < maximumSteps && ActionCode.Length > 0 && (!NeedsElement || selection != null) && (!NeedsExpect || expectation != null);
            save.Enabled = !busy && stepCount > 0 && processName.Text.Trim().Length > 0;
            save.Text = pendingInputs || pendingRecordingReview ? "초안 저장 · 실행 불가" : editingExisting && saveMode.SelectedIndex == 0 ? "기존 작업 저장" : "새 작업 저장";
            trial.Enabled = save.Enabled && !pendingInputs && !pendingRecordingReview;
            reviewRecording.Enabled = !busy;
            int index = steps.SelectedIndices.Count == 1 ? steps.SelectedIndices[0] : -1;
            var entry = index >= 0 ? steps.Items[index].Tag as Dictionary<string, object> : null;
            editInput.Enabled = !busy && entry != null && Object.Equals(entry.ContainsKey("editable_input") ? entry["editable_input"] : null, true);
            up.Enabled = !busy && index > 0; down.Enabled = !busy && index >= 0 && index < stepCount - 1; remove.Enabled = !busy && index >= 0;
            undo.Enabled=!busy&&canUndo;redo.Enabled=!busy&&canRedo;
            bool inputSelected=inputList.SelectedItems.Count==1;
            newInput.Enabled=!busy&&inputDefinitions.Count<30;changeInput.Enabled=removeInput.Enabled=!busy&&inputSelected;bindInput.Enabled=unbindInput.Enabled=!busy&&inputSelected&&index>=0;
            previewSkill.Enabled=!busy&&savedTask!=null&&!UnsavedChanges;exportSkill.Enabled=!busy&&skillPreview!=null&&!UnsavedChanges;
        }
        void ShowError(string message) { status.ForeColor = Color.FromArgb(173, 48, 38); status.Text = message; }
        void Finish(string outcome, Dictionary<string, object> extra)
        {
            if (done) return; done = true; timer.Stop(); var result = extra ?? new Dictionary<string, object>(); result["nonce"] = nonce; result["status"] = outcome;
            try { Write(response, result); } finally { Close(); }
        }
    }
}
