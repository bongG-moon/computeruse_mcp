"""Native editor input contracts, without target-app input or a visible UI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


COMPILER = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


@unittest.skipUnless(os.name == "nt" and COMPILER.is_file(), "Windows .NET compiler required")
class LocalAuthoringNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        harness = root / "AuthoringTests.cs"
        harness.write_text(r'''
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;
internal static class AuthoringTests {
 [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
 [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
 static Dictionary<string,object> Spec(string type) { return new Dictionary<string,object>{{"type",type}}; }
 static bool Reject(Dictionary<string,object> spec,string value) { try { ProcessEditor.ParseInputText(spec,value); return false; }catch(ArgumentException) {return true;} }
 static bool InParents(Control control) {
  for(Control child=control;child.Parent!=null;child=child.Parent) {
   if(!child.Parent.ClientRectangle.Contains(child.Bounds))return false;
  }
  return true;
 }
 [STAThread] static void Main() {
  try{SetProcessDpiAwarenessContext(new IntPtr(-4));}catch(EntryPointNotFoundException){SetProcessDPIAware();}
  Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
  var result=new Dictionary<string,object>();
  result["integer_typed"]=ProcessEditor.ParseInputText(Spec("integer"),"12") is long;
  result["integer_fraction_rejected"]=Reject(Spec("integer"),"1.2");
  result["infinity_rejected"]=Reject(Spec("number"),"Infinity") && Reject(Spec("number"),"NaN");
  var number=Spec("number"); number["minimum"]=1.5; number["maximum"]=2.5;
  result["number_bounds"]=Reject(number,"1.4")&&Reject(number,"2.6")&&(double)ProcessEditor.ParseInputText(number,"2.25")==2.25;
  result["date_exact"]=Reject(Spec("date"),"2026-2-3")&&Reject(Spec("date"),"2026-02-30")&&(string)ProcessEditor.ParseInputText(Spec("date"),"2026-02-03")=="2026-02-03";
  var enumeration=Spec("enum"); enumeration["values"]=new[]{"N","P"};
  result["enum_exact"]=Reject(enumeration,"n") && (string)ProcessEditor.ParseInputText(enumeration,"N")=="N";
  result["boolean_typed"]=(bool)ProcessEditor.ParseInputText(Spec("boolean"),"true") && !(bool)ProcessEditor.ParseInputText(Spec("boolean"),"false") && Reject(Spec("boolean"),"");
  var list=Spec("list"); list["items"]=Spec("integer"); list["max_items"]=2;
  var parsed=ProcessEditor.ParseInputText(list,"1\r\n2") as List<object>;
  result["list_typed"]=parsed!=null&&parsed.Count==2&&parsed[0] is long&&(long)parsed[1]==2;
  result["list_bounds"]=Reject(list,"")&&Reject(list,"1\n2\n3")&&Reject(list,"1\nno");
  result["roundtrip"]=(string)ProcessEditor.InputText(new object[]{1,true,"N"})=="1\r\ntrue\r\nN";
  result["text_bounds"]=Reject(Spec("text"),new string('a',4001))&&Reject(Spec("text"),"a\0b");
  var defs=new object[]{new Dictionary<string,object>{{"name","choice"},{"spec",enumeration}}};
  using(var form=new ProcessEditor.LocalInputForm(new Dictionary<string,object>{{"inputs",defs},{"timeout_seconds",30}})) {
   var choices=form.Controls.Find("LocalInput_choice",true);
   result["required_enum_not_preselected"]=choices.Length==1 && ((ComboBox)choices[0]).SelectedIndex==-1;
   result["form_not_shown"]=!form.Visible;
   float dpi=Math.Max(96F,ProcessEditor.GetDpiForWindow(form.Handle));Rectangle working=Screen.FromControl(form).WorkingArea;
   result["input_scales_to_monitor"]=form.Width>=Math.Min(working.Width,(int)(620*dpi/96F))&&form.AutoScaleMode==AutoScaleMode.Dpi;
   result["input_footer_contained"]=InParents(form.Controls.Find("ConfirmLocalInputs",true)[0])&&InParents(form.Controls.Find("CancelLocalInputs",true)[0]);
   ProcessEditor.FitLocalDialog(form,new Rectangle(20,30,960,650));form.PerformLayout();
   result["input_small_workarea"]=form.Width<=960&&form.Height<=650&&form.Left>=20&&form.Top>=30&&InParents(form.Controls.Find("ConfirmLocalInputs",true)[0]);
   result["input_field_fits_width"]=choices[0].Right<=choices[0].Parent.ClientSize.Width;
  }
  var choicesSpec=Spec("list");choicesSpec["items"]=enumeration;choicesSpec["max_items"]=2;choicesSpec["default"]=new[]{"P"};
  var choicesDefs=new object[]{new Dictionary<string,object>{{"name","programs"},{"spec",choicesSpec}}};
  using(var form=new ProcessEditor.LocalInputForm(new Dictionary<string,object>{{"inputs",choicesDefs},{"timeout_seconds",30},{"multiple_choice_lists",true},{"title","local chooser"},{"intro","choose"},{"confirm_label","open"}})) {
   var checklist=form.Controls.Find("LocalInput_programs",true)[0] as CheckedListBox;
   result["chooser_default"]=checklist!=null&&checklist.CheckedItems.Count==1&&((ProcessEditor.Choice)checklist.CheckedItems[0]).Code=="P";
   checklist.SetItemChecked(0,true);
   var selected=ProcessEditor.LocalInputForm.ReadControlValue(choicesSpec,checklist) as List<object>;
   result["chooser_array"]=selected!=null&&selected.Count==2&&(string)selected[0]=="N"&&(string)selected[1]=="P";
   choicesSpec["max_items"]=1;bool capped=false;try{ProcessEditor.LocalInputForm.ReadControlValue(choicesSpec,checklist);}catch(ArgumentException){capped=true;}choicesSpec["max_items"]=2;
   checklist.SetItemChecked(0,false);checklist.SetItemChecked(1,false);bool required=false;try{ProcessEditor.LocalInputForm.ReadControlValue(choicesSpec,checklist);}catch(ArgumentException){required=true;}
   result["chooser_bounds"]=capped&&required;
   result["chooser_labels"]=form.Text=="local chooser"&&form.Controls.Find("ConfirmLocalInputs",true)[0].Text=="open";
  }
  choicesSpec["default"]=new[]{"P","N","P"};choicesSpec["max_items"]=3;
  using(var form=new ProcessEditor.LocalInputForm(new Dictionary<string,object>{{"inputs",choicesDefs},{"timeout_seconds",30}})) {
   var control=form.Controls.Find("LocalInput_programs",true)[0];var selected=ProcessEditor.LocalInputForm.ReadControlValue(choicesSpec,control) as List<object>;
   result["regular_list_order_preserved"]=control is TextBox&&selected.Count==3&&(string)selected[0]=="P"&&(string)selected[1]=="N"&&(string)selected[2]=="P";
  }
  var localResult=new Dictionary<string,object>{{"status","needs_review"},{"task_verified",false},{"completed_steps",2},{"total_steps",3},{"diagnostic",new Dictionary<string,object>{{"code","fixture_error"},{"message","<script>local only</script>"}}}};
  using(var form=new ProcessEditor.LocalResultForm(new Dictionary<string,object>{{"result",localResult},{"timeout_seconds",30}})) {
   var body=form.Controls.Find("LocalResultDetails",true)[0] as TextBox;
   result["result_plain_text"]=body!=null&&body.ReadOnly&&body.Text.Contains("<script>local only</script>")&&body.Text.Contains("2 / 3");
   result["result_close_only"]=form.Controls.Find("CloseLocalResult",true).Length==1&&form.Controls.Find("ConfirmLocalInputs",true).Length==0&&!form.Visible;
   float dpi=Math.Max(96F,ProcessEditor.GetDpiForWindow(form.Handle));Rectangle working=Screen.FromControl(form).WorkingArea;
   result["result_scales_to_monitor"]=form.Width>=Math.Min(working.Width,(int)(650*dpi/96F))&&form.AutoScaleMode==AutoScaleMode.Dpi;
   result["result_footer_contained"]=InParents(form.Controls.Find("CloseLocalResult",true)[0]);
   ProcessEditor.FitLocalDialog(form,new Rectangle(20,30,960,650));form.PerformLayout();
   result["result_small_workarea"]=form.Width<=960&&form.Height<=650&&InParents(form.Controls.Find("CloseLocalResult",true)[0])&&body.Height>40;
  }
  var programs=new object[]{new Dictionary<string,object>{{"program_id","fixture"},{"pid",1},{"window_id",1}}};
  var draft=new Dictionary<string,object>{{"name","test"},{"description",""},{"steps",new object[0]},{"inputs",defs},{"dirty",false}};
  var request=new Dictionary<string,object>{{"nonce",new string('a',64)},{"programs",programs},{"draft",draft},{"timeout_seconds",30}};
  using(var form=new ProcessEditor.EditorForm(request,"unused-response")) {
   var tabs=(TabControl)form.Controls.Find("AuthoringTabs",true)[0];
   result["three_tabs"]=tabs.TabPages.Count==3&&tabs.TabPages[1].Text=="입력값"&&tabs.TabPages[2].Text=="스킬 보기";
   result["unsaved_preview_blocked"]=!form.Controls.Find("PreviewSkill",true)[0].Enabled&&!form.Controls.Find("ExportSkill",true)[0].Enabled;
   string rawMarkdown="---\nname: local\n---\r\n\n# Local workflow\n";
   var preview=new Dictionary<string,object>{{"skill_markdown",rawMarkdown},{"sha256","unchanged-hash"},{"preview_id","unchanged-preview"},{"revision",1}};
   form.DisplaySkillPreview(preview);
   var displayed=(TextBox)form.Controls.Find("SkillPreviewText",true)[0];
   result["skill_preview_multiline"]=displayed.Multiline&&displayed.Lines.Length>=5&&displayed.Text.Contains("---\r\nname: local\r\n---\r\n\r\n# Local workflow\r\n")&&!displayed.Text.Contains("\r\r\n");
   result["skill_preview_raw_unchanged"]=(string)preview["skill_markdown"]==rawMarkdown&&(string)preview["sha256"]=="unchanged-hash"&&(string)preview["preview_id"]=="unchanged-preview";
  }
  Console.WriteLine(ProcessEditor.Json.Serialize(result));
 }
}
''', encoding="utf-8")
        cls.executable = root / "AuthoringTests.exe"
        built = subprocess.run([str(COMPILER), "/nologo", "/target:exe", "/main:AuthoringTests", "/codepage:65001",
                                "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Drawing.dll",
                                "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
                                "/out:" + str(cls.executable), str(Path(__file__).with_name("ProcessEditor.cs")), str(harness)],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)
        executed = subprocess.run([str(cls.executable)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        if executed.returncode:
            raise AssertionError(executed.stdout + executed.stderr)
        cls.results = json.loads(executed.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_scalar_types_and_validation(self):
        for key in ("integer_typed", "integer_fraction_rejected", "infinity_rejected", "number_bounds", "date_exact", "enum_exact", "boolean_typed", "text_bounds"):
            with self.subTest(key=key):
                self.assertTrue(self.results[key])

    def test_list_values_and_limits(self):
        for key in ("list_typed", "list_bounds", "roundtrip"):
            with self.subTest(key=key):
                self.assertTrue(self.results[key])

    def test_local_form_requires_enum_selection_without_default(self):
        self.assertTrue(self.results["required_enum_not_preselected"])
        self.assertTrue(self.results["form_not_shown"])

    def test_editor_has_integrated_authoring_and_blocks_unsaved_export(self):
        self.assertTrue(self.results["three_tabs"])
        self.assertTrue(self.results["unsaved_preview_blocked"])

    def test_result_window_is_local_read_only_text(self):
        self.assertTrue(self.results["result_plain_text"])
        self.assertTrue(self.results["result_close_only"])

    def test_explicit_checkbox_chooser_returns_bounded_array(self):
        for key in ("chooser_default", "chooser_array", "chooser_bounds", "chooser_labels"):
            with self.subTest(key=key):
                self.assertTrue(self.results[key])

    def test_regular_list_input_preserves_order_and_duplicates(self):
        self.assertTrue(self.results["regular_list_order_preserved"])

    def test_local_dialog_dpi_and_small_workarea_keep_footer_accessible(self):
        for key in ("input_scales_to_monitor", "input_footer_contained", "input_small_workarea", "input_field_fits_width",
                    "result_scales_to_monitor", "result_footer_contained", "result_small_workarea"):
            with self.subTest(key=key):
                self.assertTrue(self.results[key])

    def test_skill_preview_displays_lines_without_changing_export_identity(self):
        self.assertTrue(self.results["skill_preview_multiline"])
        self.assertTrue(self.results["skill_preview_raw_unchanged"])


if __name__ == "__main__":
    unittest.main()
