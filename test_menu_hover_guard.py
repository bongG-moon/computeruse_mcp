"""Semantic hover has native image provenance and never clicks the desktop."""
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import transient_popup as popup
import test_image_steps as image_tests
import test_native_pointer as pointer_tests
from vendor.guard import GuardError

TARGET = image_tests.TARGET


class MenuHoverGuardTests(unittest.TestCase):
    setUp = image_tests.ImageGuardTests.setUp
    mutations = image_tests.ImageGuardTests.mutations

    def run_hover(self, *, response=None, proven=True, changed_backend=False,
                  changed_proof=False, capture_failure=False, check=None,
                  parent_bounds=None, parent_revalidate=None, paired=False):
        captures = 0
        proof = {"process": (1, "Editor.exe"), "path": [200, 1],
                 "window": {"thread_id": 7}, "bounds": self.geometry[:4]}
        def capture(target, geometry):
            nonlocal captures
            captures += 1
            if capture_failure:
                return {"isError": True, "structuredContent": {"code": "capture_refused"}}
            width, height = geometry[2]-geometry[0], geometry[3]-geometry[1]
            return {"structuredContent": {**target, "backend": (
                "cua-driver" if changed_backend and captures > 1 else "owned-popup-printwindow"),
                "screenshot_width": width, "screenshot_height": height},
                "content": [{"type": "image", "mimeType": "image/png", "data": image_tests.png(width, height)}]}
        def current_proof(_):
            answer = copy.deepcopy(proof)
            if changed_proof and captures > 1:
                answer["process"] = (2, "Editor.exe")
            return answer
        response = response or {"structuredContent": {"backend": "owned-popup-messages",
            "input_sent": True, "task_verified": False, "delivery_phase": "move", "release_delivery": "not_queued"}}
        with mock.patch.object(popup, "proven_popup", return_value=proven), \
                mock.patch.object(popup, "capture", side_effect=capture), \
                mock.patch.object(popup, "popup_proof", side_effect=current_proof), \
                mock.patch.object(popup, "hover", return_value=response) as hover, \
                mock.patch.object(popup, "click") as click:
            result = self.guard.expand_menu_once({"operation": "expand_menu", "expansion_method": "hover",
                "image_target": self.step["image_target"],
                **({"selector": {"role": "MenuItem", "name": "Export"}} if paired else {})},
                TARGET, self.matcher, check or (lambda: None), parent_bounds=parent_bounds,
                parent_revalidate=parent_revalidate)
        click.assert_not_called()
        return result, hover, captures

    def test_native_image_pairs_hover_without_driver_input_or_click(self):
        result, hover, captures = self.run_hover()
        hover.assert_called_once()
        self.assertEqual(hover.call_args.args[:3], (TARGET, {"x": 30, "y": 40}, self.geometry))
        self.assertEqual(captures, 2)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.guard.action_count, 1)
        self.assertEqual(result["operation"], "expand_menu")
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["native_delivery"]["delivery_phase"], "move")
        rows = [json.loads(line) for line in (self.guard.run_dir/"actions.jsonl").read_text().splitlines()]
        self.assertTrue(any(row.get("tool") == "expand_menu" and row.get("event") == "request" for row in rows))
        self.assertFalse(any(row.get("tool") == "click" for row in rows))

    def test_changed_capture_backend_or_lifetime_blocks_movement(self):
        for values in ({"changed_backend": True}, {"changed_proof": True}):
            with self.subTest(values=values):
                result, hover, _ = self.run_hover(**values)
                hover.assert_not_called()
                self.assertFalse(result["input_dispatched"])

    def test_geometry_change_before_input_blocks_movement(self):
        def compare(*_):
            self.geometry = (1, 0, 101, 100, 2)
            return self.match
        self.matcher.side_effect = compare
        result, hover, _ = self.run_hover()
        hover.assert_not_called()
        self.assertFalse(result["input_dispatched"])

    def test_budget_and_approval_still_apply(self):
        self.guard.action_count = self.guard.policy["max_actions"]
        result, hover, _ = self.run_hover()
        hover.assert_not_called()
        self.assertFalse(result["input_dispatched"])
        self.guard.action_count = 0
        self.guard.approve = mock.Mock(side_effect=GuardError("denied"))
        result, hover, _ = self.run_hover()
        hover.assert_not_called()
        self.assertFalse(result["input_dispatched"])
        self.guard.approve.assert_called_once()
        self.assertEqual(self.guard.approve.call_args.args[0], "expand_menu")

    def test_ambiguous_image_and_capture_refusal_send_nothing(self):
        self.match["status"] = "ambiguous"
        result, hover, _ = self.run_hover()
        hover.assert_not_called()
        self.assertEqual(result["diagnostic"]["code"], "image_ambiguous")
        self.match["status"] = "matched"
        result, hover, _ = self.run_hover(capture_failure=True)
        hover.assert_not_called()
        self.assertFalse(result["input_dispatched"])

    def test_unproven_main_is_explicitly_unsupported(self):
        result, hover, captures = self.run_hover(proven=False)
        hover.assert_not_called()
        self.assertEqual(captures, 0)
        self.assertEqual(result["diagnostic"]["code"], "menu_expand_method_unsupported")
        self.assertFalse(result["input_dispatched"])

    def test_unknown_input_is_never_retried_or_reported_as_sent(self):
        result, hover, _ = self.run_hover(response={"isError": True, "structuredContent": {
            "backend": "owned-popup-messages", "input_sent": None, "task_verified": False,
            "code": "popup_input_unknown", "delivery_phase": "move", "release_delivery": "not_queued"}})
        hover.assert_called_once()
        self.assertIsNone(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["verification_deferred"])
        self.assertEqual(self.transport.calls, [])

    def test_public_tool_cannot_request_hover_and_proof_is_consumed(self):
        result, _, _ = self.run_hover()
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(self.guard.observed_targets)
        self.assertTrue(self.guard.call("expand_menu", {**TARGET, "x": 1, "y": 1})["isError"])
        self.assertTrue(self.guard._dispatch_menu_hover(TARGET, {"x": 1, "y": 1})["isError"])

    def test_stop_before_input_sends_nothing(self):
        self.transport.close = mock.Mock()
        (self.guard.run_dir/"stop.flag").touch()
        result, hover, _ = self.run_hover()
        hover.assert_not_called()
        self.assertFalse(result["input_dispatched"])

    def test_target_pid_change_blocks_capture_and_hover(self):
        self.owner = TARGET["pid"] + 1
        result, hover, captures = self.run_hover()
        hover.assert_not_called()
        self.assertEqual(captures, 0)
        self.assertFalse(result["input_dispatched"])

    def test_dpi_and_moved_popup_keep_physical_point(self):
        self.geometry = (300, 200, 569, 299, 2)
        self.match.update(x=112, y=50, screenshot={"width": 269, "height": 99})
        result, hover, _ = self.run_hover()
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(hover.call_args.args[1:3], ({"x": 112, "y": 50}, self.geometry))

    def test_logical_hover_requires_fresh_parent_bounds_callback(self):
        result, hover, captures = self.run_hover(paired=True)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_bounds_unverified")
        self.assertFalse(result["input_dispatched"])
        hover.assert_not_called()
        self.assertEqual(captures, 0)

    def test_logical_hover_matches_same_physical_menuitem_after_approval(self):
        self.geometry = (300, 200, 400, 300, 2)
        bounds = {"x": 320, "y": 235, "width": 30, "height": 20}
        revalidate = mock.Mock(return_value=dict(bounds))
        result, hover, _ = self.run_hover(paired=True, parent_bounds=bounds, parent_revalidate=revalidate)
        self.assertTrue(result["input_dispatched"])
        hover.assert_called_once()
        revalidate.assert_called_once()

    def test_parent_menuitem_image_point_mismatch_blocks_hover(self):
        bounds = {"x": 0, "y": 0, "width": 10, "height": 10}
        result, hover, _ = self.run_hover(paired=True, parent_bounds=bounds,
            parent_revalidate=lambda: dict(bounds))
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_image_mismatch")
        hover.assert_not_called()

    def test_parent_menuitem_replacement_blocks_hover(self):
        bounds = {"x": 20, "y": 30, "width": 30, "height": 20}
        result, hover, _ = self.run_hover(paired=True, parent_bounds=bounds,
            parent_revalidate=lambda: {**bounds, "x": 21})
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_changed")
        hover.assert_not_called()


