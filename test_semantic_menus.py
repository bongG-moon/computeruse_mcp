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


if __name__ == "__main__":
    unittest.main()
