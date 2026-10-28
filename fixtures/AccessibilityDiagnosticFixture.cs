// Developer-only hidden window. It never handles business input or changes focus.
using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;
class AccessibilityDiagnosticFixture {
    [STAThread] static void Main() {
        Console.OutputEncoding=new System.Text.UTF8Encoding(false);
        var form=new Form { Text="Accessibility diagnostic hidden fixture", Width=500, Height=300,
            Location=new Point(-30000,-30000), StartPosition=FormStartPosition.Manual };
        var family=new ComboBox {Name="FamilyInput",AccessibleName="Family",Left=40,Top=30,Width=200};
        family.Items.AddRange(new object[]{"Hybrid","Memory"});family.SelectedIndex=0;
        var value=new TextBox {Name="ReadOnlyValue",AccessibleName="Selected Family",Text="Hybrid",ReadOnly=true,Left=40,Top=80};
        var password=new TextBox {Name="PasswordInput",AccessibleName="Protected fixture",Text="DO-NOT-EXPOSE",UseSystemPasswordChar=true,Left=40,Top=120};
        form.Controls.Add(family);form.Controls.Add(value);form.Controls.Add(password);
        IntPtr hwnd=form.Handle;
        foreach(Control control in form.Controls) {IntPtr child=control.Handle;}
        var timer=new System.Windows.Forms.Timer {Interval=20000};
        timer.Tick+=delegate {timer.Stop();form.Dispose();Application.ExitThread();};timer.Start();
        Console.WriteLine(hwnd.ToInt64());Console.Out.Flush();
        var input=new Thread(delegate() {Console.ReadLine();try {form.BeginInvoke((MethodInvoker)delegate {form.Dispose();Application.ExitThread();});}catch {}});
        input.IsBackground=true;input.Start();
        Application.Run();
    }
}
