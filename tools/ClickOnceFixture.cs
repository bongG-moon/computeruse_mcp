// Local developer fixture, not a substitute for testing a corporate application.
// No network, file writes, or registry changes. ClickOnce installation is performed
// only through the normal Windows launcher, separately from the build script.
using System;
using System.Deployment.Application;
using System.Drawing;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("1.0.0.0")]

internal static class ClickOnceFixture
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Form form = new Form { Text = "Computer Use - Local ClickOnce Fixture", Width = 840, Height = 610 };
        form.Font = new Font("Segoe UI", 11);
        Label heading = new Label { Text = "Local ClickOnce workflow test", Left = 24, Top = 20, Width = 760, Height = 36, Font = new Font("Segoe UI", 18) };
        Label deployment = new Label { Name = "DeploymentStatus", AccessibleName = "Deployment status", Left = 24, Top = 65, Width = 760, Height = 28,
            Text = ApplicationDeployment.IsNetworkDeployed ? "ClickOnce deployment: active" : "Direct EXE: not deployed through ClickOnce" };
        Label familyLabel = new Label { Text = "Family", Left = 24, Top = 112, Width = 90 };
        ComboBox family = new ComboBox { Name = "Family", AccessibleName = "Family", Left = 120, Top = 108, Width = 230, DropDownStyle = ComboBoxStyle.DropDownList };
        family.Items.AddRange(new object[] { "W", "N", "P", "C" });
        family.SelectedIndex = 0;
        Label searchLabel = new Label { Text = "Search", Left = 380, Top = 112, Width = 90 };
        TextBox search = new TextBox { Name = "SearchText", AccessibleName = "Search text", Left = 470, Top = 108, Width = 310, Text = "Sample" };
        CheckBox include = new CheckBox { Name = "IncludeInactive", AccessibleName = "Include inactive", Text = "Include inactive", Left = 24, Top = 155, Width = 230 };
        Button query = new Button { Name = "Query", AccessibleName = "Query", Text = "Query", Left = 560, Top = 151, Width = 105, Height = 38 };
        Button reset = new Button { Name = "Reset", AccessibleName = "Reset", Text = "Reset", Left = 675, Top = 151, Width = 105, Height = 38 };
        DataGridView grid = new DataGridView { Name = "Results", AccessibleName = "Results", Left = 24, Top = 210, Width = 756, Height = 255,
            ReadOnly = true, AllowUserToAddRows = false, RowHeadersVisible = false, AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill };
        grid.Columns.Add("Family", "Family"); grid.Columns.Add("Item", "Item"); grid.Columns.Add("State", "State");
        Label status = new Label { Name = "ResultStatus", AccessibleName = "Result status", Text = "Ready - no query yet", Left = 24, Top = 485, Width = 756, Height = 48 };
        int queries = 0;
        query.Click += delegate {
            grid.Rows.Clear(); queries++;
            for (int index = 1; index <= 5; index++) grid.Rows.Add(family.Text, search.Text + " " + index, include.Checked ? "All" : "Active");
            status.Text = "Query " + queries + " completed: Family=" + family.Text + "; Search=" + search.Text + "; Rows=5";
        };
        reset.Click += delegate { grid.Rows.Clear(); family.SelectedIndex = 0; search.Text = "Sample"; include.Checked = false; status.Text = "Reset completed"; };
        form.Controls.AddRange(new Control[] { heading, deployment, familyLabel, family, searchLabel, search, include, query, reset, grid, status });
        Application.Run(form);
    }
}
