"""Menu hierarchy, independent providers, and bounded one-shot expansion."""
import copy
import unittest
from unittest import mock

import semantic_menus as menus
import test_menu_expansion as legacy
from menu_expansion import execute_menu_expansion, verify_menu_expansion, validate_menu_expansion
from operations import OperationError
from test_image_steps import image_target


TARGET, CHILD = legacy.TARGET, legacy.CHILD
PARENT_NAME, LEAF_NAME = "Export to excel", "Active sheet"


def target(*names):
    return {"path":[{"name":name,"role":"MenuItem"} for name in names]}


def row(index, name, parent=-1, *, is_parent=False, **extra):
    return {"index":index,"element_index":index,"parent_index":parent,"name":name,"role":"MenuItem",
            "enabled":True,"visible":True,"has_children":is_parent,"has_popup":is_parent,
            "bounds":{"x":20,"y":20+index*24,"width":100,"height":24},
            "locator":{"source":"uia_raw","path":[index],"root_object_id":-4},
            "evidence":{"target":dict(TARGET),"path":[{"identity":str(index)}],"observed_at_ms":100},
            "supported_actions":["expand","hover","click_parent"] if is_parent else ["invoke"],**extra}


def source(elements=(), *, complete=True, truncated=False, status="ok"):
    return {"status":status,"complete":complete,"truncated":truncated,"elements":list(elements)}


def observation(elements=(), *, uia=None, msaa=None, actual=TARGET):
    return {"target":dict(actual),"read_only":True,"authorizes_input":False,"sources":{
        "uia_raw":source(elements) if uia is None else uia,
        "direct_msaa":source(complete=False,status="unavailable") if msaa is None else msaa}}


def step(**extra):
    return {"operation":"expand_menu","menu_target":target(PARENT_NAME),
            "child_menu_target":target(PARENT_NAME,LEAF_NAME),"expansion_method":"click",
            "verification_timeout_ms":0,**extra}


def physical_step(**extra):
    value = step(child_window={"title":"","class_name":legacy.POPUP_CLASS},
                 child_image_target=image_target(),**extra)
    value.pop("child_menu_target")
    return value


class Runtime(legacy.Runtime):
    def __init__(self):
        super().__init__()
        self.semantic_reads=[]
        self.semantic_actions=[]
        self.after_semantic_read=None

    def observe_menu(self, actual, *, timeout_ms=2000):
        self.check_active()
        self.semantic_reads.append((dict(actual),timeout_ms))
        if actual == CHILD:
            elements=[row(0,LEAF_NAME)] if self.expanded and self.show_child else []
        else:
            elements=[row(0,PARENT_NAME,is_parent=True,**self.parent_changes)]
            if self.show_child:
                elements.append(row(1,LEAF_NAME,0,visible=self.expanded,**self.child_changes))
        result=observation(elements,actual=actual)
        if self.after_semantic_read:
            self.after_semantic_read(actual)
        return result

    def menu_action(self, actual, menu_target, operation, *, timeout_ms=10000):
        self.check_active()
        self.semantic_actions.append((dict(actual),copy.deepcopy(menu_target),operation,timeout_ms))
        if isinstance(self.helper_result,Exception):
            raise self.helper_result
        if self.helper_result.get("input_dispatched") is True:
            self.open_child()
        return copy.deepcopy(self.helper_result)

    def image_action(self, request, actual):
        if actual == TARGET:
            raise AssertionError("semantic parent must never compare an image")
        return super().image_action(request,actual)


