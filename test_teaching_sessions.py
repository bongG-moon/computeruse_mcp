"""Public direct teaching lifecycle; no desktop or business-app input."""
import copy
import gc
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from operations import OperationError
from server import ComputerManager
from test_learning import TARGET
from test_learning_tools import MutableRuntime, FIELD_SELECTOR
from test_server import config_at


def native_choice():
    return {"status": "selected", "human_confirmed": True, **TARGET,
            "element": {"role": "Edit", "automation_id": "applicant", "name": "신청자", "is_password": False},
            "ancestors": [], "point": {"x": 10, "y": 10},
            "bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}


class AsyncTeachingTests(unittest.TestCase):
    def setUp(self):
        # Earlier hidden Tk widget tests leave collectible Variable cycles.
        # Collect them on the test/main thread before starting a worker: Tcl
        # finalizers on a worker wait for a mainloop these tests do not run.
        # The production stdio MCP process never constructs those Tk fixtures.
        gc.collect()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = config_at(self.folder.name)
        self.manager = ComputerManager(self.config)
        self.runtime = MutableRuntime(self.config)
        self.runtime.run_dir = Path(self.folder.name)
        self.manager.session = self.runtime
        self.release = threading.Event()
        self.addCleanup(self.manager.teachings.close)
        self.addCleanup(self.release.set)
        self.args = {**TARGET, "program_id": "editor", "label": "신청자 입력"}

    def helper(self, runtime, target, label, timeout, *, on_ready, cancel_event):
        on_ready({"helper_pid": 100, "helper_window_id": 200})
        while not self.release.wait(.005):
            if cancel_event.is_set() or runtime.stop_event.is_set():
                raise OperationError("cancelled", "picker_cancelled")
        return native_choice()

    def start(self, **extra):
        return self.manager.call("computer_teach_element", {**self.args, **extra})["structuredContent"]

    def status(self, job, **extra):
        return self.manager.call("computer_teach_status", {"teaching_id": job["teaching_id"], **extra})["structuredContent"]

    def terminal(self, job, *, cancel=False):
        deadline = time.monotonic() + 10
        answer = self.status(job, cancel=cancel, wait_ms=100)
        while answer["status"] in {"starting", "awaiting_selection", "verifying_selection", "cancelling"} and time.monotonic() < deadline:
            answer = self.status(job, wait_ms=100)
        self.assertIn(answer["status"], {"learned", "cancelled", "learning_failed"}, answer)
        return answer

    def test_visible_pending_returns_before_selection_without_any_uia_read(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            self.assertEqual(job["status"], "awaiting_selection")
            self.assertTrue(job["picker_visible"])
            self.assertEqual(self.runtime.calls, [])
            self.assertEqual(self.manager.elements.all(), [])
            self.release.set()
            done = self.terminal(job)
        self.assertEqual(done["status"], "learned")
        self.assertEqual(done["selector"], FIELD_SELECTOR)
        self.assertEqual(self.runtime.observation_count, 1)
        self.assertEqual(self.runtime.mutations, [])

    def test_duplicate_and_different_requests_cannot_open_another_picker(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper) as helper:
            job = self.start()
            again = self.start()
            other = self.start(label="different")
            self.assertEqual(job["teaching_id"], again["teaching_id"])
            self.assertTrue(again["reused"])
            self.assertEqual(job["teaching_id"], other["teaching_id"])
            self.assertTrue(other["busy"])
            self.assertEqual(helper.call_count, 1)
            self.release.set()
            done = self.terminal(job)
            repeated = self.status(job, wait_ms=0)
        self.assertEqual(done["status"], "learned", done)
        self.assertEqual(done["id"], repeated["id"])
        self.assertEqual(len(self.manager.elements.all()), 1)
        self.assertEqual(self.runtime.observation_count, 1)

    def test_index_changes_before_human_confirms_do_not_change_identity(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            self.runtime.answer["structuredContent"]["elements"][2]["element_index"] = 19
            self.release.set()
            done = self.terminal(job)
        self.assertEqual(done["status"], "learned")
        self.assertEqual(done["selector"], FIELD_SELECTOR)

    def test_cancel_and_stop_do_not_save_even_when_selection_arrives_late(self):
        for kind in ("cancel", "stop"):
            with self.subTest(kind=kind), mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
                self.runtime.stop_event.clear()
                self.release.clear()
                job = self.start()
                if kind == "cancel":
                    done = self.terminal(job, cancel=True)
                else:
                    self.runtime.stop_event.set()
                    done = self.terminal(job)
                self.release.set()
                self.assertEqual(done["status"], "cancelled")
                self.assertFalse(done["picker_visible"])
                self.assertEqual(self.manager.elements.all(), [])
                self.assertEqual(self.runtime.calls, [])

    def test_status_stays_available_after_session_is_removed(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            self.manager.teachings.stop(self.runtime)
            self.manager.session = None
            done = self.terminal(job)
        self.assertEqual(done["status"], "cancelled")

    def test_while_human_selects_business_input_and_new_inspection_are_blocked(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            for name, args in (("computer_launch", {"program_id": "editor"}),
                               ("computer_inspect", TARGET),
                               ("get_window_state", TARGET),
                               ("computer_perform", {**TARGET, "step": {"operation": "set_value", "value": "no"}})):
                blocked = self.manager.call(name, args)["structuredContent"]
                self.assertEqual(blocked["status"], "teaching_pending")
                self.assertEqual(blocked["teaching_id"], job["teaching_id"])
            self.assertEqual(self.runtime.calls, [])
            self.terminal(job, cancel=True)

    def test_start_failure_does_not_claim_visible_or_instruct_f8(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=OperationError("helper missing", "picker_missing")) as helper:
            failed = self.start()
        self.assertEqual(failed["status"], "learning_failed")
        self.assertEqual(failed["diagnostic"]["stage"], "starting_picker")
        self.assertEqual(failed["diagnostic"]["code"], "picker_missing")
        self.assertFalse(failed["picker_visible"])
        self.assertFalse(failed["automatic_retry"])
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(self.runtime.calls, [])

    def test_start_failure_identifies_supported_picker_and_connection_recovery(self):
        from settings import VERSION
        with mock.patch("teaching_sessions._run_helper", side_effect=OperationError("missing", "picker_missing")):
            failed = self.start()
        self.assertEqual(failed["server_version"], VERSION)
        self.assertTrue(failed["uia_picker_supported"])
        self.assertTrue(Path(failed["server_directory"]).is_absolute())
        self.assertEqual(failed["recovery"]["next_tool"], "computer_status")
        self.assertFalse(failed["recovery"]["repeat_f8"])
        self.assertNotIn("suggested_arguments", failed["recovery"])

    def test_visual_session_does_not_open_or_claim_unsupported_uia_picker(self):
        self.runtime.mode = "visual"
        with mock.patch("teaching_sessions._run_helper") as helper:
            failed = self.start()
        helper.assert_not_called()
        self.assertEqual(failed["diagnostic"]["code"], "unsupported_mode")
        self.assertTrue(failed["uia_picker_supported"])

    def test_only_unsupported_control_can_offer_exact_image_process_target(self):
        from teaching_sessions import failure_guidance
        for code in ("picker_not_found", "picker_controls_not_exposed"):
            guidance = failure_guidance(code, "verifying_selection", self.args)
            self.assertEqual(guidance["next_tool"], "computer_process_editor")
            self.assertEqual(guidance["suggested_arguments"]["targets"], [{**TARGET, "program_id": "editor"}])
        for code in ("picker_cancelled", "picker_missing", "target_mismatch", "picker_read_timeout"):
            guidance = failure_guidance(code, "waiting_for_human", self.args)
            self.assertNotEqual(guidance.get("next_tool"), "computer_process_editor")
        guidance = failure_guidance("picker_not_found", "verifying_selection", self.args, cleanup_pending=True)
        self.assertEqual(guidance["next_tool"], "computer_teach_status")

    def test_read_failure_after_selection_reports_verifying_stage_without_reopening(self):
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper) as helper:
            job = self.start()
            self.runtime.observation_error = {"isError": True}
            self.release.set()
            failed = self.terminal(job)
        self.assertEqual(failed["status"], "learning_failed")
        self.assertEqual(failed["diagnostic"]["stage"], "verifying_selection")
        self.assertEqual(failed["diagnostic"]["code"], "observation_failed")
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(self.manager.elements.all(), [])

    def test_status_timeout_is_pending_until_real_observation_and_commit_finish(self):
        entered, finish_read = threading.Event(), threading.Event()
        self.addCleanup(finish_read.set)
        original = self.runtime.call
        def slow_read(*args, **kwargs):
            entered.set()
            if not finish_read.wait(10):
                raise AssertionError("test failed to release observation")
            return original(*args, **kwargs)
        self.runtime.call = slow_read
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            self.release.set()
            self.assertTrue(entered.wait(5))
            pending = self.status(job, wait_ms=10)
            self.assertEqual(pending["status"], "verifying_selection", pending)
            self.assertNotIn("id", pending)
            self.assertEqual(self.manager.elements.all(), [])
            finish_read.set()
            done = self.terminal(job)
        self.assertEqual(done["status"], "learned", done)
        self.assertEqual(len(self.manager.elements.all()), 1)

    def test_duplicate_start_during_blocked_verification_returns_original_job_promptly(self):
        entered, finish_read, replied = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(finish_read.set)
        original = self.runtime.call
        answers = []
        def slow_read(*args, **kwargs):
            entered.set()
            if not finish_read.wait(10):
                raise AssertionError("test failed to release observation")
            return original(*args, **kwargs)
        def repeat():
            try:
                answers.append(self.start(label="different requested label"))
            finally:
                replied.set()
        self.runtime.call = slow_read
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper) as helper:
            job = self.start()
            self.release.set()
            self.assertTrue(entered.wait(5))
            repeating = threading.Thread(target=repeat)
            repeating.start()
            try:
                self.assertTrue(replied.wait(1), "repeat start blocked behind the in-flight UIA read")
                self.assertEqual(answers[0]["status"], "verifying_selection", answers)
                self.assertTrue(answers[0]["busy"])
                self.assertEqual(answers[0]["teaching_id"], job["teaching_id"])
                self.assertEqual(answers[0]["label"], self.args["label"])
                self.assertEqual({key: answers[0][key] for key in TARGET}, TARGET)
                self.assertEqual(helper.call_count, 1)
            finally:
                finish_read.set()
                repeating.join(5)
            done = self.terminal(job)
        self.assertEqual(done["status"], "learned", done)

    def test_cancellation_during_observation_is_checked_at_commit(self):
        original = self.runtime.call
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            def read_and_cancel(*args, **kwargs):
                answer = original(*args, **kwargs)
                self.manager.teachings.jobs[job["teaching_id"]]["cancel"].set()
                return answer
            self.runtime.call = read_and_cancel
            self.release.set()
            cancelled = self.terminal(job)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.manager.elements.all(), [])

    def test_wrong_program_preflight_never_starts_picker(self):
        self.runtime.guard.window_resolver = lambda hwnd: 999
        with mock.patch("teaching_sessions._run_helper") as helper:
            failed = self.start()
        self.assertEqual(failed["diagnostic"]["code"], "target_mismatch")
        helper.assert_not_called()

    def test_request_cancelled_after_ready_stops_helper_without_saving(self):
        cancelled = threading.Event()
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.manager.call("computer_teach_element", self.args, cancel_event=cancelled)["structuredContent"]
            cancelled.set()
            done = self.terminal(job)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(self.manager.elements.all(), [])
        self.assertEqual(self.runtime.calls, [])

    def test_human_confirmation_is_required_for_native_selection(self):
        def unconfirmed(*args, **kwargs):
            kwargs["on_ready"]({"helper_pid": 100, "helper_window_id": 200})
            selected = native_choice()
            selected.pop("human_confirmed")
            return selected
        with mock.patch("teaching_sessions._run_helper", side_effect=unconfirmed):
            job = self.start()
            done = self.terminal(job)
        self.assertEqual(done["status"], "learning_failed")
        self.assertEqual(done["diagnostic"]["code"], "picker_confirmation_required")
        self.assertEqual(self.manager.elements.all(), [])
        self.assertEqual(self.runtime.calls, [])

    def stop_runtime(self, reason):
        self.runtime.stop_event.set()
        self.runtime.state = "stopped"

    def prepare_runtime_stop(self):
        self.runtime.stop = self.stop_runtime
        self.runtime.status = lambda: {"state": self.runtime.state}
        self.runtime.wait_stopped = lambda timeout: self.runtime.stop_event.is_set()
        self.runtime.state = "active"

    def test_stop_distinguishes_owned_helper_cleanup_from_driver_stopped(self):
        self.prepare_runtime_stop()
        def delayed(*args, **kwargs):
            kwargs["on_ready"]({"helper_pid": 100, "helper_window_id": 200})
            self.release.wait(5)
            return native_choice()
        with mock.patch("teaching_sessions._run_helper", side_effect=delayed):
            job = self.start()
            stopped = self.manager.stop()
            self.assertTrue(stopped["teaching_cleanup_pending"])
            self.assertFalse(stopped["stopped"])
            self.release.set()
            self.assertEqual(self.terminal(job)["status"], "cancelled")
        self.assertEqual(self.manager.elements.all(), [])

    def test_connection_close_joins_picker_worker_and_preserves_cancelled_status(self):
        self.prepare_runtime_stop()
        with mock.patch("teaching_sessions._run_helper", side_effect=self.helper):
            job = self.start()
            self.manager.close()
            done = self.status(job)
        self.assertEqual(done["status"], "cancelled")
        self.assertIsNone(self.manager.teachings.pending())
        self.assertFalse(self.manager.teachings.jobs[job["teaching_id"]]["worker"].is_alive())


if __name__ == "__main__":
    unittest.main()
