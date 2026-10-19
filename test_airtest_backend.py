"""Actual optional image backend tests; no desktop capture or input."""
import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import airtest_backend as backend
from image_targets import validate_match
from operations import OperationError


class DependencyImportTests(unittest.TestCase):
    def test_broken_official_opencv_import_cannot_write_to_mcp_stdout(self):
        loader = Path(backend.__file__).parent/backend.RUNTIME_DIRECTORY/"cv2/__init__.py"
        if not loader.is_file():
            self.skipTest("Official OpenCV loader is not bundled")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"cv2").mkdir()
            shutil.copy2(loader, root/"cv2/__init__.py")
            (root/"numpy").mkdir()
            (root/"numpy/__init__.py").write_text("raise ImportError('intentional missing numpy dependency')\n", encoding="utf-8")
            (root/"BACKEND-MANIFEST.json").write_text(json.dumps({"format": "computer-image-backend/v1", "airtest": "1.4.3"}), encoding="utf-8")
            script = """import json,sys
import airtest_backend as backend
backend.RUNTIME_DIRECTORY = sys.argv[1]
try:
    backend._load()
except backend.BackendUnavailable as error:
    assert isinstance(error.__cause__, ImportError), type(error.__cause__)
    print(json.dumps({'native_fallback': True}))
else:
    raise AssertionError('broken dependency unexpectedly loaded')
"""
            result = subprocess.run([sys.executable, "-c", script, str(root)],
                cwd=Path(backend.__file__).parent, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"native_fallback": True})
            self.assertNotIn("pip install", result.stdout)
            self.assertNotIn("OpenCV bindings", result.stdout)


