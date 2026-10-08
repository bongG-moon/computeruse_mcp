"""Execute the production capture geometry with synthetic monitor layouts.

This compiles VisualTools.cs but neither opens a window nor captures the desktop.
The native picker/recorder acceptance checks cover the actual Win32 API results.
"""
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
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
internal static class CaptureGeometryProbe {
    static Rectangle Rect(object raw) {
        var v = (IList)raw;
        return new Rectangle(Convert.ToInt32(v[0]), Convert.ToInt32(v[1]), Convert.ToInt32(v[2]), Convert.ToInt32(v[3]));
    }
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        var json = new JavaScriptSerializer();
        var d = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[0], Encoding.UTF8));
        try {
            if (d.ContainsKey("message")) {
                Console.WriteLine(json.Serialize(new { message = VisualTools.CaptureMessage((string)d["message"]) }));
            } else {
                var monitors = new List<Rectangle>();
                foreach (object item in (IEnumerable)d["monitors"]) monitors.Add(Rect(item));
                Rectangle? visible = d["visible"] == null ? (Rectangle?)null : Rect(d["visible"]);
                Rectangle result = VisualTools.ResolveCaptureBounds(Rect(d["raw"]), visible, (bool)d["maximized"], Rect(d["monitor"]), Convert.ToInt32(d["border"]), monitors);
                Console.WriteLine(json.Serialize(new { rect = new int[] { result.X, result.Y, result.Width, result.Height } }));
            }
        } catch (InvalidOperationException e) { Console.WriteLine(json.Serialize(new { code = e.Message })); }
        catch (Exception e) { Console.WriteLine(json.Serialize(new { harness_error = e.ToString() })); return 1; }
        return 0;
    }
}
"""


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class CaptureGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-capture-geometry-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "capture-geometry.exe"
        probe = cls.root / "probe.cs"
        probe.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        compiled = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:CaptureGeometryProbe", "/codepage:65001",
                        *("/reference:" + ref for ref in refs), "/out:" + str(cls.helper), str(ROOT / "VisualTools.cs"), str(probe)],
                       capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, **override):
        data = {"raw": [100, 100, 816, 638], "visible": [108, 100, 800, 630], "maximized": False,
                "monitor": [0, 0, 1920, 1080], "border": 8, "monitors": [[0, 0, 1920, 1080]], **override}
        request = self.root / "request.json"
        request.write_text(json.dumps(data), encoding="utf-8")
        answer = subprocess.run([str(self.helper), str(request)], capture_output=True, text=True, encoding="utf-8-sig",
                                check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(answer.stdout)

    def test_visible_frame_keeps_exact_pixel_origin(self):
        self.assertEqual(self.call(), {"rect": [108, 100, 800, 630]})

    def test_maximized_invisible_resize_border_is_not_offscreen(self):
        self.assertEqual(self.call(raw=[-8, -8, 1936, 1056], visible=[0, 0, 1920, 1040], maximized=True),
                         {"rect": [0, 0, 1920, 1040]})

    def test_dwm_unavailable_only_trims_maximized_os_border(self):
        self.assertEqual(self.call(raw=[-8, -8, 1936, 1096], visible=None, maximized=True),
                         {"rect": [0, 0, 1920, 1080]})

    def test_dpi_scaled_maximized_border_on_negative_monitor(self):
        self.assertEqual(self.call(raw=[-2904, -24, 2928, 2128], visible=None, maximized=True, border=24,
                                   monitor=[-2880, 0, 2880, 2160], monitors=[[-2880, 0, 2880, 2160], [0, 0, 1920, 1080]]),
                         {"rect": [-2880, 0, 2880, 2104]})

    def test_normal_window_is_never_silently_clipped(self):
        self.assertEqual(self.call(raw=[-8, -8, 800, 600], visible=None), {"code": "target_offscreen"})

    def test_large_offscreen_portion_is_not_treated_as_resize_border(self):
        self.assertEqual(self.call(raw=[-80, -8, 2008, 1096], visible=None, maximized=True), {"code": "target_offscreen"})

    def test_visible_window_on_left_monitor_keeps_negative_origin(self):
        self.assertEqual(self.call(raw=[-1408, 100, 816, 638], visible=[-1400, 100, 800, 630],
                                   monitor=[-1920, 0, 1920, 1080], monitors=[[-1920, 0, 1920, 1080], [0, 0, 1920, 1080]]),
                         {"rect": [-1400, 100, 800, 630]})

    def test_window_can_cross_contiguous_monitor_seam(self):
        self.assertEqual(self.call(raw=[-208, 100, 816, 638], visible=[-200, 100, 800, 630],
                                   monitors=[[-1920, 0, 1920, 1080], [0, 0, 1920, 1080]]),
                         {"rect": [-200, 100, 800, 630]})

    def test_virtual_screen_gap_is_not_real_captureable_display(self):
        self.assertEqual(self.call(raw=[1700, 100, 816, 638], visible=[1708, 100, 800, 630],
                                   monitors=[[0, 0, 1920, 1080], [1920, 1080, 1920, 1080]]),
                         {"code": "target_offscreen"})

    def test_ultrawide_limit_has_specific_diagnosis(self):
        self.assertEqual(self.call(raw=[0, 0, 5120, 1440], visible=[0, 0, 5120, 1440],
                                   monitor=[0, 0, 5120, 1440], monitors=[[0, 0, 5120, 1440]]),
                         {"code": "target_capture_too_large"})

    def test_invalid_frame_size_has_specific_diagnosis(self):
        self.assertEqual(self.call(visible=[10, 10, 0, 200]), {"code": "target_capture_size_invalid"})

    def test_failures_show_actionable_distinct_codes(self):
        for code in ("target_requires_foreground", "target_offscreen", "target_capture_too_large", "target_occluded", "target_unavailable"):
            with self.subTest(code=code):
                message = self.call(message=code)["message"]
                self.assertIn("[" + code + "]", message)
                self.assertNotIn("대상 창을 화면 안에 보이게 한 뒤 다시 캡처하세요", message)

    def test_unexpected_error_does_not_echo_arbitrary_exception_text(self):
        message = self.call(message="private path and arbitrary exception detail")["message"]
        self.assertIn("[screen_capture_failed]", message)
        self.assertNotIn("private path", message)


if __name__ == "__main__":
    unittest.main()
