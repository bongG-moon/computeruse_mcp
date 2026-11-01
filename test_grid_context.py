"""Real Python Guard, fake Driver/provider contracts. No desktop input is sent.

These tests establish dispatch/continuity boundaries, not SmartFX acceptance.
"""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import grid_context as context
from grid_intent import inspect_grid_intent
from operations import OperationError
from vendor.guard import Guard, GuardError
import test_grid_intent as grid_fixture
import test_semantic_menus as menu_fixture


TARGET = {"pid": 10, "window_id": 20}
MENU = menu_fixture.target("Export to excel")


def step(scope="whole_active_sheet"):
    return {"operation": "open_grid_menu", "grid_intent": grid_fixture.contract(scope),
            "menu_target": copy.deepcopy(MENU), "query_step_id": "query", "verification_timeout_ms": 500}


class Transport:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def driver_request(self, method, request, timeout=None):
        self.calls.append(copy.deepcopy(request))
        name = request["name"]
        self.runtime.events.append("driver:" + name)
        if name == "get_window_state":
            return {"structuredContent": self.runtime.driver_snapshot()}
        if name != "right_click":
            raise AssertionError("Unexpected mutation: " + name)
        if self.runtime.driver_error is not None:
            raise self.runtime.driver_error
        self.runtime.menu_visible = self.runtime.show_menu_after_click
        if self.runtime.after_click:
            self.runtime.after_click()
        return copy.deepcopy(self.runtime.input_result)


class Runtime:
    id = "same-session"
    mode = "uia"
    state = "active"

    def __init__(self, folder):
        self.stop_event = threading.Event()
        self.events = []
        self.scene = grid_fixture.snapshot()
        self.driver_changes = None
        self.native_reads = 0
        self.menu_visible = False
        self.menu_identity = "menu-runtime-1"
        self.menu_state = "collapsed"
        self.show_menu_after_click = True
        self.driver_error = None
        self.after_click = None
        self.input_result = {"structuredContent": {"input_sent": True}}
        self.transport = Transport(self)
        self.policy = {"driver": str(Path(folder) / "driver.exe"), "run_dir": folder,
                       "allowed_apps": [str(Path(folder) / "Fixture.exe")], "mode": "uia",
                       "approval_mode": "run", "max_actions": 10}
        self.guard = Guard(self.policy, transport=self.transport,
                           process_resolver=lambda pid: self.policy["allowed_apps"][0],
                           window_resolver=lambda hwnd: TARGET["pid"])
        self.guard.approve = mock.Mock(side_effect=lambda *args, **kwargs: self.events.append("approved"))

    def check_active(self):
        if self.stop_event.is_set():
            raise OperationError("cancelled", "session_stopped")

    def observe_grid_context(self, target, intent, **kwargs):
        self.check_active()
        self.native_reads += 1
        self.events.append("native:" + str(self.native_reads))
        return copy.deepcopy(self.scene)

    def observe_menu(self, target, **kwargs):
        self.check_active()
        self.events.append("menu_observe")
        rows = [menu_fixture.row(0, "Export to excel", is_parent=True)] if self.menu_visible else []
        if rows:
            rows[0].update(identity=self.menu_identity, identity_kind="uia_runtime_id", expansion_state=self.menu_state)
            rows[0]["evidence"]["target"] = dict(target)
            rows[0]["evidence"]["path"] = [{"identity": self.menu_identity, "identity_kind": "uia_runtime_id",
                                            "name": "Export to excel", "role": "MenuItem",
                                            "expansion_state": self.menu_state}]
        return menu_fixture.observation(rows, actual=target,
            msaa=menu_fixture.source() if not rows else None)

    def driver_snapshot(self):
        result = {**TARGET, "snapshot_id": "driver-current", "elements": copy.deepcopy(self.scene["elements"])}
        for row in result["elements"]:
            row.pop("verification_only", None)
            row["element_token"] = "driver-current:" + str(row["element_index"])
        if self.driver_changes:
            self.driver_changes(result)
        return result

    def call(self, name, arguments):
        return self.guard.call(name, arguments)


class GridContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name)
        self.saved = []
        self.episodes = []

    def run_grid(self, **kwargs):
        return context.execute_grid_menu(self.runtime, kwargs.pop("step", step()), TARGET,
            query_evidence=kwargs.pop("query_evidence", grid_fixture.proof()), run_id="run",
            save_context=kwargs.pop("save_context", lambda value: self.saved.append(value)),
            save_menu_episode=kwargs.pop("save_menu_episode", lambda value: self.episodes.append(value)), **kwargs)

    def mutations(self):
        return [call for call in self.runtime.transport.calls if call["name"] != "get_window_state"]

    def prior_hash(self, scope="whole_active_sheet"):
        return context.context_hash(inspect_grid_intent(self.runtime.scene, grid_fixture.contract(scope))["context"])

    def test_current_provider_cell_is_remapped_to_driver_and_revalidated_after_approval(self):
        result = self.run_grid()
        self.assertTrue(result["task_verified"], result)
        self.assertFalse(result["business_result_verified"])
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(result["metrics"]["image_comparisons"], 0)
        self.assertEqual(len(self.saved), 1)
        calls = self.mutations()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "right_click")
        self.assertEqual(calls[0]["arguments"]["element_token"], "driver-current:2")
        events = self.runtime.events
        self.assertLess(events.index("approved"), events.index("native:2"))
        self.assertLess(events.index("native:2"), events.index("driver:right_click"))
        self.assertIsNone(self.runtime.guard._grid_input_contract)

    def test_readonly_native_snapshot_cannot_be_used_as_driver_input(self):
        self.runtime.driver_changes = lambda value: value.update(read_only=True)
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "read_only_observation")
        self.assertEqual(self.mutations(), [])

    def test_saved_contract_failure_stops_before_observation_or_input(self):
        def fail(_): raise OperationError("cannot persist", "grid_context_save_failed")
        result = self.run_grid(save_context=fail)
        self.assertEqual(result["diagnostic"]["code"], "grid_context_save_failed")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.runtime.transport.calls, [])

    def test_missing_query_freshness_never_uses_old_visible_grid(self):
        result = self.run_grid(query_evidence={**grid_fixture.proof(), "freshness_verified": False})
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "grid_query_freshness_unverified")
        self.assertEqual(self.runtime.transport.calls, [])

    def test_cell_change_during_approval_blocks_all_input(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: self.runtime.scene["elements"][2].update(name="new value")
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_cell_changed")
        self.assertEqual(self.mutations(), [])
        self.assertIsNone(self.runtime.guard._grid_input_contract)

    def test_active_sheet_change_during_approval_blocks_all_input(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: self.runtime.scene["elements"][1].update(selected=False)
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_active_sheet_unavailable")
        self.assertEqual(self.mutations(), [])

    def test_window_change_during_approval_blocks_all_input(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: setattr(self.runtime, "id", "replacement-session")
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_window_changed")
        self.assertEqual(self.mutations(), [])

    def test_approval_denial_and_budget_denial_are_explicit_not_sent(self):
        self.runtime.guard.approve.side_effect = GuardError("denied")
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])
        self.runtime.guard.approve.side_effect = None
        self.runtime.guard.action_count = 10
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_budget_changed_during_approval_is_rechecked_before_dispatch(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: setattr(self.runtime.guard, "action_count", 10)
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_stop_after_approval_blocks_input_without_retry(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: self.runtime.stop_event.set()
        result = self.run_grid()
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_unknown_driver_delivery_is_unknown_and_never_retried(self):
        self.runtime.input_result = {"isError": True, "structuredContent": {"input_sent": None, "error_code": "driver_timeout"}}
        result = self.run_grid()
        self.assertIsNone(result["input_dispatched"], result)
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(self.mutations()), 1)
        resumed = self.run_grid(verify_only=True, prior_context_hash=self.saved[0])
        self.assertEqual(len(self.mutations()), 1)
        self.assertFalse(resumed["business_result_verified"])

    def test_timeout_exception_after_driver_dispatch_is_unknown(self):
        self.runtime.driver_error = OSError("transport timeout")
        result = self.run_grid()
        self.assertIsNone(result["input_dispatched"], result)
        self.assertEqual(len(self.mutations()), 1)

    def test_resume_without_saved_context_cannot_adopt_current_selection(self):
        self.runtime.menu_visible = True
        result = self.run_grid(verify_only=True)
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "grid_context_changed")
        self.assertEqual(self.runtime.transport.calls, [])

    def test_resume_with_matching_context_is_read_only(self):
        self.assertTrue(self.run_grid()["task_verified"])
        self.runtime.transport.calls.clear()
        result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0], prior_menu_episode=self.episodes[0])
        self.assertTrue(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(self.runtime.transport.calls, [])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.episodes), 1)

    def test_menu_absence_does_not_reclick_or_claim_export(self):
        self.runtime.show_menu_after_click = False
        result = self.run_grid()
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["business_result_verified"])
        self.assertEqual(len(self.mutations()), 1)
        resumed = self.run_grid(verify_only=True, prior_context_hash=self.saved[0])
        self.assertFalse(resumed["task_verified"])
        self.assertEqual(len(self.mutations()), 1)

    def test_preexisting_same_named_menu_is_not_attributed_to_this_grid(self):
        self.runtime.menu_visible = True
        result = self.run_grid()
        self.assertFalse(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(result["business_result_verified"])

    def test_menu_appearing_during_approval_blocks_before_right_click(self):
        self.runtime.guard.approve.side_effect = lambda *a, **k: setattr(self.runtime, "menu_visible", True)
        result = self.run_grid()
        self.assertFalse(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_resume_rejects_missing_or_replaced_menu_episode_without_reclick(self):
        self.assertTrue(self.run_grid()["task_verified"])
        result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0])
        self.assertFalse(result["task_verified"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_episode_changed")
        self.runtime.menu_identity = "different-runtime-instance"
        result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0], prior_menu_episode=self.episodes[0])
        self.assertFalse(result["task_verified"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_episode_changed")
        self.assertEqual(len(self.mutations()), 1)

    def test_same_menu_identity_can_expand_before_read_only_resume(self):
        self.assertTrue(self.run_grid()["task_verified"])
        self.runtime.menu_state = "expanded"
        result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0], prior_menu_episode=self.episodes[0])
        self.assertTrue(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(self.mutations()), 1)

    def test_same_menu_identity_disabled_after_input_cannot_pass_resume(self):
        self.assertTrue(self.run_grid()["task_verified"])
        original = self.runtime.observe_menu
        def disabled(*args, **kwargs):
            result = original(*args, **kwargs)
            result["sources"]["uia_raw"]["elements"][0]["enabled"] = False
            return result
        with mock.patch.object(self.runtime, "observe_menu", side_effect=disabled):
            result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0],
                                   prior_menu_episode=self.episodes[0])
        self.assertFalse(result["task_verified"], result)
        self.assertEqual(result["diagnostic"]["code"], "menu_item_disabled")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(self.mutations()), 1)

    def test_episode_save_failure_after_input_does_not_repeat_right_click(self):
        def fail(_): raise OperationError("persistence failed", "grid_menu_episode_save_failed")
        result = self.run_grid(save_menu_episode=fail)
        self.assertFalse(result["task_verified"], result)
        self.assertTrue(result["input_dispatched"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_episode_save_failed")
        result = self.run_grid(verify_only=True, prior_context_hash=self.saved[0])
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(self.mutations()), 1)

    def test_incomplete_menu_baseline_cannot_authorize_a_new_right_click(self):
        with mock.patch.object(self.runtime, "observe_menu", return_value=menu_fixture.observation(
                uia=menu_fixture.source(complete=False, status="timeout"), actual=TARGET)):
            result = self.run_grid()
        self.assertFalse(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_complete_uia_baseline_accepts_same_source_when_msaa_times_out(self):
        original = self.runtime.observe_menu
        def incomplete_baseline(*args, **kwargs):
            if not self.runtime.menu_visible:
                return menu_fixture.observation(msaa=menu_fixture.source(complete=False, status="timeout"), actual=TARGET)
            return original(*args, **kwargs)
        with mock.patch.object(self.runtime, "observe_menu", side_effect=incomplete_baseline):
            result = self.run_grid()
        self.assertTrue(result["task_verified"], result)
        self.assertTrue(result["input_dispatched"], result)
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(result["menu_recognition"]["baseline_sources"], ["uia_raw"])
        self.assertEqual(result["menu_recognition"]["baseline_source_statuses"]["direct_msaa"], "timeout")

    def test_missing_or_failed_provider_is_not_an_empty_menu_baseline(self):
        for status in ("unavailable", "error", "partial", "timeout"):
            with self.subTest(status=status):
                data = menu_fixture.observation(uia=menu_fixture.source(complete=False, status=status),
                                               msaa=menu_fixture.source(complete=False, status=status), actual=TARGET)
                with mock.patch.object(self.runtime, "observe_menu", return_value=data):
                    result = self.run_grid()
                self.assertFalse(result["task_verified"], result)
                self.assertFalse(result["input_dispatched"], result)
                self.assertEqual(self.mutations(), [])

    def test_complete_msaa_baseline_and_new_menu_work_when_uia_unavailable(self):
        def msaa_only(*args, **kwargs):
            rows = [menu_fixture.row(0, "Export to excel", is_parent=True)] if self.runtime.menu_visible else []
            for row in rows:
                row["locator"]["source"] = "direct_msaa"
                row["evidence"]["target"] = dict(TARGET)
                row["evidence"]["path"] = [{"identity": "msaa-current", "identity_kind": "iacc_identity"}]
            return menu_fixture.observation(uia=menu_fixture.source(complete=False, status="unavailable"),
                                            msaa=menu_fixture.source(rows), actual=TARGET)
        with mock.patch.object(self.runtime, "observe_menu", side_effect=msaa_only):
            result = self.run_grid()
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(result["menu_recognition"]["selected_source"], "direct_msaa")
        self.assertEqual(result["menu_recognition"]["baseline_sources"], ["direct_msaa"])
        self.assertEqual(len(self.mutations()), 1)

    def test_partial_positive_parent_blocks_despite_complete_empty_peer(self):
        for provider in ("uia_raw", "direct_msaa"):
            with self.subTest(provider=provider):
                data = menu_fixture.observation(msaa=menu_fixture.source(), actual=TARGET)
                data["sources"][provider] = menu_fixture.source(
                    [menu_fixture.row(0, "Export to excel", is_parent=True)], complete=False, status="partial")
                with mock.patch.object(self.runtime, "observe_menu", return_value=data):
                    result = self.run_grid()
                self.assertEqual(result["diagnostic"]["code"], "grid_menu_already_open")
                self.assertFalse(result["input_dispatched"], result)
                self.assertEqual(self.mutations(), [])

    def test_known_different_menu_path_does_not_replace_expected_parent(self):
        original = self.runtime.observe_menu
        def unrelated_parent(*args, **kwargs):
            if self.runtime.menu_visible:
                return original(*args, **kwargs)
            rows = [menu_fixture.row(0, "Other panel", is_parent=True),
                    menu_fixture.row(1, "Export to excel", parent=0, is_parent=True)]
            return menu_fixture.observation(msaa=menu_fixture.source(rows), actual=TARGET)
        with mock.patch.object(self.runtime, "observe_menu", side_effect=unrelated_parent):
            result = self.run_grid()
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(len(self.mutations()), 1)

    def test_partial_visible_parent_with_missing_ancestry_is_not_discarded(self):
        data = menu_fixture.observation(msaa=menu_fixture.source([
            menu_fixture.row(0, "Export to excel", parent=77, is_parent=True)],
            complete=False, status="partial"), actual=TARGET)
        with mock.patch.object(self.runtime, "observe_menu", return_value=data):
            result = self.run_grid()
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_already_open")
        self.assertEqual(self.mutations(), [])

    def test_changed_source_after_input_does_not_certify_appearance_or_retry(self):
        def switch_source(*args, **kwargs):
            rows = [menu_fixture.row(0, "Export to excel", is_parent=True)] if self.runtime.menu_visible else []
            return menu_fixture.observation(uia=menu_fixture.source(),
                msaa=menu_fixture.source(rows) if rows else menu_fixture.source(complete=False, status="unavailable"),
                actual=TARGET)
        with mock.patch.object(self.runtime, "observe_menu", side_effect=switch_source):
            result = self.run_grid()
        self.assertFalse(result["task_verified"], result)
        self.assertTrue(result["input_dispatched"], result)
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_source_changed")
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.episodes, [])

    def test_provider_completeness_is_rechecked_after_approval(self):
        approved = []
        self.runtime.guard.approve.side_effect = lambda *args, **kwargs: approved.append(True)
        def lost_evidence(*args, **kwargs):
            return menu_fixture.observation(
                uia=menu_fixture.source(complete=not approved, status="timeout" if approved else "ok"), actual=TARGET)
        with mock.patch.object(self.runtime, "observe_menu", side_effect=lost_evidence):
            result = self.run_grid()
        self.assertEqual(result["diagnostic"]["code"], "grid_menu_baseline_unavailable")
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_foreign_baseline_observation_cannot_authorize_a_new_right_click(self):
        with mock.patch.object(self.runtime, "observe_menu", return_value=menu_fixture.observation(
                actual={"pid": 10, "window_id": 999})):
            result = self.run_grid()
        self.assertFalse(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_preexisting_owned_popup_blocks_before_new_grid_input(self):
        scene = {"process_exited": False, "target_present": True, "creation_time": "birth-1",
            "executable": "Fixture.exe", "windows": [
                {**TARGET, "class_name": "FixtureWindow", "thread_id": 1, "visible": True},
                {"pid": 10, "window_id": 21, "class_name": "WindowsForms10.Window.20808.app.0.abc_r1_ad1",
                 "thread_id": 1, "owner_window_id": 20, "visible": True, "title": ""}]}
        class Probe:
            def capture(self): return copy.deepcopy(scene)
            def snapshot(self): return copy.deepcopy(scene)
            def transition_snapshot(self): return copy.deepcopy(scene)
            def close(self): pass
        self.runtime.create_transition_probe = lambda target: Probe()
        result = self.run_grid(menu_wait={"operation": "wait_for_window", "program_id": "fixture",
            "window_ref": "root_menu", "owner_ref": "main", "title": "",
            "class_name": "WindowsForms10.Window.20808.app.0.abc_r1_ad1", "timeout_ms": 500})
        self.assertFalse(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"], result)
        self.assertEqual(self.mutations(), [])

    def test_changed_output_selection_after_click_stops_without_second_input(self):
        self.runtime.scene["grid_selection"]["selected_items"] = [2, 3]
        self.runtime.after_click = lambda: self.runtime.scene["grid_selection"].update(selected_items=[2])
        result = self.run_grid(step=step("selected_ranges"))
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "grid_selection_scope_mismatch")
        self.assertEqual(len(self.mutations()), 1)

    def test_grid_context_hash_ignores_data_cells_but_preserves_ranges(self):
        first = self.prior_hash()
        self.runtime.scene["elements"][2]["name"] = "other data"
        self.runtime.scene["grid_selection"]["selected_items"] = [3]
        self.assertEqual(self.prior_hash(), first)
        self.runtime.scene["grid_selection"]["selected_items"] = [2, 3]
        selected = self.prior_hash("selected_ranges")
        self.runtime.scene["elements"][3]["grid_item"]["column"] += 1
        self.assertNotEqual(self.prior_hash("selected_ranges"), selected)

    def test_driver_label_alias_maps_only_current_matching_cell(self):
        self.runtime.driver_changes = lambda data: [row.update(label=row.pop("name")) for row in data["elements"]]
        result = self.run_grid()
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(len(self.mutations()), 1)

    def test_driver_geometry_or_duplicate_cell_does_not_choose_first_candidate(self):
        def grid_moved(data): data["elements"][0]["bounds"]["x"] += 1
        def cell_moved(data): data["elements"][2]["bounds"]["x"] += 1
        def duplicate(data):
            row = copy.deepcopy(data["elements"][2]); row.update(element_index=99, element_token="driver-current:99")
            data["elements"].append(row)
        for change in (grid_moved, cell_moved, duplicate):
            with self.subTest(change=change.__name__):
                self.runtime.driver_changes = change
                result = self.run_grid()
                self.assertFalse(result["input_dispatched"], result)
                self.assertEqual(self.mutations(), [])

    def test_export_context_recheck_has_no_input_side_effect(self):
        evidence = self.prior_hash()
        self.assertTrue(context.verify_export_context(self.runtime, TARGET, grid_fixture.contract(), evidence))
        self.runtime.scene["elements"][5]["value"] = "different filter"
        with self.assertRaises(OperationError):
            context.verify_export_context(self.runtime, TARGET, grid_fixture.contract(), evidence)
        self.assertEqual(self.runtime.transport.calls, [])


class MenuIdentityContractTests(unittest.TestCase):
    def selection(self, source="uia_raw", identity_kind="uia_runtime_id"):
        return {"source": source, "item": {"evidence": {"path": [
            {"identity": "parent-instance", "identity_kind": identity_kind},
            {"identity": "item-instance", "identity_kind": identity_kind,
             "name": "Export to excel", "role": "MenuItem", "expansion_state": "collapsed"}]}}}

    def test_msaa_child_path_without_iacc_identity_is_not_continuation_evidence(self):
        selected = self.selection("direct_msaa", "child_path")
        self.assertIsNone(context.menu_identity_hash(selected, {"window": "birth-1"}))
        selected["item"]["evidence"]["path"][1]["identity_kind"] = "iacc_identity"
        self.assertIsNone(context.menu_identity_hash(selected, {"window": "birth-1"}))
        selected["item"]["evidence"]["path"][0]["identity_kind"] = "iacc_identity"
        self.assertIsNotNone(context.menu_identity_hash(selected, {"window": "birth-1"}))

    def test_current_provider_identity_allows_hover_geometry_but_not_another_ancestor(self):
        selected = self.selection()
        original = context.menu_identity_hash(selected, {"window": "birth-1"})
        selected["item"]["evidence"]["path"][1].update(
            expansion_state="expanded", bounds={"x": -250, "y": 80, "width": 160, "height": 25}, state=3)
        self.assertEqual(context.menu_identity_hash(selected, {"window": "birth-1"}), original)
        selected["item"]["evidence"]["path"][0]["identity"] = "other-parent-instance"
        self.assertNotEqual(context.menu_identity_hash(selected, {"window": "birth-1"}), original)

    def test_hwnd_lifetime_and_provider_source_remain_part_of_identity(self):
        selected = self.selection()
        original = context.menu_identity_hash(selected, {"window": "birth-1"})
        self.assertNotEqual(context.menu_identity_hash(selected, {"window": "birth-2"}), original)
        other = self.selection("direct_msaa", "iacc_identity")
        self.assertNotEqual(context.menu_identity_hash(other, {"window": "birth-1"}), original)

    def test_missing_identity_cannot_be_replaced_by_names_or_indices(self):
        selected = self.selection()
        node = selected["item"]["evidence"]["path"][1]
        node.pop("identity")
        node.update(index=0, native_window_handle=20)
        self.assertIsNone(context.menu_identity_hash(selected, {"window": "birth-1"}))


class GridChainContractTests(unittest.TestCase):
    def chain(self):
        intent = grid_fixture.contract()
        query = {"operation": "click", "program_id": "fixture", "window_ref": "main", "step_id": "query",
                 "selector": {"name": "Search", "role": "Button"},
                 "expect": [{**copy.deepcopy(intent["ready_when"][0]), "require_change": True}]}
        opening = {**step(), "program_id": "fixture", "window_ref": "main", "step_id": "open-menu"}
        expand = {"operation": "expand_menu", "program_id": "fixture", "window_ref": "main", "step_id": "expand",
                  "menu_target": copy.deepcopy(MENU), "child_menu_target": menu_fixture.target("Export to excel", "Active sheet"),
                  "expansion_method": "hover"}
        command = {"operation": "menu_command", "program_id": "fixture", "window_ref": "main", "step_id": "command",
                   "menu_target": copy.deepcopy(expand["child_menu_target"]), "completion_mode": "human"}
        return [query, opening, expand, command]

    def test_chain_keeps_query_grid_parent_leaf_links(self):
        chain = context.grid_chain(self.chain(), 1)
        self.assertEqual((chain["query_index"], chain["parent_index"], chain["command_index"]), (0, 2, 3))
        self.assertIsNone(chain["menu_wait"])

    def test_other_query_readiness_or_iteration_is_not_inherited(self):
        mutations = [lambda rows: rows[0].update(window_ref="other"),
                     lambda rows: rows[0].update(iteration_id="loop:1"),
                     lambda rows: rows[0]["expect"][0].update(equals="unrelated"),
                     lambda rows: rows[0]["expect"][0].pop("require_change")]
        for change in mutations:
            with self.subTest(change=mutations.index(change)):
                rows = self.chain(); change(rows)
                with self.assertRaises(OperationError): context.grid_chain(rows, 1)

    def test_all_sheet_or_unrelated_parent_cannot_replace_active_sheet_leaf(self):
        for name in ("All sheet", "Other command"):
            rows = self.chain(); rows[3]["menu_target"] = menu_fixture.target("Export to excel", name)
            with self.subTest(name=name), self.assertRaises(OperationError): context.grid_chain(rows, 1)
        rows = self.chain(); rows[2]["menu_target"] = menu_fixture.target("Other parent")
        with self.assertRaises(OperationError): context.grid_chain(rows, 1)


if __name__ == "__main__": unittest.main()
