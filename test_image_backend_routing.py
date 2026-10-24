"""Backend selection must never turn ambiguity/cancellation into a click target."""
from types import SimpleNamespace
import unittest
from unittest import mock

import airtest_backend
import image_targets
from operations import OperationError
from test_image_steps import image_target, png


class BackendRoutingTests(unittest.TestCase):
    def setUp(self):
        self.target = image_target()
        self.shot = png(100, 100)
        self.runtime = SimpleNamespace(check_active=mock.Mock())
        self.good = {"status": "matched", "score": .99, "candidate_count": 1,
            "rect": {"x": 10, "y": 10, "width": 16, "height": 16}, "screenshot": {"width": 100, "height": 100}}
        self.air = self.enterContext(mock.patch.object(airtest_backend, "match_isolated", return_value=self.good))
        self.native = self.enterContext(mock.patch.object(image_targets, "_native_match", return_value=self.good))
        self.present = self.enterContext(mock.patch.object(airtest_backend, "available", return_value=True))
        self.fingerprint = self.enterContext(mock.patch.object(image_targets, "_backend_fingerprint", return_value="same-build"))

    def match(self, shot=None):
        return image_targets.match_image(self.runtime, self.target, shot or self.shot)

    def test_selection_capability_is_private_explicit_and_part_of_cache_identity(self):
        self.native.return_value = {"status":"not_found"}
        def compare(*args, allow_selection_normalization, **kwargs):
            return self.good if allow_selection_normalization else {"status":"not_found"}
        self.air.side_effect = compare
        self.assertEqual(self.match()["status"],"not_found")
        self.assertEqual(image_targets.match_image(self.runtime,self.target,self.shot,
            allow_selection_normalization=True)["status"],"matched")
        last = self.match()
        self.assertEqual(last["status"],"not_found")
        self.assertTrue(last["search_reused"])
        self.assertEqual(self.air.call_count,2)
        self.assertEqual([call.kwargs['allow_selection_normalization'] for call in self.air.call_args_list],[False,True])
        with self.assertRaises(OperationError):
            image_targets.match_image(self.runtime,self.target,self.shot,allow_selection_normalization='true')

    def test_appearance_rejection_is_preserved_as_bounded_diagnostic(self):
        self.native.return_value = {"status":"not_found"}
        self.air.return_value = {"status":"not_found","score":.85,"appearance_rejected":"foreground_ink_changed"}
        result = self.match()
        self.assertEqual(result['appearance_rejected'],'foreground_ink_changed')
        self.assertEqual(result['score'],.85)

    def test_small_frame_also_prefers_measured_faster_native_and_reports_timing(self):
        result = self.match()
        self.assertEqual(result["backend"], "native-rgb")
        self.assertGreaterEqual(result["search_ms"], 0)
        self.air.assert_not_called()

    def test_large_frame_prefers_native(self):
        self.native.return_value = {**self.good, "screenshot": {"width": 800, "height": 800}}
        result = self.match(png(800, 800))
        self.assertEqual(result["backend"], "native-rgb")
        self.air.assert_not_called()

    def test_airtest_ambiguity_does_not_retry_native_after_its_not_found(self):
        self.native.return_value = {"status": "not_found"}
        self.air.return_value = {"status": "ambiguous", "score": 1, "second_score": 1, "candidate_count": 2}
        self.assertEqual(self.match()["status"], "ambiguous")
        self.native.assert_called_once()

    def test_native_ambiguity_does_not_try_airtest(self):
        self.native.return_value = {"status": "ambiguous", "score": 1, "second_score": 1, "candidate_count": 2}
        self.assertEqual(self.match(png(800, 800))["status"], "ambiguous")
        self.air.assert_not_called()

    def test_incomplete_budget_diagnostic_is_preserved_without_claiming_duplicate_count(self):
        self.native.return_value = {"status": "not_found"}
        self.air.return_value = {"status": "ambiguous", "code": "candidate_budget_exceeded", "score": .932,
            "search_complete": False, "candidate_evaluations": 256, "candidate_budget": 256}
        result = self.match()
        self.assertEqual(result["search_diagnostic"], "candidate_budget_exceeded")
        self.assertFalse(result["search_complete"])
        self.assertEqual(result["score"], .932)
        self.assertEqual(result["candidate_evaluations"], 256)
        self.assertNotIn("candidate_count", result)
        self.assertNotIn("x", result)

    def test_malformed_budget_diagnostic_cannot_reach_target_or_claim_complete_search(self):
        valid = {"status": "ambiguous", "code": "candidate_budget_exceeded", "score": .932,
            "search_complete": False, "candidate_evaluations": 256, "candidate_budget": 256}
        for changed in ({"status": "matched"}, {"search_complete": True}, {"candidate_budget": True},
                        {"candidate_evaluations": 255}, {"candidate_count": 257}):
            with self.subTest(changed=changed), self.assertRaises(OperationError):
                image_targets.validate_match({**valid, **changed}, self.target, self.shot)

    def test_low_detail_does_not_try_another_backend(self):
        self.native.return_value = {"status": "not_found", "code": "template_low_detail"}
        self.assertTrue(self.match()["low_detail"])
        self.air.assert_not_called()

    def test_missing_optional_runtime_keeps_existing_native_functionality(self):
        self.present.return_value = False
        self.assertEqual(self.match()["backend"], "native-rgb")
        self.air.assert_not_called()

    def test_cold_import_failure_falls_back_without_user_configuration(self):
        self.native.return_value = {"status": "not_found"}
        self.air.side_effect = airtest_backend.BackendUnavailable("missing wheel")
        result = self.match()
        self.assertEqual(result["backend"], "native-rgb")
        self.assertEqual(result["backend_fallback"], "image_backend_unavailable")

    def test_invalid_airtest_response_is_not_exposed_as_a_target(self):
        self.native.return_value = {"status": "not_found"}
        self.air.return_value = {**self.good, "rect": {"x": 999, "y": 1, "width": 16, "height": 16}}
        result = self.match()
        self.assertEqual(result["backend"], "native-rgb")
        self.assertEqual(result["backend_fallback"], "image_backend_failed")
        self.assertEqual(result["status"], "not_found")
        self.assertNotIn("x", result)

    def test_cancel_inside_backend_even_valueerror_is_never_recovered(self):
        self.native.return_value = {"status": "not_found"}
        def cancel(*args, check_active, **kwargs):
            self.runtime.check_active.side_effect = ValueError("cancelled")
            check_active()
        self.air.side_effect = cancel
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.match()
        self.native.assert_called_once()

    def test_native_true_not_found_may_try_optional_backend(self):
        self.native.return_value = {"status": "not_found"}
        self.air.return_value = {**self.good, "screenshot": {"width": 800, "height": 800}}
        result = self.match(png(800, 800))
        self.assertEqual(result["backend"], "airtest-template-rgb")
        self.native.assert_called_once()

    def test_native_failure_is_not_misrepresented_as_not_found(self):
        self.native.side_effect = OperationError("invalid native response", "image_match_invalid")
        with self.assertRaises(OperationError):
            self.match(png(800, 800))
        self.air.assert_not_called()

    def test_failed_optional_fallback_retains_native_not_found(self):
        self.native.return_value = {"status": "not_found"}
        self.air.side_effect = airtest_backend.BackendUnavailable("missing wheel")
        result = self.match(png(800, 800))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["backend"], "native-rgb")

    def test_identical_capture_reuses_search_but_backend_manifest_change_invalidates_it(self):
        self.assertFalse(self.match()["search_reused"])
        self.assertTrue(self.match()["search_reused"])
        self.assertEqual(self.native.call_count, 1)
        self.fingerprint.return_value = "updated-runtime-manifest"
        self.assertFalse(self.match()["search_reused"])
        self.assertEqual(self.native.call_count, 2)

    def test_a_changed_capture_is_always_searched_again(self):
        self.match()
        self.native.return_value = {"status": "ambiguous", "candidate_count": 2}
        result = self.match(png(101, 100))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(self.native.call_count, 2)


if __name__ == "__main__":
    unittest.main()