class IsolatedWorkerTests(unittest.TestCase):
    def setUp(self):
        from test_image_steps import image_target, png
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.runtime = SimpleNamespace(run_dir=self.folder, check_active=lambda: None, stop_event=threading.Event())
        self.target, self.shot = image_target(), png(100, 100)

    def test_cancel_terminates_only_owned_image_worker_and_removes_png_requests(self):
        running = [True]
        child = SimpleNamespace(poll=lambda: None if running[0] else 0,
            terminate=mock.Mock(side_effect=lambda: running.__setitem__(0, False)), wait=mock.Mock())
        checks = [0]
        def stop():
            checks[0] += 1
            if checks[0] > 1:
                raise RuntimeError("cancel image search")
        with mock.patch.object(backend.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(RuntimeError, "cancel image search"):
                backend.match_isolated(self.runtime, self.target, self.shot, check_active=stop)
        child.terminate.assert_called_once()
        self.assertEqual(list((Path(self.folder)/"im").iterdir()), [])

    def test_startup_deadline_terminates_a_stuck_import(self):
        running = [True]
        child = SimpleNamespace(poll=lambda: None if running[0] else 0,
            terminate=mock.Mock(side_effect=lambda: running.__setitem__(0, False)), wait=mock.Mock())
        with mock.patch.object(backend.subprocess, "Popen", return_value=child):
            with self.assertRaises(TimeoutError):
                backend.match_isolated(self.runtime, self.target, self.shot, deadline=time.monotonic()-1)
        child.terminate.assert_called_once()

    def test_wrong_response_nonce_is_rejected_before_candidate_delivery(self):
        def child_start(arguments, **kwargs):
            Path(arguments[-1]).write_text(json.dumps({"nonce": "0"*64, "status": "matched"}), encoding="utf-8")
            return SimpleNamespace(poll=lambda: 0, returncode=0)
        with mock.patch.object(backend.subprocess, "Popen", side_effect=child_start):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                backend.match_isolated(self.runtime, self.target, self.shot)

    @unittest.skipUnless(backend.available(), "Optional image runtime not bundled")
    def test_actual_worker_from_background_thread_returns_without_loading_numpy_in_parent(self):
        # The subprocess imports NumPy on its own main thread, while the MCP
        # analogue waits only on process completion and remains cancellable.
        result, errors = [], []
        def run():
            try:
                result.append(backend.match_isolated(self.runtime, self.target, self.shot))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        with mock.patch.object(backend, "_load", side_effect=AssertionError("MCP parent must not import image libraries")):
            thread.start()
            thread.join(15)
        self.assertFalse(thread.is_alive(), "Isolated worker failed to finish")
        self.assertFalse(errors, errors)
        self.assertIn(result[0]["status"], ("matched", "not_found", "ambiguous"))
        self.assertEqual(list((Path(self.folder)/"im").iterdir()), [])


@unittest.skipUnless(backend.available(), "Optional offline image runtime is not bundled")
class AirtestBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv2, cls.np, _ = backend._load()

    def encoded(self, value):
        return base64.b64encode(self.cv2.imencode(".png", value)[1]).decode()

    def setUp(self):
        self.template = self.np.full((30, 70, 3), (40, 80, 145), self.np.uint8)
        self.cv2.rectangle(self.template, (1, 1), (68, 28), (230, 230, 230), 1)
        self.cv2.putText(self.template, "A1", (9, 22), self.cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1, self.cv2.LINE_AA)
        self.screen = self.np.full((250, 400, 3), 25, self.np.uint8)
        self.screen[80:110, 120:190] = self.template
        self.target = {"format": "computer-image-target/v1", "template_png": self.encoded(self.template),
            "width": 70, "height": 30, "anchor": {"x": .8, "y": .3},
            "capture_window": {"width": 400, "height": 250}, "min_score": .94, "ambiguity_margin": .03}

    def match(self, screen=None, target=None):
        screen = self.screen if screen is None else screen
        return backend.match_candidates(target or self.target, self.encoded(screen))

    def test_exact_target_keeps_recorded_noncentral_click_offset(self):
        result = validate_match(self.match(), self.target, self.encoded(self.screen))
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["x"], result["y"]), (176, 89))

    def test_small_border_change_preserves_source_pixels_and_anchor(self):
        screen = self.screen[1:-1,1:-1].copy()
        result = validate_match(self.match(screen),self.target,self.encoded(screen))
        self.assertEqual(result["status"],"matched")
        self.assertEqual((result["x"],result["y"]),(175,88))
        self.assertEqual(result["frame_comparison"]["size_relation"],"small_border_difference")
        self.assertFalse(result["frame_comparison"]["dpi_change_proven"])

    def test_changed_highlight_is_review_candidate_not_automatic_click(self):
        changed = self.template.copy()
        background = self.np.all(changed == (40,80,145), axis=2)
        changed[background] = (130,170,220)
        self.screen[80:110,120:190] = changed
        result = validate_match(self.match(),self.target,self.encoded(self.screen))
        self.assertEqual(result["status"],"not_found")
        self.assertNotIn("x",result)
        self.assertNotIn("rect",result)
        self.assertTrue(any(item["rect"]["x"]==120 and item["rect"]["y"]==80 for item in result["review_candidates"]))
        self.assertTrue(all(item["review_only"] for item in result["review_candidates"]))
        self.screen[180:210,20:90] = changed
        duplicate = self.match()
        self.assertEqual(duplicate["status"],"not_found")
        self.assertGreaterEqual(len(duplicate["review_candidates"]),2)
        self.assertNotIn("rect",duplicate)

    def test_changed_field_value_retains_surrounding_context_for_review(self):
        template=self.np.full((90,220,3),235,self.np.uint8)
        self.cv2.putText(template,'Family',(8,22),self.cv2.FONT_HERSHEY_SIMPLEX,.6,(20,20,20),1)
        self.cv2.rectangle(template,(7,34),(208,80),(40,40,40),2)
        self.cv2.putText(template,'W',(20,67),self.cv2.FONT_HERSHEY_SIMPLEX,.7,(20,20,20),2)
        changed=template.copy(); changed[40:77,12:60]=235
        self.cv2.putText(changed,'N',(20,67),self.cv2.FONT_HERSHEY_SIMPLEX,.7,(20,20,20),2)
        screen=self.np.full((250,400,3),25,self.np.uint8);screen[80:170,100:320]=changed
        target={**self.target,'width':220,'height':90,'template_png':self.encoded(template),'min_score':.999}
        result=self.match(screen,target)
        self.assertEqual(result['status'],'not_found')
        self.assertTrue(any(abs(c['rect']['x']-100)<=1 and abs(c['rect']['y']-80)<=1 for c in result['review_candidates']))
        self.assertNotIn('rect',result)

    def test_duplicate_at_a_distant_location_is_ambiguous(self):
        self.screen[180:210, 20:90] = self.template
        result = self.match()
        self.assertEqual(result["status"], "ambiguous")
        self.assertNotIn("rect", result)
        self.assertGreaterEqual(result["candidate_count"], 2)

    def test_new_duplicate_is_found_after_previous_success(self):
        self.assertEqual(self.match()["status"], "matched")
        self.screen[180:210, 20:90] = self.template
        self.assertEqual(self.match()["status"], "ambiguous")

    def test_missing_target_does_not_match_uniform_background(self):
        self.screen[:] = 25
        result = self.match()
        self.assertEqual(result["status"], "not_found")
        self.assertNotIn("rect", result)

    def test_white_background_runnerups_without_a_threshold_match_are_not_duplicate_targets(self):
        pattern = self.np.full((60, 300, 3), 255, self.np.uint8)
        self.cv2.rectangle(pattern, (1, 1), (298, 58), (215, 215, 215), 1)
        self.cv2.putText(pattern, "Search WEBREC", (8, 38), self.cv2.FONT_HERSHEY_SIMPLEX,
                         .65, (130, 130, 130), 1, self.cv2.LINE_AA)
        screen = self.np.full((250, 400, 3), 255, self.np.uint8)
        target = {**self.target, "template_png": self.encoded(pattern), "width": 300, "height": 60}
        result = self.match(screen, target)
        self.assertEqual(result["status"], "not_found")
        self.assertGreater(result["score"], .91)
        self.assertLess(result["score"], .94)
        self.assertEqual(result["candidate_count"], 0)
        self.assertNotIn("rect", result)
        self.assertNotEqual(result.get("code"), "candidate_budget_exceeded")

    def test_lower_scoring_distinct_runnerup_inside_margin_still_blocks_input(self):
        self.screen[180:210, 20:90] = self.np.clip(self.template.astype(self.np.int16)+5, 0, 255).astype(self.np.uint8)
        result = self.match()
        self.assertEqual(result["status"], "ambiguous")
        self.assertLess(result["score"]-result["second_score"], self.target["ambiguity_margin"])
        self.assertNotIn("rect", result)

    def test_candidate_budget_reports_incomplete_search_and_actual_score_without_fake_count(self):
        # Simulate a response map that cannot be exhausted within its bounded
        # scan. Evaluating the same real perfect patch is not 257 duplicate UI
        # controls, and an unfinished search must never return coordinates.
        with mock.patch.object(self.cv2, "minMaxLoc", return_value=(0., 1., (0, 0), (120, 80))):
            result = self.match()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["code"], "candidate_budget_exceeded")
        self.assertEqual(result["score"], 1)
        self.assertIs(result["search_complete"], False)
        self.assertEqual((result["candidate_evaluations"], result["candidate_budget"]), (256, 256))
        self.assertNotIn("candidate_count", result)
        self.assertNotIn("rect", result)

    def test_changed_color_cannot_pass_on_high_correlation_alone(self):
        self.screen[80:110, 120:190] = self.np.clip(self.template.astype(self.np.int16)+90, 0, 255).astype(self.np.uint8)
        self.assertNotEqual(self.match()["status"], "matched")

    def test_moved_scaled_target_is_resolved_in_current_pixels(self):
        scaled = self.cv2.resize(self.template, (105, 45), interpolation=self.cv2.INTER_CUBIC)
        self.screen[:] = 25
        self.screen[123:168, 233:338] = scaled
        result = validate_match(self.match(), self.target, self.encoded(self.screen))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["rect"], {"x": 233, "y": 123, "width": 105, "height": 45})
        self.assertEqual((result["x"], result["y"]), (317, 136))

    def test_low_detail_target_is_not_accepted_by_opencv_constant_correlation(self):
        target = {**self.target, "template_png": self.encoded(self.np.full_like(self.template, 80))}
        result = self.match(target=target)
        self.assertEqual(result["code"], "template_low_detail")
        self.assertEqual(result["status"], "not_found")

    def test_cancellation_propagates_and_returns_no_candidate(self):
        def stop():
            raise RuntimeError("stop requested")
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            backend.match_candidates(self.target, self.encoded(self.screen), check_active=stop)

    def test_expired_deadline_does_not_scan(self):
        with self.assertRaises(TimeoutError):
            backend.match_candidates(self.target, self.encoded(self.screen), deadline=time.monotonic()-1)

    def test_input_target_is_not_modified(self):
        before = copy.deepcopy(self.target)
        self.match()
        self.assertEqual(before, self.target)

    def test_malformed_png_is_rejected_before_dependency_loading(self):
        with mock.patch.object(backend, "_load", side_effect=AssertionError("must validate first")):
            with self.assertRaises(OperationError):
                backend.match_candidates(self.target, "AAAA")

    def test_recorded_full_window_border_clip_preserves_source_pixel_anchor(self):
        root = Path(__file__).resolve().parent/"test_data"
        template = self.cv2.imread(str(root/"recorded-border-template.png"))
        screen = self.cv2.imread(str(root/"recorded-border-screen.png"))
        h, w = template.shape[:2]
        target = {**self.target, "template_png": self.encoded(template), "width": w, "height": h,
                  "capture_window": {"width": 764, "height": screen.shape[0]},
                  "source_size": {"width": 764, "height": 96}}
        result = validate_match(self.match(screen, target), target, self.encoded(screen))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["template_clip"]["left"], 1)
        self.assertEqual(result["x"], int(764*.8)-1)


if __name__ == "__main__":
    unittest.main()
