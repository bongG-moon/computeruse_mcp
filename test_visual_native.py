"""Run the offline C# matcher against generated PNGs; never opens or controls apps."""
import base64
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib

SOURCE = Path(__file__).resolve().parent
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


def png(width, height, pixel):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    body = b"".join(b"\0" + bytes(channel for x in range(width) for channel in pixel(x, y)) for y in range(height))
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(body)) + chunk(b"IEND", b"")).decode()


def pattern(x, y):
    # Distinctive color blocks and thin edges distinguish icon from blank panel.
    return ((x * 13 + y * 5) % 240, (x * 7 + y * 11) % 240, (x * 3 + y * 17) % 240)


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class NativeMatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-native-match-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "visual.exe"
        references = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        references += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:winexe", "/optimize+", "/codepage:65001",
                                 *("/reference:" + ref for ref in references), "/out:" + str(cls.helper), str(SOURCE / "VisualTools.cs")],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, template, screenshot, **extra):
        request, response = self.root / "request.json", self.root / "response.json"
        response.unlink(missing_ok=True)
        data = {"nonce": "a" * 64, "template_png": template, "screenshot_png": screenshot, "min_score": .94,
                "ambiguity_margin": .03, **extra}
        request.write_text(json.dumps(data), encoding="utf-8")
        subprocess.run([str(self.helper), "--match", str(request), str(response)], timeout=22, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(response.is_file(), "Helper must return structured failure or result")
        result = json.loads(response.read_text(encoding="utf-8-sig"))
        self.assertEqual(result["nonce"], "a" * 64)
        return result

    def test_unique_template_exact_pixel_coordinates(self):
        template = png(32, 24, pattern)
        image = png(180, 110, lambda x, y: pattern(x - 53, y - 37) if 53 <= x < 85 and 37 <= y < 61 else (250, 250, 250))
        result = self.call(template, image)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["rect"], {"x": 53, "y": 37, "width": 32, "height": 24})
        self.assertEqual(result["screenshot"], {"width": 180, "height": 110})

    def crop(self, image, regions):
        request, response = self.root / "crop-request.json", self.root / "crop-response.json"
        response.unlink(missing_ok=True)
        request.write_text(json.dumps({"nonce": "a" * 64, "screenshot_png": image, "regions": regions}), encoding="utf-8")
        subprocess.run([str(self.helper), "--crop", str(request), str(response)], timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(response.read_text(encoding="utf-8-sig"))
        self.assertEqual(result["nonce"], "a" * 64)
        return result

    def test_batch_crop_preserves_requested_pixels_without_desktop_access(self):
        image = png(180, 110, pattern)
        result = self.crop(image, [{"x": 13, "y": 24, "width": 36, "height": 22}, {"x": 50, "y": 40, "width": 21, "height": 31}])
        self.assertEqual(result["status"], "cropped", result)
        self.assertEqual(len(result["crops"]), 2)
        first = self.call(result["crops"][0], image)
        self.assertEqual(first["status"], "matched")
        self.assertEqual(first["rect"], {"x": 13, "y": 24, "width": 36, "height": 22})

    def test_batch_crop_rejects_unsafe_or_fractional_regions(self):
        image = png(80, 80, pattern)
        for regions in ([], [{"x": -1, "y": 0, "width": 8, "height": 8}], [{"x": 78, "y": 0, "width": 8, "height": 8}], [{"x": 1.2, "y": 0, "width": 8, "height": 8}], [{"x": 0, "y": 0, "width": 8, "height": 8, "extra": True}]):
            with self.subTest(regions=regions):
                result = self.crop(image, regions)
                self.assertEqual(result["status"], "failed", result)
                self.assertNotIn("crops", result)

    def test_small_window_border_difference_preserves_exact_template_size(self):
        # Native DWM capture and Driver capture may differ by a one-pixel border
        # while the button's pixels are unchanged (424x230 vs 422x228 in live QA).
        template = png(160, 96, pattern)
        image = png(422, 228, lambda x, y: pattern(x - 89, y - 61)
                    if 89 <= x < 249 and 61 <= y < 157 else (248, 248, 248))
        result = self.call(template, image, capture_window={"width": 424, "height": 230})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["rect"], {"x": 89, "y": 61, "width": 160, "height": 96})

    def test_recorded_full_width_row_matches_across_two_pixel_capture_border(self):
        template = base64.b64encode((SOURCE / "test_data" / "recorded-border-template.png").read_bytes()).decode()
        screen = base64.b64encode((SOURCE / "test_data" / "recorded-border-screen.png").read_bytes()).decode()
        result = self.call(template, screen, source_size={"width": 764, "height": 96}, capture_window={"width": 764, "height": 520})
        self.assertEqual(result["status"], "matched", result)
        self.assertGreaterEqual(result["score"], .94)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["rect"], {"x": 0, "y": 315, "width": 762, "height": 96})
        self.assertEqual(result["template_clip"], {"left": 1, "top": 0, "right": 1, "bottom": 0, "original_width": 764, "original_height": 96})

    def test_border_clipping_and_refinement_do_not_hide_a_second_identical_target(self):
        # Keep the same capture dimensions, but duplicate the actual painted
        # row at another location. Both must survive the common candidate set.
        from image_pixels import decode_png, encode_png
        template = base64.b64encode((SOURCE / "test_data" / "recorded-border-template.png").read_bytes()).decode()
        screen = base64.b64encode((SOURCE / "test_data" / "recorded-border-screen.png").read_bytes()).decode()
        width, height, color, channels, rows = decode_png(screen)
        rows[100:196] = rows[315:411]
        result = self.call(template, encode_png(width, height, color, rows), source_size={"width": 764, "height": 96}, capture_window={"width": 764, "height": 520})
        self.assertEqual(result["status"], "ambiguous", result)
        self.assertGreaterEqual(result["candidate_count"], 2)

    def test_border_clipping_is_not_applied_to_a_large_window_size_change(self):
        template = base64.b64encode((SOURCE / "test_data" / "recorded-border-template.png").read_bytes()).decode()
        screen = base64.b64encode((SOURCE / "test_data" / "recorded-border-screen.png").read_bytes()).decode()
        result = self.call(template, screen, source_size={"width": 764, "height": 96}, capture_window={"width": 770, "height": 526})
        self.assertNotIn("template_clip", result)

    def test_native_recorded_popup_survives_driver_border_difference(self):
        # Unmodified recorder output and Driver PNG from our owned WinForms
        # fixture, not a hand-built substitute for the live failure.
        result = self.call(NATIVE_POPUP_TEMPLATE_PNG, NATIVE_POPUP_DRIVER_PNG,
                           capture_window={"width": 424, "height": 230})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["rect"], {"x": 51, "y": 113, "width": 160, "height": 96})

    def test_sparse_native_field_exact_crop_is_not_lost_by_coarse_search(self):
        result = self.call(NATIVE_FIELD_PNG, NATIVE_SCREEN_PNG, source_size={"width": 329, "height": 74},
                           capture_window={"width": 722, "height": 548})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["rect"], {"x": 22, "y": 392, "width": 329, "height": 74})

    def test_neighboring_periodic_candidates_must_not_merge_as_one(self):
        def stripe(x, y):
            return pattern(x % 16, y)
        template = png(96, 32, stripe)
        image = png(160, 80, lambda x, y: stripe(x - 16, y - 20) if 16 <= x < 144 and 20 <= y < 52 else (248, 248, 248))
        self.assertEqual(self.call(template, image)["status"], "ambiguous")

    def test_same_icon_in_two_rows_is_ambiguous(self):
        template = png(32, 24, pattern)
        def pixel(x, y):
            for top in (17, 69):
                if 40 <= x < 72 and top <= y < top + 24:
                    return pattern(x - 40, y - top)
            return (248, 248, 248)
        self.assertEqual(self.call(template, png(150, 120, pixel))["status"], "ambiguous")

    def test_context_distinguishes_repeated_icons(self):
        def row(x, y, selected):
            if 50 <= x < 82 and 4 <= y < 28:
                return pattern(x - 50, y - 4)
            return ((x * (11 if selected else 17)) % 240, (y * 9) % 230, 170) if x < 35 else (248, 248, 248)
        template = png(86, 32, lambda x, y: row(x, y, True))
        def pixel(x, y):
            for top, chosen in ((13, True), (65, False)):
                if 20 <= x < 106 and top <= y < top + 32:
                    return row(x - 20, y - top, chosen)
            return (248, 248, 248)
        result = self.call(template, png(150, 115, pixel))
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["rect"]["x"], result["rect"]["y"]), (20, 13))

    def test_blank_template_rejected(self):
        result = self.call(png(30, 20, lambda x, y: (245, 245, 245)), png(100, 90, lambda x, y: (245, 245, 245)))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["code"], "template_low_detail")

    def test_missing_image_never_returns_coordinates(self):
        result = self.call(png(32, 24, pattern), png(150, 120, lambda x, y: (245, 245, 245)))
        self.assertEqual(result["status"], "not_found")
        self.assertNotIn("rect", result)

    def test_invalid_png_is_explicit_failure(self):
        result = self.call(base64.b64encode(b"not a PNG").decode(), png(50, 50, pattern))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "invalid_png")

    def test_threshold_cannot_be_weakened_arbitrarily(self):
        result = self.call(png(32, 24, pattern), png(100, 80, pattern), min_score=.4)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "invalid_match_threshold")

    def test_source_size_matches_downsampled_large_crop(self):
        def smooth(x, y):
            if x in (0, 31) or y in (0, 23):
                return (25, 40, 190)
            return (35 + x * 5, 45 + y * 5, 80 + x * 2)
        template = png(32, 24, smooth)
        image = png(240, 160, lambda x, y: smooth((x - 63) // 2, (y - 45) // 2)
                    if 63 <= x < 127 and 45 <= y < 93 else (250, 250, 250))
        result = self.call(template, image, source_size={"width": 64, "height": 48}, capture_window={"width": 240, "height": 160})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["rect"], {"x": 63, "y": 45, "width": 64, "height": 48})

    def test_refined_gradient_remains_ambiguous_across_nested_scales(self):
        # Refinement exposes an alternate 48x36 inner-gradient alignment:
        # .99516660 - .96516732 = .02999928, below the required .03 margin.
        # Keep that genuine ambiguity instead of rounding away the threshold.
        def smooth(x, y):
            return (35 + x * 5, 45 + y * 5, 80 + x * 2)
        image = png(240, 160, lambda x, y: smooth((x - 63) // 2, (y - 45) // 2)
                    if 63 <= x < 127 and 45 <= y < 93 else (250, 250, 250))
        result = self.call(png(32, 24, smooth), image, source_size={"width": 64, "height": 48}, capture_window={"width": 240, "height": 160})
        self.assertEqual(result["status"], "ambiguous", result)
        self.assertLess(result["score"] - result["second_score"], .03)
        self.assertNotIn("rect", result)

    def test_capture_ratio_and_source_size_combine(self):
        def smooth(x, y):
            return (35 + x * 5, 45 + y * 5, 80 + x * 2)
        image = png(240, 160, lambda x, y: smooth((x - 63) // 2, (y - 45) // 2)
                    if 63 <= x < 127 and 45 <= y < 93 else (250, 250, 250))
        result = self.call(png(32, 24, smooth), image, source_size={"width": 128, "height": 96}, capture_window={"width": 480, "height": 320})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["rect"], {"x": 63, "y": 45, "width": 64, "height": 48})

    def test_png_declared_size_is_checked_before_image_decode(self):
        raw = bytearray(base64.b64decode(png(32, 24, pattern)))
        raw[16:20] = struct.pack(">I", 999999)
        result = self.call(base64.b64encode(raw).decode(), png(100, 80, pattern))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "image_dimensions_invalid")

    def test_second_similar_candidate_rejects_small_score_margin(self):
        template = png(32, 24, pattern)
        def pixel(x, y):
            for left, delta in ((17, 0), (87, 2)):
                if left <= x < left + 32 and 30 <= y < 54:
                    return tuple(min(255, c + delta) for c in pattern(x - left, y - 30))
            return (248, 248, 248)
        self.assertEqual(self.call(template, png(150, 100, pixel))["status"], "ambiguous")


