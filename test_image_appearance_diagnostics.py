"""Per-call scalar evidence preserves image safety and local privacy boundaries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import airtest_backend as backend
import image_targets
from local_delivery import receipt
from local_result_details import project_appearance_diagnostic, project_local_diagnostics
from test_image_steps import image_target, png
import test_menu_appearance as menu_fixture
import test_hover_matching as hover_fixture


@unittest.skipUnless(backend.available(), "Optional offline image runtime is not bundled")
class ImageAppearanceDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv, cls.np, _ = backend._load()
        menu_fixture.MenuAppearanceTests.setUpClass()
        hover_fixture.HoverTests.setUpClass()

    def setUp(self):
        self.menu = menu_fixture.MenuAppearanceTests()
        self.hover = hover_fixture.HoverTests()

    def diagnostic(self, a, b, anchor=.5):
        return backend._ink_diagnostics(a, b, anchor, allow_selection_normalization=True)

    def test_shape_and_insufficient_ink_have_distinct_reasons(self):
        blank = self.np.full((32, 80, 3), 255, self.np.uint8)
        self.assertEqual(self.diagnostic(blank, blank[:20]),
                         {"accepted": False, "reason": "shape_mismatch", "scope": "whole_template"})
        result = self.diagnostic(blank, blank)
        self.assertEqual(result["reason"], "insufficient_ink")
        self.assertEqual(result["whole_template"]["ink_pixels"], 0)
        self.assertEqual(result["clicked_band"]["minimum_ink_pixels"], 12)

    def test_color_changes_report_counts_without_pixels_or_labels(self):
        a, b = self.menu.menu(), self.menu.menu(selected=1)
        self.cv.rectangle(a, (90,75), (100,85), (180,60,20), -1)
        self.cv.rectangle(b, (90,75), (100,85), (20,60,180), -1)
        result = self.diagnostic(a, b)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "colored_ink_changed")
        self.assertEqual(result["changed_color_pixels"], 121)
        self.assertEqual(result["scope"], "whole_template")
        self.assertEqual(result["whole_template"]["minimum_dice"], .98)
        self.assertEqual(result["clicked_band"]["minimum_dice"], .95)
        self.assertEqual(project_appearance_diagnostic(result)["changed_color_pixels"], 121)

    def test_changed_glyph_reports_missing_ink_and_both_region_metrics(self):
        result = self.diagnostic(self.menu.menu(), self.menu.menu(selected=1, labels=('Y','P','P')))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "missing_ink")
        self.assertGreaterEqual(result["missing_ink_pixels"], 2)
        for scope in ("whole_template", "clicked_band"):
            for key in ("dice", "mean_difference", "correlation", "contrast_difference"):
                self.assertIn(key, result[scope])

    def test_clicked_band_is_reported_when_whole_template_passes(self):
        a = self.np.full((220, 269, 3), 255, self.np.uint8)
        for y in (22, 44, 66, 88, 154, 176, 198, 219):
            self.cv.putText(a, 'Surrounding context', (5,y), self.cv.FONT_HERSHEY_SIMPLEX,.6,(20,20,20),1)
        self.cv.putText(a, 'N', (12,115), self.cv.FONT_HERSHEY_SIMPLEX,.6,(20,20,20),1)
        b = a.copy()
        b[99:120][b[99:120] == 20] = 70
        result = self.diagnostic(a,b)
        self.assertTrue(result["whole_template"]["accepted"])
        self.assertFalse(result["clicked_band"]["accepted"])
        self.assertEqual((result["reason"],result["scope"]), ("ink_metrics_changed","clicked_band"))
        self.assertGreater(result["clicked_band"]["contrast_difference"],40)

    def test_hover_selection_disabled_and_changed_glyph_keep_expected_acceptance(self):
        fixtures = [
            (self.hover.image(), self.hover.image(background=(240,220,200)), True),
            (self.menu.menu(), self.menu.menu(selected=1), True),
            (self.menu.menu(), self.menu.menu(selected=1, labels=('Y','P','P')), False),
            (self.menu.menu(), self.menu.menu(selected=1, ink=160), False),
            (self.menu.menu(labels=('State',)), self.menu.menu(selected=0,labels=('State',)), False),
        ]
        for a,b,accepted in fixtures:
            with self.subTest(accepted=accepted, shape=a.shape):
                result = self.diagnostic(a,b)
                self.assertIs(result["accepted"], accepted)
                self.assertIs(backend._ink_agrees(a,b,.5,allow_selection_normalization=True),accepted)
        self.assertEqual(self.hover.match(self.hover.image(background=(240,220,200)),duplicate=True)["status"],"ambiguous")

    def test_success_and_rejection_keep_original_and_normalized_scores(self):
        a = self.hover.image()
        b = self.hover.image('Export.', background=(240,220,200))
        target = {"format":"computer-image-target/v1", "template_png":self.hover.png(a),
                  "width":160,"height":32,"anchor":{"x":.4,"y":.5},
                  "capture_window":{"width":160,"height":32},"min_score":.94,"ambiguity_margin":.03}
        # Isolate the existing unique-candidate gate to exercise its subsequent
        # ink rejection, independent of alternate-scale duplicate enumeration.
        original = {"status":"not_found","score":.910771,"screenshot":{"width":160,"height":32}}
        candidate = {"status":"matched","score":.99,"rect":{"x":0,"y":0,"width":160,"height":32},
                     "screenshot":{"width":160,"height":32},"candidate_count":1}
        with mock.patch.object(backend,"_rgb_candidates",side_effect=[original,candidate]):
            answer = backend.match_candidates(target,self.hover.png(b))
        self.assertEqual(answer["status"],"not_found")
        self.assertEqual(answer["appearance_rejected"],"foreground_ink_changed")
        self.assertFalse(answer["appearance_diagnostic"]["accepted"])
        validated = image_targets.validate_match(answer,target,self.hover.png(b))
        self.assertEqual(validated["original_score"],.910771)
        self.assertEqual(validated["appearance_score"],.99)
        self.assertNotIn("rect",validated)
        self.assertNotIn("x",validated)
        success = self.hover.match(self.hover.image(background=(240,220,200)))
        self.assertTrue(success["appearance_diagnostic"]["accepted"])
        self.assertLess(success["original_score"],.94)
        self.assertGreaterEqual(success["appearance_score"],.94)

    def test_concurrent_calls_keep_their_own_reasons_and_metrics(self):
        a = self.menu.menu()
        inputs = [self.menu.menu(selected=1),self.menu.menu(selected=1,labels=('Y','P','P')),
                  self.menu.menu(selected=1,ink=160)]
        expected = [self.diagnostic(a,b) for b in inputs]
        with ThreadPoolExecutor(max_workers=3) as pool:
            actual = list(pool.map(lambda b:self.diagnostic(a,b), inputs*3))
        self.assertEqual(actual,expected*3)
        actual[0]["whole_template"]["dice"] = -99
        self.assertNotEqual(actual[3]["whole_template"]["dice"],-99)
        self.assertEqual(self.diagnostic(a,inputs[0]),expected[0])


class LocalImageProjectionTests(unittest.TestCase):
    def evidence(self):
        return {"status":"not_found","score":.910771,"threshold":.94,"original_score":.910771,
                "appearance_score":.99,"appearance_rejected":"foreground_ink_changed",
                "backend":"airtest-template-rgb","appearance_diagnostic": {
                    "accepted":False,"reason":"missing_ink","scope":"whole_template",
                    "missing_ink_pixels":3,"changed_color_pixels":0,
                    "whole_template":{"dice":.97,"correlation":.98,"mean_difference":12.5,"contrast_difference":17}}}

    def test_parent_precondition_and_wait_nested_evidence_are_projected(self):
        match = self.evidence()
        raw = {"last_result":{"menu_expansion":{"stage":"parent_observation",
                  "menu_path":["PRIVATE MENU"],"observation":{"parent_image_match":{
                  "diagnostic":{"image_match":match}}}}}}
        clean = project_local_diagnostics(raw)["image_comparison"]
        self.assertEqual((clean["source"],clean["stage"]),("parent_image_match","parent_precondition"))
        self.assertEqual(clean["appearance_diagnostic"]["missing_ink_pixels"],3)
        wait = {"diagnostic":{"last_match":match,"cause":{"message":"PRIVATE"}}}
        self.assertEqual(project_local_diagnostics(wait)["image_comparison"]["source"],"last_match")

    def test_child_stage_prefers_child_failure_over_earlier_parent_success(self):
        raw = {"menu_expansion":{"stage":"child_observation","observation":{
               "parent_image_match":{"image_match":{"status":"matched","score":1}},
               "child_image_match":{"diagnostic":{"image_match":self.evidence()}}}}}
        clean = project_local_diagnostics(raw)["image_comparison"]
        self.assertEqual((clean["source"],clean["stage"],clean["status"]),
                         ("child_image_match","child_observation","not_found"))

    def test_projection_drops_business_content_unknown_enums_and_nonfinite_values(self):
        value = self.evidence()
        value.update(template_png="PRIVATE",message="PRIVATE",path="C:/PRIVATE",rect={"x":12},
                     backend="PRIVATE",candidate_count=True,second_score=float('nan'))
        value["appearance_diagnostic"]["whole_template"].update(secret="PRIVATE",dice=float('inf'))
        before = copy.deepcopy(value)
        clean = project_local_diagnostics({"image_match":value})
        encoded = json.dumps(clean,allow_nan=False)
        self.assertNotIn("PRIVATE",encoded)
        self.assertNotIn("candidate_count",encoded)
        self.assertNotIn("second_score",encoded)
        self.assertNotIn('"rect"',encoded)
        self.assertEqual(value["message"],before["message"])
        self.assertNotIn("image_comparison",receipt({"status":"failed",**clean}))

    def test_unknown_nested_paths_cycles_and_excess_depth_are_bounded(self):
        self.assertEqual(project_local_diagnostics({"unapproved":{"image_match":self.evidence()}}),{})
        cycle = {}; cycle["diagnostic"] = cycle
        self.assertEqual(project_local_diagnostics(cycle),{})
        value = {"image_match":self.evidence()}
        for _ in range(12): value = {"cause":value}
        self.assertEqual(project_local_diagnostics(value),{})

    def test_match_image_validates_diagnostics_and_cache_copies_them(self):
        runtime = SimpleNamespace(check_active=lambda:None)
        with mock.patch.object(image_targets,"_backend_fingerprint",return_value="fixture"), \
                mock.patch.object(image_targets,"_native_match",return_value={"status":"not_found"}), \
                mock.patch.object(backend,"available",return_value=True), \
                mock.patch.object(backend,"match_isolated",return_value=self.evidence()):
            first = image_targets.match_image(runtime,image_target(),png(100,100))
            first["appearance_diagnostic"]["missing_ink_pixels"] = 100
            second = image_targets.match_image(runtime,image_target(),png(100,100))
        self.assertTrue(second["search_reused"])
        self.assertEqual(second["appearance_diagnostic"]["missing_ink_pixels"],3)
        self.assertEqual(second["appearance_score"],.99)


if __name__ == "__main__":
    unittest.main()
