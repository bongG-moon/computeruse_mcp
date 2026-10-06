// Human-authored process editor. Local file IPC only; never injects input,
// reads screen values, launches applications, or runs arbitrary code.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
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
    internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 2097152 };
    internal static string Str(Dictionary<string, object> data, string key, string fallback)
    { object value; return data != null && data.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : fallback; }
    internal static int Num(Dictionary<string, object> data, string key, int fallback)
    { int result; return Int32.TryParse(Str(data, key, ""), out result) ? result : fallback; }
    internal static long Long(Dictionary<string, object> data, string key, long fallback)
    { long result; return Int64.TryParse(Str(data, key, ""), out result) ? result : fallback; }
    internal static Dictionary<string, object> Map(Dictionary<string, object> data, string key)
    { object value; return data != null && data.TryGetValue(key, out value) ? value as Dictionary<string, object> : null; }
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
        if (Encoding.UTF8.GetByteCount(text) > 2097152) throw new InvalidOperationException("message_too_large");
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
        if (args.Length != 3 || args[0] != "--edit") return 2;
        string nonce = null, response = null;
        try {
            if (!Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[2])) return 2;
            string request = Path.GetFullPath(args[1]), result = Path.GetFullPath(args[2]);
            if (!String.Equals(Path.GetDirectoryName(request), Path.GetDirectoryName(result), StringComparison.OrdinalIgnoreCase)) return 2;
            var data = Json.Deserialize<Dictionary<string, object>>(Read(request, 262144));
            string token = Str(data, "nonce", ""); if (!ValidNonce(token)) return 2;
            nonce = token; response = result;
            if (!Environment.UserInteractive) throw new InvalidOperationException("interactive_desktop_unavailable");
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            using (EditorForm editor = new EditorForm(data, result)) Application.Run(editor);
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
        readonly ComboBox program = new ComboBox(), action = new ComboBox(), expectProperty = new ComboBox(), expectBoolean = new ComboBox();
        readonly Label selected = new Label(), expectSelected = new Label(), status = new Label(), valueCaption = new Label(), timeCaption = new Label(), timeUnit = new Label(), actionHelp = new Label();
        readonly TextBox value = new TextBox(), optionOrder = new TextBox(), expected = new TextBox(), processName = new TextBox(), description = new TextBox();
        readonly NumericUpDown seconds = new NumericUpDown();
        readonly ListView steps = new ListView();
        readonly FlowLayoutPanel fields = new FlowLayoutPanel();
        readonly Panel valueSection = new Panel(), optionSection = new Panel(), timeSection = new Panel(), expectSection = new Panel();
        readonly Button pick = new Button(), pickImage = new Button(), record = new Button(), editInput = new Button(), pickExpect = new Button(), add = new Button(), save = new Button(), up = new Button(), down = new Button(), remove = new Button();
        readonly PictureBox thumbnail = new PictureBox();
        readonly List<Choice> allActions = new List<Choice>();
        Dictionary<string, object> selection, expectation;
        int seq, pendingSeq, stepCount;
        string pendingAction, pendingPurpose;
        bool busy, done, hiddenForPicker, loadingActions, pendingInputs;
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
        bool NeedsExpect { get { string code = ActionCode; return !IsImage && (code == "click" || code == "double_click" || code == "right_click" || code == "press_key" || code == "hotkey"); } }
        bool NeedsElement { get { return ActionCode != "delay" && ActionCode != "checkpoint"; } }

        internal EditorForm(Dictionary<string, object> request, string response)
        {
            this.response = response; nonce = Str(request, "nonce", "");
            int lifetime = Num(request, "timeout_seconds", 600); if (lifetime < 30 || lifetime > 1800) throw new ArgumentException("invalid_timeout");
            deadline = DateTime.UtcNow.AddSeconds(lifetime);
            var programs = List(request, "programs"); if (programs == null) throw new ArgumentException("missing_programs");
            foreach (object raw in programs) {
                var data = raw as Dictionary<string, object>; if (data == null) throw new ArgumentException("invalid_program");
                string id = Str(data, "program_id", "");
                if (id.Length == 0 || Long(data, "window_id", 0) <= 0 || Num(data, "pid", 0) <= 0) throw new ArgumentException("invalid_program_identity");
                program.Items.Add(new Choice { Code = id, Label = Str(data, "label", id), Data = data });
                if (program.Items.Count > 100) throw new ArgumentException("too_many_programs");
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
            var targetSection = Section(148); Caption("대상 프로그램과 창", 0, targetSection); ConfigureCombo(program); Input(program, "TargetProgram", Box(0, 29, 394, 34), targetSection);
            ButtonStyle(record, "RecordActions", "● 동작 녹화", Box(0, 72, 394, 34), false, targetSection);
            targetSection.Controls.Add(new Label { Text = "연결한 창에서 직접 하는 클릭·입력만 기록합니다.", ForeColor = muted, Font = new Font(Font.FontFamily, 8), Bounds = Box(0, 111, 394, 32) });
            var elementSection = Section(184); Caption("1  동작할 요소", 0, elementSection);
            selected.Text = "아직 선택한 요소가 없습니다"; selected.AutoEllipsis = true; selected.Bounds = Box(0, 28, 394, 28); elementSection.Controls.Add(selected); Identify(selected, "SelectedElement", "현재 선택한 요소");
            ButtonStyle(pick, "PickElement", "요소 직접 선택", Box(0, 63, 190, 34), false, elementSection);
            ButtonStyle(pickImage, "PickImage", "이미지로 선택", Box(204, 63, 190, 34), false, elementSection);
            thumbnail.Bounds = Box(0, 104, 394, 70); thumbnail.SizeMode = PictureBoxSizeMode.Zoom; thumbnail.BackColor = Color.FromArgb(245, 247, 251); thumbnail.Visible = false; Identify(thumbnail, "TargetThumbnail", "선택한 대상 이미지"); elementSection.Controls.Add(thumbnail);
            var operationSection = Section(92); Caption("2  어떤 동작을 할까요?", 0, operationSection); ConfigureCombo(action); Input(action, "StepAction", Box(0, 29, 394, 34), operationSection);
            actionHelp.ForeColor = muted; actionHelp.Font = new Font(Font.FontFamily, 8); actionHelp.Bounds = Box(0, 65, 394, 25); operationSection.Controls.Add(actionHelp);
            AddSection(valueSection, 95); valueCaption.Bounds = Box(0, 0, 394, 25); valueCaption.ForeColor = muted; valueCaption.Font = new Font(Font.FontFamily, 9, FontStyle.Bold); valueSection.Controls.Add(valueCaption);
            value.Multiline = true; value.MaxLength = 16000; value.ScrollBars = ScrollBars.Vertical; Input(value, "StepValue", Box(0, 29, 394, 58), valueSection);
            AddSection(optionSection, 124); Caption("선택 항목의 실제 순서 (한 줄에 하나, 선택 사항)", 0, optionSection);
            optionOrder.Multiline = true; optionOrder.ScrollBars = ScrollBars.Vertical; optionOrder.MaxLength = 30000; Input(optionOrder, "OptionOrder", Box(0, 29, 394, 82), optionSection);
            AddSection(timeSection, 74); timeCaption.Bounds = Box(0, 0, 394, 25); timeCaption.ForeColor = muted; timeCaption.Font = new Font(Font.FontFamily, 9, FontStyle.Bold); timeSection.Controls.Add(timeCaption);
            seconds.DecimalPlaces = 0; seconds.Minimum = 0; seconds.Maximum = 60; seconds.Value = 20; Input(seconds, "StepSeconds", Box(0, 29, 130, 34), timeSection);
            timeUnit.Text = "초"; timeUnit.Bounds = Box(142, 32, 80, 28); timeUnit.ForeColor = muted; timeSection.Controls.Add(timeUnit);
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
            processName.MaxLength = 100; Input(processName, "ProcessName", Box(20, 46, 508, 34), right);
            right.Controls.Add(new Label { Text = "업무 설명 (선택 사항)", Bounds = Box(20, 93, 508, 25), ForeColor = muted, Font = new Font(Font.FontFamily, 9, FontStyle.Bold) });
            description.Multiline = true; description.MaxLength = 4000; description.ScrollBars = ScrollBars.Vertical; Input(description, "ProcessDescription", Box(20, 122, 508, 56), right);
            right.Controls.Add(new Label { Text = "실행 순서 · 두 번 클릭 / Enter로 대상 확인", Bounds = Box(20, 195, 508, 25), ForeColor = blue, Font = new Font(Font.FontFamily, 11, FontStyle.Bold) });
            steps.Bounds = Box(20, 229, 508, 260); steps.View = View.Details; steps.FullRowSelect = true; steps.MultiSelect = false; steps.HideSelection = false;
            steps.HeaderStyle = ColumnHeaderStyle.Nonclickable; steps.GridLines = false; steps.BorderStyle = BorderStyle.FixedSingle;
            steps.Columns.Add("순서", Px(44)); steps.Columns.Add("프로그램", Px(104)); steps.Columns.Add("동작 / 대상", Px(166)); steps.Columns.Add("내용", Px(170)); Identify(steps, "ProcessSteps", "프로세스 실행 순서"); right.Controls.Add(steps);
            ButtonStyle(up, "MoveStepUp", "위로", Box(20, 507, 82, 38), false, right); ButtonStyle(down, "MoveStepDown", "아래로", Box(110, 507, 82, 38), false, right);
            ButtonStyle(editInput, "ResolveInput", "입력 내용 지정", Box(200, 507, 176, 38), false, right); ButtonStyle(remove, "RemoveStep", "단계 삭제", Box(384, 507, 144, 38), false, right);
            status.Text = "요소를 선택하면 가능한 동작을 고를 수 있습니다."; status.ForeColor = muted; status.Bounds = Box(26, 688, 700, 44); Identify(status, "ProcessEditorStatus", "프로세스 작성 상태"); Controls.Add(status);
            ButtonStyle(save, "SaveProcess", "프로세스 저장", Box(748, 692, 176, 38), true, this);
            var cancel = new Button(); ButtonStyle(cancel, "CancelProcess", "닫기", Box(940, 692, 96, 38), false, this);
            allActions.AddRange(new Choice[] {
                new Choice { Code = "click", Label = "클릭" }, new Choice { Code = "double_click", Label = "두 번 클릭" }, new Choice { Code = "right_click", Label = "오른쪽 클릭" },
                new Choice { Code = "set_value", Label = "글자 입력" }, new Choice { Code = "select_option", Label = "선택 상자에서 항목 선택" },
                new Choice { Code = "press_key", Label = "키 누르기 (Enter, Tab 등)" }, new Choice { Code = "hotkey", Label = "단축키 (Ctrl+S 등)" }, new Choice { Code = "scroll", Label = "스크롤 (이미지)" },
                new Choice { Code = "wait_for_element", Label = "요소가 나타날 때까지 대기" }, new Choice { Code = "delay", Label = "정해진 시간 대기" }, new Choice { Code = "checkpoint", Label = "화면 캡처 후 확인할 때까지 일시정지" }
            });
            program.SelectedIndexChanged += delegate { if (!busy) { selection = expectation = null; ShowThumbnail(null); selected.Text = "아직 선택한 요소가 없습니다"; expectSelected.Text = "확인할 요소를 선택하세요"; PopulateActions(); } };
            action.SelectedIndexChanged += delegate { if (!loadingActions) UpdateActionFields(); };
            expectProperty.SelectedIndexChanged += delegate { UpdateExpectFields(); };
            pick.Click += delegate { Pick("action", false); }; pickImage.Click += delegate { Pick("action", true); }; pickExpect.Click += delegate { Pick("expect", false); }; add.Click += delegate { AddStep(); };
            record.Click += delegate { Send("record", new Dictionary<string, object>(), null); if (busy) { hiddenForPicker = true; Hide(); } };
            editInput.Click += delegate { ResolveInput(); };
            save.Click += delegate { Send("save", new Dictionary<string, object> { { "name", processName.Text.Trim() }, { "description", description.Text } }, null); };
            cancel.Click += delegate { Finish("cancelled", null); };
            up.Click += delegate { MoveStep(-1); }; down.Click += delegate { MoveStep(1); };
            remove.Click += delegate { if (steps.SelectedIndices.Count == 1) Send("remove_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null); };
            steps.SelectedIndexChanged += delegate { UpdateButtons(); }; processName.TextChanged += delegate { UpdateButtons(); };
            steps.DoubleClick += delegate { if (!busy && steps.SelectedIndices.Count == 1) Send("preview_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null); };
            steps.KeyDown += delegate(object sender, KeyEventArgs e) {
                if (e.KeyCode == Keys.Enter && !busy && steps.SelectedIndices.Count == 1) {
                    e.Handled = true; e.SuppressKeyPress = true;
                    Send("preview_step", new Dictionary<string, object> { { "index", steps.SelectedIndices[0] } }, null);
                }
            };
            program.SelectedIndex = 0; PopulateActions(); UpdateExpectFields();
            var draft = Map(request, "draft"); processName.Text = Str(draft, "name", ""); description.Text = Str(draft, "description", ""); UpdateSteps(List(draft, "steps")); UpdateButtons();
            FormClosing += delegate(object sender, FormClosingEventArgs args) { if (!done) { args.Cancel = true; Finish("cancelled", null); } };
            Shown += delegate { BeginInvoke(new Action(delegate {
                if (!Visible || !IsWindowVisible(Handle)) { Finish("startup_failed", new Dictionary<string, object> { { "code", "editor_not_visible" } }); return; }
                Write(response + ".ready.json", new Dictionary<string, object> { { "nonce", nonce }, { "status", "ready" }, { "helper_pid", Process.GetCurrentProcess().Id }, { "helper_window_id", Handle.ToInt64() } });
                timer.Interval = 100; timer.Tick += Tick; timer.Start();
            })); };
        }

        void PopulateActions()
        {
            loadingActions = true; string previous = ActionCode; action.Items.Clear();
            var allowed = new HashSet<string>(StringComparer.Ordinal); var raw = List(selection, "actions");
            if (raw != null) foreach (object item in raw) allowed.Add(Convert.ToString(item));
            if (allowed.Contains("click")) { allowed.Add("double_click"); allowed.Add("right_click"); }
            foreach (Choice choice in allActions) {
                bool independent = choice.Code == "delay" || choice.Code == "checkpoint";
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
            valueSection.Visible = code == "set_value" || code == "select_option" || code == "press_key" || code == "hotkey" || code == "checkpoint" || code == "scroll";
            optionSection.Visible = code == "select_option"; timeSection.Visible = code == "delay" || code == "wait_for_element" || code == "scroll"; expectSection.Visible = NeedsExpect;
            valueCaption.Text = code == "set_value" ? (IsImage ? "입력칸 전체를 바꿀 내용 (Ctrl+A 후 입력)" : "입력할 내용") : code == "select_option" ? "선택할 항목의 정확한 이름" : code == "press_key" ? "누를 키 이름 (예: Enter, Tab)" : code == "hotkey" ? "동시에 누를 키 (예: Ctrl+S)" : code == "scroll" ? "스크롤 방향 (up / down / left / right)" : "확인 지점 설명";
            timeCaption.Text = code == "delay" ? "기다릴 시간 (초)" : code == "scroll" ? "스크롤 양 (1~20)" : "요소가 나타나기를 기다릴 최대 시간 (초)";
            timeUnit.Text = code == "scroll" ? "칸" : "초"; seconds.Minimum = code == "scroll" ? 1 : 0; seconds.Maximum = code == "scroll" ? 20 : 60;
            if (code == "scroll" && value.Text != "up" && value.Text != "down" && value.Text != "left" && value.Text != "right") value.Text = "down";
            actionHelp.Text = IsImage && code != "wait_for_element" && code != "delay" && code != "checkpoint" ? "현재 이미지 위치를 찾고 실행한 뒤 화면 확인에서 멈춥니다." : code == "set_value" || code == "select_option" ? "입력·선택한 값이 실제로 반영됐는지 자동 확인합니다." : code == "checkpoint" ? "실행 시 화면을 캡처하고 사람이 확인할 때까지 멈춥니다." : code == "wait_for_element" ? "대상이 나타나는지 읽기만 하며 버튼을 누르지 않습니다." : code == "delay" ? "요소 선택 없이 추가할 수 있습니다." : "선택한 요소에 대해 한 번 실행하고 결과를 확인합니다.";
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
        void Pick(string purpose, bool image)
        {
            var payload = Target(); payload["purpose"] = purpose; Send(image ? "pick_image" : "pick_element", payload, purpose);
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
        void ShowStepPreview(Dictionary<string, object> item)
        {
            bool replace = false;
            using (Form dialog = new Form()) {
                dialog.Text = "기록한 동작과 대상 확인"; dialog.Font = Font; dialog.BackColor = Color.FromArgb(245, 247, 251);
                dialog.StartPosition = FormStartPosition.CenterParent; dialog.FormBorderStyle = FormBorderStyle.FixedDialog; dialog.MaximizeBox = dialog.MinimizeBox = false; dialog.ClientSize = new Size(Px(540), Px(360));
                dialog.Controls.Add(new Label { Text = Str(item, "program", "") + " · " + Str(item, "action_label", "") + "\n" + Str(item, "detail", ""), Bounds = Box(24, 18, 492, 80), ForeColor = ink });
                using (var picture = new PictureBox { Bounds = Box(24, 106, 492, 182), SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.White }) {
                    dialog.Controls.Add(picture); string encoded = Str(item, "thumbnail_png", "");
                    try { if (encoded.Length > 0 && encoded.Length <= 1500000) using (var stream = new MemoryStream(Convert.FromBase64String(encoded))) using (Image source = Image.FromStream(stream)) picture.Image = new Bitmap(source); }
                    catch (ArgumentException) { } catch (FormatException) { }
                    if (picture.Image == null) dialog.Controls.Add(new Label { Text = Str(item, "label", "현재 창") + "\n인식 방식: " + Str(item, "recognition", "uia"), Bounds = Box(40, 142, 460, 100), ForeColor = muted, BackColor = Color.White });
                    var close = new Button(); ButtonStyle(close, "CloseStepPreview", "확인", Box(404, 307, 112, 34), true, dialog); close.DialogResult = DialogResult.OK; dialog.AcceptButton = close; dialog.CancelButton = close;
                    if (encoded.Length > 0) {
                        var choose = new Button(); ButtonStyle(choose, "RetargetStep", "이미지 대상 다시 선택", Box(24, 307, 244, 34), false, dialog);
                        choose.Click += delegate { replace = true; dialog.DialogResult = DialogResult.OK; dialog.Close(); };
                    }
                    dialog.ShowDialog(this); if (picture.Image != null) picture.Image.Dispose();
                }
            }
            if (replace) { Send("retarget_step", new Dictionary<string, object> { { "index", Num(item, "index", -1) } }, null); if (busy) { hiddenForPicker = true; Hide(); } }
        }
        void AddStep()
        {
            if (NeedsElement && selection == null) { ShowError("먼저 동작할 요소를 직접 선택해 주세요."); return; }
            if (NeedsExpect && expectation == null) { ShowError("동작 뒤 확인할 요소를 선택해 주세요."); return; }
            var payload = Target(); string code = ActionCode; payload["action"] = code;
            if (NeedsElement) payload["selection_id"] = Str(selection, "selection_id", "");
            if (code == "set_value" || code == "select_option" || code == "checkpoint") payload["value"] = value.Text;
            if (code == "press_key") payload["key"] = value.Text.Trim();
            if (code == "hotkey") { var keys = new List<string>(); foreach (string item in value.Text.Split('+')) if (item.Trim().Length > 0) keys.Add(item.Trim()); payload["keys"] = keys; }
            if (code == "delay") payload["seconds"] = (int)seconds.Value;
            if (code == "wait_for_element") payload["timeout_seconds"] = (int)seconds.Value;
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
            if (busy) { status.Text = command == "pick_element" || command == "pick_image" ? "선택 창에서 대상을 고르고 확인해 주세요." : command == "record" ? "녹화 창에서 시작한 뒤 연결한 프로그램을 직접 조작하세요." : "요청한 내용을 확인하고 있습니다…"; status.ForeColor = muted; }
            UpdateButtons();
        }
        void Tick(object sender, EventArgs args)
        {
            if (done) return; if (DateTime.UtcNow >= deadline) { Finish("timeout", null); return; } if (!busy) return;
            string path = response + ".event.json"; if (!File.Exists(path)) return;
            Dictionary<string, object> result;
            try {
                result = Json.Deserialize<Dictionary<string, object>>(Read(path, 2097152));
            } catch (InvalidDataException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_too_large" } }); return;
            } catch (IOException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_read_failed" } }); return;
            } catch (UnauthorizedAccessException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_read_failed" } }); return;
            } catch (ArgumentException) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "invalid_event" } }); return; }
            if (Str(result, "nonce", "") != nonce) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_nonce_mismatch" } }); return; }
            int acknowledged = Num(result, "seq", -1); if (acknowledged < pendingSeq) return;
            if (acknowledged != pendingSeq) { Finish("runtime_failed", new Dictionary<string, object> { { "code", "event_sequence_mismatch" } }); return; }
            busy = false; if (hiddenForPicker) { hiddenForPicker = false; Show(); Activate(); }
            if (Str(result, "status", "") != "ok") { ShowError(Str(result, "message", "요청을 처리하지 못했습니다. 입력 내용을 확인해 주세요.")); UpdateButtons(); return; }
            if (pendingAction == "pick_element" || pendingAction == "pick_image") {
                var picked = Map(result, "selection");
                if (picked == null || Str(picked, "selection_id", "").Length == 0) { ShowError("선택 결과를 받지 못했습니다. 요소를 다시 선택해 주세요."); UpdateButtons(); return; }
                string purpose = Str(result, "purpose", pendingPurpose ?? "action"); string caption = Str(picked, "label", "이름 없는 요소") + "  ·  " + (Str(picked, "recognition", "") == "image" ? "이미지 인식" : Str(picked, "role", "") + " / 요소 인식");
                if (purpose == "expect") { expectation = picked; expectSelected.Text = caption; } else { selection = picked; selected.Text = caption; ShowThumbnail(picked); PopulateActions(); }
            }
            if (result.ContainsKey("steps")) UpdateSteps(List(result, "steps"));
            if (pendingAction == "preview_step") { var preview = Map(result, "preview"); if (preview != null) ShowStepPreview(preview); }
            if (pendingAction == "save") {
                var saved = Map(result, "saved_task");
                if (saved == null || Str(saved, "id", "").Length == 0) { ShowError("저장 완료를 확인하지 못했습니다. 다시 저장하기 전에 연결 상태를 확인해 주세요."); UpdateButtons(); return; }
                Finish("saved", new Dictionary<string, object> { { "saved_task", saved } }); return;
            }
            status.ForeColor = muted; status.Text = Str(result, "message", pendingAction == "add_step" ? "단계를 추가했습니다. 다음 요소와 동작을 이어서 알려주세요." : "내용을 반영했습니다."); UpdateButtons();
        }
        void UpdateSteps(IEnumerable entries)
        {
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
            steps.ShowItemToolTips = true; steps.EndUpdate();
        }
        void UpdateButtons()
        {
            program.Enabled = action.Enabled = value.Enabled = optionOrder.Enabled = seconds.Enabled = expectProperty.Enabled = expected.Enabled = expectBoolean.Enabled = processName.Enabled = description.Enabled = !busy;
            pick.Enabled = pickImage.Enabled = pickExpect.Enabled = record.Enabled = !busy;
            add.Enabled = !busy && stepCount < 30 && ActionCode.Length > 0 && (!NeedsElement || selection != null) && (!NeedsExpect || expectation != null);
            save.Enabled = !busy && !pendingInputs && stepCount > 0 && processName.Text.Trim().Length > 0;
            int index = steps.SelectedIndices.Count == 1 ? steps.SelectedIndices[0] : -1;
            var entry = index >= 0 ? steps.Items[index].Tag as Dictionary<string, object> : null;
            editInput.Enabled = !busy && entry != null && Object.Equals(entry.ContainsKey("editable_input") ? entry["editable_input"] : null, true);
            up.Enabled = !busy && index > 0; down.Enabled = !busy && index >= 0 && index < stepCount - 1; remove.Enabled = !busy && index >= 0;
        }
        void ShowError(string message) { status.ForeColor = Color.FromArgb(173, 48, 38); status.Text = message; }
        void Finish(string outcome, Dictionary<string, object> extra)
        {
            if (done) return; done = true; timer.Stop(); var result = extra ?? new Dictionary<string, object>(); result["nonce"] = nonce; result["status"] = outcome;
            try { Write(response, result); } finally { Close(); }
        }
    }
}
