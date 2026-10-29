"""Semantic menu dispatch retains Guard scope, fresh evidence and one-shot input."""
import copy
import time
import unittest
from unittest import mock

import menu_accessibility
import menu_guard
import privileges
import test_image_steps as image_tests
import transient_popup
from operations import OperationError


TARGET = image_tests.TARGET
MENU = {"path": [{"name": "Export", "role": "MenuItem"}]}


def observation(stamp=None, *, identity="runtime-1", parent=True):
    evidence = {"target": {**TARGET, "process_start_ticks": "123"},
                "path": [{"name": "Export", "identity": identity}],
                "observed_at_ms": time.time()*1000 if stamp is None else stamp}
    row = {"index":0,"element_index":0,"parent_index":-1,"name":"Export","role":"MenuItem",
           "visible":True,"enabled":True,"has_children":parent,"has_popup":parent,
           "bounds":{"x":20,"y":20,"width":60,"height":20},
           "locator":{"source":"uia_raw","path":[],"root_object_id":-4},
           "expected":evidence,"evidence":evidence,
           "supported_actions":["expand","hover","click_parent"] if parent else ["invoke"]}
    return {"target":dict(TARGET),"read_only":True,"authorizes_input":False,"sources":{
            "uia_raw":{"status":"ok","complete":True,"truncated":False,"elements":[row]},
            "direct_msaa":{"status":"unavailable","complete":False,"truncated":False,"elements":[]}}}