# PNGs from the owned, per-monitor-DPI-aware synthetic WinForms fixture.
# The field is an exact crop at (22,392); no user application data is included.
NATIVE_FIELD_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAUkAAABKCAIAAAC5CzHlAAAFFElEQVR4nO3dz2vbZhzH8cfFOwzGCnLj/AOuk8bkGoMJWdixlKUw'
    '6HGUgA+9B3JYj70Uctgth0AYOwY6Egg5hiyM0OSaOUmN/wG5ttkgWzbq1sOK49qyJEuypMRfvV+XJLb0PHqEPn5+SG4Tl1dNBUCc'
    'e7d9AABCQbYBmcg2IBPZBmQi24BMZBuQiWwDMpFtQCayDchEtgGZyDYgE9kGZCLbgExkG5CJbAMykW1AJrINyJSMvsrG8cbmga5y'
    'z1aeZG9eq5/8vH6oq5nvXz7OBFub15LDO5LPhbdNfrv8fKoyUl2eD9XqzEMsb9nuXBzWJheKxTnN11E03l0YpZ5dlB9nhF52jaNf'
    'NvarcWs1YtVvW9Cmpif3q7qamc5afZqkn64uBd+JRqtyZAS73V0Xbj4A63atBm4p28EP6lJzz1/ODb7cONptDxPSauyVyyVjaPNd'
    'N9j2rQaCwFoaIBPZBmQKe75d3nu9dTZ8MG9e8u1ftDvdfn16/Vt68cUP+VR3t8ruq+32WHdo+dYLWqbSPDdHzQ5fCKjsrL3pHLxS'
    '+uHGq0PjN6Olzgvd3uty2J218fgJMdsWi+qlrbXS7MJiOIVfl1/rXazqY/4gUKp6sL52bru9ixrbHzph3C0bsa5Qzzzinu3Pl1dv'
    'j2F0YocHw3fX8sWVfLebte+yBgpX+v7u8ZRFb3yxs13q66g7R6jvb+4+GL40WNkZbM71h8XZm52sQ4+aWVpZXbrpRd3dJvRdVxBn'
    'HvHOdrsTML/WP74tvzUuL/OgN7O0Upww3+b1Kfd0tdh3lWeWlhermwd69X1dKXO2z0rmTk/LF5eVEYPSbyeFrGPkynvGuNrUnMyT'
    'Fwu19UP99Oh4PuNtbB9eXRGcecR5La1ybsz0ZguDV6FWKOQCqEHLFwa7Ly1l3C2r1RqDO+SeDY5mtfw3M+2futUObpqTmpufbY/t'
    'z985F+DBiHWFf+YR6/vblQtj9Sj3yHL0qE1MKmX3cNtoD3Las3k+JJvNqbOSqtXrKpPy0xxtIq1UVX/fUMrfI3nB1hXtmUdc19LS'
    'E0ENUy2ZlpFHYjmMdzMTCctodYV85jEe7sYzp1713crqWWnru+HkAWGAPEkpz2b7U2/U3G7q/Wa4f1HWBalCWUsz5nV2qz71yvmI'
    'U75OINOPpkzBbtRtl4HbM+pBnW9iTU4/TA1vTnvcHroR6wr5zGOchJPth7n2grW+/7Yy8F7l96HrXgOqDVfL0J3bP9ZFHPx60rDr'
    '/3MZzUVzSlt7g80J2oh1BX3mMcYSl1dNu/c+fvzQ/O+q1foU7SEBGCKRuPf3py+0r770Od/+8O8/9+9/nUyO5ZwcEKzZbP6l/6kc'
    's+08Jm8RbOAOSiaTLTVkQM13PAGZyDYgk8+5dCKR6P2z1WrZbWP5lnNpzsUCCCXb3Rx2g5cw+I6i3b7XxRJvIIoxeTeHvZHr/mnX'
    '/foTRplAfHjItnPnTLyBO4W1NCDe2XYzo6brBu4O+m1AJrINyES2AZnINiAT2QZkItuATGQbiHe23dy7HuWp8ijLBOKAfhtQcc+2'
    'c9dNpw2Mcb/djXdvwkf8jqcdRuNApN/ftoy3ZapbrZZpM8ttHDZjmg1E/e+uuExdsJsBcI+1NEAmsg3IRLYBmcg2IBPZBmQi24BM'
    'ZBuQiWwDMpFtIH7PpT348Y8IjwSAN5c/zfvst6fSTv9zN4BbNDSeicurZlQHAyA6zLcBmcg2IBPZBpRI/wNqsp2bnM1T0wAAAABJ'
    'RU5ErkJggg=='
)
NATIVE_SCREEN_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAtIAAAIkCAYAAAAzocyuAACldklEQVR4Ae3AA6AkWZbG8f937o3IzKdyS2Oubdu2bdu2bdu2bWmM'
    'npZKr54yMyLu+Xa3anqmhztr1a/u7++bq6666qqrrrrqqquuuupfg+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiu'
    'uuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqq'
    'q6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2C'
    'q6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tei8u/0l3/+V/xXetmXfxmuuuqqq6666qqrrrrqvxmV/wAv+/Ivw4viL//8'
    'r5g96e2wTVpkGtu0hEzINC1NJkxpsplmaA1amhte++e56qqrrrrqqquuuuqq/wGo/Bez4aXf6eshB9zWMC1xW8F4SE6HMB7BtI/H'
    'Qzwd4vEQj0f8wm89mauuuuqqq6666qqrrvofgsp/MadhOsJtjacltCOYjvB4COMBng7xeADjITkewXSIpyNaM1ddddVVV1111VVX'
    'XfU/BMF/sQZ4PMTDPh4u4fUlcn0Rry/g9UW8vojXu3i9C8MuHvbIYZ+W5qqrrrrqqquuuupf6+joiA/5kA/hzJkzbG9vs729zfb2'
    'Ntvb22xvb7O9vc329jbb29tsb2+zvb3N9vY229vbbG9vs729zZkzZ/iQD/kQjo6OuOpf9tIv+/K80Ru/MXt7e/xb7e3t8cZv8ia8'
    'zMu9Av9DEfwXywQPl2DYxcMuOVzEq4vkehcPu3h9Ea93yWGPHPbI4QBPR7Tkqquuuuqqq6666l/t4z7u4/j+7/9+VqsV/1ar1Yrv'
    '//7v5+M+7uO46l927TWn+bM//wve9u3fnr29Pf619vb2eNu3f3v+9M/+nGuvOc3/UAT/xZwmh11yvQvri7C6gNcX8foCubqIh0vk'
    'sIeHfXI8JKcjclrTmrnqqqv+/d7mbd6G7e1trvqv80d/9Edsb2/z1V/91Vx11VX/9X78x38cgB/90R/ljjvu4I477uCOO+7gjjvu'
    '4I477uCOO+7gjjvu4I477uCOO+7gjjvu4I477uCOO+7gjjvu4I477uBHf/RHAfjxH/9xrvqX/diP/igv+7Ivw1/+5V/xtm//9uzt'
    '7fGi2tvb423f/u35y7/8K172ZV+GH/2RH+F/KIL/AH/553/FX/75X/GXf/5X/OWf/xV/+ed/xV/++V/xl3/+V/zln/8Vf/nnf8Vf'
    '/vlfAdBsWF+E4QK5voDXF/Gwi9eX8HCJXO/hcZ+cDvG4wtNIThMt+Td7m7d5G7a3t7nq+fujP/ojtre32d7eZnt7m+3tbba3t9ne'
    '3mZ7e5vt7W3+6I/+iP8un/M5n8P29jbf+Z3fyf9Et956K5/zOZ/Da73Wa7G9vc329jbb29u8zdu8Db/yK7/CVc/f53zO57C9vc13'
    'fud38p/hbd7mbdje3mZ7e5vt7W22t7fZ3t7mtV7rtfjqr/5qLly4wFVXXfX/w2q1AuBVX/VVOXbsGMeOHePYsWMcO3aMY8eOcezY'
    'MY4dO8axY8c4duwYx44d49ixYxw7doxjx45x7Ngx3viN3xiA1WrFVf+ynZ0dfvLHf5yXfdmX4S//8q9427d/e/b29viX7O3t8bZv'
    '//b85V/+FS/7si/DT/74j7Ozs8P/UFT+Azz4o1+XBBqiJTTEZGgWk8VomBD+xl8hG3h9EY+HMB2Q4xFMR7gt8bQkpzW0gWwTdsPN'
    'ZEI2c9V/roc85CE87GEP4/nZ2triquf1d3/3d7zpm74pu7u7ALz+678+AH/+53/Or//6rwPwRm/0RvxXunDhAl/3dV/H7u4uX/VV'
    'X8V/p+/8zu/ke77ne/id3/kd/ru8/uu/Pve7cOECf/mXf8lf/uVf8p3f+Z387M/+LA9+8IO56qqrrvqXSOKqf52dnR1+8sd/nLd9'
    '+7fnL//yr3jbt397fvLHf5ydnR2en729Pd727d+ev/zLv+JlX/Zl+Mkf/3F2dnb4H4zKf4DB4rov/FEAPI7kNOFhINdrcr0mVyv+'
    '8ps+hw2gGby+iKdDPB7h6QhPK9yWeBpwjjgbbo00uIENLc1V/7ne933fl4/+6I/mf5rP+qzP4rM+67P4n+bChQu86Zu+Kbu7u3zN'
    '13wN7/u+78sD/dEf/RF/8id/wn+1Jz7xiXz5l385r//6r89/t4/6qI/iBfmsz/osPuuzPov/bD/1Uz/FA91666189md/Nj/xEz/B'
    'x3zMx/BTP/VTXHXVVVdd9Z9jZ2eHn/zxH+dt3/7t+cu//Cve9u3fnp/88R9nZ2eHB9rb2+Nt3/7t+cu//Cte9mVfhp/88R9nZ2eH'
    '/+EI/gOsUrhNtNWStlySBwdM+/uMe3uMe5cYL+1y2AKAbInXu3h9iRz2yGEfjwd4WJFtTU4jOTVag5ygNWgNMrnqqv9Rfvqnf5rd'
    '3V0+/uM/nvd93/flub3Kq7wKH/3RH81V/7M8+MEP5iu/8it5yEMewq//+q9z6623ctVVV1111X+enZ0dfvLHf5yXfdmX4S//8q94'
    '27d/e/b29rjf3t4eb/v2b89f/uVf8bIv+zL85I//ODs7O/wvQPAfYJlBWy7JwyOmgwPGg33GvT3apUtMu7uMu5c4yAJAS8hxjxz2'
    '8XiAxyNyWpG5xtNENpMNnNASnJAJLbnqqv9R9vb2ADh27BhX/e9y8uRJXu/1Xg+Au+++m6uuuuqqq/5z7ezs8JM//uO87Mu+DH/5'
    'l3/F277927O3t8fe3h5v+/Zvz1/+5V/xsi/7Mvzkj/84Ozs7/C9B8B/gKEU7OGDa36NdusR06RLT7i7j7i7jxV2mixfYbwKgJeR6'
    'H48HeFjiNpDTSLakNcgG2aA1cIOWkAlp8x/tq7/6q9ne3uaP/uiP+Lu/+zve+73fm+3tbba3t3nv935vbr31VgAuXLjAx3zMx3Dz'
    'zTezvb3NS77kS/LVX/3VPD8XLlzgq7/6q3mt13ottre32d7e5uabb+ZjPuZjuHDhAs/PhQsX+Oqv/mpe8iVfku3tbba3t3nv935v'
    '/u7v/o6v/uqvZnt7m6/+6q/m+fmVX/kV3uZt3obt7W22t7d5yZd8Sb76q7+a/0x/9Ed/xPb2Nl/91V/NhQsX+Oqv/mpe8iVfku3t'
    'bW6++WY+5mM+hgsXLvD8XLhwga/+6q/mJV/yJdne3mZ7e5v3fu/35u/+7u/46q/+ara3t/nqr/5qHuirv/qr2d7e5o/+6I94oLd5'
    'm7dhe3sbgD/6oz/ivd/7vdne3mZ7e5vXeq3X4ld+5Vd4Qf7u7/6O937v9+bmm29me3ubm2++mY/5mI/hwoULvKhuuukmAH7nd36H'
    'f8nHfMzHsL29zXd+53fygrz3e78329vb/Mqv/AoAX/3VX8329jZ/9Ed/xK233srHfMzHcPPNN7O9vc1LvuRL8tVf/dU80B/90R+x'
    'vb3NG77hGwLw67/+62xvb7O9vc3bvM3b8Pz83d/9He/93u/N9vY229vbvNZrvRa/8iu/wgvyd3/3d7z3e783N998M9vb29x88818'
    'zMd8DBcuXOCBtre32d7e5n7b29tsb2+zvb3N/b76q7+a7e1t/uiP/ojn51d+5Vd47/d+b26++Wa2t7fZ3t7mvd/7vfm7v/s7/iM8'
    '6EEP4rl99Vd/Ndvb2/zRH/0Rf/d3f8fbvM3bsL29zdu8zdvwQLfeeisf8zEfw0u+5Euyvb3N9vY2L/mSL8nnfM7ncOHCBf4lX/3V'
    'X81rvdZrsb29zfb2Nu/93u/NH/3RH/GCXLhwgY/5mI/hJV/yJdne3mZ7e5v3fu/35u/+7u+46qqrrvrfYmdnh5/88R/nZV/2ZfjL'
    'v/wr3vbt3563ffu35y//8q942Zd9GX7yx3+cnZ0d/hch+A9w2Arj3h7TpT3GS5cYL+4yXtpl3L3IuHuRcXeXw6kA0NLkdEhOK7IN'
    'tGkkm8kJ3CATMiETWkJLaAmZ5j/L4x//eN70Td+US5cu8f7v//485CEP4Sd+4id4y7d8S2699Vbe5m3eht/4jd/g7d/+7Xn91399'
    'nv70p/MZn/EZfPVXfzXP7f3e7/34jM/4DADe7u3ejvd///fnxIkTfPu3fztv8zZvw3O7cOECb/M2b8NnfMZnAPB2b/d2vP/7vz9/'
    '+Zd/yZu+6Zvy13/917wgH/MxH8Pbv/3b8+d//ue83du9He///u8PwGd8xmfwWq/1WvxXeJu3eRu+8zu/k9d7vdfj7d7u7QD49m//'
    'dt7mbd6G53bhwgXe5m3ehs/4jM8A4O3e7u14//d/f/7yL/+SN33TN+Wv//qv+bf4lV/5Fd7wDd+QS5cu8fEf//G87Mu+LH/5l3/J'
    '27/92/Mrv/IrPLcf//Ef51Vf9VX5iZ/4CV7+5V+ej//4j+ehD30o3/7t385LvdRLceutt/KiePu3f3te9mVfll//9V/nbd7mbbj1'
    '1lt5Qd73fd8XgK/+6q/m+blw4QI/8RM/wUMe8hDe6I3eiAe68847eY3XeA3+8i//kvd///fn9V//9Xn605/OZ3zGZ/A5n/M53G9r'
    'a4vXf/3X52Vf9mUBOH78OK//+q/P67/+6/PSL/3SPLdf+ZVf4VVf9VV5+tOfzsd//Mfzsi/7svzlX/4lb//2b8+v/Mqv8Nx+/Md/'
    'nFd91VflJ37iJ3j5l395Pv7jP56HPvShfPu3fzsv9VIvxa233sr9Xv/1X5/Xf/3X536v//qvz+u//uvz+q//+rwoPuZjPoa3f/u3'
    '5yd+4id4+Zd/ed7//d+fl33Zl+UnfuIn+I3f+A3+I/zO7/wOL8je3h7v9m7vxq//+q/z3H78x3+cl3iJl+Dbv/3bOXHiBO///u/P'
    '273d2wHw5V/+5bzUS70Uf/d3f8cL8jEf8zF8xVd8BSdPnuTjP/7jedmXfVl+4id+gjd8wzfkx3/8x3luf/d3f8dLvdRL8e3f/u2c'
    'OHGCj//4j+f1X//1+Ymf+Ale9VVflV/5lV/hqv+dtre32d7eZnt7m+3tbba3t9ne3mZ7e5vt7W22t7fZ3t5me3ub7e1ttre32d7e'
    'Znt7m+3tbba3t9ne3mZ7e5vt7W2uuup/g52dHX7yx3+cl33Zl+Ev//Kv+Mu//Cte9mVfhp/88R9nZ2eH/2Wo/Ac4aMG0e4lcHtGW'
    'S9rRklwtacsluVzRlksOMwBozXgacE64GSekIRvYkAYbsoENCWRCS/7TfNZnfRZf9VVfxdu//dsDcOHCBd7v/d6PX//1X+ct3/It'
    'OXHiBL/927/NyZMnAfijP/oj3vAN35Cv+Iqv4KM/+qN5oJd+6ZfmEz/xE3mVV3kVHuht3uZt+PVf/3V+5Vd+hTd6ozfifu/3fu/H'
    'X/7lX/L+7//+fNVXfRUP9DEf8zF8+7d/O8/Pd37nd/Lt3/7tvOzLviw/9VM/xcmTJ7nfe7/3e/MTP/ET/PiP/zhv//Zvz4vqMz7j'
    'M/iMz/gMntvrv/7r81M/9VM8t6/4iq/g9V7v9fipn/opTp48CcCFCxd4qZd6Kf7yL/+SX/mVX+GN3uiNuN/7vd/78Zd/+Ze8//u/'
    'P1/1VV/FA33Mx3wM3/7t386/xfu///vzq7/6q7zKq7wKAJ/1WZ/FV3/1V/MZn/EZfOEXfiFv9EZvxP3+7u/+jvd5n/fh+PHj/OIv'
    '/iIv8RIvAcBnfdZn8Z3f+Z181Ed9FF/zNV/DV33VV/Gi+J7v+R7e673ei1//9V/nJV7iJXj/939/PuqjPooHP/jBPNBLvMRL8JCH'
    'PISnP/3p/N3f/R0v8RIvwQP95m/+JgDv+77vy3P7mI/5GD7u4z6Oj/7oj+Z+f/RHf8QbvuEb8uVf/uV8xEd8BCdPnuQlXuIl+Kmf'
    '+in+6I/+iDd8wzfk5V/+5fmpn/opXpD3f//351d/9Vd5lVd5FQA+67M+i6/+6q/mMz7jM/iET/gE3uiN3oj7/d3f/R3v8z7vw/Hj'
    'x/nFX/xFXuIlXgKAz/qsz+I7v/M7+aiP+ii+5mu+hq/6qq8C4Kd+6qcA2N7eBuCnfuqneFF9zud8Dt/+7d/Oy77sy/I93/M9PPjB'
    'D+Z+f/RHf8TjH/94/r3+7u/+jl//9V/nIQ95CK/yKq/Cc/uhH/ohTpw4wc/+7M/y4Ac/mPv93d/9He/zPu/D8ePH+dEf/VFe5VVe'
    'hQf6mI/5GL7927+dD//wD+d3fud3eG7f+Z3fyYkTJ/ibv/kbTp48CcBnfdZn8eM//uO8z/u8Dx/zMR/Dy7/8y/PgBz8YgAsXLvBu'
    '7/Zu7O7u8l3f9V28/du/Pff7u7/7O171VV+VT/iET+CN3uiNuOqqq6666r8cwX+A/RaMuxcZL15ivLjLdGmXcfcS0+4u06VLtL09'
    'DjMAaAm/9rdzfuVvN/mlv9viF/52k5//mw1+7m8X/OzfLPiZv1rwU38556f+esZP/NWMn/yLnp/8yx6b/zSv93qvx9u//dtzv5Mn'
    'T/LBH/zBADz96U/ne77nezh58iT3e5VXeRUe8pCHsLu7y9/93d/xQJ/1WZ/Fq7zKq/DcXuu1XguAxz/+8dzv7/7u7/j1X/91HvKQ'
    'h/BVX/VVPLev+qqv4mVf9mV5fj7rsz6L48eP81M/9VOcPHmSB/rsz/5sAL7u676Of42HPOQhvP7rvz6v//qvz+u//uvz+q//+rz+'
    '678+L/3SL80L8pVf+ZWcPHmS+508eZKP+7iPA+Dxj3889/u7v/s7fv3Xf52HPOQhfNVXfRXP7au+6qt42Zd9Wf4tPu7jPo5XeZVX'
    '4YE++qM/GoC//Mu/5IG+4iu+AoAf/dEf5SVe4iV4oPd93/flIQ95CN/+7d/OhQsXeFE8+MEP5nd+53f4vM/7PI4fP863f/u38xIv'
    '8RJ8zud8DhcuXOCBPvqjPxqAn/zJn+S5fd3XfR0Ab/3Wb81ze/mXf3k++qM/mgd6lVd5FV7/9V8fgCc+8Yn8W3zcx30cr/Iqr8ID'
    'ffRHfzQAT3/607lw4QL3+4qv+AoAfvRHf5SXeImX4IHe933fl4c85CF8+7d/OxcuXODf49Zbb+XLv/zLOX78OD/1Uz/Fgx/8YB7o'
    'VV7lVXjf931f/q1uvfVWfvzHf5w3fdM3BeDLvuzLeH7+8i//kp/6qZ/iwQ9+MA/0FV/xFQB81Vd9Fa/yKq/Cc/uqr/oqXv/1X5+/'
    '/Mu/5Fd+5Vd4bk9/+tP5nu/5Hk6ePMkDvf3bvz3v//7vz+7uLr/5m7/J/X76p3+apz/96Xze530eb//2b88DvcRLvATv//7vz9Of'
    '/nR+5Vd+hav+99nf32d/f5/9/X329/fZ399nf3+f/f199vf32d/fZ39/n/39ffb399nf32d/f5/9/X329/fZ399nf3+f/f199vf3'
    'ueqq/w329vZ427d/e/7yL/+Kl33Zl+FlX/Zl+Mu//Cve9u3fnr29Pf6XofIf4CCD6eIubXlELpe01YpcrWirNblek8OaZQYAN7z2'
    'z/M/zUu/9Evz3HZ2dgB4yEMewoMf/GCe28Me9jCe/vSnc3BwwHO79dZb+fM//3PuuOMOfud3focLFy7wl3/5lzy3P/uzPwPgfd/3'
    'fXlB3uZt3oa//Mu/5IH+6I/+iN3dXd7u7d6OkydP8twe/OAHA/CXf/mX/Gu87/u+Lx/90R/Ni+rlX/7lOXnyJM/tlV7plQD4nd/5'
    'HT76oz8agD/7sz8D4H3f9315Qd7mbd6Gv/zLv+Rf65Ve6ZV4fl7/9V+fX//1X+eP/uiPeJVXeRUAfuInfoLjx4/zKq/yKjw/D3vY'
    'w3j605/OE5/4RF7lVV6FF9VHf/RH857v+Z587/d+L1/xFV/Bl3/5l/Obv/mbfP3Xfz0v8RIvAcBbv/Vb81Ef9VH8xE/8BJ/1WZ/F'
    '/W699Vb+8i//krd7u7fjwQ9+MM/ttV7rtXh+Xuu1Xotf//Vf50/+5E94lVd5Ff61XumVXonn5/Vf//X59V//dZ74xCfyKq/yKgD8'
    'xE/8BMePH+dVXuVVeH4e9rCH8fSnP50nPvGJvMqrvAr/Vr/5m78JwPu///tz8uRJ/iNsb2/z/HzXd30Xb/RGb8Tz83Zv93acPHmS'
    '5/YTP/ETALz92789L8hbvMVb8Ou//us8/vGP543e6I14oNd//dfnwQ9+MM/PO77jO/Lt3/7t/O7v/i7v+77vC8DP/dzPAfB6r/d6'
    'PD8PetCDAHj84x/PG73RG3HVVVdd9T/Z3t4eb/v2b89f/uVf8bIv+zL85I//OABv+/Zvz1/+5V/xtm//9vzkj/84Ozs7/C9B5T/A'
    'QStMuxeZlityvSKXS3K9JocBDyM5DqyyA+BXdm/hgTJNa402Jdka49hoU2OaJsax0caJYWxM48RHvUbhP8MrvdIr8YI87GEP41/j'
    'cz7nc/jyL/9y7veyL/uynDx5ktd//dfn13/913mgvb09AF7plV6Jf4uf+Imf4Cd+4if47/Jar/VavKj29vYAeKVXeiX+o73Kq7wK'
    '/xq7u7tsb2/zH+3kyZN89Ed/NO/5nu/Jx37sx/ITP/ETfPiHfzi/8zu/A8DJkyd5//d/f77927+dv/u7v+MlXuIlAPie7/keAN78'
    'zd+c5+eVXumV+M/wKq/yKvxr7O7usr29zX+mvb09AF75lV+Z/yiv//qvzwO9xVu8Ba/7uq/Lgx/8YF6Qm2++mRfk9V//9XlhHvOY'
    'xwDwO7/zO3z0R380D/Rar/Va/EsuXbrEc3vVV31Vrrrqqqv+N9vb2+Nt3/7t+cu//Cte9mVfhp/88R9nZ2cHgJ/88R/nbd/+7fnL'
    'v/wr3vbt356f/PEfZ2dnh/8FqPwHOGriu//0rxlTTBYNmCyaYbJIetI8y6NPPYgHaglTg6HB0GAYYT3BeoL1CMMET37cLwOb/E/2'
    '1V/91Xz5l385L/uyL8vXf/3X8xIv8RLc76u/+qv59V//dR7o0qVL/Hs85CEP4WEPexj/G1y6dIn/KY4fP87Lv/zL88JsbW3xb3Xy'
    '5Em++7u/m0uXLvHrv/7r/Mqv/Apv9EZvBMAbv/Eb8+3f/u1853d+J1/1VV8FwE/8xE9w/Phx3v7t357/yY4fP87Lv/zL88JsbW3x'
    'H2FnZ4f/KD/1Uz/Fv9ZjHvMY/r2OHTvGf5TXf/3X54W56aabuOqqq676n2pvb4+3ffu35y//8q942Zd9GX7yx3+cnZ0d7rezs8NP'
    '/viP87Zv//b85V/+FW/79m/PT/74j7Ozs8P/cFT+A7zRD/84L6rM5DLDlDA2GBqMDYYRVg2GEdYjDBMMI4wTHB2tgE3+J/upn/op'
    'AL7ne76HBz/4wfxLjh07BsDjH/94XuVVXoXn59KlS7wgr/d6r8dXfdVX8b/BsWPHAHj84x/Pq7zKq/D8XLp0if8KJ06c4Kd+6qf4'
    'z/Zar/Va/Pqv/zqPf/zjeaM3eiMA3uiN3oiHPOQh/PiP/zhf9VVfxR/90R/x9Kc/nY//+I/nf7oTJ07wUz/1U/xX+JM/+RNe5VVe'
    'hf+Jfv3Xf50X5vGPfzwAL/3SL81ze8YznsELcueddwLw0i/90jy3r/qqr+LBD34wV1111VX/2+zt7fG2b//2/OVf/hUv+7Ivw0/+'
    '+I+zs7PDc9vZ2eEnf/zHedu3f3v+8i//ird9+7fnJ3/8x9nZ2eF/MIL/AC/78i/Dy778y/CyL/8yvOzLvwwAL/vyL8PLvvzL8LIv'
    '/zK87Mu/DC/78i8DwLieGBscDXCwhv017C/h0hHsHsGlQ7h0BHuHsH8IB0cjR4dHrA5X/E/3l3/5lwA8+MEP5rn9zu/8Ds/tpptu'
    'AuDnfu7neH4uXLjAt3/7t/PcHvWoRwHw4z/+41y4cIH/DW666SYAfu7nfo7n58KFC3z7t387/9le9mVflqc//en80R/9Ef/ZLl26'
    'BMDOzg4P9L7v+77s7u7yK7/yK/zoj/4oAO/1Xu/F/2Qv+7Ivy9Of/nT+6I/+iP9Mr/RKrwTAd37nd/I/0du93dsB8OM//uO8ID/3'
    'cz8HwOu93uvx3H78x3+cCxcu8Pz8wA/8AACv/MqvzP1e+qVfGoCf/umf5qqrrrrqf5u9vT3e9u3fnr/8y7/iZV/2ZfjJH/9xdnZ2'
    'eEF2dnb4yR//cV72ZV+Gv/zLv+Jt3/7t2dvb438wgv9i6/XAwRr217C3hEtHcGkJl45gbwn7R3BwaA6XI0dHhywP9jja32V5tOZ/'
    'upd92ZcF4Du/8zt5oO/8zu/k13/913lub//2b89DHvIQfv3Xf52P+ZiP4YEuXLjA533e5/H8nDx5ko//+I9nd3eXj/3Yj+XChQs8'
    '0IULF/jqr/5q/uiP/oj/Kd7+7d+ehzzkIfz6r/86H/MxH8MDXbhwgc/7vM/jv8KnfuqnAvDJn/zJ/N3f/R3P7cd//Mf5zu/8Tl4U'
    '3/md38l3fud3cuutt/LcfuVXfoVv//Zv5/jx47zu674uD/TWb/3WAPzyL/8yP/7jP87rv/7r8+AHP5j/KNdffz0Af/7nf86FCxf4'
    'j/Cpn/qpAHzyJ38yf/d3f8dz+/Ef/3G+8zu/k+f2kIc8BIA/+qM/4kXxKq/yKrz+678+T3/603nv935vLly4wAP90R/9Ed/5nd/J'
    'f5cP+qAPAuBjPuZj+KM/+iOe28d8zMfw67/+67z+678+L/ESL8Fz293d5fM+7/O4cOECD/QxH/Mx/Pqv/zov+7Ivyxu90Rtxv/d6'
    'r/fi+PHjfMVXfAW/8iu/wnP7oz/6Iz7ncz6Hq6666oWbz+cA/OEf/iG2+bf4pV/6JQDm8zlX/cv29vZ427d/e/7yL/+Kl33Zl+En'
    'f/zH2dnZ4V+ys7PDT/74j/OyL/sy/OVf/hVv+/Zvz97eHv9DUfkvtloOXFrCeoRhgvUIwwjDCNOYTNPIOA5M45ppWDIOa9q4ZLUe'
    '+J/uUz/1U3n7t397PuqjPoqf+7mfA+CpT30qFy9e5O3e7u34iZ/4CZ7bD/zAD/Cmb/qmfPu3fzu/8Ru/wcMe9jAA/vzP/5yHPvSh'
    'vP/7vz9f/uVfznP7iI/4CH7zN3+Tn/iJn+A3fuM3ePmXf3nu9+d//ufs7u7yq7/6q/xrfOd3fie/8zu/w/Pz0i/90nzWZ30W/x4/'
    '8AM/wJu+6Zvy7d/+7fzGb/wGD3vYwwD48z//cx760Ify/u///nz5l385/5ne6I3eiPd///fn27/923nVV31VXv/1X5/7PfWpT+Xp'
    'T386n/d5n8eLYm9vj8/4jM8A4Pjx47z8y788AH/+53/O7u4uAN/1Xd/Fgx/8YB7owQ9+MG/3dm/Ht3/7twPwbu/2bvxHevCDH8zL'
    'vuzL8pd/+Ze89mu/Ng972MN48IMfzFd91Vfxb/VGb/RGvP/7vz/f/u3fzqu+6qvy+q//+tzvqU99Kk9/+tP5vM/7PJ7b273d2/Hl'
    'X/7lvOM7viMv//Ivz4ULF/id3/kdXpjv+I7v4G3e5m34iZ/4CX7jN36Dl3/5lwfgwoUL/OVf/iWf93mfx3+XV3mVV+G7vuu7eJ/3'
    'eR/e8A3fkJd92Zfl5MmTADz1qU/l6U9/Oi/7si/Ld3zHd/D8fPzHfzzf/u3fzo//+I/z8i//8gA89alP5elPfzrHjx/n67/+63mg'
    'Bz/4wXzVV30V7/M+78Pbv/3b87Iv+7KcPHkSgAsXLvCXf/mXvP7rvz5XXXXVC/f2b//2fP/3fz/v+I7vyL/X27/923PVv+wd3+md'
    '+Mu//Cte9mVfhp/88R9nZ2eHF9XOzg4/+eM/ztu+/dvzl3/5V7zDO74jv/LLv8z/QAT/xZZHKy4dwt4h7B3C/iEcHjaOjlYsj/Y5'
    'OrzE8uAiy4PzLA8usDo4x+rgPONq5H+6N3qjN+LHf/zHedmXfVl+/dd/nV//9V/nZV/2ZfnFX/xFXvqlX5rn5yVe4iX4xV/8Rd7u'
    '7d6Opz/96fz6r/86T33qU/m4j/s4fuqnfopjx47x/Jw8eZLf+Z3f4fM+7/N46EMfyq//+q/z67/+6/z5n/85r/d6r8ev/uqv8iqv'
    '8ir8azz96U/n13/91/n1X/91fv3Xf51f//Vf59d//df59V//df76r/+af6+XeImX4Bd/8Rd5u7d7O57+9Kfz67/+6zz1qU/l4z7u'
    '4/ipn/opjh07xn+Fr/qqr+LHf/zHef3Xf31+/dd/nV//9V/n13/913nYwx7Gd33Xd/HRH/3RvChe6ZVeifd///fnZV/2Zdnd3eXX'
    'f/3X+fVf/3Ue+tCH8vEf//H83d/9HW//9m/P8/Pmb/7mABw/fpy3f/u35z/a13/91/P6r//6PP3pT+fXf/3XOX78OP9eX/VVX8WP'
    '//iP8/qv//r8+q//Or/+67/Or//6r/Owhz2M7/qu7+KjP/qjeW4f8REfwfu///uzu7vLr//6r/OiOHnyJD/1Uz/F533e5/HQhz6U'
    'X//1X+fXf/3XuXjxIh//8R/Pe77ne/Lf6e3f/u351V/9Vd7//d+fpz3tafz6r/86v/7rv87DHvYwvuZrvobf+Z3f4eTJkzw/x44d'
    '4/d+7/d4+7d/e/78z/+cX//1Xwfg/d///fmbv/kbXuIlXoLn9vZv//b84R/+IW/3dm/H0572NH7913+dX//1Xwfg8z7v8/iO7/gO'
    'rrrqqhfuK77iK3j3d3935vM5/1bz+Zx3f/d35yu+4iu46l92733neMVXeHl+8sd/nJ2dHf61dnZ2+Mkf/3Fe4eVfjnvvO8f/UGh/'
    'f9/8O/zln/8VL/vyL8MD/eWf/xUv+/Ivw3P7yz//K77m9xqnr3tthhGmcWIcB6ZpzTisacOKcVzShiVtXNHGFdO0JMeBO++5nR/4'
    '1JfhZV/+Zfj/5HM+53P48i//cr7ru76Lt3/7t+f/ss/5nM/hy7/8y/mu7/ou3v7t357/y378x3+c93mf9+HjP/7j+azP+iyuuuqq'
    'q6666qr/daj8F1seLnnaE36ecRyZxsY4TExTYxonprHRWmOaGtmS1hrZDIDF/0u/+Zu/CcCjHvUo/q/7zd/8TQAe9ahH8X/dz//8'
    'zwPwXu/1Xlx11VVXXXXVVf8rof39ffPv8Jd//lf8V3rZl38Z/r/4oz/6I97wDd+Q48ePc/vtt/N/2R/90R/xhm/4hhw/fpzbb7+d'
    '/8tuvfVWXuIlXoLXf/3X56d+6qe46qqrrrrqqqv+V6Ly7/SyL/8yXPVv9zmf8zm84Ru+Ia/yKq/CA/3RH/0R7/iO7wjAx33cx/F/'
    'wed8zufwhm/4hrzKq7wKD/RHf/RHvOM7viMAH/dxH8f/dZ/92Z8NwCd+4idy1VVXXXXVVVf9r0Xlqv9Wf/3Xf82Xf/mX85CHPISH'
    'PexhAFy4cIG//Mu/BOD93//9+eiP/mj+L/jrv/5rvvzLv5yHPOQhPOxhDwPgwoUL/OVf/iUA7//+789Hf/RH83/RV3/1V/M7v/M7'
    'PPWpT+XpT3867//+78+rvMqrcNVVV1111VVX/a9F5ar/Vp/4iZ/Igx/8YH7jN36DX//1Xwfg+PHjvN3bvR3v8i7vwhu90Rvxf8Un'
    'fuIn8uAHP5jf+I3f4Nd//dcBOH78OG/3dm/Hu7zLu/BGb/RG/F+1s7PDr//6r3P8+HE+7/M+j4/+6I/mqquuuuqqq676Xw3t7++b'
    'q6666qqrrrrqqquuuupfg+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq666'
    '6qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L'
    '4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfCwE+WE5cddVV'
    'V1111VVXXXXVVS+arUUluOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq666'
    '6qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK4'
    '6qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqr'
    'rrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUI'
    'rrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrq'
    'qquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8t'
    'gquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rWo/Df6syfey5894R5ekFd6zHW8ymOv56qr'
    'rrrqqquuuuqq/19ampbmfzAq/43+7An38FU/8Ze8IB/3Di/La77kDVx11VVXXXXVVVdd9f/MBC3N/2AEV1111VVXXXXVVVddddW/'
    'FsFVV1111VVXXXXVVVdd9a9FcNVVV1111VVXXXXVVVf9axFcddVVV1111VVXXXXVVf9aBFddddVVV1111VVXXXXVvxbBVVddddVV'
    'V1111VVXXfWvRXDVVVddddVVV1111VVX/WsRXHXVVVddddVVV1111VX/WgRXXXXVVVddddVVV1111b8WwVVXXXXVVVddddVVV131'
    'r0Vw1VVXXXXVVVddddVVV/1rEVx11VVXXXXVVVddddVV/1oEV1111VVXXXXVVVddddW/FsFVV1111VVXXXXVVVdd9a9FcNVVV111'
    '1VVXXXXVVVf9a1G56qqrrrrqqquuuuq/3NOfcSeHR0vu13eVRz78wVz1vwaVq6666qqrrrrqqqv+U43jyJ/8+d/xV3/7BP7qbx/P'
    'xd09XpC+63ill38JXvolH83LvuRjOH58h6v+R6Jy1VVXXXXVVVddddV/ivvOXuBHfvKX+c3f/RNeVMM48nt/9Jf83h/9JQAv+1KP'
    '4d3f6S14yINu5Kr/UahcddVVV1111VVXXfUf6vBoyY/85C/zK7/+BwzjyL/HX/7N4/nLv3k8r/uar8R7vNObc/z4Dlf9j0Dlqquu'
    'uuqqq6666qr/ME96yq187pd8M4dHS/4j/ebv/gm//0d/ycd++HvySi//klz1347gqquuuuqqq6666qr/EL/5u3/CZ3z+13N4tOQ/'
    'wzCOfPFXfQc/+bO/zlX/7ahcddVVV1111VVXXfXv9sM/8Uv8yE/+Mv8Vvu9Hfo47776Pj/igd+Wq/zYEV1111VVXXXXVVVf9u/zK'
    'b/wBP/KTv8x/pd/83T/h+3/k57jqvw3BVVddddVVV1111VX/Zn//+Kfwnd/3U/x3+Imf/XV+/4/+kqv+WxBcddVVV1111VVXXfVv'
    'sru7xxd/5bczjCP/Xb7uW36Qpz/jTq76L0dw1VVXXXXVVVddddW/yU/+/G9weLTkRXX8+A7/0YZx5Ed+8pe56r8cwVVXXXXVVVdd'
    'ddVV/2p33nUvv/Lrf8C/xhu/3qvxmZ/4wWxuLPiP9Cd//rf8/eOfwlX/pQiuuuqqq6666qqrrvpX+74f+XmGceRf62Ve6jF8/Zd9'
    'Ki/zUo/hP9J3ft9PctV/KYKrrrrqqquuuuqqq/5VDo+W/Mmf/y3/VseP7/CZn/jBvMc7vQVd1/Ef4enPuJMnPeVWrvovQ3DVVVdd'
    'ddVVV1111b/Kn/z53/If4W3f8vX5ks/5GG684Vr+I/zl3zyeq/7LEFx11VVXXXXVVVdd9a/y13/7BP6jPORBN/JVX/gJvNHrvRr/'
    'Xn/wx3/FVf9lCK666qqrrrrqqquu+lf5y795PP+Ruq7jg9/3HfnMT/xgNjcW/Fvdcde93Hf2Alf9lyC46qqrrrrqqquuuupFtru7'
    'x+HRkv8ML/NSj+Hrv+xTeZmXegz/Vvedu8BV/yUIrrrqqquuuuqqq656kd137gL/mY4f3+EzP/GDeY93egu6ruNf676z57nqvwTB'
    'VVddddVVV1111VUvsou7e/xXeNu3fH2+5HM+hhtvuJZ/jd3dfa76L0Fw1VVXXXXVVVddddWLbPfSPv9VHvKgG/mqL/wEXvwxD+dF'
    'NYwjV/2XoHLVVVddddVVV1111Ytsc2PBf5XDoyXf+X0/xd8//ilc9T8Olauuuuqqq6666qqrXmTHj+/wX+FJT7mVr/2WH+TOu+7l'
    'X+OaMye56r8ElateJM/4jm/mwe9/L8/ftXz7rR/M+z2IF8lvf8Ln8DpfzgtwLd9+6wfzfg/iX/bbP41e5294vj7+nfGXPYp/nSfy'
    'CfphvpwX4uPfGX/Zo/g3++2fRq/zN/ybffw74y97FP8qv/3T6HX+hufrjV+fW3/p1XgQL8x5vuNNvp73/2VesI9/Z/xlj+Lf7Ld/'
    'Gr3O3/B8ffw74y97FM/y2z+NXudv+E/1xq/Prb/0ajyIZ/rtn0av8zf8u73x63PrL70aD+K5/PZPo9f5G/5l1/Ltt34w7/cg/g3O'
    '8x1v8vW8/y/zL/v4d8Zf9ij+KzzjO76ZB7//vbwwH/9bn8WXvTZXXXXV/xDXnD7Jf7af/Nlf5/t+5Of4tzhxbIer/ksQXPUiedDr'
    'vwRvzAtyLz/+6+d50ZznqX/PC3EvP/7r53lR/PYv/A0vyBs/+jQvmvN8x5t8DtLnIP0wX86/4Mt/GOlzkD6HN/mO8/yX+/IfRvoc'
    'pM/hE36b/xLP+I4f4/1/mRfsjV+fW7/sUVz1X+FefvzXz/Nv8own8OO/zP8w5/n1H7+Xf8mX/8ITueqqq/7nuObMSf6z7O7u8Umf'
    '9VV834/8HP9Wm5sLrvovQXDVi+ZBp3lxXrBffsI5XiTPeAI//su8UL/8hHP8y87z1L/nBbiWt3/9U/xLfvsTPgfp63n/X+bf5Jff'
    '/+uRvpnveAb/Lb78dT4Hvckf8Az+Ez3jD/jg97+XF+yl+K1fejUexFX/VX75x5/AM/jXe8av/x2/zP8wz3gCP/7L/Mu+/PH8Nldd'
    'ddX/JC/+2EfwH+33/+gv+fBP+EKe9JRb+bc6cXyHRz78wVz1X4LgqhfRo3izj+cF+/tzPIN/2TN+/e/4Zf4FX/54fpt/yTme8Mu8'
    'ANfxsAfxQjyRT9Dn8Dpfzn+Ae3n/B38Ob/Id5/lv8cu/zoPf5A94Bv8ZzvMdH/zr/DIv2Mf/1lvz2lz1X+qX/45ffwb/Suf59R+/'
    'l/9pnvHrf8cv86L4G37ht7nqqqv+B3mZl3g0/1EOj5Z83bf8IF/x9d/D4dGSf4+XecnHcNV/GYKrXmQPefS1vEC/fJan8y97+hPu'
    '5V92D099Bi/cM87x97wAH/8YXpsX5Il8gn6YL+c/1i+//9fzJt9xnv8Wv/zrfP1v8x/uGd/xY7z/L/OCffw782WvzVX/5e7lx3/9'
    'PP8qz3gCP/7L/A9znl//8Xt5UX35LzyRq6666n+OV3r5l+A/wpOeciuf9FlfxW/+7p/wH+HVX/lluOq/DMFVL7IHvf5L8Ma8IH/D'
    'L/w2/4In8gtfzovgXn7818/zwjzj1/+OX+b5e+NHn+b5O893vMkP8+X85/jl9/96PuG3+bd749fnVn8W9mdhfxb2Z2F/Fv6tl+Jf'
    '8uVf8gc8g/9Az/gDPvj97+UFeyl+68sexVX/PX75x5/AM3jRPePX/45f5n+Y3/493v+XedF9+eP5ba666qr/KW684Voe8qAb+ff4'
    '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'
    'bG4suOq/FcFV/3oPejRv/8a8APfw1GfwnJ5+ll/meb3xo08DAKd59BvzfH35LzyR5/Dbj+fLeX6u5dEP4Xk9/Sy/zAtyLY9+CP92'
    'DzrNi/Of7Tzf8Safw4Pf/15ekI//rQ/m/R7Ev9FZvv5L/oYX5I2//R14vwdx1X+7R/FmH8/z9cvv/3v8Ns/rGb/+d/wyz+1a3v71'
    'T/Hf5Rm//nf8Ms/Ptbz965/iOTzo0bz9G/P8/fLf8evP4Kqrrvof6i3e5LX5vE//CB7yoBv5j3TNmZN88se8H+/77m/DVf8jULnq'
    '3+AUD3tx4Jd5Pu7lCU8HHsSz/PYv/A3P61re/vVPAQCneP23vxZ++V6ex9+f4xk8igdxxTOeeg/P1xu/BK//IP53++Vf58H6df41'
    '3vjbP5wve23+7X75b/hyXpCX4pPe7xRX/c/w2m/2UvDlf8Pz+ht+4bffmtd+bR7gPL/+4/fyPN74JXj9B/Hf5Dy//uP38ny98Uvw'
    '+g/iuZzi9d/+Wvjle3le9/Ljv36e93u/U1x11VX/M734Yx7OV37hJ/L7f/SXfN+P/Bz3nb3Av9XmxoJ3ets35i3e5LW56n8Ugqv+'
    'TV77zV6KF+TLf+GJPNsT+YUv5/m4joc9iGd50MOu4/n65bM8nWd7+hPu5fl547d/NA/i/5Nr+fZbP4tfer9T/Lu88bW8MS/I3/A6'
    'b/IHPIOr/kd47cfw8Tx/X/4LT+Q5POMJ/Pgv8zze+O0fzYP4b/KMJ/Djv8zz9cZv/2gexPN60Ou/BG/M8/fLP/4EnsFVV131P92r'
    'v8rL8i1f/Vl88se8H2/8eq/GieM7vCg2Nxa8xqu8LB/34e/Fd33j5/EWb/LaXPU/DpWr/m0ecoY3Bn6Z5+Pvz/EMHsWDAJ5xjr/n'
    '+fj4x/DaPMBrP4aP52/4cp7b3/ALv/3WvPZrA5znqX/P8/XiDzvF/w/X8u23fjDv9yD+g7wEb//x9/LLX87z98u/zgd/x6P5pfc7'
    'xVX/3R7Fm308fPmX87y+/PH89pc9itfmimf8+t/xyzy3a3n71z/Ff5dn/Prf8cs8P9fy9q9/iufrQY/m7d/41/nlX+Z5/fLf8evP'
    'eDXe70FcddVV/wu80su/JK/08i/JB73vO/Kkp9zK4eGSJz7lVp7bQx50I5ubG7z4Yx7OVf/jEVz1b/OgR/P2b8zz98tneTpXPOPX'
    '/45f5nl9/Js9iud0mke/Mc/X3z/1PABwjif8Ms/HS/Fmr82/wb084en82z3jHH/PC/biDzvFf7x7+fFfP89/pNf/snfm43nBfvn9'
    'f4zveAZX/Q/w2m/2Ujx/f8Mv/DbP9ES+/v3v5Xm88Uvw+g/iv8kT+fr3v5fn7zoe9iBegFO8/ttfy/N3L+//9U/kqquu+t/nkQ9/'
    'MC/zUo/hnd/uTXjnt3sT3vnt3oR3frs34Z3f7k14pZd/SV78MQ/nqv8VqFz1b3SK13/7a+GX7+V53cNTnwGv/aDz/PqP38vzupZH'
    'P4TncorXf/tr4Zfv5bn98o8/gWe836vxoN9+PF/O8/HGZ3gIL8BDzvDGwC/z/P39U8/Da5/i3+TpZ/llXpBrefRD+E/xy+//9egJ'
    '74y/7FH8x3gUX/ZbL8WXv87f8Pzdy/s/+Kd5mN+a1+aq5/HGr8+tv/RqPIj/Aq/9GD6ev+HLeV5f/gtP5Mte+1HwjHP8Pc/rjd/+'
    '0TyI/ya//Xi+nBfkb3gd/Q3/Jl/+eH77yx7Fa3PVVVddddV/A4Kr/s0e9LDreP7u5cd//Txwjif8Ms/rjV+C138Qz+NBD7uO5+uX'
    'z/J04BlPvYfn543f/tE8iBfgQad5cV6wX/7xJ/AM/m2e8dR7eMGu42EP4l/njV+fW/1Z2J+F/Vn81sfzgn35D/Mm33Ge/zCv/db8'
    '1sfzQvwNr/MJT+Sq/26P4s0+nufv78/xDOAZv/53/DLP7Vre/vVP8d/lt3/hb/jP8Tf8wm9z1VVXXXXVfw+Cq/7tXvsxfDzP3y8/'
    '4Rz89uP5cp6PFz/Ng3g+XvsxfDzPz9/wC78NT3/CvTw/L/6wU7xgj+LNPp4X7Jf/jl9/Bv8GT+Tr3/9eXqA3PsND+Pd57S/7LH7r'
    '43mBfvn9v55P+G3+w7z2l70zH88L8eU/zCf8Nlf9N3vtD3993pjn45f/jl9/xnl+/cfv5Xm88Uvw+g/iv8kT+YUv5z/Nl//CE7nq'
    'qquuuuq/BcFV/w6nefQb8/z9/Tl++6n38Px8/Js9iufvNI9+Y56vv3/qE3nq3/N8vBRv9tq8UK/9Zi/FC3Yv7//1T+Rf6xnf8Vt8'
    'OS/YG7/9o3kQ/36v/WUfzre/MS/Ql7/OT/Pb/Ed5FF/2Wy/FC/Plr/PT/DZX/bd60KN5+zfm+biXJzz9HE/4ZZ7HG7/9o3kQ/01+'
    '+/F8Of+Jvvzx/DZXXXXVVVf9NyC46t/hFK//9tfygjz1CffyvK7l0Q/hBTjF67/9tTw/v/yEx/OEX+Z5ffxjeG3+Ba/9GD6eF+LL'
    'f5g3+Y7zvMie8Qd88Pvfywv2UnzS+53iP8Yp3u+TXooX7G94nU94Iv9hXvut+a2P54X4G17nTf6AZ3DVf59TvP7bX8vz8/df8lt8'
    'Oc/tWt7+9U/x3+W3f+Fv+M/1N/zCb3PVVVddddV/PYKr/l0e9LDreL5++e/48b/neb3xS/D6D+IFetDDruP5+vK/4ct5Xm/86NP8'
    'yx7Fh3/7tbwwv/z+X8+bfMd5/kXP+APe5MG/zi/zgr3xt78Gr81/oNd+a37r43nBvvy3+I5n8B/mtT/89XljXohf/nU++DvOc9V/'
    'nwe9/kvwxjyvX/7le3keb/wSvP6D+G/yRH7hy/lP9+W/8ESuuuqqq676L0dw1b/Paz+Gj+f5uZdf/mWexxu//aN5EC/Eaz+Gj+dF'
    'dS1v//qneFE86P3egW9/Y16oX37/r0f6HD7ht3k+nsgn6HPQg3+dX+aFeOPX55vf7xT/0V77y96Zj+cFuZf3/+A/4Bn8B3nQq/HN'
    '334tL8wvv/+P8R3P4Kr/Lg96NG//xrxI3vjtH82D+O/xjO/4Lb6cF+Sl+C1/FvZnYX8W9mdhfxb2Z2F/FvZnYX8W9mdhfxa3fvu1'
    'vEBf/nh+m6uuuuqqq/6LEVz173SaR78xL7IXf9gpXrhH8WYfz4voOh72IF5Ep3i/b3593ph/2Ze/zucgfQ7S5yB9DtLnIP0wX86/'
    '5Fq+/ZtfjQfxn+FRfPi3X8sL9Mu/zgd/x3n+ozzo/d6Bb39jXoh7ef8P/gOewVX88q/zYH0O0ucgfQ7S5yB9DtLnIH0O0ucgfQ7S'
    '5yB9Dm/yHef59zvF67/9tfzLruXtX/8U/z3O8+s/fi8v0Mc/htfmRfeg138J3pgX5G/4hd/mqquuuuqq/1oEV/07neL13/5aXjQv'
    'xZu9Nv+ihzz6Wl4kH/8YXpt/hQe9Gr/0Wy/Ff5aP/60P5v0exH+aB73fO/Dtb8wL9Mvv/2N8xzP4D3KK9/vm1+eNeSF++dd58Cc8'
    'kav+ezzo9V+CN+Zf8PGvw/s9iP8ez3gCP/7LvEAf/2aP4l/lQY/m7d+YF+jLf+GJXHXVVVdd9V+K4Kp/twe9/kvwxrwI3vgMD+Ff'
    '9qDXfwnemH/ZGz/6NP9qr/3W+NbX5435j3Qt337rZ/Flr81/slO83ze/Pm/MC3Iv7//Bf8Az+A/yoFfjm7/9Wl6oL/9hPuG3ueq/'
    'w4Mezdu/MS/Ux7/Zo/jv8oxf/zt+mRfkpXiz1+Zf6RSv//bX8gJ9+W/xHc/gqquuuuqq/zoEV/37Peg0L86/7I3f/tE8iBfBg07z'
    '4vxLruXtX/8U/yYPejV+yR/Ot78x/35v/Prc6g/m/R7Ef40HvRqf9PG8YL/863zwd5znP8qD3u8d+PY35oX68tf5aX6bq/7rneL1'
    '3/5aXrCX4s1em/8m5/n1H7+XF+jjH8Nr86/3oNd/Cd6YF+RefvzXz3PVVVddddV/GYKr/gM8ijf7eP5FL/6wU7xoHsWbfTz/gut4'
    '2IP4dzjF+/3SZ+FbX5835t/iWr791s/Cv/RqPIj/Wq/9Ze/Mx/OC/fL7/x6/zX+UU7zfN78+b8wL8ze8zic8kav+6z3o9V+CN+YF'
    '+PjH8Nr8N3nGE/jxX+YF+vg3exT/Jg96NG//xrxAv/zjT+AZXHXVVVdd9V+E4Kr/EA959LW8cC/Fm702L7KHPPpaXqiPfwyvzX+A'
    'B70av+TPwv4s/Fsvxb/k43/rs7A/C/uDeb8H8d/kUXz4t1/LC/Y3vM6b/AHP4D/Ig16Nb/72a3mhvvyH0Sc8kav+iz3o0bz9G/N8'
    'ffybPYr/Lr/99b/OL/OCXMujH8K/0Sle/+2v5QX65b/j15/BVVddddVV/zUQ4IPlxH+Hb/yZv+GrfuIveUE+7h1elo97h5flqquu'
    'uuqqq6666qr/X8bJDFPyP9XWohJcddVVV1111VVXXXXVVf9aBFddddVVV1111VVXXXXVvxaV/0av8Ojr+Ji3e1lekFd41HWMk7nq'
    'qquuuuqqq6666v+XluZ/OAT4YDlx1VVXXXXVVVddddVVV71othaV4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK66'
    '6qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqr'
    'rrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKr'
    'rrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrq'
    'qquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vg'
    'qquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LSr/jZ7wxHt5/BPu4QV5scdcx4s/9nquuuqq'
    'q6666qqrrvr/paVpaf4Ho/Lf6PFPuIcf/om/5AV513d4WV7mJW/gqquuuuqqq6666qr/ZyZoaf4HI7jqqquuuuqqq6666qqr/rUI'
    'rrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrq'
    'qquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8t'
    'gquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquu'
    'uuqqq6666l+LylVXXXXVVVddddVV/+We/ow7OTxacr++qzzy4Q/mqv81qFx11VVXXXXVVVdd9Z9qHEf+5M//jr/62yfwV3/7eC7u'
    '7vGC9F3HK738S/DSL/loXvYlH8Px4ztc9T8Slauuuuqqq6666qqr/lPcd/YCP/KTv8xv/u6f8KIaxpHf+6O/5Pf+6C8BeNmXegzv'
    '/k5vwUMedCNX/Y9C5aqrrrrqqquuuuqq/1CHR0t+5Cd/mV/59T9gGEf+Pf7ybx7PX/7N43nd13wl3uOd3pzjx3e46n8EKlddddVV'
    'V1111VVX/Yd50lNu5XO/5Js5PFryH+k3f/dP+P0/+ks+9sPfk1d6+Zfkqv92BFddddVVV1111VVX/Yf4zd/9Ez7j87+ew6Ml/xmG'
    'ceSLv+o7+Mmf/XWu+m9H5aqrrrrqqquuuuqqf7cf/olf4kd+8pf5r/B9P/Jz3Hn3fXzEB70rV/23Ibjqqquuuuqqq6666t/lV37j'
    'D/iRn/xl/iv95u/+Cd//Iz/HVf9tCK666qqrrrrqqquu+jf7+8c/he/8vp/iv8NP/Oyv8/t/9Jdc9d+C4Kqrrrrqqquuuuqqf5Pd'
    '3T2++Cu/nWEc+e/ydd/ygzz9GXdy1X85gquuuuqqq6666qqr/k1+8ud/g8OjJS+q48d3+I82jCM/8pO/zFX/5Qiuuuqqq6666qqr'
    'rvpXu/Oue/mVX/8D/jXe+PVejc/8xA9mc2PBf6Q/+fO/5e8f/xSu+i9FcNVVV1111VVXXXXVv9r3/cjPM4wj/1ov81KP4eu/7FN5'
    'mZd6DP+RvvP7fpKr/ksRXHXVVVddddVVV131r3J4tORP/vxv+bc6fnyHz/zED+Y93ukt6LqO/whPf8adPOkpt3LVfxmCq6666qqr'
    'rrrqqqv+Vf7kz/+W/whv+5avz5d8zsdw4w3X8h/hL//m8Vz1X4bgqquuuuqqq6666qp/lb/+2yfwH+UhD7qRr/rCT+CNXu/V+Pf6'
    'gz/+K676L0Nw1VVXXXXVVVddddW/yl/+zeP5j9R1HR/8vu/IZ37iB7O5seDf6o677uW+sxe46r8EwVVXXXXVVVddddVVL7Ld3T0O'
    'j5b8Z3iZl3oMX/9ln8rLvNRj+Le679wFrvovQXDVVVddddVVV1111YvsvnMX+M90/PgOn/mJH8x7vNNb0HUd/1r3nT3PVf8lCK66'
    '6qqrrrrqqquuepFd3N3jv8LbvuXr8yWf8zHceMO1/Gvs7u5z1X8Jgquuuuqqq6666qqrXmS7l/b5r/KQB93IV33hJ/Dij3k4L6ph'
    'HLnqvwSVq6666qqrrrrqqqteZJsbC/6rHB4t+c7v+yn+/vFP4ar/cahcddVVV1111VVXXfUiO358h/8KT3rKrXztt/wgd951L/8a'
    '15w5yVX/Jahc9SJ5xnd8Mw9+/3t5/q7l22/9YN7vQbxIfvsTPofX+XJegGv59ls/mPd7EP+y3/5p9Dp/w/P18e+Mv+xR/Os8kU/Q'
    'D/PlvBAf/874yx7Fv9lv/zR6nb/h3+zj3xl/2aP4V/ntn0av8zc8X2/8+tz6S6/Gg3hhzvMdb/L1vP8v84J9/DvjL3sU/2a//dPo'
    'df6G5+vj3xl/2aN4lt/+afQ6f8N/qjd+fW79pVfjQTzTb/80ep2/4d/tjV+fW3/p1XgQz+W3fxq9zt/wL7uWb7/1g3m/B/FvcJ7v'
    'eJOv5/1/mX/Zx78z/rJH8R/it38avc7f8G/2xq/Prb/0ajyIq6666n+Ka06f5D/bT/7sr/N9P/Jz/FucOLbDVf8lCK56kTzo9V+C'
    'N+YFuZcf//XzvGjO89S/54W4lx//9fO8KH77F/6GF+SNH32aF815vuNNPgfpc5B+mC/nX/DlP4z0OUifw5t8x3n+y335DyN9DtLn'
    '8Am/zX+JZ3zHj/H+v8wL9savz61f9iiu+q9wLz/+6+f5N3nGE/jxX+Z/n1/+dR6sz0H6HN7kO85z1VVX/fe75sxJ/rPs7u7xSZ/1'
    'VXzfj/wc/1abmwuu+i9BcNWL5kGneXFesF9+wjleJM94Aj/+y7xQv/yEc/zLzvPUv+cFuJa3f/1T/Et++xM+B+nref9f5t/kl9//'
    '65G+me94Bv8tvvx1Pge9yR/wDP4TPeMP+OD3v5cX7KX4rV96NR7EVf9VfvnHn8Az+Nd7xq//Hb/M/26//P5fj/TT/DZXXXXVf7cX'
    'f+wj+I/2+3/0l3z4J3whT3rKrfxbnTi+wyMf/mCu+i9BcNWL6FG82cfzgv39OZ7Bv+wZv/53/DL/gi9/PL/Nv+QcT/hlXoDreNiD'
    'eCGeyCfoc3idL+c/wL28/4M/hzf5jvP8t/jlX+fBb/IHPIP/DOf5jg/+dX6ZF+zjf+uteW2u+i/1y3/Hrz+Df6Xz/PqP38v/DX/D'
    '67zJH/AMrrrqqv9OL/MSj+Y/yuHRkq/7lh/kK77+ezg8WvLv8TIv+Riu+i9DcNWL7CGPvpYX6JfP8nT+ZU9/wr38y+7hqc/ghXvG'
    'Of6eF+DjH8Nr84I8kU/QD/Pl/Mf65ff/et7kO87z3+KXf52v/23+wz3jO36M9/9lXrCPf2e+7LW56r/cvfz4r5/nX+UZT+DHf5n/'
    'O3751/ng7zjPVVdd9d/nlV7+JfiP8KSn3MonfdZX8Zu/+yf8R3j1V34ZrvovQ3DVi+xBr/8SvDEvyN/wC7/Nv+CJ/MKX8yK4lx//'
    '9fO8MM/49b/jl3n+3vjRp3n+zvMdb/LDfDn/OX75/b+eT/ht/u3e+PW51Z+F/VnYn4X9WdifhX/rpfiXfPmX/AHP4D/QM/6AD37/'
    'e3nBXorf+rJHcdV/j1/+8SfwDF50z/j1v+OX+R/ojV+fW/1Z2J+F/VnYn4X9Wdz67dfyL/nlH38Cz+Cqq67673LjDdfykAfdyL/H'
    'T/7sr/NJn/VV3HnXvfxHOHF8hxd/7MO56r8MlatedA86zYsDv8zz9/dPPQ+vfYoX6Lcfz5fzovnlJ5wDTvGCPP0J9/L8Xcvbv/4p'
    'np/f/oSv5/1/mX/Rx//WZ/Flr81zOc93vMnX8/6/zAv15a/zzTz61g/m/R7Ef5zXfmt86xne5MG/zi/zAvzy3/Hrz3g13u9B/Ac4'
    'z3d88K/zy7wg1/Ltt741r81/k9d+a+y35oX67Z9Gr/M3PF9v/Prc+kuvxoP4D/LGr8+tv/RqPIj/BB//Unz8l/8NX85z+eW/49ef'
    '8Wq834N4EZzn13/8Xp7bG7/xtfzyL9/L/0QPer8Pxq//B7zJg3+dX+YF+OWzPB14EFddddV/l3d62zfmi7/qO/jX2t3d44u+6jt4'
    '0lNu5T/S27z569F1HVf9lyG46l/hUbzZx/MC/fITzvHCPOOp9/Ai+/tzPIMX5DxP/XtegOt42IN4Xs/4A77ky3mh3vjbPxz7s/iy'
    '1+b5OMX7/dJn4VtfnzfmhbmX9//6J/If7kGvxjd/+7X8V3jGd/wY7//LvEBv/O3vwPs9iKv+K/w98MY8H/fy479+nhfJM57Aj/8y'
    'z+PFX/w6/kd70KvxSR/PVVdd9T/YK738S/KyL/UY/jX+8m8ez4d/whfypKfcyn+khzzoRt7iTV6bq/5LEVz1r/KQR1/LC/T353gG'
    'L8h5fv3H7+V5vPG1vDHPxy//Hb/+DF6Aczzhl3n+Pv4xvDbP7Tzf8cG/zi/zgr3xt384v/R+p/gXPejV+KVbX5835oX48t/iO57B'
    'f7gHPew6XrB7ecLT+fd7xh/wwe9/Ly/QG78+3/x+p7jqv8oZHv3iPF+//ONP4Bn8y57x63/HL/PcXoo3ezP+x3vIo6/lqquu+p/t'
    '3d/pLfjXeNJTbuXwaMl/tHd62zfmqv9yBFf9qzzo9V+CN+YF+OW/49efwQtwjif8Ms/rxa/jxXl+7uUJT+f5e8Y5/p7n7+Pf7FE8'
    'j2c8gR//ZV6wN359vvn9TvEie9Cr8c3ffi0v2L38+K+f5z/cQ87wxvxnOs93fPCv88u8IC/Fb/3Sq/Egrvqv9Ppv9lI8X7/8d/z6'
    'M/gXnOfXf/xenscbn4Gn3sP/am98hodw1VVX/Xd7yINu5H3f/W347/QWb/LavNLLvyRX/ZcjuOpf50GP5u3fmBfgXp7wdJ6/3348'
    'X87zeuNHP4ZHvzHP15f/whN5fp7x63/HL/P8XMujH8LzeMav/x2/zAtyLd/+za/Gg/jXedD7vQ4fzwv2yz/+BJ7Bf6VrefRD+Pf5'
    '7d/j/X+ZF+jjf+uteW2u+i/1y2d5+ms/ho/n+bmXH//187xQz3gCP/7LPI83fvtH8xD+pzvPr//4vbwgH/9Jr8aDuOqqq/4neIs3'
    'eW3e+PVejf8OL/tSj+F93/1tuOq/BcFV/0qneNiL8wL9/VPP8/z89i/8Dc/rWt7+9R/F67/9tTxff3+OZ/C8nv6Ee3m+3vgleP0H'
    '8VzO8+s/fi8v0Bu/BK//IP4NHsWHf/u1vEC//Hf8+jP4D/WMX/87fpkX5Doe9iD+HZ7IJ7zO3/ACffw782WvzVX/LR7Fm308z9cv'
    '//gTeAYv2DN+/e/4ZZ7btbz965/if7zf/j3e/5d5/j7+nfmy1+aqq676H+R93+NteNmXegz/lR7yoBv52A9/L676b0Nw1b/aa7/Z'
    'S/GC/PKPP4Fn8NzO89S/5/m4joc9CB70sOt4vn757/j1Z/BczvPUv+f5e/HTPIjndo4n/DIv0Bu//aN5EP82D3rYdbxg9/KEp/Mf'
    '6Dy//uP38gJ9/GN4bf7tfvsTfpgv5wV449fn1i97FFf993ntN3spnq9f/jt+/Rm8AOf59R+/l+fxxi/B6z+I/9F++xM+B73O3/B8'
    'vfHrc+uXPYqrrrrqf5au6/jkj3k/Xvc1X4n/Ci/7Uo/h8z79I9jcWHDVfxsqV/3rPeQMbwz8Ms/HL5/l6cCDeIBnPIEf/2We18c/'
    'htcGeO3H8PH8DV/Oc7uXJzwdeBAPcI4n/DLP18e/2aN4Hs84x9/zgr34w07xb/aQM7wx8Ms8f3//1PPw2qf4j/CM7/gx3v+XeQGu'
    '5ds//FH8m/32T/M6X84LcC3f/s2vxoO46gX65V/nwfp1/mUvxW/5rXlt/g1e+zF8PH/Dl/Pc7uXHf/087/d+p3gez3gCP/7LPI83'
    'fvtH8yDgGfwP8Mu/zoP167zIPv6d8Zc9iquuuup/pq7r+IgPelcefMsNfOf3/xT/Wd7iTV6b9333t+Gq/3YEV/3rPejRvP0b8wLc'
    'w1OfwXN6+ll+mef1xo8+DQCc5tFvzPP15b/wRJ7Dbz+eL+f5uZZHP4Tn9fSz/DIvyLU8+iH82z3oNC/Of7bzfMebfA4Pfv97eUE+'
    '/rc+mPd7EP9GZ/n6L/kbXpA3/vZ34P0exFX/7R7Fm308z9cvv//v8ds8r2f8+t/xyzy3a3n71z/F/zZv/O0fjv1Z+MsexVVXXfU/'
    '31u8yWvzeZ/+ETzkQTfyH+maMyf55I95P9733d+Gq/5HoHLVv8EpHvbiwC/zfNzLE54OPIhn+e1f+Bue17W8/eufAgBO8fpvfy38'
    '8r08j78/xzN4FA/iimc89R6erzd+CV7/Qfzv9su/zoP16/xrvPG3fzhf9tr82/3y3/DlvCAvxSe93ymu+p/htd/speDL/4bn9Tf8'
    'wm+/Na/92jzAeX79x+/lebzxS/D6D+J/nV9+/69H7w98/DvjL3sUV1111f98L/6Yh/OVX/iJ/P4f/SXf9yM/x31nL/Bvtbmx4J3e'
    '9o15izd5ba76H4Xgqn+T136zl+IF+fJfeCLP9kR+4ct5Pq7jYQ/iWR70sOt4vn75LE/n2Z7+hHt5ft747R/Ng/j/5Fq+/dbP4pfe'
    '7xT/Lm98LW/MC/I3vM6b/AHP4Kr/EV77MXw8z9+X/8ITeQ7PeAI//ss8jzd++0fzIP4X+/IfRvocPuG3ueqqq/6XePVXeVm+5as/'
    'i0/+mPfjjV/v1ThxfIcXxebGgtd4lZfl4z78vfiub/w83uJNXpur/sehctW/zUPO8MbAL/N8/P05nsGjeBDAM87x9zwfH/8YXpsH'
    'eO3H8PH8DV/Oc/sbfuG335rXfm2A8zz173m+Xvxhp/j/4Vq+/dYP5v0exH+Ql+DtP/5efvnLef5++df54O94NL/0fqe46r/bo3iz'
    'j4cv/3Ke15c/nt/+skfx2lzxjF//O36Z53Ytb//6p/i/4Mtf53P4+2//cH7p/U5x1VVX/e/wSi//krzSy78kH/S+78iTnnIrh4dL'
    'nviUW3luD3nQjWxubvDij3k4V/2PR3DVv82DHs3bvzHP3y+f5elc8Yxf/zt+mef18W/2KJ7TaR79xjxff//U8wDAOZ7wyzwfL8Wb'
    'vTb/BvfyhKfzb/eMc/w9L9iLP+wU//Hu5cd//Tz/kV7/y96Zj+cF++X3/zG+4xlc9T/Aa7/ZS/H8/Q2/8Ns80xP5+ve/l+fxxi/B'
    '6z+I/1ne+PW51Z+F/VnYn4X9Wdifhf1Z/NbH80L98vv/GN/xDK666qr/hR758AfzMi/1GN757d6Ed367N+Gd3+5NeOe3exPe+e3e'
    'hFd6+ZfkxR/zcK76X4Hgqn+jU7z+21/L83cPT30GwHl+/cfv5Xldy6MfwnM5xeu//bU8P7/840/gGQC//Xi+nOfjjc/wEF6Ah5zh'
    'jXnB/v6p5/k3e/pZfpkX5Foe/RD+U/zy+389+oQn8h/nUXzZb70UL9i9vP+Df5rf5qrn641fn1v9Wdifhf1Z2J+F/VnYn4X9Wdif'
    'hf1Z2G/Na/Pv9NqP4eN5/r78F54IAM84x9/zvN747R/Ng/jf47W/7LPwra/PG/OC3Mv7f/0Tueqqq6666r8NwVX/Zg962HU8f/fy'
    '479+HjjHE36Z5/XGL8HrP4jn8aCHXcfz9ctneTrwjKfew/Pzxm//aB7EC/Cg07w4L9gv//gTeAb/Ns946j28YNfxsAfxr/PGr8+t'
    '/izsz8L+LH7r43nBvvyHeZPvOM9/mNd+a37r43kh/obX+YQnctV/t0fxZh/P8/f353gG8Ixf/zt+med2LW//+qf4X+dBr8Y3f/u1'
    'vEBf/nh+m6uuuuqqq/6bEFz1b/faj+Hjef5++Qnn4Lcfz5fzfLz4aR7E8/Haj+HjeX7+hl/4bXj6E+7l+Xnxh53iBXsUb/bxvGC/'
    '/Hf8+jP4N3giX//+9/ICvfEZHsK/z2t/2WfxWx/PC/TL7//1fMJv8x/mtb/snfl4Xogv/2E+4be56r/Za3/46/PGPB+//Hf8+jPO'
    '8+s/fi/P441fgtd/EP8rPehh1/GC/Q2/8NtcddVVV13134Pgqn+H0zz6jXn+/v4cv/3Ue3h+Pv7NHsXzd5pHvzHP198/9Yk89e95'
    'Pl6KN3ttXqjXfrOX4gW7l/f/+ifyr/WM7/gtvpwX7I3f/tE8iH+/1/6yD+fb35gX6Mtf56f5bf6jPIov+62X4oX58tf5aX6bq/5b'
    'PejRvP0b83zcyxOefo4n/DLP443f/tE8iP+lHnKGN+aqq6666qr/gQiu+nc4xeu//bW8IE99wr08r2t59EN4AU7x+m9/Lc/PLz/h'
    '8Tzhl3leH/8YXpt/wWs/ho/nhfjyH+ZNvuM8L7Jn/AEf/P738oK9FJ/0fqf4j3GK9/ukl+IF+xte5xOeyH+Y135rfuvjeSH+htd5'
    'kz/gGVz13+cUr//21/L8/P2X/BZfznO7lrd//VP8r/X0s/wyV1111VVX/Q9EcNW/y4Medh3P1y//HT/+9zyvN34JXv9BvEAPeth1'
    'PF9f/jd8Oc/rjR99mn/Zo/jwb7+WF+aX3//reZPvOM+/6Bl/wJs8+Nf5ZV6wN/721+C1+Q/02m/Nb308L9iX/xbf8Qz+w7z2h78+'
    'b8wL8cu/zgd/x3mu+u/zoNd/Cd6Y5/XLv3wvz+ONX4LXfxD/a/32L/wNL9i1PPohXHXVVVdd9d+D4Kp/n9d+DB/P83Mvv/zLPI83'
    'fvtH8yBeiNd+DB/Pi+pa3v71T/GieND7vQPf/sa8UL/8/l+P9Dl8wm/zfDyRT9DnoAf/Or/MC/HGr883v98p/qO99pe9Mx/PC3Iv'
    '7//Bf8Az+A/yoFfjm7/9Wl6YX37/H+M7nsFV/10e9Gje/o15kbzx2z+aB/G/1G//NK/z5bxgb/wSvP6DuOqqq6666r8Hlav+nU7z'
    '6DcGfpkXyYs/7BQv3KN4s4+HL/9yXgTX8bAH8SI6xft98+vz4w/+dX6ZF+7LX+dz+HL+La7l27/51XgQ/xkexYd/+7V8+fvfy/P1'
    'y7/OB3/Ho/ml9zvFf4QHvd878O0//vW8/y/zAtzL+3/wH/D6v/RqPIj/537513mwfp0X1Rt/+4fzS+93in+fU7z+218Lv3wvL9y1'
    'vP3rn+J/o9/+hM/hdb6cF+qN3/7RPIirrrrqqqv+mxBc9e90itd/+2t50bwUb/ba/Ise8uhreZF8/GN4bf4VHvRq/NJvvRT/WT7+'
    'tz6Y93sQ/2ke9H7vwLe/MS/QL7//j/Edz+A/yCne75tfnzfmhfjlX+fBn/BErvrv8aDXfwnemH/Bx78O7/cg/uf65V/nwfocpM9B'
    '+hykz0H6HKTP4XW+nH/BS/FJ73eKq6666qqr/tsQXPXv9qDXfwnemBfBG5/hIfzLHvT6L8Eb8y9740ef5l/ttd8a3/r6vDH/ka7l'
    '22/9LL7stflPdor3++bX5415Qe7l/T/4D3gG/0Ee9Gp887dfywv15T/MJ/w2V/13eNCjefs35oX6+Dd7FP83Xcu33/rWvDZXXXXV'
    'VVf9NyK46t/vQad5cf5lb/z2j+ZBvAgedJoX519yLW//+qf4N3nQq/FL/nC+/Y3593vj1+dWfzDv9yD+azzo1fikj+cF++Vf54O/'
    '4zz/UR70fu/At78xL9SXv85P89tc9V/vFK//9tfygr0Ub/ba/B90Ld9+6wfzfg/iqquuuuqq/14EV/0HeBRv9vH8i178Yad40TyK'
    'N/t4/gXX8bAH8e9wivf7pc/Ct74+b8y/xbV8+62fhX/p1XgQ/7Ve+8vemY/nBfvl9/89fpv/KKd4v29+fd6YF+ZveJ1PeCJX/dd7'
    '0Ou/BG/MC/Dxj+G1+T/m498Z+4N5vwdx1VVXXXXVfz+Cq/5DPOTR1/LCvRRv9tq8yB7y6Gt5oT7+Mbw2/wEe9Gr8kj8L+7Pwb70U'
    '/5KP/63Pwv4s7A/m/R7Ef5NH8eHffi0v2N/wOm/yBzyD/yAPejW++duv5YX68h9Gn/BErvov9qBH8/ZvzPP18W/2KP5veCl+y5+F'
    '/Vn4yx7FVVddddVV/2MgwAfLif8OP/Uzf8MP/8Rf8oK86zu8LO/6Di/LVVddddVVV1111VX/v4yTGabkf6qtRSW46qqrrrrqqquu'
    'uuqqq/61CK666qqrrrrqqquuuuqqfy0q/40e8+jreOe3e1lekMc86jrGyVx11VVXXXXVVVdd9f9LS/M/HAJ8sJy46qqrrrrqqquu'
    'uuqqq140W4tKcNVVV1111VVXXXXVVVf9axFcddVVV1111VVXXXXVVf9aBFddddVVV1111VVXXXXVvxbBVVddddVVV1111VVXXfWv'
    'RXDVVVddddVVV1111VVX/WsRXHXVVVddddVVV1111VX/WgRXXXXVVVddddVVV1111b8WwVVXXXXVVVddddVVV131r0Vw1VVXXXXV'
    'VVddddVVV/1rEVx11VVXXXXVVVddddVV/1oEV1111VVXXXXVVVddddW/FsFVV1111VVXXXXVVVdd9a9FcNVVV1111VVXXXXVVVf9'
    'axFcddVVV1111VVXXXXVVf9aBFddddVVV1111VVXXXXVvxbBVVddddVVV1111VVXXfWvRXDVVVddddVVV1111VVX/WsRXHXVVVdd'
    'ddVVV1111VX/WgRXXXXVVVddddVVV1111b8WwVVXXXXVVVddddVVV131r0Vw1VVXXXXVVVddddVVV/1rEVx11VVXXXXVVVddddVV'
    '/1oEV1111VVXXXXVVVddddW/FpX/Rnc88V7ueMI9vCC3POY6HvzY67nqqquuuuqqq6666v+Xlqal+R+Myn+jO55wD7/zE3/JC/I6'
    '7/CyPOIlb+Cqq6666qqrrrrqqv9nJmhp/gcjuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrq'
    'X4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrq'
    'qquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu'
    '+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vKVVddddVVV1111VX/5Z7+jDs5PFpy'
    'v76rPPLhD+aq/zWoXHXVVVddddVVV131n2ocR/7kz/+Ov/rbJ/BXf/t4Lu7u8YL0XccrvfxL8NIv+Whe9iUfw/HjO1z1PxKVq666'
    '6qqrrrrqqqv+U9x39gI/8pO/zG/+7p/wohrGkd/7o7/k9/7oLwF42Zd6DO/+Tm/BQx50I1f9j0Llqquuuuqqq6666qr/UIdHS37k'
    'J3+ZX/n1P2AYR/49/vJvHs9f/s3jed3XfCXe453enOPHd7jqfwQqV1111VVXXXXVVVf9h3nSU27lc7/kmzk8WvIf6Td/90/4/T/6'
    'Sz72w9+TV3r5l+Sq/3YEV1111VVXXXXVVVf9h/jN3/0TPuPzv57DoyX/GYZx5Iu/6jv4yZ/9da76b0flqquuuuqqq6666qp/tx/+'
    'iV/iR37yl/mv8H0/8nPcefd9fMQHvStX/bchuOqqq6666qqrrrrq3+VXfuMP+JGf/GX+K/3m7/4J3/8jP8dV/20Irrrqqquuuuqq'
    'q676N/v7xz+F7/y+n+K/w0/87K/z+3/0l1z134Lgqquuuuqqq6666qp/k93dPb74K7+dYRz57/J13/KDPP0Zd3LVfzmCq6666qqr'
    'rrrqqqv+TX7y53+Dw6MlL6rjx3f4jzaMIz/yk7/MVf/lCK666qqrrrrqqquu+le78657+ZVf/wP+Nd749V6Nz/zED2ZzY8F/pD/5'
    '87/l7x//FK76L0Vw1VVXXXXVVVddddW/2vf9yM8zjCP/Wi/zUo/h67/sU3mZl3oM/5G+8/t+kqv+SxFcddVVV1111VVXXfWvcni0'
    '5E/+/G/5tzp+fIfP/MQP5j3e6S3ouo7/CE9/xp086Sm3ctV/GYKrrrrqqquuuuqqq/5V/uTP/5b/CG/7lq/Pl3zOx3DjDdfyH+Ev'
    '/+bxXPVfhuCqq6666qqrrrrqqn+Vv/7bJ/Af5SEPupGv+sJP4I1e79X49/qDP/4rrvovQ3DVVVddddVVV1111b/KX/7N4/mP1HUd'
    'H/y+78hnfuIHs7mx4N/qjrvu5b6zF7jqvwTBVVddddVVV1111VUvst3dPQ6PlvxneJmXegxf/2Wfysu81GP4t7rv3AWu+i9BcNVV'
    'V1111VVXXXXVi+y+cxf4z3T8+A6f+YkfzHu801vQdR3/WvedPc9V/yUIrrrqqquuuuqqq656kV3c3eO/wtu+5evzJZ/zMdx4w7X8'
    'a+zu7nPVfwmCq6666qqrrrrqqqteZLuX9vmv8pAH3chXfeEn8OKPeTgvqmEcueq/BJWrrrrqqquuuuqqq15kmxsL/qscHi35zu/7'
    'Kf7+8U/hqv9xqFx11VVXXXXVVVdd9SI7fnyH/wpPesqtfO23/CB33nUv/xrXnDnJVf8lqFz1InnGd3wzD37/e3n+ruXbb/1g3u9B'
    'vEh++xM+h9f5cl6Aa/n2Wz+Y93sQ/7Lf/mn0On/D8/Xx74y/7FH86zyRT9AP8+W8EB//zvjLHsW/2W//NHqdv+Hf7OPfGX/Zo/hX'
    '+e2fRq/zNzxfb/z63PpLr8aDeGHO8x1v8vW8/y/zgn38O+MvexT/Zr/90+h1/obn6+PfGX/Zo3iW3/5p9Dp/w3+qN359bv2lV+NB'
    'PNNv/zR6nb/h3+2NX59bf+nVeBDP5bd/Gr3O3/Avu5Zvv/WDeb8H8W9wnu94k6/n/X+Zf9nHvzP+skfxX+oZf8CbPPjX+WX+BW/8'
    '+tz6S6/Gg7jqqqv+u1xz+iT/2X7yZ3+d7/uRn+Pf4sSxHa76L0Fw1YvkQa//ErwxL8i9/Pivn+dFc56n/j0vxL38+K+f50Xx27/w'
    'N7wgb/zo07xozvMdb/I5SJ+D9MN8Of+CL/9hpM9B+hze5DvO81/uy38Y6XOQPodP+G3+SzzjO36M9/9lXrA3fn1u/bJHcdV/hXv5'
    '8V8/z7/JM57Aj/8y/7M84w94E30O0uegB/86v8yL4Jd/nQfrc5C+me94BlddddV/g2vOnOQ/y+7uHp/0WV/F9/3Iz/Fvtbm54Kr/'
    'EgRXvWgedJoX5wX75Sec40XyjCfw47/MC/XLTzjHv+w8T/17XoBrefvXP8W/5Lc/4XOQvp73/2X+TX75/b8e6Zv5jmfw3+LLX+dz'
    '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'
    'z3Hf2Qv8W21uLHint31j3uJNXpur/kchuOrf5LXf7KV4Qb78F57Isz2RX/hyno/reNiDeJYHPew6nq9fPsvTebanP+Fenp83fvtH'
    '8yD+P7mWb7/1s/il9zvFv8sbX8sb84L8Da/zJn/AM7jqf4TXfgwfz/P35b/wRJ7DM57Aj/8yz+ON3/7RPIirrrrqqv86r/4qL8u3'
    'fPVn8ckf83688eu9GieO7/Ci2NxY8Bqv8rJ83Ie/F9/1jZ/HW7zJa3PV/zhUrvq3ecgZ3hj4ZZ6Pvz/HM3gUDwJ4xjn+nufj4x/D'
    'a/MAr/0YPp6/4ct5bn/DL/z2W/Parw1wnqf+Pc/Xiz/sFP8/XMu33/rBvN+D+A/yErz9x9/LL385z98v/zof/B2P5pfe7xRX/Xd7'
    'FG/28fDlX87z+vLH89tf9ihemyue8et/xy/z3K7l7V//FFddddVV/x1e6eVfkld6+Zfkg973HXnSU27l8HDJE59yK8/tIQ+6kc3N'
    'DV78MQ/nqv/xCK76t3nQo3n7N+b5++WzPJ0rnvHrf8cv87w+/s0exXM6zaPfmOfr7596HgA4xxN+mefjpXiz1+bf4F6e8HT+7Z5x'
    'jr/nBXvxh53iP969/Pivn+c/0ut/2Tvz8bxgv/z+P8Z3PIOr/gd47Td7KZ6/v+EXfptneiJf//738jze+CV4/QfxP8S9POHpXHXV'
    'Vf9PPfLhD+ZlXuoxvPPbvQnv/HZvwju/3Zvwzm/3Jrzz270Jr/TyL8mLP+bhXPW/AsFV/0aneP23v5bn7x6e+gyA8/z6j9/L87qW'
    'Rz+E53KK13/7a3l+fvnHn8AzAH778Xw5z8cbn+EhvAAPOcMb84L9/VPP82/29LP8Mi/ItTz6Ifyn+OX3/3r0CU/kP86j+LLfeile'
    'sHt5/wf/NL/NVc/XG78+t/qzsD8L+7OwPwv7s7A/C/uzsD8L+7Ow35rX5t/ptR/Dx/P8ffkvPBEAnnGOv+d5vfHbP5oH8V/oIWd4'
    'Y16wv3/qea666qqrrvpfjeCqf7MHPew6nr97+fFfPw+c4wm/zPN645fg9R/E83jQw67j+frlszwdeMZT7+H5eeO3fzQP4gV40Gle'
    'nBfsl3/8CTyDf5tnPPUeXrDreNiD+Nd549fnVn8W9mdhfxa/9fG8YF/+w7zJd5znP8xrvzW/9fG8EH/D63zCE7nqv9ujeLOP5/n7'
    '+3M8A3jGr/8dv8xzu5a3f/1T/Jd60GlenBfsl3/8CTyDq6666qqr/hcjuOrf7rUfw8fz/P3yE87Bbz+eL+f5ePHTPIjn47Ufw8fz'
    '/PwNv/Db8PQn3Mvz8+IPO8UL9ije7ON5wX757/j1Z/Bv8ES+/v3v5QV64zM8hH+f1/6yz+K3Pp4X6Jff/+v5hN/mP8xrf9k78/G8'
    'EF/+w3zCb3PVf7PX/vDX5415Pn757/j1Z5zn13/8Xp7HG78Er/8g/oud5tFvzAv2y7/O1/82V1111VVX/e9FcNW/w2ke/cY8f39/'
    'jt9+6j08Px//Zo/i+TvNo9+Y5+vvn/pEnvr3PB8vxZu9Ni/Ua7/ZS/GC3cv7f/0T+dd6xnf8Fl/OC/bGb/9oHsS/32t/2Yfz7W/M'
    'C/Tlr/PT/Db/UR7Fl/3WS/HCfPnr/DS/zVX/rR70aN7+jXk+7uUJTz/HE36Z5/HGb/9oHsR/tVO8/ttfywvz5a/z0/w2V1111VVX'
    '/S9FcNW/wyle/+2v5QV56hPu5Xldy6Mfwgtwitd/+2t5fn75CY/nCb/M8/r4x/Da/Ate+zF8PC/El/8wb/Id53mRPeMP+OD3v5cX'
    '7KX4pPc7xX+MU7zfJ70UL9jf8Dqf8ET+w7z2W/NbH88L8Te8zpv8Ac/gqv8+p3j9t7+W5+fvv+S3+HKe27W8/euf4r/Dg17/JXhj'
    'Xpi/4XXe5A94BlddddVVV/0vRHDVv8uDHnYdz9cv/x0//vc8rzd+CV7/QbxAD3rYdTxfX/43fDnP640ffZp/2aP48G+/lhfml9//'
    '63mT7zjPv+gZf8CbPPjX+WVesDf+9tfgtfkP9NpvzW99PC/Yl/8W3/EM/sO89oe/Pm/MC/HLv84Hf8d5rvrv86DXfwnemOf1y798'
    'L8/jjV+C138Q/z0e9Gp80sfzwv3yr/NgfTPf8Qz+VX77E76Z73gGV1111VVX/fchuOrf57Ufw8fz/NzLL/8yz+ON3/7RPIgX4rUf'
    'w8fzorqWt3/9U7woHvR+78C3vzEv1C+//9cjfQ6f8Ns8H0/kE/Q56MG/zi/zQrzx6/PN73eK/2iv/WXvzMfzgtzL+3/wH/AM/oM8'
    '6NX45m+/lhfml9//x/iOZ3DVf5cHPZq3f2NeJG/89o/mQfz3ee0ve2c+nn/Jvbz/gz8H6Zv5jmfwAj3jO74Z6XOQPofX+XKuuuqq'
    'q67670Xlqn+n0zz6jYFf5kXy4g87xQv3KN7s4+HLv5wXwXU87EG8iE7xft/8+vz4g3+dX+aF+/LX+Ry+nH+La/n2b341HsR/hkfx'
    '4d9+LV/+/vfyfP3yr/PB3/Fofun9TvEf4UHv9w58+49/Pe//y7wA9/L+H/wHvP4vvRoP4v+5X/51Hqxf50X1xt/+4fzS+53i3+cU'
    'r//218Iv38sLdy1v//qn+O/1KL7st16KL3+dv+Ffdi/v/+DP4f256qqrrrrqfwGCq/6dTvH6b38tL5qX4s1em3/RQx59LS+Sj38M'
    'r82/woNejV/6rZfiP8vH/9YH834P4j/Ng97vHfj2N+YF+uX3/zG+4xn8BznF+33z6/PGvBC//Os8+BOeyFX/PR70+i/BG/Mv+PjX'
    '4f0exH+/135rbv32a7nqqquuuur/FIKr/t0e9PovwRvzInjjMzyEf9mDXv8leGP+ZW/86NP8q732W+NbX5835j/StXz7rZ/Fl702'
    '/8lO8X7f/Pq8MS/Ivbz/B/8Bz+A/yINejW/+9mt5ob78h/mE3+aq/w4PejRv/8a8UB//Zo/if4oHvd8Hc+u3X8tVV1111VX/ZxBc'
    '9e/3oNO8OP+yN377R/MgXgQPOs2L8y+5lrd//VP8mzzo1fglfzjf/sb8+73x63OrP5j3exD/NR70anzSx/OC/fKv88HfcZ7/KA96'
    'v3fg29+YF+rLX+en+W2u+q93itd/+2t5wV6KN3tt/kd50Pt9ML719Xljrrrqqquu+j+A4Kr/AI/izT6ef9GLP+wUL5pH8WYfz7/g'
    'Oh72IP4dTvF+v/RZ+NbX5435t7iWb7/1s/AvvRoP4r/Wa3/ZO/PxvGC//P6/x2/zH+UU7/fNr88b88L8Da/zCU/kqv96D3r9l+CN'
    'eQE+/jG8Nv8DPejV+CV/Fv6tl+Lf5Y1fgtd/EFddddVVV/33IbjqP8RDHn0tL9xL8WavzYvsIY++lhfq4x/Da/Mf4EGvxi/5s7A/'
    'C//WS/Ev+fjf+izsz8L+YN7vQfw3eRQf/u3X8oL9Da/zJn/AM/gP8qBX45u//VpeqC//YfQJT+Sq/2IPejRv/8Y8Xx//Zo/if7TX'
    'fmvsz8L+LG799mt5kXz8O2N/FvZn4V96NR7EVVddddVV/40Q4IPlxH+HP/6Zv+F3fuIveUFe5x1eltd5h5flqquuuuqqq6666qr/'
    'X8bJDFPyP9XWohJcddVVV1111VVXXXXVVf9aBFddddVVV1111VVXXXXVvxaV/0Y3Pfo6XuvtXpYX5KZHXcc4mauuuuqqq6666qqr'
    '/n9paf6HQ4APlhNXXXXVVVddddVVV1111Ytma1EJrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqq'
    'q6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqq'
    'q6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq666'
    '6qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK66'
    '6qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqr'
    'rrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKr'
    'rrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrq'
    'qquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vg'
    'qquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquu'
    'uuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tci'
    'uOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vK/zMX/uTb+M7fvhde7B35+Dd/JM92nj/9'
    '7m/id++Fx77dp/OmD+e/yHn+9Lu/id+9Fx77dp/Omz6cf6Xz/Ol3fxO/ey889u0+nTd9OP+FzvOn3/1N/O69XHHt6/K+7/0onvLd'
    '38Tv3guPfbtP500fzn+A8/zpd38Tv3svPPbtPp03fTj/ahf+5Nv4zt++F17sHfn4N38kV1111VX/H1z4k2/jO3/7Xnixd+Tj3/yR'
    '/Oud50+/+5v43Xu54trX5X3f+1E85bu/id+9Fx77dp/Omz6c/wDn+dPv/iZ+91547Nt9Om/6cP7VLvzJt/Gdv30vvNg78vFv/kiu'
    'uuq/AJX/gS78ybfxnb99Ly/ctbzmB3wAr3iS/xgXnsgT7uWyxz3hSbzpwx/JVS/EhT/ke7/tN7mP53LhiTzhXi573BOexJs+/JFc'
    'ddVV/3dc+JNv4zt/+15eoGtfl/d971flJFf9r3fhD/neb/tN7uO5XHgiT7iXyx73hCfxpg9/JFdd9f8UlauuOPkoHn3tb3LfvfDY'
    'Rz+S5+fCn3wb3/nb1/DWn/RWPJz/357yh7/JfQDXvi7v+96vyknud55HX/ub3HcvPPbRj+Sqq676f+be3+Q7v+Q3eezbfTpv+nD+'
    'W1z4k2/jO3/7Gt76k96Kh/Pf78KffBvf+dvX8Naf9FY8nP89nvKHv8l9ANe+Lu/73q/KSe53nkdf+5vcdy889tGP5Kqr/h+j8j/Z'
    'i70jH//mj+S/xile8b0/nVfkBbjwh/z8b98LXMNVT+JJ/wBwLa/5lq/KSR7oFK/43p/OK3LVVVf9n/Zi78jHv/kjeU5P4he/5Ed5'
    'HPC4n/g2Tn/AB/CKJ/mvdeEP+fnfvhe4hv8RLvwhP//b9wLX8L/Lk3jSPwBcy2u+5atykgc6xSu+96fzilx11f97BFddddVVV131'
    'H+KRvOknfQiveS3AvfzuHz6Jq6666qr/wwiuuuqqq6666j/MKR7+mGu57B8ez1O46qqrrvo/i8r/SU/iF7/kR3kcD/Bi78jHv/kj'
    'ecHO86ff/U387r3w2Lf7dN704Vx24U++je/87Xt5tr/jp7/k77jfNa/9IbznK53igZ7y85/PT/8Dz+nF3pGPf/NH8iK58Id877f9'
    'JvfxbNe89ofwnq90in+7J/GLX/KjPI4Hegne+pPeiofzInrKz/DlP/F3PNu9/O63fT6/yxWPfbtP500ffp4//e5v4nfvhce+3afz'
    'pg/nBXgSv/glP8rjeKCX4K0/6a14OP9WT+IXv+RHeRwP8GLvyMe/+SO56qqr/uucPHUNcC/P6Tx/+t3fxO/eC499u0/nTR9+nj/9'
    '7m/id+/lsse+3afzpg/nAc7zp9/9TfzuvTyna1+X933vV+Ukz+nCn3wb3/nb9/Jsf8dPf8nfcb9rXvtDeM9XOsVzehK/+CU/yuN4'
    'oJfgrT/prXg4L9xTfv7z+el/4Lm8BG/9SW/Fw4ELf/JtfOdv38uz/R0//SV/x/2uee0P4T1f6RTP6Un84pf8KI/jgV6Ct/6kt+Lh'
    'vDBP4he/5Ed5HA/wYu/Ix7/5I/lXe8rP8OU/8Xc827387rd9Pr/LFY99u0/nTR9+nj/97m/id++Fx77dp/OmD+cFeBK/+CU/yuN4'
    'oJfgrT/prXg4/1ZP4he/5Ed5HA/wYu/Ix7/5I7nqqv8GVP6PufAn38Z3/va9PI9/+FG+/B9egtd8bf7TXPiTb+M7f/tenq9/+FG+'
    '/Nzr8r7v/aqc5AV7ys9/Pj/9DzyP+377m/jyx78u7/ver8pJ/nUu/Mm38Z2/fS/P6+/46S/5Ox77dp/Omz6c/zIX/uTb+M7fvpfn'
    '9Xf89Jf8HY99u0/nTR/Ov8qFP/k2vvO37+V5/MOP8uX/8BK85mtz1VVX/Y9xnj/97m/id+/l+brwJ9/Gd/72vTxf9/4m3/klv8k1'
    'r/0hvOcrneLf6sKffBvf+dv38rz+jp/+kr/jsW/36bzpw3leT/kZvvwn/o7n7+/4wz95dR7+Sqf417rwJ9/Gd/72vTyvv+Onv+Tv'
    'eOzbfTpv+nCex4U/+Ta+87fv5Xn8w4/y5f/wErzma/Pf4sKffBvf+dv38rz+jp/+kr/jsW/36bzpw/lXufAn38Z3/va9PI9/+FG+'
    '/B9egtd8ba666r8alf9DLvzJt/Gdv30vl73YO/Lxb/5InuUpP8OX/8Tf8bu/zb/KyVf6AD7+lYALf8j3fttvch8vwVt/0lvxcF6I'
    'F3tHPv7NH8mzPOVn+PKf+Du49zf5+T95FO/5Sqd4vp7wM/z0P8A1r/0hvOcrneJ+F/7k2/jO374X7v1NvvPnT/Pxb/5IXmRP+Rm+'
    '87fvBYAXe0c+/s0fyf2e8vOfz0//AzzuJ36GR37SW/Fw/gUPfys+/pPeCngSv/glP8rjuJbX/IAP4BVP8qJ7ys/wnb99LwC82Dvy'
    '8W/+SO73lJ//fH76H+BxP/EzPPKT3oqH86K58Cffxnf+9r1c9mLvyMe/+SN5lqf8DF/+E3/H7/42V1111X+RC+fv47Jrz3CS5+MJ'
    'v8/v3vsSvPUnvRUP57k85Wf4zt++l8uufV3e971flZPc7zx/+t3fxO/eC/f99jfxi6c+nTd9OJedfKUP4ONfCbjwh3zvt/0m9/ES'
    'vPUnvRUP5/l4ys/wnb99LwC82Dvy8W/+SO73lJ//fH76H+BxP/EzPPKT3oqH8wBP+Rm+/Cf+DgB4Cd76k96Kh/NsF/7k2/h5rjj5'
    'Sh/Ax78ScOEP+d5v+03u4yV46096Kx7O8/GUn+E7f/teAHixd+Tj3/yR3O8pP//5/PQ/wON+4md45Ce9FQ/n2S78ybfxnb99LwC8'
    '2Dvy8W/+SJ7lKT/Dl//E3/G7v82/zsPfio//pLcCnsQvfsmP8jiu5TU/4AN4xZO86J7yM3znb98LAC/2jnz8mz+S+z3l5z+fn/4H'
    'eNxP/AyP/KS34uG8aC78ybfxnb99L5e92Dvy8W/+SJ7lKT/Dl//E3/G7v81VV/1XI/if7B9+lC//ks/ny7/k8/nyL/l8vvxLPp8v'
    '/5LP58u/5PP53j85z3N6En/82/cCcM1rfwgf/+aP5Dk8/K34+A94Xa7hP9OL8daf9Ol8/Js/kufw8LfifV/7WgDuO3ueF+Rx//B3'
    'PPbtPp33fKVTPNDJV/oA3ve1r+Wyf/gd/vQCL6In8Ys/8XcAXPPaH8LHv/kjeaCHv/mH8JrXAvwdf/gn5/nP9yR+8Sf+DoBrXvtD'
    '+Pg3fyQP9PA3/xBe81qAv+MP/+Q8L5on8ce/fS8A17z2h/Dxb/5InsPD34qP/4DX5Rquuuqq/xpP4o9/+14ArnnMozjJ83rcP9zH'
    'a37AW/FwntuT+MWf+DsArnntD+Hj3/tVOckDneIV3/vTeesX47LH/f4fcoF/rSfxiz/xdwBc89ofwse/+SN5oIe/+YfwmtcC/B1/'
    '+Cfnebbz/Onv/x2Xvdg78vGf9FY8nOd08pU+gPd8pVP86zyJX/yJvwPgmtf+ED7+zR/JAz38zT+E17wW4O/4wz85z7M9iT/+7XsB'
    'uOa1P4SPf/NH8hwe/lZ8/Ae8LtfwX+1J/OJP/B0A17z2h/Dxb/5IHujhb/4hvOa1AH/HH/7JeV40T+KPf/teAK557Q/h49/8kTyH'
    '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'
    '9wiuuuqqq6666qqrrrrqqn8tgv9BJAFgmxeFbQAk8V/BNgCSuOqqq6666qqrrrrq/zWCq6666qqrrrrqqquuuupfi+Cqq6666qqr'
    'rrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrX'
    'Irjqqquuuuqqq6666qqr/rUI/gexDYAkXhSSALDNfwVJANjmqquuuuqqq6666qr/1wiuuuqqq6666qqrrrrqqn8tgv9hbAMgiRdG'
    'EgC2+a8gCQDbXHXVVVddddVVV131/x6V/4FsIwlJANjmfpK4n23+K0gCwDZXXXXVVVddddVVV10FUPkfyjYAkpDEA9nmBbGNJCTx'
    'orCNbSQhiRfENlddddVVV1111VVXXfVMVP6Hs82/lm3+tWxz1VVXXXXVVVddddVVLyKCq6666qqrrrrqqquuuupfi+Cqq6666qqr'
    'rrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrX'
    'Irjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+Lyn+j05/291x11VVX/V9y8NWvzv8N4ulP'
    'fzoPechDuOqqq676j3DXXXexsbHJ/yFU/hs96poFT7xvyVVXXXXV/wWPumbB/xXdbMEv/MIPcOnSJa666qqr/iMcO3aM93qf9+f/'
    'EAT4YDlx1VVXXXXVVVddddVVV71othaV4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2C'
    'q6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq666'
    '6qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L'
    '4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61qFx11X+hrUUF4GA5cdVVV1111VVXXfW/GMFVV1111VVXXXXVVVdd9a9F'
    '5X+grUXlX3KwnHh+thaVF9XBcuL52VpUntvBcuL52VpUAA6WE/+SrUUF4GA5AbC1qAAcLCdeVFuLyr/WwXLiRbG1qLwwB8uJ/w5b'
    'i8oLcrCcuOqqq6666qqrrvpvQOV/mK1FBeBgOfGCbC0qW4vKwXLigbYWFYCD5cS/xdaicr+D5cT9thaVrUUF4GA58d/pYDnx3LYW'
    'FYCD5cS/1daiAnCwnHh+thaVrUXlYDnxb7W1qAAcLCdeVFuLCsDBcuK5bS0qW4vKwXLiqquuuuqqq6666r8Ywf9CB8sJgK1F5T/K'
    '1qICcLCcOFhOPNDBcuJgOQGwtaj8f3SwnADYWlT+q2wtKgAHy4nn52A5AbC1qFx11VVXXXXVVVf9FyP4X+pgOQGwtaj8e20tKgAH'
    'y4kX5mA5AbC1qFx11VVXXXXVVVdd9f8awVVXXXXVVVddddVVV131r0Xlqn+Vg+XEVf96W4sKwMFy4qqrrrrqqquuuur/AIKrOFhO'
    'AGwtKlf9z3GwnDhYTrwgW4sKwMFy4qqrrrrqqquuuuq/GMH/UluLCsDBcuI/wsFyAmBrUdlaVK56TluLCsDBcuKqq6666qqrrrrq'
    'Kqj8L7S1qAAcLCeen61F5V9ysJx4bgfLCYCtRWVrUbnfwXLi/4utReV/g61FBeBgOXHVVVddddVVV13134DK/1Bbi8oLc7CceEEO'
    'lhP/HgfLifttLSpbi8r9DpYT/5cdLCdekK1FZWtROVhO/GtsLSoAB8uJ/whbiwrAwXLiqquuuuqqq6666r8Jlf+hDpYTL8jWorK1'
    'qBwsJ/6zHSwn7re1qGwtKgfLif+PDpYTW4vK1qJysJz4r7a1qNzvYDlx1VVXXXXVVVdd9d+I4H+hg+UEwNai8l/pYDkBsLWoXPVf'
    'a2tRAThYThwsJ6666qqrrrrqqqv+m1G56qr/4bYWFYCD5cRVV1111VVXXXXV/xAE/0sdLCcAthaVf6+tRWVrUbnqP97WogJwsJz4'
    't9haVAAOlhNXXXXVVVddddVV/4MQXPXvcrCcANhaVF6YrUUF4GA5cdVVV1111VVXXXXV/3oEV3GwnADYWlRemK1FBeBgOXHVf76t'
    'RQXgYDlx1VVXXXXVVVdd9T8MlasuO1hObC0qW4sKwMFy4n5bi8r9DpYTz+1gObG1qGwtKgAHy4n7bS0q9ztYTvxvtrWoABwsJ666'
    '6qqrrrrqqqv+n0OAD5YT/1NsLSoAB8uJf8nWogJwsJy439ai8qI6WE48P1uLynM7WE68KLYWled2sJx4QbYWlRfVwXLi+dlaVAAO'
    'lhP/HluLyr/kYDnxotpaVAAOlhP/FluLyovqYDlx1VVXXXXVVVdd9V9la1ER4IPlxFVX/UfbWlQADpYTV1111VVXXXXVVf+XbC0q'
    'wVVXXXXVVVddddVVV131r0Xlqqv+kxwsJ6666qqrrrrqqqv+jyK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq666'
    '6qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqr'
    'rrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqr'
    'rrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrq'
    'qquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjq'
    'qquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquu'
    'uuqqq/61CK666qqrrrrqqquuuuqqfy2Cq6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiu'
    'uuqqq6666qqrrrrqqn8tgquuuuqqq6666qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqq'
    'q6666qp/LYKrrrrqqquuuuqqq6666l+L4Kqrrrrqqquuuuqqq6761yK46qqrrrrqqquuuuqqq/61CK666qqrrrrqqquuuuqqfy2C'
    'q6666qqrrrrqqquuuupfi+Cqq6666qqrrrrqqquu+tciuOqqq6666qqrrrrqqqv+tQiuuuqqq6666qqrrrrqqn8tgquuuuqqq666'
    '6qqrrrrqX4vgqquuuuqqq6666qqrrvrXIrjqqquuuuqqq6666qqr/rUIrrrqqquuuuqqq6666qp/LYKrrrrqqquuuuqqq6666l8L'
    'Aeaqq6666qqrrrrqqquu+tfgHwEuG7qr5yv2OgAAAABJRU5ErkJggg=='
)

