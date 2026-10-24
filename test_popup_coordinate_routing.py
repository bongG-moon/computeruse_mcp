"""Native physical captures cannot be fed to Driver resize coordinates."""
import copy
import json
import unittest
from unittest import mock

import transient_popup as popup
import test_image_steps as image_tests
from test_transient_popup import missing
from vendor.guard import Guard, GuardError

TARGET, png = image_tests.TARGET, image_tests.png

class PopupCoordinateRoutingTests(unittest.TestCase):
    setUp = image_tests.ImageGuardTests.setUp
    run_step = image_tests.ImageGuardTests.run_step
    mutations = image_tests.ImageGuardTests.mutations

    def native_capture(self, *, failure=None, backend_change=False, changed_proof=False):
        captures = 0
        proof = {"process": (123, "Editor.exe"), "window": {"window_id": 200}, "path": [200, 1],
                 "bounds": self.geometry[:4], "foreground": 200}
        def driver(request, result):
            nonlocal captures
            if request["name"] == "get_window_state":
                captures += 1
                if not (backend_change and captures > 1): result.clear(); result.update(missing())
        self.transport.after = driver
        def capture(target, geometry):
            width, height = geometry[2]-geometry[0], geometry[3]-geometry[1]
            return {"structuredContent": {**target, "backend": "owned-popup-printwindow",
                    "screenshot_width": width, "screenshot_height": height},
                    "content": [{"type": "image", "mimeType": "image/png", "data": png(width, height)}]}
        def current_proof(_):
            return {**proof, "process": (999, "Editor.exe")} if changed_proof and captures > 1 else copy.deepcopy(proof)
        receipt = {"backend": "owned-popup-messages", "input_sent": True, "task_verified": False,
                   "delivery_phase": "up", "release_delivery": "queued_once", "point_evidence": {
                       "capture_point": {"x": 30, "y": 40}, "screen_point": {"x": 30, "y": 40},
                       "client_point": {"x": 30, "y": 40}, "child_window_id": 300,
                       "coordinate_space": "physical_pixels", "driver_resize_applied": False,
                       "pointer_aligned": True}}
        answer = {"structuredContent": receipt} if failure is None else failure
        with mock.patch.object(popup, "capture", side_effect=capture), \
                mock.patch.object(popup, "popup_proof", side_effect=current_proof), \
                mock.patch.object(popup, "click", return_value=answer) as click:
            result = self.run_step()
        return result, click

    def test_native_capture_pairs_unscaled_click_without_driver_input(self):
        result, click = self.native_capture()
        self.assertEqual(self.mutations(), [])
        click.assert_called_once()
        self.assertEqual(click.call_args.args[:3], (TARGET, {"x": 30, "y": 40}, self.geometry))
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["native_delivery"]["release_delivery"], "queued_once")
        journal = [json.loads(row) for row in (self.guard.run_dir / "actions.jsonl").read_text().splitlines()]
        self.assertTrue(any(row.get("summary", {}).get("native_delivery", {}).get("release_delivery") == "queued_once"
                            for row in journal))
        self.assertEqual(self.guard.action_count, 1)

    def test_changed_capture_backend_blocks_input(self):
        result, click = self.native_capture(backend_change=True)
        click.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(result["input_dispatched"])

    def test_popup_pixel_point_is_not_scaled_by_owner_capture_ratio(self):
        self.geometry = (300, 200, 569, 299, 2)
        self.match.update(x=112, y=50, screenshot={"width": 269, "height": 99})
        result, click = self.native_capture()
        self.assertEqual(click.call_args.args[1], {"x": 112, "y": 50})
        self.assertEqual(click.call_args.args[2], self.geometry)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(result["native_delivery"]["capture_backend"], "owned-popup-printwindow")

    def test_replaced_window_lifetime_blocks_input(self):
        result, click = self.native_capture(changed_proof=True)
        click.assert_not_called()
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.mutations(), [])

    def test_geometry_change_blocks_all_input(self):
        def match(*_):
            self.geometry = (1, 0, 101, 100, 2)
            return self.match
        self.matcher.side_effect = match
        result, click = self.native_capture()
        click.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(result["input_dispatched"])

    def test_budget_blocks_native_input(self):
        self.guard.action_count = self.policy["max_actions"]
        result, click = self.native_capture()
        click.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(result["input_dispatched"])

    def test_denied_approval_blocks_native_input(self):
        self.guard.approve = mock.Mock(side_effect=GuardError("denied"))
        result, click = self.native_capture()
        click.assert_not_called()
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.guard.action_count, 0)

    def test_native_uncertainty_is_never_retried(self):
        result, click = self.native_capture(failure={"isError": True, "structuredContent": {
            "backend": "owned-popup-messages", "input_sent": None, "code": "popup_input_unknown",
            "delivery_phase": "up", "release_delivery": "not_queued", "task_verified": False}})
        click.assert_called_once()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(result["diagnostic"]["code"], "image_input_unconfirmed")
        self.assertFalse(result["verification_deferred"])
        self.assertIsNone(result["native_delivery"]["input_sent"])

    def test_driver_uncertainty_never_falls_back(self):
        def driver(request, result):
            if request["name"] == "click":
                result.clear(); result.update({"isError": True, "structuredContent": {
                    "code": "tool_invocation_failed", "diagnostic_code": "target_denied",
                    "message": "foreground_unavailable", "input_sent": None}})
        self.transport.after = driver
        with mock.patch.object(popup, "click") as click:
            result = self.run_step()
        click.assert_not_called()
        self.assertEqual(len(self.mutations()), 1)
        self.assertFalse(result["verification_deferred"])

    def test_public_click_cannot_choose_native_backend_and_observation_is_consumed(self):
        policy = {**self.policy, "mode": "visual"}
        guard = Guard(policy, transport=self.transport, process_resolver=self.guard.process_resolver,
                      window_resolver=self.guard.window_resolver)
        guard.call("get_window_state", TARGET)
        guard._native_popup_frame = (*TARGET.values(), self.geometry, {})
        with mock.patch.object(popup, "click") as click:
            guard.call("click", {**TARGET, "x": 30, "y": 40})
            again = guard.call("click", {**TARGET, "x": 30, "y": 40})
        click.assert_not_called()
        self.assertEqual(len(self.mutations()), 1)
        self.assertTrue(again["isError"])
        self.assertIsNone(guard._native_popup_frame)

    def test_failed_observation_invalidates_native_frame(self):
        self.guard._native_popup_frame = (*TARGET.values(), self.geometry, {})
        answer = self.guard.call("get_window_state", {"pid": -1, "window_id": 200})
        self.assertTrue(answer["isError"])
        self.assertIsNone(self.guard._native_popup_frame)
