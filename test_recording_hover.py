"""Witnessed pre-hover image policy; no application windows or input."""
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
using System.Web.Script.Serialization;
internal static class HoverProbe {
 static int Main(string[] args) {
  string mode=args[0];bool result;
  if(mode.StartsWith("witness")) {
   DateTime now=DateTime.UtcNow, captured=now.AddMilliseconds(-500);
   int before=2,current=2;Rectangle bounds=new Rectangle(-800,40,480,320),currentBounds=bounds;
   bool cursor=true,matching=true,blocked=false;
   if(mode=="witness_stale") captured=now.AddMilliseconds(-5001);
   if(mode=="witness_future") captured=now.AddMilliseconds(1);
   if(mode=="witness_input") current=3;
   if(mode=="witness_moved") currentBounds.X++;
   if(mode=="witness_resized") currentBounds.Width++;
   if(mode=="witness_cursor") cursor=false;
   if(mode=="witness_mode") matching=false;
   if(mode=="witness_occluded") blocked=true;
   result=VisualTools.RecordingPreHoverWitness(captured,now,before,current,bounds,currentBounds,cursor,matching,blocked);
  } else if(mode=="files") {
   using(var before=new Bitmap(args[1])) using(var current=new Bitmap(args[2]))
    result=VisualTools.RecordingPreHoverFrame(before,current,new Point(286,48),new Point(200,48));
  } else {
   using(var before=new Bitmap(480,320)) using(var current=new Bitmap(mode=="size"?481:480,320)) {
    using(var g=Graphics.FromImage(before)) {
     g.Clear(Color.FromArgb(32,32,32));g.FillRectangle(Brushes.CornflowerBlue,20,20,48,48);
     using(var background=new SolidBrush(Color.FromArgb(64,64,64))) g.FillRectangle(background,238,116,96,64);
     g.FillRectangle(Brushes.White,278,136,16,24);
    }
    using(var g=Graphics.FromImage(current)) {
     g.DrawImageUnscaled(before,0,0);
     using(var background=new SolidBrush(Color.FromArgb(92,92,92))) g.FillRectangle(background,238,116,96,64);
     g.FillRectangle(Brushes.White,278,136,16,24);
     if(mode=="outside") g.FillRectangle(Brushes.White,10,10,8,8);
     if(mode=="glyph") g.FillRectangle(Brushes.Black,278,136,16,24);
     if(mode=="color") g.FillRectangle(Brushes.Blue,238,116,96,64);
     if(mode=="large") g.FillRectangle(Brushes.Gray,100,70,240,180);
     if(mode=="identical") g.DrawImageUnscaled(before,0,0);
    }
    if(mode=="antialias") { before.SetPixel(277,136,Color.FromArgb(160,160,160));current.SetPixel(277,136,Color.FromArgb(174,174,174)); }
    if(mode=="high_dpi") { before.SetResolution(192,192);current.SetResolution(192,192); }
    Point anchor=new Point(mode=="away"?100:286,148), cursor=new Point(mode=="hovered"?286:200,148);
    result=VisualTools.RecordingPreHoverFrame(before,current,anchor,cursor);
   }
  }
  Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=result}));return 0;
 }
}
"""


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class RecordingHoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-recording-hover-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "hover.exe"
        source = cls.root / "probe.cs"
        source.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:HoverProbe", "/codepage:65001",
            *("/reference:" + ref for ref in refs), "/out:" + str(cls.helper), str(ROOT / "VisualTools.cs"), str(source)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, mode, *args):
        result = subprocess.run([str(self.helper), mode, *map(str, args)], capture_output=True, text=True,
            check=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)["accepted"]

    def test_neutral_hover_keeps_glyph_and_antialiased_edges(self):
        for mode in ("neutral", "antialias", "high_dpi"):
            with self.subTest(mode=mode):
                self.assertTrue(self.call(mode))

    def test_changed_text_colored_state_and_other_content_are_not_hover(self):
        for mode in ("glyph", "color", "outside", "large", "size"):
            with self.subTest(mode=mode):
                self.assertFalse(self.call(mode))

    def test_prior_cursor_must_be_away_and_current_action_inside_transition(self):
        for mode in ("hovered", "away", "identical"):
            with self.subTest(mode=mode):
                self.assertFalse(self.call(mode))

    def test_recent_same_window_geometry_and_input_witness_is_accepted(self):
        self.assertTrue(self.call("witness_valid"))

    def test_stale_intervening_input_movement_and_occlusion_fail_closed(self):
        for mode in ("witness_stale", "witness_future", "witness_input", "witness_moved", "witness_resized", "witness_cursor", "witness_mode", "witness_occluded"):
            with self.subTest(mode=mode):
                self.assertFalse(self.call(mode))


if __name__ == "__main__":
    unittest.main()
