// Isolated offscreen HWND fixture. IPC changes only this process's own forms.
// No external input, business app, recording, selection or export is performed.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class ProofForm : Form {
    public bool ListClass;
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get {
            var p = base.CreateParams;
            p.ExStyle &= ~0x00040000;
            p.ExStyle |= 0x08000080;
            if (ListClass) p.ClassStyle = 0x808;
            return p;
        }
    }
    public ProofForm(string title, bool list) {
        ListClass = list; Text = title; ShowInTaskbar = true;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(-12000, -12000); Size = new Size(300, 150);
    }
}

sealed class ComboPopupProofFixture : ApplicationContext {
    [DllImport("user32.dll", EntryPoint="SetWindowLongPtrW", SetLastError=true)]
    static extern IntPtr SetWindowLongPtr64(IntPtr hwnd, int index, IntPtr value);
    [DllImport("user32.dll", EntryPoint="SetWindowLongW", SetLastError=true)]
    static extern IntPtr SetWindowLong32(IntPtr hwnd, int index, IntPtr value);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint flag);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly ProofForm root = new ProofForm("Offscreen combo proof", false);
    readonly Form body = new Form { TopLevel = false, FormBorderStyle = FormBorderStyle.None,
        Text = "Embedded business form", Dock = DockStyle.Fill };
    readonly ProofForm popup = new ProofForm("", true);
    readonly ProofForm second = new ProofForm("", true);
    readonly ProofForm independent = new ProofForm("", true);
    readonly ProofForm modal = new ProofForm("Searchable list", true);
    readonly Panel listPanel = new Panel { Text = "Popup list area", Dock = DockStyle.Fill };
    readonly ListBox list = new ListBox { Dock = DockStyle.Fill };
    public ComboPopupProofFixture() {
        root.Controls.Add(body); root.Show(); body.Show();
        list.Items.AddRange(new object[] { "N", "P", "W" });
        listPanel.Controls.Add(list); popup.Controls.Add(listPanel);
        popup.Handle.ToInt64(); second.Handle.ToInt64(); independent.Handle.ToInt64(); modal.Handle.ToInt64();
        listPanel.Handle.ToInt64(); list.Handle.ToInt64();
        Own(popup); Own(second); Own(modal);
        Emit(new { ready = true, pid = Process.GetCurrentProcess().Id });
        var input = new Thread(delegate() {
            string line;
            while ((line = Console.ReadLine()) != null) {
                var command = json.Deserialize<Dictionary<string,object>>(line);
                root.BeginInvoke((Action)delegate {
                    try { Command(Convert.ToString(command["state"])); }
                    catch (Exception error) { Emit(new { error = error.GetType().Name, message = error.Message }); }
                });
            }
        }); input.IsBackground = true; input.Start();
        var expiry = new System.Windows.Forms.Timer { Interval = 60000 };
        expiry.Tick += delegate { Quit(); }; expiry.Start();
    }
    void Emit(object value) { Console.WriteLine(json.Serialize(value)); Console.Out.Flush(); }
    void ShowOwned(ProofForm form) {
        form.Show();
        Own(form);
    }
    void Own(ProofForm form) {
        // A real embedded child HWND owns the popup, as in a mixed host.
        if (IntPtr.Size == 8) SetWindowLongPtr64(form.Handle, -8, body.Handle);
        else SetWindowLong32(form.Handle, -8, body.Handle);
        if (GetWindow(form.Handle, 4) != body.Handle) throw new InvalidOperationException("owner not embedded HWND");
    }
    void Command(string state) {
        if (state == "quit") { Quit(); return; }
        if (state == "reset") { popup.Hide(); second.Hide(); independent.Hide(); modal.Hide(); root.Enabled = true; }
        else if (state == "show") ShowOwned(popup);
        else if (state == "hide") popup.Hide();
        else if (state == "independent") independent.Show();
        else if (state == "modal") ShowOwned(modal);
        else if (state == "second") ShowOwned(second);
        else if (state == "disabled") { root.Enabled = false; ShowOwned(popup); }
        else if (state != "inspect") throw new ArgumentException("state");
        root.BeginInvoke((Action)delegate {
            Emit(new { state = state, pid = Process.GetCurrentProcess().Id,
                window_id = root.Handle.ToInt64(), embedded_window_id = body.Handle.ToInt64(),
                popup_window_id = popup.Handle.ToInt64(), popup_owner_window_id = GetWindow(popup.Handle,4).ToInt64(),
                panel_window_id = listPanel.Handle.ToInt64(), list_window_id = list.Handle.ToInt64(),
                independent_window_id = independent.Handle.ToInt64(), modal_window_id = modal.Handle.ToInt64(),
                foreground_window_id = GetForegroundWindow().ToInt64() });
        });
    }
    void Quit() {
        popup.Dispose(); second.Dispose(); independent.Dispose(); modal.Dispose(); root.Dispose(); ExitThread();
    }
    [STAThread] static void Main() {
        Console.InputEncoding = System.Text.Encoding.UTF8; Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new ComboPopupProofFixture());
    }
}