class SemanticMenuGuardTests(unittest.TestCase):
    def setUp(self):
        image_tests.ImageGuardTests.setUp(self)
        self.observations = [observation()]
        self.read_count = 0
        def read(target, **kwargs):
            kwargs["check"]()
            result = self.observations[min(self.read_count,len(self.observations)-1)]
            self.read_count += 1
            return copy.deepcopy(result)
        self.read = self.enterContext(mock.patch.object(menu_accessibility,"observe",side_effect=read))
        self.native = self.enterContext(mock.patch.object(menu_accessibility,"act",return_value={
            "status":"ok","input_dispatched":True,"delivery":"sent","business_result_verified":False}))
        self.integrity = self.enterContext(mock.patch.object(privileges,"process_integrity",return_value={
            "checked":True,"rid":0x2000,"ui_access":False}))
        self.popup = self.enterContext(mock.patch.object(transient_popup,"proven_popup",return_value=False))
        self.approval = self.enterContext(mock.patch.object(self.guard,"approve"))
        self.identity = mock.Mock()

    def act(self, operation="expand", **kwargs):
        return menu_guard.act(self.guard,TARGET,MENU,operation,lambda:None,self.identity,**kwargs)

    def test_one_action_runs_under_existing_guard_and_never_grants_driver_tokens(self):
        self.guard.observed_targets.add((100,200))
        self.guard._native_popup_frame = object()
        result = self.act()
        self.assertTrue(result["input_dispatched"])
        self.assertTrue(result["verification_deferred"])
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["business_result_verified"])
        self.approval.assert_called_once()
        self.native.assert_called_once()
        self.assertEqual(self.read_count,2)
        self.assertEqual(self.guard.action_count,1)
        self.assertEqual(self.guard.observed_targets,set())
        self.assertIsNone(self.guard._native_popup_frame)
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(self.approval.call_args.args[0],"expand_menu")

    def test_fresh_observation_timestamp_is_allowed_and_fresh_evidence_dispatched(self):
        stamp = time.time()*1000
        self.observations = [observation(stamp),observation(stamp+50)]
        result = self.act()
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(self.native.call_args.args[2]["observed_at_ms"],stamp+50)

    def test_semantic_identity_change_after_approval_blocks_input(self):
        self.observations = [observation(identity="old"),observation(identity="new")]
        result = self.act()
        self.assertEqual(result["diagnostic"]["code"],"menu_item_changed")
        self.assertFalse(result["input_dispatched"])
        self.native.assert_not_called()
        self.assertEqual(self.guard.action_count,0)

    def test_changed_geometry_after_approval_blocks_input(self):
        self.approval.side_effect = lambda *args,**kwargs:setattr(self,"geometry",(5,0,105,100,2))
        result = self.act()
        self.assertEqual(result["diagnostic"]["code"],"menu_target_changed")
        self.assertFalse(result["input_dispatched"])
        self.native.assert_not_called()

    def test_changed_window_identity_blocks_input(self):
        self.identity.side_effect = OperationError("replaced", "menu_parent_changed")
        result = self.act()
        self.assertEqual(result["diagnostic"]["code"],"menu_parent_changed")
        self.native.assert_not_called()
        self.approval.assert_not_called()

    def test_out_of_scope_pid_and_foreground_refusal_send_nothing(self):
        self.owner = 999
        result = self.act()
        self.assertFalse(result["input_dispatched"])
        self.read.assert_not_called()
        self.native.assert_not_called()
        self.approval.assert_not_called()
        self.owner, self.ready = TARGET["pid"], False
        result = self.act()
        self.assertEqual(result["diagnostic"]["code"],"menu_foreground_unavailable")
        self.native.assert_not_called()

    def test_action_budget_is_checked_before_and_after_approval(self):
        self.guard.action_count = self.guard.policy["max_actions"]
        self.assertFalse(self.act()["input_dispatched"])
        self.approval.assert_not_called()
        self.native.assert_not_called()
        self.guard.action_count = 0
        self.approval.side_effect = lambda *args,**kwargs:setattr(self.guard,"action_count",self.guard.policy["max_actions"])
        self.assertFalse(self.act()["input_dispatched"])
        self.native.assert_not_called()

    def test_unknown_or_higher_integrity_never_dispatches(self):
        for values, code in (([{"checked":False},{"checked":True,"rid":0x2000}],"menu_integrity_unknown"),
                ([{"checked":True,"rid":None},{"checked":True,"rid":0x2000}],"menu_integrity_unknown"),
                ([{"checked":True,"rid":0x2000,"ui_access":False},{"checked":True,"rid":0x3000}],"elevation_required")):
            with self.subTest(code=code):
                self.integrity.side_effect = values
                result = self.act()
                self.assertEqual(result["diagnostic"]["code"],code)
                self.assertFalse(result["input_dispatched"])
                self.native.assert_not_called()

    def test_unknown_native_delivery_is_not_retried(self):
        self.native.side_effect = OperationError("timeout", "menu_accessibility_timeout")
        result = self.act()
        self.assertIsNone(result["input_dispatched"])
        self.assertFalse(result["verification_deferred"])
        self.assertFalse(result["diagnostic"]["automatic_replay"])
        self.native.assert_called_once()
        self.assertEqual(self.guard.action_count,1)

    def test_native_refusal_preserves_exact_nested_reason(self):
        self.native.return_value = {"status":"error","input_dispatched":False,"delivery":"not_sent",
                                    "diagnostic":{"code":"menu_accessibility_evidence_changed"}}
        result = self.act()
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"],"menu_accessibility_evidence_changed")
        self.native.assert_called_once()

    def test_approval_receives_step_deadline_and_native_timeout_obeys_adapter_limit(self):
        result = self.act(timeout_ms=120000)
        self.assertTrue(result["input_dispatched"])
        self.assertIn("request_deadline",self.approval.call_args.kwargs)
        self.assertLessEqual(self.native.call_args.kwargs["timeout"],30)

    def test_invoke_requires_leaf_and_keeps_business_result_unverified(self):
        blocked = self.act("invoke")
        self.assertFalse(blocked["input_dispatched"])
        self.native.assert_not_called()
        self.read_count = 0
        self.observations = [observation(parent=False)]
        result = self.act("invoke")
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["business_result_verified"])
        self.assertEqual(self.approval.call_args.args[0],"menu_command")

    def test_command_selects_observed_invoke_or_leaf_click_before_single_dispatch(self):
        for actions, expected in ((["invoke","click_leaf"],"invoke"),(["click_leaf"],"click_leaf")):
            with self.subTest(actions=actions):
                self.read_count = 0
                self.observations = [observation(parent=False)]
                self.observations[0]["sources"]["uia_raw"]["elements"][0]["supported_actions"] = actions
                self.native.reset_mock()
                result = self.act("command")
                self.assertTrue(result["input_dispatched"])
                self.native.assert_called_once()
                self.assertEqual(self.native.call_args.args[3],expected)
                self.assertEqual(self.approval.call_args.args[0],"menu_command")

    def test_failed_invoke_never_falls_back_to_leaf_click(self):
        self.observations = [observation(parent=False)]
        self.observations[0]["sources"]["uia_raw"]["elements"][0]["supported_actions"] = ["invoke","click_leaf"]
        self.native.side_effect = OperationError("timeout", "menu_accessibility_timeout")
        result = self.act("command")
        self.assertIsNone(result["input_dispatched"])
        self.native.assert_called_once()
        self.assertEqual(self.native.call_args.args[3],"invoke")

    def test_subminimum_remaining_budget_stops_before_approval_or_dispatch(self):
        result = self.act(timeout_ms=199)
        self.assertFalse(result["input_dispatched"])
        self.approval.assert_not_called()
        self.native.assert_not_called()
        self.assertEqual(self.guard.action_count,0)


if __name__ == "__main__":
    unittest.main()