class NativeMenuHoverTests(unittest.TestCase):
    def run_hover(self, **kwargs):
        # Reuse the established fake Win32 fixture, never touch real desktop.
        with mock.patch.object(popup, "click", popup.hover):
            return pointer_tests.NativePointerTests.run_click(self, **kwargs)

    def test_move_only_never_down_up(self):
        result, calls, user = self.run_hover()
        self.assertEqual([row[0] for row in calls], ["pointer", "move"])
        self.assertEqual(user.SendMessageTimeoutW.call_count, 1)
        user.PostMessageW.assert_not_called()
        self.assertTrue(result["structuredContent"]["input_sent"])
        self.assertFalse(result["structuredContent"]["task_verified"])
        self.assertEqual(result["structuredContent"]["operation"], "expand_menu")

    def test_occluded_point_blocks_pointer_and_messages(self):
        result, calls, _ = self.run_hover(occluded=True)
        self.assertEqual(calls, [])
        self.assertFalse(result["structuredContent"]["input_sent"])

    def test_pointer_failure_sends_no_move(self):
        result, calls, _ = self.run_hover(align=False)
        self.assertEqual([row[0] for row in calls], ["pointer"])
        self.assertFalse(result["structuredContent"]["input_sent"])

    def test_pointer_may_expand_before_identity_change_never_retry(self):
        result, calls, _ = self.run_hover(changed_after_pointer=True)
        self.assertEqual([row[0] for row in calls], ["pointer"])
        self.assertIsNone(result["structuredContent"]["input_sent"])

    def test_move_timeout_is_unknown_and_never_clicks(self):
        result, calls, user = self.run_hover(fail_phase="move")
        self.assertEqual([row[0] for row in calls], ["pointer", "move"])
        self.assertIsNone(result["structuredContent"]["input_sent"])
        user.PostMessageW.assert_not_called()


