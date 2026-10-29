"""Semantic menu transitions using deterministic providers; no desktop input."""
import copy
import threading
import unittest
from unittest import mock

from menu_expansion import execute_menu_expansion, verify_menu_expansion
from operations import OperationError, Operations, validate_step, verification_step
from recording_windows import execute_window_wait
from test_image_steps import image_target


TARGET = {"pid": 7, "window_id": 12}
CHILD = {"pid": 7, "window_id": 22}
POPUP_CLASS = "WindowsForms10.Window.20808.app.0.abc_r10_ad1"
PARENT = {"role": "MenuItem", "name": "Export to excel"}
LEAF = {"role": "MenuItem", "name": "Active sheet"}


def step(**extra):
    return {"operation": "expand_menu", "selector": PARENT,
            "child_selector": LEAF, "expansion_method": "click",
            "verification_timeout_ms": 0, **extra}


def image_step(**extra):
    return {"operation": "expand_menu", "image_target": image_target(),
            "child_window": {"title": "", "class_name": POPUP_CLASS},
            "child_image_target": image_target(), "expansion_method": "hover",
            "verification_timeout_ms": 0, **extra}


def element(index, name, parent=None, **extra):
    return {"element_index": index, "element_token": f"fresh:{index}",
            "role": "MenuItem", "name": name, "enabled": True,
            "is_offscreen": False, "actions": ["invoke"],
            **({"parent_index": parent} if parent is not None else {}), **extra}


def window(hwnd, owner=0, **extra):
    return {"pid": 7, "window_id": hwnd, "visible": True, "title": "",
            "class_name": POPUP_CLASS, "thread_id": 19,
            "owner_window_id": owner, "root_owner_window_id": owner,
            "bounds": [100, 100, 400, 300], **extra}


class Probe:
    def __init__(self, runtime, target):
        self.runtime, self.target, self.closed = runtime, target, False

    def capture(self):
        return self.snapshot()

    def snapshot(self):
        return {"creation_time": self.runtime.creation_time, "executable": "fixture.exe",
                "process_exited": False,
                "target_present": any(row["window_id"] == self.target["window_id"] for row in self.runtime.windows),
                "windows": copy.deepcopy(self.runtime.windows)}

    transition_snapshot = snapshot

    def close(self):
        self.closed = True


class Runtime:
    mode = "uia"

    def __init__(self):
        self.stop_event = threading.Event()
        self.expanded = False
        self.show_child = True
        self.creation_time = 100
        self.windows = [window(12, class_name="Main")]
        self.calls, self.probes, self.expansions = [], [], []
        self.private_evidence = []
        self.parent_changes = {}
        self.child_changes = {}
        self.extra_elements = []
        self.driver_error = None
        self.helper_result = {"input_dispatched": True, "task_verified": False,
                              "verification_deferred": True}
        self.image_result = {"input_dispatched": False, "task_verified": True}
        self.parent_image_result = {"input_dispatched": False, "task_verified": True}
        self.physical = False
        self.after_image = None
        self.on_observe = None

    def check_active(self):
        if self.stop_event.is_set():
            raise OperationError("stopped", "session_stopped")

    def create_transition_probe(self, target):
        probe = Probe(self, target)
        self.probes.append(probe)
        return probe

    def capture(self, target):
        items = [element(0, "Export to excel", **{"actions": ["expand"], **self.parent_changes})]
        if self.show_child:
            items.append(element(1, "Active sheet", 0,
                **{"is_offscreen": not self.expanded, **self.child_changes}))
        items.extend(copy.deepcopy(self.extra_elements))
        result = {**target, "snapshot_id": "fresh", "elements": items}
        if self.on_observe:
            self.on_observe()
        return result

    def open_child(self):
        self.expanded = True
        if self.physical and not any(r["window_id"] == 22 for r in self.windows):
            owner = self.windows[0].get("owner_window_id") or 12
            self.windows.append(window(22, owner))

    def call(self, name, args):
        self.check_active()
        self.calls.append((name, copy.deepcopy(args)))
        if name == "get_window_state":
            return {"structuredContent": self.capture({k: args[k] for k in ("pid", "window_id")})}
        if name in {"click", "press_key"}:
            if self.driver_error is not None:
                if isinstance(self.driver_error, Exception):
                    raise self.driver_error
                return self.driver_error
            self.open_child()
            return {"structuredContent": {"input_sent": True}}
        raise AssertionError(f"unexpected tool {name}")

    def expand_menu_once(self, target, request, **private_arguments):
        self.check_active()
        self.expansions.append((copy.deepcopy(target), copy.deepcopy(request)))
        self.private_evidence.append(private_arguments)
        if private_arguments.get("parent_revalidate"):
            private_arguments["parent_revalidate"]()
        if isinstance(self.helper_result, Exception):
            raise self.helper_result
        if self.helper_result.get("input_dispatched") is True:
            self.open_child()
        return copy.deepcopy(self.helper_result)

    def image_action(self, request, target):
        self.check_active()
        self.calls.append((request["operation"], copy.deepcopy(target)))
        if request["operation"] == "wait_for_image":
            if target == TARGET:
                return copy.deepcopy(self.parent_image_result)
            if self.after_image:
                self.after_image()
            return copy.deepcopy(self.image_result)
        self.open_child()
        return {"input_dispatched": True, "verification_deferred": True, "task_verified": False}