class ResolveSemanticMenuTests(unittest.TestCase):
    def assert_code(self, code, observed, wanted, **kwargs):
        with self.assertRaises(OperationError) as caught:
            menus.resolve_menu(observed,wanted,**kwargs)
        self.assertEqual(caught.exception.code,code)

    def test_exact_parent_chain_selects_only_the_recorded_branch(self):
        values=[row(0,PARENT_NAME,is_parent=True),row(1,LEAF_NAME,0),
                row(2,"Other",is_parent=True),row(3,LEAF_NAME,2)]
        selected=menus.resolve_menu(observation(values),target(PARENT_NAME,LEAF_NAME),purpose="command")
        self.assertEqual(selected["item"]["index"],1)
        self.assertFalse(selected["authorizes_input"])
        self.assertNotIn("element_token",selected["item"])
        replaced=[row(0,"Other",is_parent=True),row(1,PARENT_NAME,0,is_parent=True),row(2,LEAF_NAME,1)]
        self.assert_code("menu_item_not_found",observation(replaced),target(PARENT_NAME,LEAF_NAME))

    def test_uia_unknown_parent_semantics_uses_independent_msaa_evidence(self):
        unknown=row(0,PARENT_NAME,has_children=None,has_popup=None,supported_actions=[])
        actual=row(0,PARENT_NAME,is_parent=True)
        actual["locator"]["source"]="direct_msaa"
        selected=menus.resolve_menu(observation(uia=source([unknown]),msaa=source([actual])),target(PARENT_NAME),purpose="parent")
        self.assertEqual(selected["source"],"direct_msaa")
        self.assertEqual(selected["item"]["locator"]["source"],"direct_msaa")

    def test_partial_uia_does_not_hide_a_complete_independent_msaa_menu(self):
        selected=menus.resolve_menu(observation(uia=source(complete=False,truncated=True,status="partial"),
            msaa=source([row(0,PARENT_NAME,is_parent=True)])),target(PARENT_NAME),purpose="parent")
        self.assertEqual(selected["source"],"direct_msaa")
        self.assert_code("menu_observation_incomplete",observation(uia=source(complete=False,truncated=True)),target(PARENT_NAME))

    def test_disabled_hidden_duplicate_and_parent_as_leaf_are_rejected(self):
        self.assert_code("menu_item_disabled",observation([row(0,PARENT_NAME,enabled=False)]),target(PARENT_NAME))
        self.assert_code("menu_item_hidden",observation([row(0,PARENT_NAME,visible=False)]),target(PARENT_NAME))
        self.assert_code("menu_item_ambiguous",observation([row(0,PARENT_NAME),row(1,PARENT_NAME)]),target(PARENT_NAME))
        self.assert_code("menu_command_not_leaf",observation([row(0,PARENT_NAME,is_parent=True)]),target(PARENT_NAME),purpose="command")

    def test_popup_flag_supports_parent_without_uia_expand_pattern(self):
        value=row(0,PARENT_NAME,has_children=False,has_popup=True,supported_actions=["hover","click_parent"])
        self.assertEqual(menus.resolve_menu(observation([value]),target(PARENT_NAME),purpose="parent")["item"]["index"],0)

    def test_saved_internal_automation_id_or_provider_locator_is_rejected(self):
        for extra in ({"automation_id":"414F29"},{"AutomationId":"414F29"},{"locator":{"path":[1]}},{"index":2}):
            value=target(PARENT_NAME)
            value["path"][0].update(extra)
            with self.subTest(extra=extra),self.assertRaises(OperationError):menus.validate_menu_target(value)
        with self.assertRaises(OperationError):menus.validate_menu_target({**target(PARENT_NAME),"pid":7})

    def test_cyclic_or_missing_ancestry_never_matches(self):
        self.assert_code("menu_observation_incomplete",observation([row(0,PARENT_NAME,1)]),target(PARENT_NAME))
        self.assert_code("menu_observation_incomplete",observation([row(0,PARENT_NAME,0)]),target(PARENT_NAME))


class SemanticMenuExpansionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("transient_popup.proven_popup",return_value=False))
        self.enterContext(mock.patch("transient_popup.register_popup",return_value=True))

    def test_expansion_uses_no_parent_image_and_only_one_parent_action(self):
        runtime=Runtime()
        result=execute_menu_expansion(runtime,step(image_target=image_target()),TARGET)
        self.assertTrue(result["task_verified"],result)
        self.assertFalse(result["business_result_verified"])
        self.assertEqual(result["menu_expansion"]["child_command_delivery"],"not_sent")
        self.assertEqual(len(runtime.semantic_actions),1)
        self.assertEqual(runtime.semantic_actions[0][2],"expand")
        self.assertEqual(result["metrics"]["parent_image_comparisons"],0)
        self.assertEqual(runtime.calls,[])
        self.assertTrue(all(probe.closed for probe in runtime.probes))

    def test_already_open_and_verify_only_resume_send_no_input(self):
        for verify in (False,True):
            runtime=Runtime();runtime.expanded=True
            runner=verify_menu_expansion if verify else execute_menu_expansion
            result=runner(runtime,step(),TARGET)
            self.assertTrue(result["task_verified"],result)
            self.assertFalse(result["input_dispatched"])
            self.assertEqual(runtime.semantic_actions,[])

    def test_unknown_delivery_is_never_replayed_on_resume(self):
        runtime=Runtime();runtime.helper_result={"input_dispatched":None,"diagnostic":{"code":"menu_input_unknown"}}
        first=execute_menu_expansion(runtime,step(),TARGET)
        self.assertIsNone(first["input_dispatched"])
        self.assertEqual(len(runtime.semantic_actions),1)
        second=verify_menu_expansion(runtime,step(),TARGET)
        self.assertFalse(second["task_verified"])
        self.assertEqual(len(runtime.semantic_actions),1)
        runtime.open_child()
        third=verify_menu_expansion(runtime,step(),TARGET)
        self.assertTrue(third["task_verified"])
        self.assertEqual(len(runtime.semantic_actions),1)

    def test_single_physical_child_requires_actual_saved_image_confirmation(self):
        runtime=Runtime();runtime.physical=True;runtime.open_child()
        runtime.image_result={"task_verified":False,"input_dispatched":False,"diagnostic":{"code":"image_not_found"}}
        failed=verify_menu_expansion(runtime,physical_step(),TARGET)
        self.assertFalse(failed["task_verified"],failed)
        self.assertTrue(runtime.calls)
        self.assertTrue(all(name=="wait_for_image" and actual==CHILD for name,actual in runtime.calls))
        self.assertEqual(runtime.semantic_actions,[])
        runtime.image_result={"task_verified":True,"input_dispatched":False}
        passed=verify_menu_expansion(runtime,physical_step(),TARGET)
        self.assertTrue(passed["task_verified"],passed)
        self.assertEqual(passed["child_target"],CHILD)

    def test_parent_precondition_ignores_image_appearance_and_requires_live_semantics(self):
        runtime=Runtime()
        result=menus.verify_menu_precondition(runtime,{"menu_target":target(PARENT_NAME),"image_target":image_target()},TARGET)
        self.assertTrue(result["task_verified"])
        self.assertEqual(result["metrics"]["image_comparisons"],0)
        self.assertFalse(result["business_result_verified"])
        runtime.parent_changes={"enabled":False}
        result=menus.verify_menu_precondition(runtime,{"menu_target":target(PARENT_NAME)},TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"],"menu_item_disabled")

    def test_too_short_step_limit_sends_no_input_and_closes_probe(self):
        runtime=Runtime()
        result=execute_menu_expansion(runtime,step(step_timeout_ms=1),TARGET)
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.semantic_actions,[])
        self.assertEqual(runtime.semantic_reads,[])
        self.assertTrue(all(probe.closed for probe in runtime.probes))

    def test_late_initial_child_observation_is_not_success_or_new_input(self):
        runtime=Runtime();runtime.expanded=True
        clock=[0.0]
        runtime.after_semantic_read=lambda actual:clock.__setitem__(0,clock[0]+.6)
        with mock.patch.object(menus.time,"monotonic",side_effect=lambda:clock[0]):
            result=execute_menu_expansion(runtime,step(step_timeout_ms=1000),TARGET)
        self.assertFalse(result["task_verified"],result)
        self.assertEqual(runtime.semantic_actions,[])

    def test_late_last_candidate_observation_does_not_choose_window(self):
        runtime=Runtime();runtime.physical=True;runtime.open_child()
        probe=runtime.create_transition_probe(TARGET)
        candidate=next(value for value in runtime.windows if value["window_id"]==CHILD["window_id"])
        clock=[0.0]
        runtime.after_semantic_read=lambda actual:clock.__setitem__(0,1.1)
        with mock.patch.object(menus.time,"monotonic",side_effect=lambda:clock[0]),self.assertRaises(OperationError):
            menus.choose_menu_window(runtime,probe,TARGET,[candidate],target(LEAF_NAME),timeout_ms=1000)

    def test_semantic_window_discovery_excludes_parent_and_verifies_path(self):
        runtime=Runtime();runtime.physical=True
        result=execute_menu_expansion(runtime,step(child_window={"title":"","class_name":legacy.POPUP_CLASS},
            child_menu_target=target(LEAF_NAME)),TARGET)
        self.assertTrue(result["task_verified"],result)
        self.assertEqual(result["child_target"],CHILD)
        self.assertTrue(any(actual==CHILD for actual,_ in runtime.semantic_reads))
        self.assertEqual(runtime.calls,[])


