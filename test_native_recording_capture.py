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
using System.Web.Script.Serialization;
internal static class RenderProbe {
    static int Main(string[] args) {
        var json = new JavaScriptSerializer();
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

if __name__ == "__main__":
    unittest.main()
