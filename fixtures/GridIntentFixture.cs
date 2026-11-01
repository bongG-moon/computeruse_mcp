// Synthetic provider fixture. Changes only its own model; never sends desktop input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Interop;

sealed class FixturePanel : StackPanel {
    protected override AutomationPeer OnCreateAutomationPeer() { return new PanelPeer(this); }
    sealed class PanelPeer : FrameworkElementAutomationPeer {
        FixturePanel Panel;
        public PanelPeer(FixturePanel owner) : base(owner) { Panel = owner; }
        protected override string GetClassNameCore() { return "FixturePanel"; }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Group; }
        protected override List<AutomationPeer> GetChildrenCore() {
            var result = new List<AutomationPeer>();
            foreach (UIElement child in Panel.Children) { var peer = CreatePeerForElement(child); if (peer != null) result.Add(peer); }
            return result;
        }
    }
}
sealed class FixtureCell : Button {
    public FixtureGrid Grid;
    public int Row, Column;
    public FixtureCell(FixtureGrid grid, int row, int column) {
        Grid = grid; Row = row; Column = column; Content = "Value-" + row;
        Width = 150; Height = 26;
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new CellPeer(this); }
    sealed class CellPeer : ButtonAutomationPeer, IGridItemProvider, ISelectionItemProvider {
        FixtureCell Item;
        public CellPeer(FixtureCell owner) : base(owner) { Item = owner; }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Custom; }
        public override object GetPattern(PatternInterface pattern) {
            if (pattern == PatternInterface.GridItem || pattern == PatternInterface.SelectionItem) return this;
            return base.GetPattern(pattern);
        }
        public int Row { get { return Item.Row; } }
        public int Column { get { return Item.Column; } }
        public int RowSpan { get { return 1; } }
        public int ColumnSpan { get { return 1; } }
        public IRawElementProviderSimple ContainingGrid { get { return ProviderFromPeer(CreatePeerForElement(Item.Grid)); } }
        public IRawElementProviderSimple SelectionContainer { get { return ContainingGrid; } }
        public bool IsSelected { get { return Item.Grid.Selected.Contains(Item.Row); } }
        public void Select() { throw new InvalidOperationException("read_only_fixture"); }
        public void AddToSelection() { throw new InvalidOperationException("read_only_fixture"); }
        public void RemoveFromSelection() { throw new InvalidOperationException("read_only_fixture"); }
    }
}
sealed class FixtureSheet : Button {
    public FixtureSheet() { Content = "Current sheet"; Width = 150; Height = 26; AutomationProperties.SetAutomationId(this, "activeSheet"); }
    protected override AutomationPeer OnCreateAutomationPeer() { return new SheetPeer(this); }
    sealed class SheetPeer : ButtonAutomationPeer, ISelectionItemProvider {
        public SheetPeer(FixtureSheet owner) : base(owner) { }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.TabItem; }
        public override object GetPattern(PatternInterface pattern) {
            if (pattern == PatternInterface.SelectionItem) return this;
            return base.GetPattern(pattern);
        }
        public bool IsSelected { get { return true; } }
        public IRawElementProviderSimple SelectionContainer { get { return null; } }
        public void Select() { throw new InvalidOperationException("read_only_fixture"); }
        public void AddToSelection() { throw new InvalidOperationException("read_only_fixture"); }
        public void RemoveFromSelection() { throw new InvalidOperationException("read_only_fixture"); }
    }
}
sealed class FixtureGrid : StackPanel {
    public readonly List<int> Selected = new List<int> { 0 };
    public readonly List<FixtureCell> Cells = new List<FixtureCell>();
    public int Count = 2;
    public bool SelectionAvailable = true;
    public FixtureGrid() {
        AutomationProperties.SetAutomationId(this, "results");
        for (int row = 0; row < 2; row++) { var cell = new FixtureCell(this, row, 0); Cells.Add(cell); Children.Add(cell); }
        Children.Add(new FixtureSheet()); Width = 300; Height = 150;
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new GridPeer(this); }
    sealed class GridPeer : FrameworkElementAutomationPeer, IGridProvider, ISelectionProvider {
        FixtureGrid Grid;
        public GridPeer(FixtureGrid owner) : base(owner) { Grid = owner; }
        protected override string GetClassNameCore() { return "SyntheticGridProvider"; }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.DataGrid; }
        protected override bool IsControlElementCore() { return true; }
        protected override bool IsContentElementCore() { return true; }
        protected override List<AutomationPeer> GetChildrenCore() {
            var result = new List<AutomationPeer>();
            foreach (UIElement child in Grid.Children) result.Add(CreatePeerForElement(child));
            return result;
        }
        public override object GetPattern(PatternInterface pattern) {
            if (pattern == PatternInterface.Grid || pattern == PatternInterface.Selection && Grid.SelectionAvailable) return this;
            return base.GetPattern(pattern);
        }
        public int RowCount { get { return Grid.Count; } }
        public int ColumnCount { get { return 1; } }
        public IRawElementProviderSimple GetItem(int row, int column) {
            if (column != 0 || row < 0 || row >= Grid.Cells.Count) throw new ArgumentOutOfRangeException();
            return ProviderFromPeer(CreatePeerForElement(Grid.Cells[row]));
        }
        public bool CanSelectMultiple { get { return true; } }
        public bool IsSelectionRequired { get { return false; } }
        public IRawElementProviderSimple[] GetSelection() {
            var result = new List<IRawElementProviderSimple>();
            foreach (int index in Grid.Selected) result.Add(ProviderFromPeer(CreatePeerForElement(Grid.Cells[index])));
            return result.ToArray();
        }
    }
}
class GridIntentFixture {
    static JavaScriptSerializer Json = new JavaScriptSerializer();
    static void Emit(object value) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
    [STAThread] static void Main() {
        var app = new Application();
        var window = new Window { Title = "CUA grid intent provider fixture", Width = 500, Height = 400,
            Left = -12000, Top = -12000, ShowInTaskbar = false, ShowActivated = false };
        var outer = new FixturePanel(); AutomationProperties.SetAutomationId(outer, "businessPanel");
        var grid = new FixtureGrid(); outer.Children.Add(grid);
        var status = new TextBlock { Text = "Ready" }; AutomationProperties.SetAutomationId(status, "status"); outer.Children.Add(status);
        var filter = new TextBox { Text = "All" }; AutomationProperties.SetAutomationId(filter, "filter"); outer.Children.Add(filter);
        window.Content = outer;
        window.ContentRendered += delegate {
            Emit(new { ready = true, pid = Process.GetCurrentProcess().Id, window_id = new WindowInteropHelper(window).Handle.ToInt64() });
            var thread = new Thread(delegate() {
                string line;
                while ((line = Console.ReadLine()) != null) {
                    string command = line;
                    if (command == "close") { window.Dispatcher.BeginInvoke(new Action(delegate { window.Close(); })); break; }
                    window.Dispatcher.Invoke(new Action(delegate {
                        if (command == "many") { grid.Selected.Clear(); grid.Selected.Add(0); grid.Selected.Add(1); }
                        else if (command == "none") grid.Selected.Clear();
                        else if (command == "unsupported") grid.SelectionAvailable = false;
                        else if (command == "changed") { grid.Count = 10; grid.Cells[0].Content = "Different result"; }
                        else if (command == "processing") status.Text = "Processing";
                        Emit(new { command = command, done = true });
                    }));
                }
            }); thread.IsBackground = true; thread.Start();
        };
        app.Run(window);
    }
}
