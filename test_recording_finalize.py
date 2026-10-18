"""Bounded native final-value collection; no windows, input, or fabricated replay."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
PROBE = r"""
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
internal static class FinalizeProbe {
 [STAThread]
 static int Main(string[] args) {
  var json = new JavaScriptSerializer();
  string mode = args[0];
  if(mode.StartsWith("layout_")) {
   float scale=Int32.Parse(mode.Substring(7))/100f;
   using(var toolbar=new FlowLayoutPanel {WrapContents=true,Margin=Padding.Empty,Font=new Font("Malgun Gothic",10*scale)}) {
    foreach(string caption in new[]{"기록 시작","일시정지","기록 마치고 검토","접어두기","취소"})
     toolbar.Controls.Add(new Button {Text=caption,AutoSize=true,MinimumSize=new Size((int)(110*scale),(int)(38*scale)),
      Width=(int)((caption=="기록 마치고 검토"?174:92)*scale),Margin=new Padding((int)(8*scale))});
    int width=(int)(618*scale);
    var recorder=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
    var measure=recorder.GetMethod("ToolbarPreferredHeight",BindingFlags.NonPublic|BindingFlags.Static);
    int height=(int)measure.Invoke(null,new object[]{toolbar,width});
    toolbar.Size=new Size(width,height);toolbar.PerformLayout();
    bool fits=true,wrapped=false;int firstTop=toolbar.Controls[0].Top;
    foreach(Control button in toolbar.Controls) {fits &= button.Bottom+button.Margin.Bottom<=toolbar.ClientSize.Height;wrapped |= button.Top!=firstTop;}
    Console.WriteLine(json.Serialize(new {fits=fits,wrapped=wrapped,buttons=toolbar.Controls.Count,height=height,
     fixed_row_would_clip=height>(int)(70*scale)}));return 0;
   }
  }
  if(mode.StartsWith("policy")) {
   var element = new Dictionary<string,object>{{"role","Edit"},{"is_password",false}};
   var ancestors = new List<Dictionary<string,object>>();
   var native = new Dictionary<string,object>{{"element",element},{"ancestors",ancestors}};
   var before = new Dictionary<string,object>{{"identity","same-edit"},{"value",""}};
   var after = new Dictionary<string,object>{{"identity","same-edit"},{"value","typed before Finish"},{"native_target",native}};
   string reason="unknown_input"; bool target=true, unchanged=true, replaced=false;
   if(mode=="policy_changed_identity") after["identity"]="different-edit";
   if(mode=="policy_password") element["is_password"]=true;
   if(mode=="policy_protected") after["protected"]=true;
   if(mode=="policy_target_switch") target=false;
   if(mode=="policy_new_input") unchanged=false;
   if(mode=="policy_combo") ancestors.Add(new Dictionary<string,object>{{"role","ComboBox"}});
   if(mode=="policy_unknown_commit") reason="recording_method_unverified";
   if(mode=="policy_shortcut") reason="shortcut_requires_manual_setup";
   if(mode=="policy_payload") reason="recording_payload_limit";
   if(mode=="policy_append") before["value"]="old text";
   if(mode=="policy_replace") { before["value"]="old text"; replaced=true; }
   if(mode=="policy_missing") after=null;
   if(mode=="policy_document") element["role"]="Document";
   if(mode=="policy_document_replace") { element["role"]="Document";before["value"]="old text";replaced=true; }
   if(mode=="policy_document_password") { element["role"]="Document";element["is_password"]=true; }
   if(mode=="policy_noneditable_role") element["role"]="Pane";
   Console.WriteLine(json.Serialize(new {accepted=VisualTools.CanFinalizeRecordedEdit(before,after,replaced,reason,target,unchanged)}));
   return 0;
  }
  var began=DateTime.UtcNow;
  var release=new ManualResetEvent(false); var exited=new ManualResetEvent(false);
  var read=new VisualTools.RecordingFinalRead(delegate {
   try { if(mode!="fast") release.WaitOne(); if(mode=="failure") throw new InvalidOperationException();
    return new Dictionary<string,object>{{"value","final"}};
   } finally { exited.Set(); }
  },began.AddMilliseconds(1200));
  Dictionary<string,object> value;
  bool ready;
  if(mode=="fast") { exited.WaitOne(1000); ready=read.TryTake(began.AddMilliseconds(20),out value);
   for(int retry=0;!ready && retry<100;retry++) { Thread.Sleep(2); ready=read.TryTake(began.AddMilliseconds(20),out value); }
  }
  else if(mode=="timeout") { ready=read.TryTake(began.AddMilliseconds(1201),out value); }
  else if(mode=="cancel") { read.Cancel(); ready=read.TryTake(began.AddMilliseconds(20),out value); }
  else if(mode=="failure") { release.Set(); exited.WaitOne(1000); Thread.Sleep(10); ready=read.TryTake(began.AddMilliseconds(20),out value); }
  else { ready=read.TryTake(began.AddMilliseconds(20),out value); }
  bool accepted=value!=null;
  release.Set(); exited.WaitOne(1000); Thread.Sleep(10);
  Dictionary<string,object> late;
  bool later=read.TryTake(began.AddMilliseconds(1300),out late);
  Console.WriteLine(json.Serialize(new {ready=ready,accepted=accepted,later=later,late_accepted=late!=null}));
  return 0;
 }
}
"""


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class RecordingFinalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-finalize-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "finalize.exe"
        source = cls.root / "probe.cs"
        source.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:FinalizeProbe", "/codepage:65001",
            *("/reference:" + ref for ref in refs), "/out:" + str(cls.helper), str(ROOT / "VisualTools.cs"), str(source)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, mode):
        result = subprocess.run([str(self.helper), mode], capture_output=True, text=True, check=True,
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)

    def test_fast_finish_reads_once_without_waiting_for_next_recording_poll(self):
        self.assertEqual(self.call("fast"), {"ready": True, "accepted": True, "later": True, "late_accepted": False})

    def test_recorder_toolbar_keeps_all_five_buttons_inside_wrapped_rows_at_high_dpi(self):
        for scale in (100, 150, 200, 300):
            with self.subTest(scale=scale):
                result = self.call("layout_" + str(scale))
                self.assertTrue(result["fits"])
                self.assertTrue(result["wrapped"])
                self.assertTrue(result["fixed_row_would_clip"])
                self.assertEqual(result["buttons"], 5)

    def test_slow_provider_never_blocks_ui_and_late_result_is_discarded(self):
        self.assertEqual(self.call("pending"), {"ready": False, "accepted": False, "later": True, "late_accepted": False})
        self.assertEqual(self.call("timeout"), {"ready": True, "accepted": False, "later": True, "late_accepted": False})

    def test_new_input_or_cancellation_discards_background_value(self):
        self.assertEqual(self.call("cancel"), {"ready": True, "accepted": False, "later": True, "late_accepted": False})

    def test_provider_failure_finishes_without_claiming_a_value(self):
        self.assertEqual(self.call("failure"), {"ready": True, "accepted": False, "later": True, "late_accepted": False})

    def test_only_same_owned_edit_or_explicit_replace_is_accepted(self):
        for mode in ("policy", "policy_replace", "policy_document", "policy_document_replace"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": True})

    def test_dropdown_password_stale_outside_and_unrecorded_inputs_stay_unconfirmed(self):
        for suffix in ("changed_identity", "password", "protected", "target_switch", "new_input", "combo",
                       "unknown_commit", "shortcut", "payload", "append", "missing", "document_password", "noneditable_role"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call("policy_" + suffix), {"accepted": False})


class RecordedDocumentImportTests(unittest.TestCase):
    def test_document_role_and_final_value_survive_driver_verified_import(self):
        import test_process_editor as fixtures
        source = fixtures.RecordingSemanticTests(); source.setUp()
        event, snapshots = source.event(value="Recorded document value")
        event["native_target"]["element"]["role"] = "Document"
        event["native_target"]["element"]["automation_id"] = ""
        row = snapshots[("editor", "main")]["elements"][0]; row["role"] = "Document"; row.pop("automation_id")
        source.draft.recorded([event], snapshots=snapshots)
        step, = source.draft.steps
        self.assertEqual(step["operation"], "set_value")
        self.assertEqual(step["selector"], {"role": "Document"})
        self.assertEqual(step["value"], "Recorded document value")
        self.assertEqual(step["expect"][0]["equals"], "Recorded document value")
        source.draft.validate()

    def test_document_names_are_omitted_and_read_only_or_ambiguous_documents_rejected(self):
        from learning import stable_selector
        from operations import OperationError
        for issue in ("same_value", "value_excerpt", "read_only", "duplicate"):
            label = "Document editor"
            row = {"element_index": 1, "role": "Document", "name": label, "actions": ["set_value"], "value": "Recorded content"}
            if issue == "same_value": row["value"] = label
            elif issue == "value_excerpt": row["value"] = "prefix " + label + " suffix"
            elif issue == "read_only": row["read_only"] = True
            elements = [row, {**row, "element_index": 2}] if issue == "duplicate" else [row]
            with self.subTest(issue=issue):
                if issue in ("read_only", "duplicate"):
                    with self.assertRaises(OperationError): stable_selector({"elements": elements}, row)
                else:
                    self.assertEqual(stable_selector({"elements": elements}, row), {"role": "Document"})

    def test_role_only_document_does_not_enable_other_role_or_ancestor_shortcuts(self):
        from operations import OperationError, validate_selector
        self.assertEqual(validate_selector({"role": "Document"}), {"role": "Document"})
        for selector in ({"role": "Edit"}, {"role": "Button"}, {"role": "Document", "within": {"role": "Document"}}):
            with self.subTest(selector=selector), self.assertRaises(OperationError): validate_selector(selector)

    def test_read_only_or_unsupported_document_never_replays_recorded_text(self):
        import test_process_editor as fixtures
        for unsupported in ("read_only", "missing_action"):
            source = fixtures.RecordingSemanticTests(); source.setUp()
            event, snapshots = source.event(value="Not replayable")
            event["native_target"]["element"]["role"] = "Document"
            row = snapshots[("editor", "main")]["elements"][0]; row["role"] = "Document"
            if unsupported == "read_only": row["read_only"] = True
            else: row["actions"] = []
            with self.subTest(unsupported=unsupported):
                source.draft.recorded([event], snapshots=snapshots)
                self.assertEqual(source.draft.steps[0]["operation"], "manual_entry")
                self.assertNotIn("value", source.draft.steps[0])


if __name__ == "__main__":
    unittest.main()
