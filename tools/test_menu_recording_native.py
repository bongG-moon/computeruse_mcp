"""Bounded menu intent and real offscreen HWND recorder tests; no desktop input.

The semantic snapshots are deliberately injected fixtures. These assertions do
not claim that the production app's UIA provider has exposed those descendants.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class NativeMenuRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / ".data/menu-native-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-menu-recording-", dir=scratch)
        cls.helper = Path(cls.directory.name) / "menu-recorder-tests.exe"
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        env = {**os.environ, "TEMP": cls.directory.name, "TMP": cls.directory.name}
        built = subprocess.run([
            str(CSC), "/nologo", "/target:exe", "/platform:anycpu", "/optimize+", "/codepage:65001",
            "/main:MenuExpansionRecorderTests", *("/reference:" + ref for ref in refs),
            "/out:" + str(cls.helper), str(ROOT / "VisualTools.cs"),
            str(ROOT / "fixtures/MenuExpansionRecorderTests.cs"),
        ], capture_output=True, text=True, timeout=30, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_fixture(self, mode):
        completed = subprocess.run([str(self.helper), mode], check=True, capture_output=True,
                                   text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(completed.stdout)
        if mode != "witness":
            self.assertTrue(result["foreground_unchanged"], result)
            self.assertEqual(result["retained_bytes_after_disposal"], 0, result)
        return result

    def test_only_complete_current_collapsed_to_expanded_intent_is_accepted(self):
        cases = self.run_fixture("witness")
        self.assertTrue(cases.pop("valid"))
        self.assertTrue(all(value is False for value in cases.values()), cases)

    def test_physical_child_episode_preserves_actual_parent_and_pre_image(self):
        result = self.run_fixture("hwnd")
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["operation"], "expand_menu")
        self.assertTrue(result["retained_original_frame"])
        self.assertEqual(result["evidence"]["child_representation"], "hwnd")
        self.assertEqual(result["evidence"]["child_window_ref"], "menu_child_episode_1")
        self.assertEqual(result["evidence"]["parent_window_ref"], "menu_parent")
        self.assertEqual(result["input_version"], 7)

    def test_left_opening_child_uses_actual_hwnd_without_right_side_assumption(self):
        result = self.run_fixture("left")
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["evidence"]["child_window_ref"], "menu_child_episode_1")

    def test_mouse_hover_without_visible_child_produces_no_action(self):
        self.assertEqual(self.run_fixture("hover_only")["queued"], 0)

    def test_logical_child_is_not_invented_as_a_new_hwnd(self):
        result = self.run_fixture("logical")
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["evidence"]["child_representation"], "logical")
        self.assertNotIn("child_window_ref", result["evidence"])
        self.assertEqual(result["evidence"]["child_items"][0]["name"], "Active sheet test")

    def test_ambiguous_children_and_duplicate_live_episode_are_rejected(self):
        for mode in ("duplicate", "two_children"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_fixture(mode)["queued"], 0)

    def test_foreign_scope_hidden_retired_and_replaced_parent_are_rejected(self):
        for mode in ("unrelated", "hidden", "retired", "identity"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_fixture(mode)["queued"], 0)

    def test_changed_input_geometry_and_incomplete_provider_do_not_create_action(self):
        for mode in ("epoch", "moved", "incomplete"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_fixture(mode)["queued"], 0)

    def test_fast_transition_repair_is_before_child_input_and_never_runnable(self):
        result = self.run_fixture("repair")
        self.assertEqual(result["queued"], 2)
        self.assertEqual(result["operation"], "manual_entry")
        self.assertEqual(result["reason"], "menu_expansion_unverified")
        self.assertEqual(result["next_operation"], "click")
        self.assertEqual(result["evidence"]["after_state"], "unknown")
        self.assertTrue(result["retained_original_frame"])


if __name__ == "__main__":
    unittest.main()