# Actual owned fixture popup record/replay PNGs; no user application data.
NATIVE_POPUP_TEMPLATE_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAKAAAABgCAYAAACaJ3mZAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAA'
    'HYcAAB2HAY/l8WUAAAViSURBVHhe7Z09cqw4FIW1KqrYDYmit4HZASELISZ9SyCaiFV4A12aorsBCUlXEr7uK8Yn+BKDDgfx'
    '8WO6y1ZfX1/mzNw3RillVNOb+bRs7FRw2T6GoOlnb1spjtzOjHNvmkDuTtOZMZBxMJquCYw79+xGb79/og89Z43p59n0W99u'
    'JMZ/vwsF63bO470VLgr4NY+m7zvTnA9w07wO6OxvJwdn57ftdO6Ba5rO9GO+3PPYm27t5Uze2rM340zncPfZujjjnz3W5QUC'
    'MnSJwb+d2YybK/7CuvB2Xpia+tTU5SoQsJCa+tTU5SoQsJCa+tTU5SoQsJCa+tTU5SoQsJCa+tTU5SoQsJCa+tTU5SoQsJCa'
    '+tTU5SrVCwj+30BAIAoEBKJAQCAKBASiQEAgCgQEokBAIAoEBKJAQCAKBASiQEAgCgQEokBAIAoEBKJAQCCKUv/8awAQ4/F4'
    'GACkgIBAFAgIRIGAQBQICESBgEAUCAhEgYBAFAgIRIGAQBQICESBgECU2ws46fWvQymj2sEsgeWgbiAgEEW9/r6cNlNg4R2A'
    'gPfmLaAyevIX3gEIeG92AZWevIV3AALeG6W1fv+Z13vehiHgvVHTMpj2fRVsh8VboXYg4L1Rj8dihva+BxEC3pvXa5hpuw23'
    'Zlj8lWyWofVu2cukTbtJ/M5p9WAmIosrJyTg/rOsxwrrBCx4Dubq72Qukxl0u9+RNtpWm2Gi7048fY65oO+G9JyVdHm/B5yM'
    '3hYEAuPhVpEIOjJxXDkhAR/WY0Xyt/t93fTJZ8PV/8XyFO88xqPVUXl4+vyEgIku26Dcq4Ydrp9j2ufO2Le/ZToEiB1Yrpyg'
    'gAWPFeHxabj6O13XA68ns9jrLYuZBm3lhI8PTx9+AZNd9oH7bZi+ahzhsZ3YsK6qgYPLlRMVKOux4simJ9yHq7+dQ827fVWn'
    'D/p3+nALmO5ifRSXdxu2w+mS9rp+Ca6cqIA5cu2Shq8qFDz9MzoGc/y+PH34BaRzTp8Fx4uF1vEnwYN4FuPKiQtIL0tNYgqW'
    '/llX6Ywcrj7sAqa7uF9GyHgnWBJOneFcOaRk0Ym+/svHBkf/sgw357w/ZVnhPiICvlYMEDqgheHxHeXLIQUkJmrffnBcGo7+'
    '5R3COU7WN/rUJWDgLCsN/4kz9pxDCxh7zovn5cLRvywjnlOeFcsREHBZFuMy7OW+G04943DlJAUMTXZQyjJY+sd+HoN4bGDp'
    'kxArd72SLsEvpFLvBI/w89njQ8nBlUMt87b1nqxtDH2W0/D0j90Kw1AHlqePJZa3zKLglVCqS1BA6p2gHU6WtDJCk8uVE59M'
    'C+fKEb+KlMDePzDXDolfELn6HDmx+bEkXUkImOzi/fDJcWaeA5zw5/Lz55Trm/t0Aa6cLAHtZ5vWvRpehav/c66tA5r8JCSS'
    'w9bHvrqtEtoZy9a1PT7XDcxjUZfzYD/EPROc24BTNsD62WUgmzMnT0D3zF8hrzYZcPV/4UoY4ymnN5a/jyeQw+pDwTNgqst5'
    '8E7kku+EP9d7fYPDDr307Y2LOdkCElf1K3D1dzKnwej2/G2Y7RssdA53n62LM37/Jk2BgIkucQEjeOEX4crJp+yBP8Xn+9PU'
    '1Keky68R8Nhe7OG6jE/3T1FTn5Iuv0RA+pZxhc/2T1NTn5Iuv0PA6IvX63y0fwY19SnpUiwgAJxAQCCKUn/+GgDEOP/vLgA+'
    'CQQEokBAIAoEBKJAQCAKBASiQEAgCgQEokBAIAoEBKJAQCAKBASiQEAgCgQEokBAIAoEBKJAQCAKBASiQEAgCgQEokBAIAoE'
    'BKJAQCAKBASiQEAgCgQEokBAIAoEBKL8ByVVXWDYG3TuAAAAAElFTkSuQmCC'
)
NATIVE_POPUP_DRIVER_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAaYAAADkCAYAAADTloDQAABGUUlEQVR4Ae3AA6AkWZbG8f937o3IzKdyS2Oubdu2bdu2bdu2'
    'bWmMnpZKr54yMyLu+Xa3anqmhztr1a/u7++bq6666qqrrvqfgeCqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquu'
    'uuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85'
    'CK666qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ueg8u/0l3/+V/xXetmXfxmuuuqqq676P4vKf4CXffmX4UXxl3/+V8ye'
    '9HbYJi0yjW1aQiZkmpYmE6Y02UwztAYtzQ2v/fNcddVVV131fxqV/2I2vPQ7fT3kgNsapiVuKxgPyekQxiOY9vF4iKdDPB7i'
    '8Yhf+K0nc9VVV1111f95VP6LOQ3TEW5rPC2hHcF0hMdDGA/wdIjHAxgPyfEIpkM8HdGaueqqq6666v88gv9iDfB4iId9PFzC'
    '60vk+iJeX8Dri3h9Ea938XoXhl087JHDPi3NVVdd9T/H0dERH/IhH8KZM2fY3t5me3ub7e1ttre32d7eZnt7m+3tbba3t9ne'
    '3mZ7e5vt7W22t7fZ3t5me3ubM2fO8CEf8iEcHR1x1b/spV/25XmjN35j9vb2+Lfa29vjjd/kTXiZl3sF/oci+C+WCR4uwbCL'
    'h11yuIhXF8n1Lh528foiXu+Swx457JHDAZ6OaMlVV131P8jHfdzH8f3f//2sViv+rVarFd///d/Px33cx3HVv+zaa07zZ3/+'
    'F7zt2789e3t7/Gvt7e3xtm//9vzpn/05115zmv+hqPwXc5ocdmE8gukAxgM8HuLpAI9H0I7IcQnTimwr3AacE631XHXVVf9z'
    '/PiP/zgAP/qjP8qrvuqr8m/xh3/4h7zjO74jP/7jP843fdM3cdUL92M/+qO87du/PX/5l3/F27792/OTP/7j7Ozs8KLY29vj'
    'bd/+7fnLv/wrXvZlX4Yf/ZEf4X8oKv8B/vLP/4oXVbNhfRGmQ3I8gPEQT0d4PMTTEZ6WuK1wG/A04mxkNlr2/Fu9zdu8Db/+'
    '67/Oc3vIQx7C673e6/FRH/VRPPjBD+aqq6560a1WKwBe9VVflWPHjvFv8cZv/MYArFYrrvqX7ezs8JM//uO87du/PX/5l3/F'
    '27792/OTP/7j7Ozs8MLs7e3xtm//9vzlX/4VL/uyL8NP/viPs7Ozw/9QVP4DPPijX5cEGqIlNMRkaBaTxWiYEP7GXyEbeH0R'
    'j4cwHZDjEUxHuC3xtCSnNbSBbBN2w81kQjbz7/X6r//6PNCv//qv8+3f/u38+I//OL/4i7/IS7zES3DVVVf915HEVf86Ozs7'
    '/OSP/zhv+/Zvz1/+5V/xtm//9vzkj/84Ozs7PD97e3u87du/PX/5l3/Fy77sy/CTP/7j7Ozs8D8Ylf8Ag8V1X/ijAHgcyWnC'
    'w0Cu1+R6Ta5W/OU3fQ4bQDN4fRFPh3g8wtMRnla4LfE04BxxNtwaaXADG1qaf6+f+qmf4oEuXLjA533e5/Ht3/7tvNu7vRt/'
    '+7d/y1VXXXXV/3Q7Ozv85I//OG/79m/PX/7lX/G2b//2/OSP/zg7Ozs80N7eHm/79m/PX/7lX/GyL/sy/OSP/zg7Ozv8D0fw'
    'H2CVwm2irZa05ZI8OGDa32fc22Pcu8R4aZfDFgBkS7zexetL5LBHDvt4PMDDimxrchrJqdEa5AStQWuQyX+4kydP8lVf9VU8'
    '5CEP4elPfzp/93d/x1VXXXXV/wY7Ozv85I//OC/7si/DX/7lX/G2b//27O3tcb+9vT3e9u3fnr/8y7/iZV/2ZfjJH/9xdnZ2'
    '+F+A4D/AMoO2XJKHR0wHB4wH+4x7e7RLl5h2dxl3L3GQBYCWkOMeOezj8QCPR+S0InONp4lsJhs4oSU4IRNa8p/m9V7v9QA4'
    'ODjgqquuuup/i52dHX7yx3+cl33Zl+Ev//KveNu3f3v29vbY29vjbd/+7fnLv/wrXvZlX4af/PEfZ2dnh/8lCP4DHKVoBwdM'
    '+3u0S5eYLl1i2t1l3N1lvLjLdPEC+00AtIRc7+PxAA9L3AZyGsmWtAbZIBu0Bm7QEjIhbf6zXLx4kRfk7/7u73jv935vbr75'
    'Zra3t7n55pv5mI/5GC5cuMAL8iu/8iu893u/NzfffDPb29tsb2/z3u/93vzd3/0dz+3Hf/zHeZu3eRu2t7fZ3t5me3ub937v'
    '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'
    'wHd+53fyL3m913s9AL7zO7+TF+SP/uiP2N3d5e3e7u246qqrrrrf3t4eb/v2b89f/uVf8bIv+zL85I//ODs7O7wgOzs7/OSP'
    '/zgv+7Ivw1/+5V/xtm//9uzt7fE/GMF/sfV64GAN+2vYW8KlI7i0hEtHsLeE/SM4ODSHy5Gjo0OWB3sc7e+yPFrzn+FVXuVV'
    'eP/3f392d3f52I/9WO73qZ/6qQB88id/Mn/3d3/Hc/vxH/9xvvM7v5P7vcqrvAqv//qvz9Of/nTe+73fmwsXLvBAf/RHf8R3'
    'fud3AvASL/ESvP7rvz5Pf/rT+ZiP+RguXLjAA/3d3/0d7/iO7wjAB33QB/Gv8Su/8ivcfPPNfM7nfA4P9OM//uNsb2/znd/5'
    'nTzQd37nd7K9vc2v/MqvcNVV/xrz+RyAP/zDP8Q2/xa/9Eu/BMB8Pueqf9ne3h5v+/Zvz1/+5V/xsi/7Mvzkj/84Ozs7/Et2'
    'dnb4yR//cV72ZV+Gv/zLv+Jt3/7t2dvb438ogv9iq+XApSXsHsGlJVw6gkuHsHcIBwfJ0eGao8MDlgeXWO5f4Gj/IsuD86zW'
    'A/9ZPuMzPoPjx4/zEz/xE/zRH/0RAG/0Rm/E+7//+/OXf/mXvOqrvipv8zZvw9u8zdvwNm/zNrzkS74k7/M+78Pe3h4P9B3f'
    '8R287Mu+LD/xEz/BS73US/E2b/M2vM3bvA2v9VqvxRu+4Ruyt7fH/b7jO76Dl33Zl+Xbv/3beamXeine5m3ehrd5m7fhbd7m'
    'bXjVV31Vdnd3+a7v+i5e5VVehX+Nxz/+8ezu7vLlX/7lPNAP/MAPAPBzP/dzPNDP/dzPAfDN3/zNXHXVv8bbv/3bA/CO7/iO'
    '7OzssL29zfb2Ntvb22xvb7O9vc329jbb29tsb2+zvb3N9vY229vbbG9vs729zTu+4zsC8PZv//Zc9S97x3d6J/7yL/+Kl33Z'
    'l+Enf/zH2dnZ4UW1s7PDT/74j/OyL/sy/OVf/hXv8I7vyP9QBP/FlkcrLh3C3iHsHcL+IRweNo6OViyP9jk6vMTy4CLLg/Ms'
    'Dy6wOjjH6uA842rkP8vJkyf5nM/5HAA+6IM+iAsXLgDwVV/1Vfz4j/84r//6r8+v//qv8+u//uv8+q//Og972MP4ru/6Lj76'
    'oz+aBzp58iQ/9VM/xed93ufx0Ic+lF//9V/n13/917l48SIf//Efz3u+53tyv5MnT/JTP/VTfM3XfA0PfehD+fVf/3V+/dd/'
    'nac+9am8//u/P3/4h3/I27/92/Ov9ZjHPAaAj//4j+eB3u3d3g2At3iLt+CBXuu1XguAD/7gD+aqq/41vuIrvoJ3f/d3Zz6f'
    '8281n89593d/d77iK76Cq/5l9953jld8hZfnJ3/8x9nZ2eFfa2dnh5/88R/nFV7+5bj3vnP8D4X29/fNv8Nf/vlf8bIv/zI8'
    '0F/++V/xsi//Mjy3v/zzv+Jrfq9x+rrXZhhhGifGcWCa1ozDmjasGMclbVjSxhVtXDFNS3IcuPOe2/mBT30ZXvblX4arrrrq'
    'qqv+z6LyX2x5uORpT/h5xnFkGhvjMDFNjWmcmMZGa41pamRLWmtkMwAWV1111VVX/d+H9vf3zb/DX/75X/Ff6WVf/mW46qqr'
    'rrrq/yy0v79vrrrqqquuuup/BoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw6Cq6666qqrrvqf'
    'g+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquu'
    'uup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85EODd3V2uuuqqq6666r/b8ePHCa666qqrrrrqfw6Cq6666qqr'
    'rvqfg+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrq'
    'qquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ucg'
    'uOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu'
    '+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqq'
    'q6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu+p+D4H+pp33TG3L8+HGO'
    'v+E38TSuuupF87RvekOOHz/O8eMfza/x3+fXPvo4x48f5/gbfhNP46r/T572TW/I8ePHOf6G38TTuOr5IPh/7Nc++g05fvw4'
    'b/jRv8ZVV1111VX/I1D5r/ZrH83xd/hu/i3e+8d2+eo34D/Ir/EL3/2nAPzpd/8Cv/bVb8AbcNVVV1111X8zgv+33oA3e+9X'
    'BOAV3/vNeAOuuuqqq676H4DKf7U3+Gr+8i8/kufx1F/hg9/hU/hTXpEv+rFv5o0exvN46EP5D/UGX/2r7H41V1111f9ST/u1'
    'b+Jrv+yneNzbfDO/+iEP5b/b037tm/jaL/spHvc238yvfshD+d/sab/2TXztl/0Uj3ubb+ZXP+Sh/Bei8t/goQ99KC/Uwx7K'
    'Qx/KVVddddUL8TR+5cs+he/+U3jFt+F/gKfxK1/2KXz3n8Irvg3/yz2NX/myT+G7/xRe8W34r0Zw1VVXXXXVVf9zEFx11VVX'
    'XXXV/xwE/2c8jV/76DfkDY8f5/jx4xw/fpzjb/iGfPSvPY0X5Nc++jjHjx/n+Bt+E0/j+Xkav/ZNH80bvuEbcvz4cY4fP87x'
    '48d5wzd8Qz76m36Np/Gv87RvekOOHz/O8eMfza9xxdN+7aN5wzc8zvHjxzl+/DjHj78hb/jR38SvPY0XydOe9mt800e/IW94'
    '/DjHjx/n+PHjHD9+nDd8w4/mm37tabwwT/umN+T48eMcP/7R/BpXPO3XPpo3fMPjHD9+nOPHj3P8+Bvyhh/9Tfza03gBnsY3'
    'veFxjh8/zht+09N4wZ7GN73hcY4fP87xj/41ntvTvukNOX78OMePfzS/xhVP+7WP5g3f8DjHjx/n+PHjHD/+hrzhR38Tv/Y0'
    '/kM97dc+mjd8w+McP36c48ePc/z4G/KGH/1N/NrTeJE87de+iY9+wzfk+PHjHD9+nOPHj/OGb/jRfNOvPY1/r6c97df4po9+'
    'Q97w+HGOHz/O8ePHOX78OG/4hh/NN/3a03hhnvZNb8jx48c5/obfxNMAeBq/9tFvyBseP87x48c5fvw4x9/wDfnoX3sa/7Kn'
    '8bRf+yY++g3fkOPHj3P8+HGOHz/OG77hR/NrTwN4Gt/0hsc5fvw4b/hNT+P5exq/9k0fzRu+4Rty/Phxjh8/zvHjx3nDN3xD'
    'Pvqbfo2n8aJ52je9IcePH+f48ZflU/6Uy/70U16W48ePc/z4cY4fP85H/xrP19Oe9mt800e/IW94/DjHjx/n+PHjHD9+nDd8'
    'w4/mm37tafxbPO2b3pDjx49z/PjL8il/ymV/+ikvy/Hjxzl+/DjHjx/no3+NF+Jp/NpHvyFvePw4x48f5/jx4xx/wzfko3/t'
    'abwonvZr38RHv+Ebcvz4cY4fP87x48d5wzf8aL7p157Gv9bTvukNOX78OMePvyyf8qdc9qef8rIcP36c48ePc/z4cT7613i+'
    'nvZr38RHv+Ebcvz4cY4fP87x48d5wzf8aL7p157GC/c0fu2bPpo3fMM35Pjx4wAE/xc87df46Dd8Wd7hu/+UP+UB/vRP+e53'
    'eFne8KN/jX+1p30Tb3j8ZXmHT/lu/vRP/5QH+tM//VO++1N+gafy7/E0vukNj/Oy7/Dd/Omf8gB/yp9+96fwDi97nI/+tafx'
    'gj2Nb/roN+RlX/Yd+JTv/lP+lOf0p3/63XzKO7wsx9/wo/m1p/EieBrf9IbHedl3+G7+9E95gD/lT7/7U3iHlz3OR//a0/iv'
    '8TS+6Q2P87Lv8N386Z/yAH/Kn373p/AOL3ucj/61p/Hv9zS+6Q2P87Lv8N386Z/yAH/Kn373p/AOL/uGfPSvPY0X7Gl80xse'
    '52Xf4VP47j/9Ux7oT//0u/mUd3hZjr/hN/E0/i2exjd99Bvysi/7DnzKd/8pf8pz+tM//W4+5R1eluNv+NH82tP4lz3t1/jo'
    '4y/LO3z3n/KnPMCf/inf/Q4vyxt+09N4wX6Nj37Dl+Vl3+FT+O4//VMe6E//9Lt5h5c9zkf/Gi/c076JNzz+srzDp3w3f/qn'
    'f8oD/emf/inf/Sm/wFP5z/Q0vumj35CXfdl34FO++0/5U57Tn/7pd/Mp7/CyHH/Dj+bXnsZ/naf9Gh/9hi/LO3z3n/KnPMCf'
    '/inf/Q4vyxt+9K/xgj2Nb3rD47zsO3wK3/2nf8oD/emffjef8g4vy/E3/Caexn+2p/FNb3icl32HT+G7//RPeaA//dPv5lPe'
    '4WU5/obfxNN4Pp72Tbzh8ZflHT7lu/nTP/1Tnongf70n8rUf/A58N+/NF/3lX7K7u8vu7i5/+ZdfxHtzxZ9+9zvw0b/Gv8Kv'
    '8dEv+yn8KcArvjc/9pe77O7usru7y+7uX/KXP/ZFvPcr8u/yCx/9snzKn74i7/1jf8lf7u6yu7vL7u4uf/ljX8QrcsV3v8MH'
    '801P4/l4Gt/0hi/Lp3z3nwLwiu/9Y/zlX+6yu7vL7u4uu3/5l/zYF703rwjwp9/NO7zsR/NrvHC/8NEvy6f86Svy3j/2l/zl'
    '7i67u7vs7u7ylz/2RbwiV3z3O3ww3/Q0/tP9wke/LJ/yp6/Ie//YX/KXu7vs7u6yu7vLX/7YF/GKXPHd7/DBfNPT+Hf5hY9+'
    'WT7lT1+R9/6xv+Qvd3fZ3d1ld3eXv/yxL+IVAfhTvvsdPphvehrPx9P4pjd8WT7lTwFekff+sb/kL3d32d3dZXf3L/mxL3pv'
    'XhHgTz+Fl/3oX+Nf52l80xu+LJ/y3X8KwCu+94/xl3+5y+7uLru7u+z+5V/yY1/03rwiwJ9+N+/wsh/Nr/HCPJGv/eB34Ltf'
    '8b35sb/8S3Z3d9nd3eUvf+yLeEWu+NNP+WC+6Wk8H7/GRx9/B777TwFekff+oh/jL3d32d3dZXd3l7/8sS/ivV8RvvsdXpZP'
    '+VNegF/jo1/2U/hTgFd8b37sL3fZ3d1ld3eX3d2/5C9/7It471fkRfbQD/lVdnd32d39S77oFbnsFb/oL9nd3WV3d5fd3V2+'
    '+g14gKfxTW/4snzKd/8pAK/43j/GX/7lLru7u+zu7rL7l3/Jj33Re/OKAH/63bzDy340v8aL7qEf8qvs7u6yu/uXfNErctkr'
    'ftFfsru7y+7uLru7u3z1G/B8PJGv/eB34Lt5b77oL/+S3d1ddnd3+cu//CLemyv+9LvfgY/+NZ6Pp/FNb/iyfMqfArwi7/1j'
    'f8lf7u6yu7vL7u5f8mNf9N68IsCffgov+9G/xovqoR/yq+zu7rK7+5d80Sty2St+0V+yu7vL7u4uu7u7fPUb8ABP45ve8GX5'
    'lD8FeEXe+8f+kr/c3WV3d5fd3b/kx77ovXlFgD/9FF72o3+N5/RrfPTLfgp/CvCK782P/eUuu7u7AAT/2/3pd/PdfBF/+atf'
    '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'
    'jx/n+PHjvMN387we+iF88xe9Ipf96afwDi97nDd8w48GILjq+Xroh/wqf/ljX8R7vyKX/emffjfv8LIvy/Hjb8hH/9rT+G/3'
    'io/iYfzrPO4pT+Oq/yVe8VE8jH+dxz3lafyneMVH8TD+fR76Ib/KX/7YF/Her8hlf/qn3807vOzLcvz4G/LRv/Y0/tO94qN4'
    'GP86j3vK07jqP9dDP+RX+csf+yLe+xW57E//9LsBCK56gR76Bh/CV//qLrt/+WN80Xu/Iq8IwJ/y3e/wshx/w2/iafzXeOzD'
    'H8rz+NMn8lT+dR778IfyH+GxD38o/1M89uEP5b/CYx/+UJ7HK34Rf7m7y+7uLru7u+zu7rK7u8vu7i67u7vs7u6yu7vL7u4u'
    'u1/9Bvyr/OkTeSr/Oo99+EP5T/GnT+Sp/Ps99A0+hK/+1V12//LH+KL3fkVeEYA/5bvf4WU5/obfxNP4T/SnT+Sp/Os89uEP'
    '5X+0V/wi/nJ3l93dXXZ3d9nd3WV3d5fd3V12d3fZ3d1ld3eX3d1ddr/6DfhP84pfxF/u7rK7u8vu7i67u7vs7u6yu7vL7u4u'
    'u7u77O7usru7y+5XvwHP7aFv8CF89a/usvuXP8YXvfcrAhBc9S976BvwIV/9q/zq7l/yY+/9ilz2p5/C1/4a/3l+7Rf4bgBe'
    'kUc9jGd56MMfyxWP4ylP41/2tKfwOABekUc9jH+7X/sFvhuAV+RRD+P5+tMnPpX/Er/2C3w3AK/Iox7Gf55f+wW+G4BX5FEP'
    '41ke+vDHAsCfPpGn8h/roQ9/LFc8jqc8jX/Z057C4wB4RR71MP5DPfThj+VF91Se+Ke8aB76BnzIV/8qv7r7l/zYe78il/3p'
    'p/C1v8Z/uIc+/LFc8Tie8jT+ZU97Co8D4BV51MP4H+mhD38sAPzpE3kq/30e+vDHAsCfPpGn8h/goW/Ah3z1rwIQXPWv8FDe'
    '4Ku/mS96RS777l/4Nf5tvptf+DVeqF/7he/msld8G97ooTzbG7wZ7w3An/JTv/I0/iVP+5Wf4k8BeCwPfygvwHfzC7/GC/Vr'
    'v/DdXPaKb8MbPZTn73FP4Wm8AE/7FX7qT3kRfDe/8Gu8UL/2C98NAK/4NrzRQ/k3+m5+4dd4oX7tF74bAF7xbXijh/JsD3sU'
    'rwjAd/Nl3/Q0/kO9wZvx3gD8KT/1K0/jX/K0X/kp/hSAx/Lwh/If62GP4hUB+G6+7Juexgv1a7/Ad/Ov9VDe4Ku/mS96RS77'
    '7l/4Nf7DvcGb8d4A/Ck/9StP41/ytF/5Kf4UgMfy8IfyP9PDHsUrAvDdfNk3PY3/Ng97FK8IwHfzZd/0NP4DEVz13+K7v+yb'
    'eBovwK99NO/w3Vz2im/zRjyUB3oD3uy9uexPP+Vl+ehf4wV72jfxwZ/ypwC84hd9JG/AC/bdX/ZNPI0X4Nc+mnf4bi57xbd5'
    'Ix7KAz2UN3qbVwSAP/0pfuVpPB9P45s++FP4U1403/1l38TTeAF+7aN5h+/msld8mzfiofzbffeXfRNP4wX4tY/mHb6by17x'
    'bd6Ih/IAD/0QPuG9uexPP+WD+aan8YI97df46G/6NV50b8CbvTeX/emnvCwf/Wu8YE/7Jj74U/4UgFf8oo/kDfgP9tA34m1e'
    'kcv+9FO+ll97Gs/f036Nj36H7+a/058+8ak8f2/Am703l/3pp7wsH/1rvGBP+yY++FP+FIBX/KKP5A34t/vTJz6V/zQP/RA+'
    '4b257E8/5YP5pqfxgj3t1/job/o1/j3+9IlP5fl66IfwCe/NZX/6KR/MNz2NF+xpv8ZHf9Ov8SIiuOp5PO2b3pA3/Ohv4tee'
    '9jSexgM87Wn82kd/MJ/ypwCvyBd95Bvwb/ann8LLvuFH802/9jSe7Wn82je9Icff4bsB4BW/iG/+kIfy3N7gq3+M935FLvvu'
    'dzjOG370r/G0p/FsT3sav/ZNH80bvuyn8KcAr/hFfPOHPJQX6k8/hZd9w4/mm37taTzb0/i1b3pDjr/DdwPAK34R3/whD+W5'
    'PfSN3oZXBOBP+ZSXfUO+6deexrM87df46Dd8WT7lT1+RV3xFXjR/+im87Bt+NN/0a0/j2Z7Gr33TG3L8Hb6by17xi/jmD3ko'
    '/y5/+im87Bt+NN/0a0/j2Z7Gr33TG3L8Hb6by17xi/jmD3koz+0NPvKLeEUA/pRPedk35KO/6dd42tN4tqc9jV/76DfkDV/2'
    'HfjuJ/Kv8gZf/WO89yty2Xe/w3He8KN/jac9jWd72tP4tW/6aN7wZT+FPwV4xS/imz/kofzHeygf8gnvzRXfzTu87Bvy0b/2'
    'NJ7G/Z7G037to3nDl30HvvsV35v3fkWer6d90xvyhh/9Tfza057G03iApz2NX/voD+ZT/hTgFfmij3wD/nUeysMfyxXf/WV8'
    '0689DQCe9k1806/xLG/w1T/Ge78il333OxznDT/613ja03i2pz2NX/umj+YNX/ZT+FOAV/wivvlDHsq/3kN5+GO54ru/jG/6'
    'tacBwNO+iW/6Nf5DvcFHfhGvCMCf8ikv+4Z89Df9Gk97Gs/2tKfxax/9hrzhy74D3/1E/g0eysMfyxXf/WV80689jcue9k18'
    '06/xLG/wkV/EKwLwp3zKy74hH/1Nv8bTnsazPe1p/NpHvyFv+LLvwHc/kefwtG96Q97wo7+JX3va03gaz4HKVc/Xn373p/AO'
    '3/0pvCCv+EXfzIc8lH+j9+bH/vJRfNnLfgqf8g7fzafwfLzie/Njv/ohPJTn5w346l/9MXjDd+C7/xT+9LvfgZf9bp6vV3zv'
    'H+Obv/oNeCgvzHvzY3/5KL7sZT+FT3mH7+ZTeD5e8b35sV/9EB7K8/HQD+Gbv+ineNlP+VPgT/mUd3hZPoUHekW+6C+/GT74'
    'ZflT/iXvzY/95aP4spf9FD7lHb6bT+H5eMX35sd+9UN4KP8e782P/eWj+LKX/RQ+5R2+m0/h+XjF9+bHfvVDeCjPx0M/hF/9'
    'S3jDl/0U/pQ/5bs/5R347k/h+XrFRz2Mf5034Kt/9cfgDd+B7/5T+NPvfgde9rt5vl7xvX+Mb/7qN+Ch/Cd5g6/mL38MPvgd'
    'vps/5U/57nd4Wb6b5/KK782P/epH8pQ3/G5ekD/97k/hHb77U3hBXvGLvpkPeSj/am/wkV/EK373p/Cn/Cmf8g4vy6dwxXv/'
    '2IfwbG/AV//qj8EbvgPf/afwp9/9Drzsd/N8veJ7/xjf/NVvwEP5t3mDj/wiXvG7P4U/5U/5lHd4WT6FK977xz6E/1AP/RB+'
    '9S/hDV/2U/hT/pTv/pR34Ls/hefrFR/1MP4t3uAjv4hX/O5P4U/5Uz7lHV6WT+GK9/6xD+FZHvoh/Opfwhu+7Kfwp/wp3/0p'
    '78B3fwrP1ys+6mE8tz/97k/hHb77U3guBFc9j4e+0SfwRV/03rziK/KcXvEVecX3/jH+8i93+dUPeSj/Lg/9EH71L3+ML3rv'
    'V+SBXvEV35sv+rG/ZPdXv5o34IV5A776V3f5yx/7It77FV+RV+SBXpFXfO8v4sf+8i/51a9+Ax7Ki+ChH8Kv/uWP8UXv/Yo8'
    '0Cu+4nvzRT/2l+z+6lfzBrxgD/2QX+Uvf+yLeO9XfEUe6BXf+4v4sb/8VT7kobzoHvoh/Opf/hhf9N6vyAO94iu+N1/0Y3/J'
    '7q9+NW/Af4CHfgi/+pc/xhe99yvyQK/4iu/NF/3YX7L7q1/NG/BCPPRD+NXdv+THvui9ecVX5Dm94ivy3l/0Y/zlX+7yqx/y'
    'UP713oCv/tVd/vLHvoj3fsVX5BV5oFfkFd/7i/ixv/xLfvWr34CH8p/roW/w1fzqX/4YX/Ter8gr8gCv+Iq89xf9GH/5q1/N'
    'G/CCPfSNPoEv+qL35hVfkef0iq/IK773j/GXf7nLr37IQ/k3eeiH8Kt/+UW89yvybK/43jzqYTyXN+Crf3WXv/yxL+K9X/EV'
    'eUUe6BV5xff+In7sL/+SX/3qN+Ch/Ds89EP41b/8It77FXm2V3xvHvUw/uM99EP41d2/5Me+6L15xVfkOb3iK/LeX/Rj/OVf'
    '7vKrH/JQ/k0e+iH86l9+Ee/9ijzbK743j3oYz+mhH8Kv7v4lP/ZF780rviLP6RVfkff+oh/jL/9yl1/9kIfyQA99o0/gi77o'
    'vXnFV+S5IcC7u7tc9Z/vad/0hrzsp/wp8N782O5X8wb893raN70hL/spfwq8Nz+2+9W8Af99nvZNb8jLfsqfAu/Nj+1+NW/A'
    'Vf+7/Bofffwd+G7gFb/oL/nVD3koV131b3H8+HEqV1111VX/Xk97Co/jisc+/KFcddW/A5Wrrrrqqn+nX/vaT+FPAXhv3uwN'
    'uOqqfw8qV1111VX/Dk/7tY/mHb6by17xiz6SN+Cqq/5dCK666qqrXoCnfdMb8oZv+NF806/9Gk972tN4tqfxtKf9Gt/00W/I'
    'y77DdwPAK34R3/whD+Wqq/6dqFx11VVXvRB/+qffzZ++w3fzKbwQr/hF/OWvfggP5aqr/t0IrrrqqqtegId+yDfzlz/2Rbz3'
    'K74ir8hzecVX5BXf+4v4sb/8S3Z/9UN4KFdd9R8CAd7d3eWqq6666qqr/rsdP36c4Kqrrrrqqqv+5yC46qqrrrrqqv85CK66'
    '6qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7n'
    'ILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw4q/4luu9T4'
    '0F+4wN/dN3JplVx11VX/fm/2iAVf9PrHueVY4aqr/g+i8p/ktkuN1/iue/noVzvFz7zXSY7Pg6uuuurfZ3eVfM9f7vIa33kv'
    'v/e+13LLscJVV/0fgwDv7u7yH+3dfvI8r/+IY3zkq57gqquu+o/1Ob9xlt98yj4//65nuOqq/0uOHz9O8J/kF5605D1f9hhX'
    'XXXVf7yPerWT/N29I1dd9X8Qlf9Ex+fBVVdd9R/v+LxwaZ1cddX/QQRXXXXVVVdd9T8HwVVXXXXVVVf9z0Fw1VVXXXXVVf9z'
    'EFx11VVXXXXV/xwEV1111VVXXfU/B8FVV1111VVX/c9BcNVVV1111VX/cxBcddVVV1111f8cBFf9j/HLH1wopVBe/Wt5Kldd'
    'ddVV/y8RXHXVVVddddX/HAT/J/wyH1wKpRRK+WB+mauuuuqqq/6XIvi/4Jd/mm/jft/GT/8yV1111VVX/e9E8H/AL//0t/FA'
    '3/bTv8xVV1111VX/KxH8r/fL/PS3cdkHfMAHAMC3/TS/zFVXXXXVVf8LEfwv99Sv/Xy+DYAP4K0/4bG8CgDfxud/7VO56qqr'
    'rrrqfx2C/9Weyi/86B9x2Qe8NW/8sDfjHV+Fy/7oR3+Bp3LVVVddddX/MgT/mz31F/jRP+KyD3jrNwYexkd++gdw2R/9KL/w'
    'VF6op37tq1NKoZQP5pe54qm//MG8+qsXSimUUijl1Xn1D/5afvmpvEBP/dpXp5RCKR/ML3PFU3/5g3n1Vy+UUiilUMqr8+of'
    '/LX88lP5V/nlDy6UUijlg/ll/iVP5WtfvVBKoXzwL/OieurXvjqlFEr5YH6ZK576yx/Mq796oZRCKYVSXp1X/+Cv5Zefyovk'
    'qU/9Zb72g1+dVy+FUgqlFEopvPqrfzBf+8tP5YV56te+OqUUSvlgfpkrnvrLH8yrv3qhlEIphVJenVf/4K/ll5/KC/BUvvbV'
    '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'
    'vomrrvq/5Pjx41Su+l/nqV/7+XwbAK/CO77Zw7jqqquu+j+EylX/yzyVX/jRPwKAD/h0PvJhXHXVVVf9X0Lwn+TYPNhdJVf9'
    'B/vlL+Nj/gjgVfiqT3hjrvr/aXfVuOqq/6MQ4N3dXf6jvfkPnuV1H77DZ73eaa666qr/WF/zBxf4jSfv8QNvd4qrrvq/5Pjx'
    '41T+k3zjm53gNb7rPo4vgvd62eMcnwdXXXXVv8/uKvmaPzjP1/zhBX7vfa7hqqv+D0KAd3d3+c9w26WJT/mFO/iFx10Em6uu'
    'uurf59i88BI37fCNb3sztxyrXHXV/zXHjx9HgHd3d7nqqquuuuqq/27Hjx8nuOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqq'
    'q676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK66'
    '6qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7n'
    'ILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw6Cq6666qqr'
    'rvqfg+Cqq6666qqr/ucguOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrq'
    'qquuuup/DoKrrrrqqquu+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85CK666qqrrrrqfw6Cq6666qqrrvqfg+Cqq6666qqr/ucg'
    'uOqqq6666qr/OQiuuuqqq6666n8Ogquuuuqqq676n4Pgqquuuuqqq/7nILjqqquuuuqq/zkIrrrqqquuuup/DoKrrrrqqquu'
    '+p+D4Kqrrrrqqqv+5yC46qqrrrrqqv85EGCuuuqqq6666n8G/hHHhqd6DdGVAgAAAABJRU5ErkJggg=='
)

if __name__ == "__main__":
    unittest.main()