class MenuCompletionTests(unittest.TestCase):
    def setUp(self):
        import test_operations as fixtures
        self.fixtures = fixtures
        class ResultRuntime(fixtures.Runtime):
            def __init__(self, observations, acknowledgement=None):
                super().__init__(observations)
                self.menu_calls = []
                self.acknowledgement = acknowledgement if acknowledgement is not None else {"input_dispatched": True}
            def menu_action(self, actual, path, operation, **kwargs):
                self.check_active()
                self.menu_calls.append((copy.deepcopy(actual), copy.deepcopy(path), operation))
                if isinstance(self.acknowledgement, Exception): raise self.acknowledgement
                return copy.deepcopy(self.acknowledgement)
        self.Runtime = ResultRuntime
        self.owner = dict(fixtures.TARGET)
        self.popup = {**self.owner, "window_id": self.owner["window_id"] + 100}
        self.states = []

    def view(self, value):
        return self.fixtures.snapshot(self.fixtures.element(value=value))

    def command(self, **extra):
        return {"operation": "menu_command", "menu_target": target(PARENT_NAME, LEAF_NAME),
            "completion_mode": "conditions", "expect": [{"selector": self.fixtures.FIELD,
                "property": "value", "equals": "Ready", "require_change": True}],
            "verification_timeout_ms": 0, **extra}

    def execute(self, runtime, step=None, **extra):
        from operations import Operations
        return Operations(runtime).execute(step or self.command(), self.popup, completion_target=self.owner,
            save_verification=lambda state: self.states.append(copy.deepcopy(state)), **extra)

    def test_authored_result_conditions_verify_owner_without_reopening_popup(self):
        runtime = self.Runtime([self.view("Pending"), self.view("Ready")])
        result = self.execute(runtime)
        self.assertTrue(result["task_verified"], result)
        self.assertTrue(result["business_result_verified"])
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(len(runtime.menu_calls), 1)
        self.assertEqual(runtime.menu_calls[0][0], self.popup)
        self.assertTrue(all(args["window_id"] == self.owner["window_id"] for _, args in runtime.calls))
        self.assertEqual(len(self.states), 2)
        self.assertEqual(result["verification_scope"], "authored_menu_postconditions")

    def test_busy_seen_before_pause_survives_read_only_resume(self):
        runtime = self.Runtime([self.view("Ready"), self.view("Busy")])
        first = self.execute(runtime)
        self.assertFalse(first["task_verified"])
        self.assertTrue(first["input_dispatched"])
        self.assertTrue(self.states[-1]["change_seen"])
        runtime.observations = [self.view("Ready")]
        resumed = self.execute(runtime, verification_state=self.states[-1], verify_only=True)
        self.assertTrue(resumed["task_verified"], resumed)
        self.assertTrue(resumed["verification_only"])
        self.assertFalse(resumed["input_dispatched"])
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_ready_only_is_not_a_new_output_generation(self):
        runtime = self.Runtime([self.view("Ready"), self.view("Ready")])
        first = self.execute(runtime)
        self.assertFalse(first["task_verified"])
        runtime.observations = [self.view("Ready")]
        second = self.execute(runtime, verification_state=self.states[-1], verify_only=True)
        self.assertFalse(second["task_verified"])
        self.assertEqual(second["checks"][0]["reason"], "change_not_observed")
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_missing_or_changed_baseline_never_recreates_it_or_repeats_command(self):
        runtime = self.Runtime([self.view("Ready")])
        missing = self.execute(runtime, verify_only=True)
        self.assertEqual(missing["diagnostic"]["code"], "change_baseline_required")
        self.assertEqual(runtime.calls, [])
        runtime = self.Runtime([self.view("Pending"), self.view("Busy")])
        self.execute(runtime)
        for mutation in (lambda state: state.update(assertions_hash="0"*64),
                         lambda state: state["target"].update(window_id=999),
                         lambda state: state.update(baselines={})):
            state = copy.deepcopy(self.states[-1]); mutation(state)
            invalid = self.execute(runtime, verification_state=state, verify_only=True)
            self.assertEqual(invalid["diagnostic"]["code"], "invalid_verification_state")
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_unknown_acknowledgement_keeps_delivery_unknown_until_result_only_resume(self):
        runtime = self.Runtime([self.view("Pending"), self.view("Ready")], {"input_dispatched": None})
        first = self.execute(runtime)
        self.assertIsNone(first["input_dispatched"])
        self.assertFalse(first["task_verified"])
        self.assertTrue(first["postconditions_satisfied"])
        runtime.observations = [self.view("Ready")]
        resumed = self.execute(runtime, verification_state=self.states[-1], verify_only=True)
        self.assertTrue(resumed["task_verified"])
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_explicit_no_delivery_never_runs_postinput_checks(self):
        runtime = self.Runtime([self.view("Pending")], {"input_dispatched": False})
        result = self.execute(runtime)
        self.assertFalse(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_observation_or_persistence_failure_before_command_sends_nothing(self):
        runtime = self.Runtime([RuntimeError("result target unavailable")])
        result = self.execute(runtime)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.menu_calls, [])
        runtime = self.Runtime([self.view("Pending")])
        from operations import Operations
        result = Operations(runtime).execute(self.command(), self.popup, completion_target=self.owner,
            save_verification=lambda state: (_ for _ in ()).throw(OSError("disk error")))
        self.assertEqual(result["diagnostic"]["code"], "verification_state_save_failed")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.menu_calls, [])

    def test_save_failure_after_command_and_native_error_never_retry(self):
        runtime = self.Runtime([self.view("Pending"), self.view("Ready")])
        from operations import Operations
        states = []
        def persist(state):
            if states: raise OSError("disk error")
            states.append(state)
        result = Operations(runtime).execute(self.command(), self.popup, completion_target=self.owner,
            save_verification=persist)
        self.assertTrue(result["input_dispatched"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "verification_state_save_failed")
        self.assertEqual(len(runtime.menu_calls), 1)
        runtime = self.Runtime([self.view("Pending")], RuntimeError("native response lost"))
        result = self.execute(runtime)
        self.assertIsNone(result["input_dispatched"])
        self.assertEqual(len(runtime.menu_calls), 1)

    def test_human_mode_is_preserved_and_cannot_be_combined_with_conditions(self):
        human = {"operation": "menu_command", "menu_target": target(PARENT_NAME, LEAF_NAME), "completion_mode": "human"}
        runtime = self.Runtime([])
        result = menus.execute_menu_command(runtime, human, self.popup)
        self.assertFalse(result["task_verified"])
        self.assertTrue(result["verification_deferred"])
        for bad in ({**human, "expect": self.command()["expect"]}, {**self.command(), "expect": []},
                    {**self.command(), "completion_mode": "automatic"}, {**self.command(), "poll_interval_ms": 0}):
            with self.assertRaises(OperationError): menus.validate_menu_command(bad)
        static = self.command()
        static["expect"][0].pop("require_change")
        with self.assertRaises(OperationError): menus.validate_menu_command(static)
        resumed = menus.execute_menu_command(runtime, human, self.popup, verify_only=True)
        self.assertEqual(resumed["diagnostic"]["code"], "human_review_required")
        self.assertEqual(len(runtime.menu_calls), 1)


if __name__ == "__main__":
    unittest.main()
