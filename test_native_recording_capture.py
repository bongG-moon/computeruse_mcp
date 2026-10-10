"""Exercise target-render validation and DWM crop without opening any windows."""
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
using System.Drawing;
using System.Drawing.Imaging;
using System.Collections.Generic;
using System.Web.Script.Serialization;
internal static class RenderProbe {
    static int Main(string[] args) {
        var json = new JavaScriptSerializer();
        if(args[0].StartsWith("record_")) {
            using(var primary=new Bitmap(160,96)) using(var context=new Bitmap(384,128)) {
                using(var g=Graphics.FromImage(primary)) g.Clear(Color.Gray);
                using(var g=Graphics.FromImage(context)) { g.Clear(Color.Gray); g.FillRectangle(Brushes.White,15,12,90,12); g.FillRectangle(Brushes.Black,200,60,80,12); }
                if(args[0]=="record_horizontal") using(var g=Graphics.FromImage(primary)) for(int y=12;y<80;y+=16) g.FillRectangle(Brushes.White,0,y,160,4);
                if(args[0]=="record_vertical") using(var g=Graphics.FromImage(primary)) for(int x=12;x<144;x+=16) g.FillRectangle(Brushes.White,x,0,4,96);
                if(args[0]=="record_gradient") for(int y=0;y<96;y++) for(int x=0;x<160;x++) primary.SetPixel(x,y,Color.FromArgb(80+y,80+y,80+y));
                var result=VisualTools.RecordedImage(primary,new Point(80,48),args[0]=="record_context"?context:null,new Point(210,66),new Size(1724,1162),"screen");
                object value; var capture=result.TryGetValue("capture_diagnostic",out value)?value:null;
                Console.WriteLine(json.Serialize(new { accepted=result.ContainsKey("template_png"),rejected=result.ContainsKey("rejected_capture"),diagnostic=capture,
                    anchor=result.TryGetValue("anchor",out value)?value:null,source_size=result.TryGetValue("source_size",out value)?value:null })); return 0;
            }
        }
        if(args[0]=="region") {
            var small=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,true,false);
            var unnamed=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,false,false);
            var expanded=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,false,true);
            Console.WriteLine(json.Serialize(new { named_width=small.Width,unnamed_width=unnamed.Width,expanded_width=expanded.Width,
                all_contain_anchor=small.Contains(1615,174)&&unnamed.Contains(1615,174)&&expanded.Contains(1615,174),
                all_inside_frame=small.Right<=1724&&unnamed.Right<=1724&&expanded.Right<=1724 }));return 0;
        }
        if(args[0].StartsWith("focus")) {
            var before = new Dictionary<string, object> { { "identity", "edit1" }, { "value", "old" } };
            var element = new Dictionary<string, object> { { "role", "Edit" }, { "is_password", false } };
            var after = new Dictionary<string, object> { { "identity", "edit1" }, { "value", "new" }, { "native_target", new Dictionary<string, object> { { "element", element } } } };
            if(args[0]=="focus_identity") after["identity"]="other";
            if(args[0]=="focus_native") after.Remove("native_target");
            if(args[0]=="focus_protected") after["protected"]=true;
            if(args[0]=="focus_password") element["is_password"]=true;
            if(args[0]=="focus_same") after["value"]="old";
            if(args[0]=="focus_empty") before["value"]="";
            if(args[0]=="focus_role") element["role"]="ComboBox";
            Console.WriteLine(json.Serialize(new { accepted=VisualTools.RecordedEditConfirmed(before,after,args[0]!="focus_append" && args[0]!="focus_empty") }));return 0;
        }
        using (var full = new Bitmap(160,120,PixelFormat.Format32bppRgb)) {
            using (var g = Graphics.FromImage(full)) g.Clear(args[0] == "sentinel" ? Color.Magenta : args[0] == "black" ? Color.Black : Color.White);
            if (args[0] == "valid" || args[0] == "outside") for(int y=0;y<120;y++) for(int x=0;x<160;x++) full.SetPixel(x,y,Color.FromArgb((x*13)%256,(y*17)%256,((x+y)*11)%256));
            if (args[0] == "sentinel") using (var g = Graphics.FromImage(full)) g.FillRectangle(Brushes.White,0,0,160,50);
            try {
                using(var crop=VisualTools.ValidateRenderedFrame(full,new Rectangle(-200,100,160,120),new Rectangle(args[0]=="outside" ? -220 : -192,106,144,108))) {
                    bool exact=true; for(int y=0;y<crop.Height;y++) for(int x=0;x<crop.Width;x++) if(crop.GetPixel(x,y).ToArgb()!=full.GetPixel(x+8,y+6).ToArgb()) exact=false;
                    Console.WriteLine(json.Serialize(new { width=crop.Width,height=crop.Height,exact=exact }));
                }
            } catch(InvalidOperationException e) { Console.WriteLine(json.Serialize(new { code=e.Message })); }
        }
        return 0;
    }
}
"""

@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class NativeRecordingCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-render-probe-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "render-probe.exe"
        probe = cls.root / "probe.cs"
        probe.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:RenderProbe", "/codepage:65001", *("/reference:"+r for r in refs), "/out:"+str(cls.helper), str(ROOT/"VisualTools.cs"), str(probe)],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise RuntimeError(result.stdout+result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self,mode):
        result = subprocess.run([str(self.helper),mode],capture_output=True,text=True,encoding="utf-8-sig",check=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)

    def test_negative_monitor_dwm_crop_preserves_every_source_pixel(self):
        self.assertEqual(self.call("valid"), {"width":144,"height":108,"exact":True})

    def test_white_and_black_unrendered_frames_are_not_templates(self):
        for mode in ("white","black"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"code":"target_render_incomplete"})

    def test_partially_rendered_sentinel_frame_is_rejected(self):
        self.assertEqual(self.call("sentinel"), {"code":"target_render_incomplete"})

    def test_geometry_outside_rendered_window_is_rejected(self):
        self.assertEqual(self.call("outside"), {"code":"target_capture_size_invalid"})

    def test_focused_edit_replacement_needs_same_owned_identity_and_new_nonprotected_value(self):
        for mode in ("focus", "focus_empty"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"accepted": True})
        for mode in ("focus_identity", "focus_native", "focus_protected", "focus_password", "focus_same", "focus_append", "focus_role"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"accepted": False})

    def test_horizontal_and_vertical_icon_detail_use_the_same_threshold(self):
        for mode in ("record_horizontal", "record_vertical"):
            with self.subTest(mode=mode):
                result = self.call(mode)
                self.assertTrue(result["accepted"])
                self.assertFalse(result["rejected"])
                self.assertEqual(result["diagnostic"]["candidate_count"], 1)
                self.assertEqual(result["anchor"], {"x": .5, "y": .5})

    def test_flat_capture_is_preserved_privately_but_never_becomes_a_target(self):
        result = self.call("record_flat")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["diagnostic"]["detail"], {"variance": 0, "horizontal_edge": 0, "vertical_edge": 0})
        self.assertIsNone(result["anchor"])

    def test_low_detail_primary_uses_own_captured_context_with_exact_anchor(self):
        result = self.call("record_context")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["diagnostic"]["candidate_count"], 2)
        self.assertTrue(result["diagnostic"]["context_expanded"])
        self.assertEqual(result["source_size"], {"width": 384, "height": 128})
        self.assertEqual(result["anchor"], {"x": 210/384, "y": 66/128})

    def test_weak_gradient_does_not_pass_new_vertical_edge_check(self):
        result = self.call("record_gradient")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["diagnostic"]["detail"]["vertical_edge"], 1)

    def test_unnamed_semantic_target_gets_context_without_moving_its_anchor(self):
        self.assertEqual(self.call("region"), {"named_width": 160, "unnamed_width": 384, "expanded_width": 640,
            "all_contain_anchor": True, "all_inside_frame": True})

if __name__ == "__main__":
    unittest.main()
