"""Message ordering and uncertain delivery without touching the desktop."""
import unittest
from unittest import mock

import transient_popup as popup
from test_image_steps import TARGET


class NativePointerTests(unittest.TestCase):
    def run_click(self, *, occluded=False, align=True, fail_phase=None, changed_after_move=False,
                  changed_after_pointer=False, stopped_after_down=False):
        user = mock.Mock()
        user.SetThreadDpiAwarenessContext.return_value = 0
        user.ScreenToClient.return_value = True
        user.ChildWindowFromPointEx.return_value = 300
        user.WindowFromPoint.return_value = 999 if occluded else 300
        user.IsWindowEnabled.return_value = True
        calls, state = [], {"moved": False, "aligned": False, "down": False}
        def thread(_, pid): pid._obj.value = TARGET["pid"]; return 7
        user.GetWindowThreadProcessId.side_effect = thread
        def pointer(x, y):
            calls.append(("pointer", x, y)); state["aligned"] = True
            return align
        user.SetCursorPos.side_effect = pointer
        def sync(child, message, flags, packed, options, timeout, value):
            phase = "move" if message == 0x200 else "down"
            calls.append((phase, child, packed)); state["moved"] = True
            if phase == "down": state["down"] = True
            return fail_phase != phase
        user.SendMessageTimeoutW.side_effect = sync
        def queued(child, message, flags, packed):
            calls.append(("up", child, packed))
            return fail_phase != "up"
        user.PostMessageW.side_effect = queued
        probe = mock.Mock()
        probe.ownership_path.side_effect = lambda child, owner: [300, 200] if child == 300 and owner == 200 else None
        proof = {"bounds": (0, 0, 100, 100)}
        def binding(_):
            changed = ((changed_after_move and state["moved"]) or (changed_after_pointer and state["aligned"]))
            return {}, {"bounds": (1, 0, 101, 100)} if changed else dict(proof)
        def check():
            if stopped_after_down and state["down"]: raise RuntimeError("cancelled")
        with mock.patch.object(popup, "_user", return_value=user), \
                mock.patch.object(popup, "_binding", side_effect=binding), \
                mock.patch("closing.NativeClosureProbe", return_value=probe), \
                mock.patch("privileges.input_integrity_preflight", return_value=None):
            result = popup.click(TARGET, {"x": 30, "y": 40}, (0, 0, 100, 100, 2), check)
        return result, calls, user

    def test_pointer_and_move_precede_click_at_same_point(self):
        result, calls, user = self.run_click()
        self.assertEqual([row[0] for row in calls], ["pointer", "move", "down", "up"])
        self.assertEqual(calls[0][1:], (30, 40))
        self.assertTrue(all(row[1:] == (300, 30 | (40 << 16)) for row in calls[1:]))
        self.assertEqual(result["structuredContent"]["point_evidence"]["coordinate_space"], "physical_pixels")

    def test_release_is_queued_once_without_waiting_for_command(self):
        result, calls, user = self.run_click()
        self.assertEqual(user.PostMessageW.call_count, 1)
        self.assertEqual(user.SendMessageTimeoutW.call_count, 2)
        self.assertEqual(result["structuredContent"]["release_delivery"], "queued_once")
        self.assertTrue(result["structuredContent"]["input_sent"])
        self.assertFalse(result["structuredContent"]["task_verified"])

    def test_failed_release_queue_is_uncertain_and_never_retried(self):
        result, calls, user = self.run_click(fail_phase="up")
        self.assertIsNone(result["structuredContent"]["input_sent"])
        self.assertEqual(result["structuredContent"]["delivery_phase"], "up")
        self.assertEqual(user.PostMessageW.call_count, 1)

    def test_occluded_point_blocks_pointer_and_click(self):
        result, calls, user = self.run_click(occluded=True)
        self.assertEqual(calls, [])
        self.assertEqual(result["structuredContent"]["code"], "popup_point_occluded")
        self.assertFalse(result["structuredContent"]["input_sent"])

    def test_failed_pointer_alignment_sends_no_click(self):
        result, calls, user = self.run_click(align=False)
        self.assertEqual([row[0] for row in calls], ["pointer"])
        self.assertFalse(result["structuredContent"]["input_sent"])

    def test_uncertain_move_does_not_retry_or_click(self):
        result, calls, user = self.run_click(fail_phase="move")
        self.assertEqual([row[0] for row in calls], ["pointer", "move"])
        self.assertIsNone(result["structuredContent"]["input_sent"])
        self.assertEqual(result["structuredContent"]["delivery_phase"], "move")

    def test_hover_replacement_after_pointer_blocks_every_message(self):
        result, calls, user = self.run_click(changed_after_pointer=True)
        self.assertEqual([row[0] for row in calls], ["pointer"])
        self.assertFalse(result["structuredContent"]["input_sent"])

    def test_hover_replacement_after_move_blocks_down(self):
        result, calls, user = self.run_click(changed_after_move=True)
        self.assertEqual([row[0] for row in calls], ["pointer", "move"])
        self.assertIsNone(result["structuredContent"]["input_sent"])

    def test_cancel_after_down_never_queues_release_or_retries(self):
        result, calls, user = self.run_click(stopped_after_down=True)
        self.assertEqual([row[0] for row in calls], ["pointer", "move", "down"])
        self.assertIsNone(result["structuredContent"]["input_sent"])