class MenuExpansionTests(unittest.TestCase):
    def setUp(self):
        self.proof = mock.patch("transient_popup.proven_popup", return_value=False).start()
        self.register = mock.patch("transient_popup.register_popup", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_selector_click_observes_fresh_descendant_and_never_invokes_leaf(self):
        runtime = Runtime()
        result = Operations(runtime).execute(step(), TARGET)
        self.assertTrue(result["task_verified"], result)
        self.assertFalse(result["business_result_verified"])
        self.assertEqual(result["verification_scope"], "menu_child_visibility_only")
        self.assertEqual(result["child_representation"], "logical_uia_child")
        self.assertEqual(result["menu_expansion"]["menu_path"], ["Export to excel", "Active sheet"])
        self.assertEqual(result["menu_expansion"]["child_command_delivery"], "not_sent")
        actions = [(name, args) for name, args in runtime.calls if name != "get_window_state"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][0], "click")
        self.assertEqual(actions[0][1]["element_token"], "fresh:0")
        self.assertTrue(all(probe.closed for probe in runtime.probes))

    def test_recorded_keyboard_uses_parent_handle_once(self):
        runtime = Runtime()
        result = execute_menu_expansion(runtime, step(expansion_method="keyboard", expansion_key="RIGHT"), TARGET)
        self.assertTrue(result["task_verified"], result)
        actions = [(name, args) for name, args in runtime.calls if name != "get_window_state"]
        self.assertEqual([(name, args["key"], args["element_token"]) for name, args in actions], [("press_key", "RIGHT", "fresh:0")])

    def test_image_keyboard_never_performs_implicit_focus_click_or_observation(self):
        for key in ("ENTER", "SPACE", "RIGHT"):
            runtime = Runtime()
            result = execute_menu_expansion(runtime, image_step(expansion_method="keyboard", expansion_key=key), TARGET)
            self.assertEqual(result["diagnostic"]["code"], "menu_expand_method_unsupported")
            self.assertFalse(result["input_dispatched"])
            self.assertEqual(runtime.calls, [])
            self.assertEqual(runtime.expansions, [])
            self.assertEqual(runtime.probes, [])

    def test_already_visible_child_and_resume_send_no_input(self):
        for verify in (False, True):
            runtime = Runtime()
            runtime.expanded = True
            runner = verify_menu_expansion if verify else execute_menu_expansion
            result = runner(runtime, step(), TARGET)
            self.assertTrue(result["task_verified"], result)
            self.assertFalse(result["input_dispatched"])
            self.assertEqual(result["step_delivery"], "not_sent")
            self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_same_name_outside_parent_is_not_accepted(self):
        runtime = Runtime()
        runtime.show_child = False
        runtime.extra_elements = [element(9, "Other menu"), element(10, "Active sheet", 9)]
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "menu_child_timeout")
        self.assertEqual([n for n, _ in runtime.calls].count("click"), 1)

    def test_duplicate_child_name_in_same_parent_is_rejected_before_input(self):
        runtime = Runtime()
        runtime.expanded = True
        runtime.extra_elements = [element(2, "Active sheet", 0)]
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_child_ambiguous")
        self.assertFalse(result["input_dispatched"])

    def test_duplicate_parent_name_is_not_guessed(self):
        runtime = Runtime()
        runtime.extra_elements = [element(9, "Export to excel")]
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "ambiguous_selector")
        self.assertFalse(result["input_dispatched"])

    def test_disabled_visible_child_prevents_input(self):
        runtime = Runtime()
        runtime.expanded = True
        runtime.child_changes = {"enabled": False}
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_child_disabled")
        self.assertFalse(result["input_dispatched"])

    def test_visibility_unknown_is_not_open_child(self):
        runtime = Runtime()
        runtime.expanded = True
        runtime.child_changes = {"is_offscreen": None}
        result = verify_menu_expansion(runtime, step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "menu_child_timeout")
        self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_zero_bounds_and_offscreen_are_not_open_children(self):
        for patch in ({"bounds": {"x": 0, "y": 0, "width": 0, "height": 0}}, {"is_offscreen": True}):
            runtime = Runtime()
            runtime.expanded = True
            runtime.child_changes = patch
            self.assertFalse(verify_menu_expansion(runtime, step(), TARGET)["task_verified"])

    def test_parent_without_expand_evidence_is_not_invoked(self):
        runtime = Runtime()
        runtime.show_child = False
        runtime.parent_changes = {"actions": ["invoke"]}
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_capability_unavailable")
        self.assertFalse(result["input_dispatched"])

    def test_accessibility_method_requires_observed_pattern_and_backend(self):
        runtime = Runtime()
        runtime.parent_changes = {"actions": ["invoke"]}
        result = execute_menu_expansion(runtime, step(expansion_method="accessibility"), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_capability_unavailable")
        self.assertEqual(runtime.expansions, [])

    def test_unsupported_hover_is_explicit_and_does_not_click(self):
        runtime = Runtime()
        runtime.helper_result = {"input_dispatched": False, "diagnostic": {"code": "menu_expand_method_unsupported"}}
        result = execute_menu_expansion(runtime, step(expansion_method="hover"), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_expand_method_unsupported")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(len(runtime.expansions), 1)
        self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_logical_hover_pairs_image_with_fresh_parent_bounds(self):
        runtime = Runtime()
        runtime.parent_changes = {"frame": {"x": -150, "y": 40, "w": 100, "h": 25}}
        request = step(expansion_method="hover", image_target=image_target())
        result = execute_menu_expansion(runtime, request, TARGET)
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(result["child_representation"], "logical_uia_child")
        self.assertEqual(runtime.private_evidence[0]["parent_bounds"], {"x": -150, "y": 40, "width": 100, "height": 25})
        self.assertTrue(callable(runtime.private_evidence[0]["parent_revalidate"]))
        self.assertEqual(len(runtime.expansions), 1)
        self.assertNotIn("parent_bounds", runtime.expansions[0][1])
        self.assertTrue(all(name == "get_window_state" for name, _ in runtime.calls))

    def test_logical_hover_without_actual_bounds_sends_no_motion(self):
        runtime = Runtime()
        result = execute_menu_expansion(runtime, step(expansion_method="hover", image_target=image_target()), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_geometry_unavailable")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.expansions, [])

    def test_logical_hover_revalidates_parent_after_approval_before_motion(self):
        runtime = Runtime()
        runtime.parent_changes = {"frame": {"x": 10, "y": 40, "w": 100, "h": 25}, "runtime_id": [1, 2, 3]}
        def guarded(target, request, **private):
            runtime.parent_changes["runtime_id"] = [1, 2, 4]
            try:
                private["parent_revalidate"]()
            except OperationError as error:
                return {"input_dispatched": False, "diagnostic": {"code": error.code}}
            self.fail("replaced parent must not authorize hover")
        runtime.expand_menu_once = guarded
        result = execute_menu_expansion(runtime, step(expansion_method="hover", image_target=image_target()), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_changed")
        self.assertFalse(result["input_dispatched"])

    def test_unknown_driver_input_is_not_retried_and_resume_only_observes(self):
        runtime = Runtime()
        runtime.driver_error = RuntimeError("response lost")
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertIsNone(result["input_dispatched"])
        self.assertEqual(result["step_delivery"], "unknown")
        runtime.expanded = True
        resumed = verify_menu_expansion(runtime, step(), TARGET)
        self.assertTrue(resumed["task_verified"], resumed)
        self.assertEqual([n for n, _ in runtime.calls].count("click"), 1)

    def test_explicit_driver_no_input_preserves_not_sent(self):
        runtime = Runtime()
        runtime.driver_error = {"isError": True, "structuredContent": {"input_sent": False, "error_code": "denied"}}
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(result["step_delivery"], "not_sent")
        self.assertEqual([n for n, _ in runtime.calls].count("click"), 1)

    def test_lifetime_change_during_read_blocks_parent_input(self):
        runtime = Runtime()
        runtime.on_observe = lambda: setattr(runtime, "creation_time", 101)
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_changed")
        self.assertFalse(result["input_dispatched"])

    def test_stop_prevents_all_input(self):
        runtime = Runtime()
        runtime.stop_event.set()
        result = execute_menu_expansion(runtime, step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "session_stopped")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.calls, [])

    def test_image_hover_opens_child_with_one_guarded_parent_motion(self):
        runtime = Runtime()
        runtime.physical = True
        result = execute_menu_expansion(runtime, image_step(), TARGET)
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(result["child_representation"], "owned_popup")
        self.assertEqual(result["child_target"], CHILD)
        self.assertEqual(len(runtime.expansions), 1)
        self.assertEqual(runtime.calls, [("wait_for_image", TARGET), ("wait_for_image", CHILD)])
        self.assertEqual(result["menu_expansion"]["child_command_delivery"], "not_sent")

    def test_physical_resume_reads_open_child_without_parent_input(self):
        runtime = Runtime()
        runtime.physical = True
        runtime.open_child()
        result = verify_menu_expansion(runtime, image_step(), TARGET)
        self.assertTrue(result["task_verified"], result)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.expansions, [])

    def test_sibling_menu_uses_proven_owner_and_excludes_parent_popup(self):
        runtime = Runtime()
        runtime.physical = True
        runtime.windows = [window(12, 8), window(8, class_name="Main")]
        self.proof.return_value = True
        with mock.patch("transient_popup.popup_owner", return_value={"pid": 7, "window_id": 8}):
            result = execute_menu_expansion(runtime, image_step(), TARGET)
        self.assertTrue(result["task_verified"], result)
        self.assertEqual(result["child_target"], CHILD)
        self.assertEqual(len(runtime.expansions), 1)

    def test_already_open_same_owner_child_requires_current_parent_image(self):
        runtime = Runtime()
        runtime.physical = True
        runtime.open_child()
        original = runtime.image_action
        def read(request, target):
            if target == TARGET:
                return {"task_verified": False, "image_match": {"status": "not_found"}, "diagnostic": {"code": "image_not_found"}}
            return original(request, target)
        runtime.image_action = read
        result = verify_menu_expansion(runtime, image_step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "menu_parent_image_not_found")
        self.assertFalse(result["input_dispatched"])
        self.assertEqual(runtime.expansions, [])

    def test_foreign_owner_candidate_is_not_selected(self):
        runtime = Runtime()
        runtime.windows.append(window(22, 99))
        result = verify_menu_expansion(runtime, image_step(), TARGET)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["diagnostic"]["code"], "menu_child_timeout")
        self.assertEqual(runtime.expansions, [])

    def test_native_uncertainty_never_retries(self):
        runtime = Runtime()
        runtime.physical = True
        runtime.helper_result = {"input_dispatched": None, "diagnostic": {"code": "popup_input_unknown"}}
        result = execute_menu_expansion(runtime, image_step(), TARGET)
        self.assertIsNone(result["input_dispatched"])
        self.assertEqual(result["diagnostic"]["code"], "popup_input_unknown")
        self.assertEqual(len(runtime.expansions), 1)

    def test_changed_child_identity_or_geometry_after_match_is_rejected(self):
        for field, changed in (("thread_id", 99), ("owner_window_id", 99), ("bounds", [0, 0, 20, 20])):
            runtime = Runtime()
            runtime.physical = True
            runtime.after_image = lambda r=runtime, k=field, v=changed: r.windows[-1].update({k: v})
            result = execute_menu_expansion(runtime, image_step(), TARGET)
            self.assertFalse(result["task_verified"], result)
            self.assertEqual(result["diagnostic"]["code"], "menu_child_changed")
            self.assertEqual(len(runtime.expansions), 1)

    def test_child_image_ambiguity_is_not_hidden_as_timeout(self):
        runtime = Runtime()
        runtime.physical = True
        runtime.image_result = {"task_verified": False, "image_match": {"status": "ambiguous"}, "diagnostic": {"code": "image_ambiguous"}}
        result = execute_menu_expansion(runtime, image_step(), TARGET)
        self.assertEqual(result["diagnostic"]["code"], "menu_child_ambiguous")
        self.assertFalse(result["business_result_verified"])

    def test_wait_excluded_parent_cannot_be_returned_by_msaa(self):
        runtime = Runtime()
        runtime.windows = [window(8, class_name="Main"), window(12, 8)]
        runtime.recover_msaa_dropdown = mock.Mock(return_value=TARGET)
        wait = {"operation": "wait_for_window", "program_id": "app", "owner_ref": "main", "window_ref": "child", "title": "", "class_name": POPUP_CLASS, "timeout_ms": 0}
        result = execute_window_wait(runtime, wait, {("app", "main"): {"pid": 7, "window_id": 8}},
                                     image_hint=image_target(), exclude_window_ids={12})
        self.assertEqual(result["diagnostic"]["code"], "recording_window_representation_mismatch")
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["input_dispatched"])
        self.assertIn("parent_menu_excluded", result["observed_candidates"][0]["rejected_by"])

    def test_schema_rejects_runtime_ids_unbound_children_and_public_verify_only(self):
        invalid = [dict(step(), verify_only=True), dict(step(), child_window={"title": "", "class_name": POPUP_CLASS, "window_id": 9}),
                   dict(step(), image_target=image_target()), dict(step(), expansion_method="invoke"),
                   dict(step(), expansion_method="keyboard"), dict(step(), selector={"role": "Button", "name": "Export"}),
                   {"operation": "expand_menu", "image_target": image_target(), "expansion_method": "hover", "child_window": {"title": "", "class_name": POPUP_CLASS}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OperationError):
                validate_step(value)
        with self.assertRaises(OperationError):
            verification_step(step())
        with self.assertRaises(OperationError):
            validate_step({"operation": "click", "selector": PARENT, "child_selector": LEAF})


if __name__ == "__main__":
    unittest.main()
