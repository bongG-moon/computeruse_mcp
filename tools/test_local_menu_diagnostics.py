"""Compact local-only menu diagnostics and read-only native result UI."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from local_delivery import receipt
from local_runs import LocalRuns
from menu_expansion import local_menu_summary

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
PRIVATE = "PRIVATE-MENU-FIXTURE"


def workflow():
    return {"status": "needs_review", "task_verified": False, "completed_steps": 4, "total_steps": 8,
            "last_result": {"input_dispatched": True, "business_result_verified": False,
                            "diagnostic": {"code": "menu_child_missing", "message": "The child is not visible"},
                            "menu_expansion": {"stage": "child_observation", "menu_depth": 2,
                                               "menu_path": [PRIVATE, "Active sheet"], "expansion_method": "hover",
                                               "child_representation": "owned_popup", "expansion_delivery": "sent",
                                               "child_command_delivery": "not_sent", "business_result_verified": False,
                                               "verify_only": False, "observation": {"tree": "X" * 1000000},
                                               "input_result": {"screenshot": "Y" * 1000000}}}}


class LocalMenuDiagnosticsTests(unittest.TestCase):
    def test_small_projection_keeps_delivery_separate_from_business_success(self):
        raw = workflow()
        before = copy.deepcopy(raw)
        summary = local_menu_summary(raw)
        self.assertEqual(raw, before)
        self.assertLess(len(json.dumps(summary)), 1500)
        self.assertEqual(summary["pending_operation"], "expand_menu")
        self.assertTrue(summary["input_dispatched"])
        self.assertFalse(summary["business_result_verified"])
        self.assertEqual(summary["menu_expansion"]["child_command_delivery"], "not_sent")
        self.assertEqual(summary["menu_expansion"]["menu_path"], [PRIVATE, "Active sheet"])
        self.assertNotIn("observation", summary["menu_expansion"])
        self.assertNotIn("input_result", summary["menu_expansion"])

    def test_flattened_trial_summary_and_workflow_summary_have_identical_menu_details(self):
        raw = workflow()
        flat = {"status": raw["status"], **raw["last_result"]}
        self.assertEqual(local_menu_summary(raw), local_menu_summary(flat))

    def test_private_path_never_crosses_existing_public_receipt_boundary(self):
        raw = workflow()
        raw.update(local_menu_summary(raw))
        public = receipt(raw)
        self.assertNotIn(PRIVATE, json.dumps(public))
        self.assertNotIn("menu_expansion", public)
        self.assertNotIn("pending_operation", public)

    def test_unknown_or_huge_values_are_bounded_without_synthesizing_success(self):
        menu = workflow()["last_result"]["menu_expansion"]
        menu.update(menu_path=["L" * 5000] * 100, expansion_delivery=["sent"], child_command_delivery="unsupported",
                    menu_depth=True, business_result_verified="true")
        result = local_menu_summary({"menu_expansion": menu})["menu_expansion"]
        self.assertEqual(len(result["menu_path"]), 16)
        self.assertTrue(all(len(item) == 256 for item in result["menu_path"]))
        self.assertNotIn("expansion_delivery", result)
        self.assertNotIn("menu_depth", result)
        self.assertNotIn("business_result_verified", result)
        self.assertEqual(result["child_command_delivery"], "unknown")

    def test_checkpoint_wait_is_distinct_from_menu_failure(self):
        for mode in ("human", "visual", "popup_image"):
            self.assertEqual(local_menu_summary({"status": "needs_review", "checkpoint": {"id": "test", "review_mode": mode}}),
                             {"pending_operation": "checkpoint", "review_kind": mode})
        self.assertEqual(local_menu_summary({"status": "needs_review", "diagnostic": {"code": "menu_child_missing"}}), {})
        raw = workflow()
        raw["checkpoint"] = {"id": "earlier", "review_mode": "human"}
        self.assertNotIn("review_kind", local_menu_summary(raw))

    def test_local_result_ipc_includes_only_bounded_diagnostics_and_does_not_execute(self):
        scratch = ROOT / ".data/menu-native-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            manager = SimpleNamespace(config={"state_dir": directory})
            api = LocalRuns(manager)
            raw = workflow()
            job = {"result": receipt(raw), "local_result": raw, "cancel": threading.Event()}
            child = mock.Mock(pid=12345)
            with mock.patch("local_runs.subprocess.Popen", return_value=child) as launch:
                api._show_result(job)
                api._show_result(job)
            self.assertEqual(launch.call_count, 1)
            request = Path(launch.call_args.args[0][2])
            shown = json.loads(request.read_text(encoding="utf-8"))["result"]
            self.assertIn(PRIVATE, json.dumps(shown))
            self.assertLess(len(request.read_bytes()), 4000)
            self.assertEqual(shown["diagnostic"]["code"], "menu_child_missing")
            self.assertNotIn("acknowledge_checkpoint", shown)
            self.assertEqual(launch.call_args.args[0][1], "--result")
            self.assertNotIn(PRIVATE, json.dumps(receipt(job["result"])))


PROBE = r'''
using System;
using System.Collections.Generic;
using System.Windows.Forms;
internal static class LocalMenuProbe {
 [STAThread] static int Main() {
  var menu=new Dictionary<string,object>{{"stage","child_visible"},{"expansion_method","hover"},{"expansion_delivery","sent"},
   {"menu_depth",2},{"menu_path",new[]{"PRIVATE-MENU-FIXTURE","Active sheet"}},{"child_representation","logical_uia_child"},
   {"child_command_delivery","not_sent"},{"business_result_verified",false},{"verify_only",true},{"observation",new string('X',100000)}};
  var result=new Dictionary<string,object>{{"status","needs_review"},{"task_verified",true},{"menu_expansion",menu}};
  string trial=ProcessEditor.TrialMessage(result),detail=String.Join("\n",ProcessEditor.LocalExecutionDetails(result));
  var checks=new Dictionary<string,object>{{"trial_parent_delivery",trial.Contains("호버 / 전송됨")},
   {"trial_child_not_sent",trial.Contains("최종 명령 미전송")},{"trial_business_not_verified",trial.Contains("업무 완료 미검증")},
   {"detail_path",detail.Contains("PRIVATE-MENU-FIXTURE > Active sheet")},{"detail_depth_representation",detail.Contains("깊이: 2")&&detail.Contains("같은 창의 논리 자식")},
   {"detail_verify_only",detail.Contains("입력을 다시 보내지 않았습니다")},{"large_observation_omitted",!detail.Contains("XXXXX")}};
  using(var form=new ProcessEditor.LocalResultForm(new Dictionary<string,object>{{"result",result},{"timeout_seconds",30}})) {
   var body=form.Controls.Find("LocalResultDetails",true)[0] as TextBox;
   checks["plain_local_readonly"]=body.ReadOnly&&body.Text.Contains("최종 자식 명령: 미전송")&&body.Text.Contains("업무 결과: 미검증");
   checks["close_only"]=form.Controls.Find("CloseLocalResult",true).Length==1&&form.Controls.Find("ConfirmLocalInputs",true).Length==0&&!form.Visible;
   checks["no_business_success_heading"]=!form.Controls.Find("LocalResultHeading",true)[0].Text.Contains("작업을 완료하고");
  }
  var checkpoint=new Dictionary<string,object>{{"status","needs_review"},{"checkpoint",new Dictionary<string,object>{{"review_mode","human"}}}};
  checks["checkpoint_wait_distinguished"]=ProcessEditor.TrialMessage(checkpoint).Contains("예정된 사람 확인")
   &&String.Join("\n",ProcessEditor.LocalExecutionDetails(checkpoint)).Contains("예정된 사람 확인");
  menu["expansion_delivery"]="unknown";checks["uncertain_not_sent_claim"]=ProcessEditor.TrialMessage(result).Contains("전달 불명");
  Console.WriteLine(ProcessEditor.Json.Serialize(checks));return 0;
 }
}
'''


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class NativeLocalMenuDiagnosticsTests(unittest.TestCase):
    def test_native_result_and_trial_rendering_remain_local_readonly_and_precise(self):
        scratch = ROOT / ".data/menu-native-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            folder = Path(directory)
            source, executable = folder / "probe.cs", folder / "probe.exe"
            source.write_text(PROBE, encoding="utf-8")
            env = {**os.environ, "TEMP": directory, "TMP": directory}
            refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
            built = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:LocalMenuProbe", "/codepage:65001",
                                    *("/reference:" + ref for ref in refs), "/out:" + str(executable),
                                    str(ROOT / "ProcessEditor.cs"), str(source)],
                                   capture_output=True, text=True, timeout=30, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            checks = json.loads(ran.stdout)
            self.assertGreaterEqual(len(checks), 12)
            self.assertTrue(all(value is True for value in checks.values()), checks)


if __name__ == "__main__":
    unittest.main()
