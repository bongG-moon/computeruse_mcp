"""Bounded reads and private timing evidence; no desktop input or Driver process."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vendor.guard import (DriverTransport, Guard, GuardError, action_guidance,
                          discover_allowed_windows, metadata_event, validate_policy)


class LegacyTransport:
    """The established two-argument fake transport remains supported."""
    def __init__(self):
        self.calls = []
        self.answer = {"structuredContent": {
            "elements": [{"label": "PRIVATE SCREEN", "value": "PRIVATE VALUE", "element_token": "fresh"}],
            "elements_complete": False, "returned_element_count": 1, "total_element_count": 3},
            "content": [{"type": "text", "text": "PRIVATE SCREEN"}]}

    def driver_request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        return copy.deepcopy(self.answer)


class TimedTransport(DriverTransport):
    """Exercise real-transport timeout routing without starting any process."""
    def __init__(self):
        LegacyTransport.__init__(self)
        self.timeouts = []
        self.error = None

    def check_running(self):
        pass

    def driver_request(self, method, params, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append((method, copy.deepcopy(params)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.answer)


class GuardPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = {"driver": str(Path(self.tmp.name) / "driver.exe"),
                       "allowed_apps": [str(Path(self.tmp.name) / "Editor.exe")],
                       "run_dir": self.tmp.name, "mode": "uia", "approval_mode": "run",
                       "max_actions": 10, "log_detail": "metadata"}
        self.target = {"pid": 100, "window_id": 200}

    def guard(self, transport=None, **policy):
        current = {**self.policy, **policy}
        return Guard(current, transport=transport or LegacyTransport(),
                     process_resolver=lambda pid: current["allowed_apps"][0],
                     window_resolver=lambda hwnd: 100)

    def test_bounded_defaults_keep_raw_completeness_and_legacy_transport(self):
        guard = self.guard()
        answer = guard.call("get_window_state", self.target)
        sent = guard.transport.calls[-1][1]["arguments"]
        self.assertEqual((sent["max_depth"], sent["max_elements"]), (12, 600))
        state = answer["structuredContent"]
        self.assertEqual(state["total_element_count"], 3)
        self.assertEqual(state["returned_element_count"], 1)
        self.assertFalse(state["elements_complete"])
        self.assertTrue(state["observation_limits"]["incomplete_reported"])
        self.assertIn("늘려", state["observation_limits"]["next_step"])
        self.assertEqual(guard.observed_targets, {(100, 200)})

    def test_explicit_expansion_preserved_and_invalid_limits_never_dispatched(self):
        guard = self.guard()
        answer = guard.call("get_window_state", {**self.target, "max_depth": 25, "max_elements": 5000})
        self.assertFalse(answer.get("isError", False))
        self.assertEqual(guard.transport.calls[-1][1]["arguments"]["max_elements"], 5000)
        for field, values in (("max_depth", [0, -1, 101, True, 2.5]),
                              ("max_elements", [0, 20001, False, "600"])):
            for value in values:
                with self.subTest(field=field, value=value):
                    count = len(guard.transport.calls)
                    result = guard.call("get_window_state", {**self.target, field: value})
                    self.assertTrue(result["isError"])
                    self.assertEqual(len(guard.transport.calls), count)

    def test_visual_mode_does_not_request_accessibility_tree_limits(self):
        guard = self.guard(mode="visual")
        answer = guard.call("get_window_state", self.target)
        sent = guard.transport.calls[-1][1]["arguments"]
        self.assertNotIn("max_depth", sent)
        self.assertNotIn("max_elements", sent)
        self.assertFalse(sent["include_accessibility_tree"])
        self.assertNotIn("elements", answer["structuredContent"])
        self.assertNotIn("observation_limits", answer["structuredContent"])

    def test_read_timeout_is_separate_from_mutation_default_and_configurable(self):
        transport = TimedTransport()
        guard = self.guard(transport)
        guard.call("get_window_state", self.target)
        guard.call("set_value", {**self.target, "element_token": "fresh", "value": "value"})
        self.assertEqual(transport.timeouts, [20, None])
        guard = self.guard(TimedTransport(), observation_timeout_seconds=7)
        guard.call("get_window_state", self.target)
        guard.call("verify_state", {**self.target, "expect": {}})
        self.assertEqual(guard.transport.timeouts, [7, 7])

    def test_invalid_timeout_configuration_is_rejected(self):
        for value in (0, -1, True, "20", float("nan"), float("inf"), 1801, 10**1000):
            with self.subTest(value=value), self.assertRaises(GuardError):
                validate_policy({**self.policy, "observation_timeout_seconds": value})

    def test_metrics_are_numeric_preserve_result_and_do_not_persist_screen_values(self):
        guard = self.guard()
        with mock.patch("vendor.guard.time.monotonic", side_effect=[10, 10.125]):
            answer = guard.call("get_window_state", {**self.target, "query": "PRIVATE QUERY"})
        metrics = answer["structuredContent"]["computer_use_metrics"]
        self.assertEqual(metrics["duration_ms"], 125.0)
        self.assertGreater(metrics["response_size_bytes"], 0)
        self.assertEqual(metrics["element_count"], 1)
        self.assertEqual(metrics["returned_element_count"], 1)
        self.assertEqual(metrics["total_element_count"], 3)
        self.assertEqual(answer["structuredContent"]["elements"][0]["value"], "PRIVATE VALUE")
        journal = (Path(self.tmp.name) / "actions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", journal)
        record = json.loads(journal.splitlines()[-1])
        for key, value in metrics.items():
            self.assertEqual(record[key], value)
            self.assertIn(type(value), (int, float))

    def test_untrusted_metrics_and_counts_cannot_pollute_numeric_journal(self):
        transport = LegacyTransport()
        transport.answer["structuredContent"].update({"computer_use_metrics": {"duration_ms": "PRIVATE"},
                                                       "element_count": "PRIVATE", "total_element_count": True})
        answer = self.guard(transport).call("get_window_state", self.target)
        metrics = answer["structuredContent"]["computer_use_metrics"]
        self.assertEqual(metrics["element_count"], 1)
        self.assertNotIn("total_element_count", metrics)
        safe = metadata_event("result", {"duration_ms": "PRIVATE", "response_size_bytes": float("nan"),
                                          "element_count": True, "total_element_count": -1})
        self.assertNotIn("duration_ms", safe)
        self.assertNotIn("response_size_bytes", safe)
        self.assertNotIn("element_count", safe)
        self.assertNotIn("total_element_count", safe)
        self.assertNotIn("total_element_count", metadata_event("result", {"total_element_count": 10**1000}))

    def test_transport_timeout_requires_new_session_without_automatic_retry(self):
        transport = TimedTransport()
        guard = self.guard(transport)
        guard.call("get_window_state", self.target)
        transport.error = GuardError("Driver request timed out; runtime was terminated.")
        answer = guard.call("get_window_state", self.target)
        guidance = answer["structuredContent"]["computer_use_guidance"]
        self.assertEqual(guidance["recovery_kind"], "restart_session")
        self.assertFalse(guidance["automatic_retry"])
        self.assertNotIn((100, 200), guard.observed_targets)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("duration_ms", answer["structuredContent"]["computer_use_metrics"])
        last = json.loads((Path(self.tmp.name) / "actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("duration_ms", last)
        self.assertFalse(last["success"])

    def test_internal_reader_timeout_guidance_does_not_replay_mutation(self):
        transport = TimedTransport()
        transport.answer = {"isError": True, "content": [{"type": "text", "text": "UIA request timeout"}]}
        guard = self.guard(transport)
        result = guard.call("get_window_state", self.target)
        self.assertEqual(result["structuredContent"]["computer_use_guidance"]["recovery_kind"], "fresh_observation")
        self.assertFalse(guard.observed_targets)
        mutation = guard.call("click", {**self.target, "element_token": "old"})
        self.assertTrue(mutation["isError"])
        self.assertEqual(len(transport.calls), 1)
        action = action_guidance(transport.answer, "click")
        self.assertEqual(action["structuredContent"]["computer_use_guidance"]["recovery_kind"], "verify_before_action")
        self.assertFalse(action["structuredContent"]["computer_use_guidance"]["automatic_retry"])

    def test_untitled_popup_exposes_only_verified_same_process_owners(self):
        requested = []
        def metadata(hwnd):
            requested.append(hwnd)
            return {"title": "", "is_on_screen": True, "owner_window_id": 200,
                    "root_owner_window_id": 200}
        rows = discover_allowed_windows(self.policy, {"pid": 100, "on_screen_only": True},
                                        process_resolver=lambda pid: self.policy["allowed_apps"][0],
                                        window_resolver=lambda hwnd: 100,
                                        handles_provider=lambda: [201], metadata_provider=metadata)
        self.assertEqual(requested, [201])
        self.assertEqual(rows[0]["window_id"], 201)
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["owner_window_id"], 200)
        self.assertEqual(rows[0]["root_owner_window_id"], 200)

    def test_owner_from_another_process_or_disappeared_window_is_not_exposed(self):
        def resolve(hwnd):
            if hwnd == 400:
                raise GuardError("Window no longer exists.")
            return 300 if hwnd == 300 else 100
        rows = discover_allowed_windows(self.policy, {},
                                        process_resolver=lambda pid: self.policy["allowed_apps"][0],
                                        window_resolver=resolve, handles_provider=lambda: [201],
                                        metadata_provider=lambda hwnd: {"is_on_screen": True,
                                            "owner_window_id": 300, "root_owner_window_id": 400})
        self.assertEqual(rows[0]["owner_window_id"], 0)
        self.assertEqual(rows[0]["root_owner_window_id"], 0)

    def test_reused_owner_handle_is_rechecked_before_exposing_relationship(self):
        owner_pids = iter([100, 300])
        def resolve(hwnd):
            return next(owner_pids) if hwnd == 200 else 100
        rows = discover_allowed_windows(self.policy, {},
                                        process_resolver=lambda pid: self.policy["allowed_apps"][0],
                                        window_resolver=resolve, handles_provider=lambda: [201],
                                        metadata_provider=lambda hwnd: {"owner_window_id": 200})
        self.assertEqual(rows[0]["owner_window_id"], 0)

    def test_denied_process_never_reads_popup_metadata_or_owner_candidates(self):
        def denied(pid):
            raise GuardError("Target process belongs to a different user or session.")
        metadata = mock.Mock(return_value={"owner_window_id": 200})
        rows = discover_allowed_windows(self.policy, {}, process_resolver=denied,
                                        window_resolver=lambda hwnd: 100,
                                        handles_provider=lambda: [201], metadata_provider=metadata)
        self.assertEqual(rows, [])
        metadata.assert_not_called()


if __name__ == "__main__":
    unittest.main()