class RuntimeMenuHoverTests(unittest.TestCase):
    def test_runtime_uses_guard_and_preserves_unknown_delivery(self):
        from session_runtime import SessionRuntime
        receipt = {"operation": "expand_menu", "input_dispatched": None, "task_verified": False}
        runtime = SimpleNamespace(execution_lock=threading.RLock(), check_active=mock.Mock(),
            report_progress=mock.Mock(), guard=mock.Mock(), _driver_ended=lambda: False, stop=mock.Mock())
        runtime.guard.expand_menu_once.return_value = receipt
        step = {"operation": "expand_menu", "expansion_method": "hover", "image_target": image_tests.image_target()}
        answer = SessionRuntime.expand_menu_once(runtime, TARGET, step)
        self.assertIs(answer, receipt)
        self.assertEqual(runtime.guard.expand_menu_once.call_args.args[:2], (step, TARGET))
        runtime.report_progress.assert_called_once_with("acting")

    def test_unsupported_method_does_not_reach_image_or_native(self):
        case = MenuHoverGuardTests()
        case.setUp()
        try:
            answer = case.guard.expand_menu_once({"operation": "expand_menu", "expansion_method": "accessibility"},
                TARGET, case.matcher, lambda: None)
            self.assertEqual(answer["diagnostic"]["code"], "menu_expand_method_unsupported")
            self.assertFalse(answer["input_dispatched"])
            case.matcher.assert_not_called()
            self.assertEqual(case.transport.calls, [])
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
