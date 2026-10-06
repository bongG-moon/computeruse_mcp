import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import learning_picker as picker
from operations import OperationError


TARGET = {"pid": 42, "window_id": 73}


def row(index=1, label="신청 상태", role="ComboBox", **extras):
    return {"element_index": index, "role": role, "label": label, "actions": ["select"], **extras}


def identity(role="ComboBox", name="신청 상태", automation_id=""):
    return {"role": role, "name": name, "automation_id": automation_id, "is_password": False}


def selected(**extra):
    return {"status": "selected", "human_confirmed": True, **TARGET, "element": identity(), "ancestors": [],
            "point": {"x": 40, "y": 80}, "bounds": {"x": 10, "y": 50, "width": 100, "height": 60}, **extra}


def snapshot(*rows, **extra):
    return {**TARGET, "elements": list(rows), **extra}


class Runtime:
    mode = "uia"

    def __init__(self, path, responses=None):
        self.run_dir = Path(path)
        self.stop_event = threading.Event()
        self.responses = list(responses or [snapshot(row()), snapshot(row())])
        self.calls = []

    def check_active(self):
        if self.stop_event.is_set():
            raise OperationError("stopped", "stopped")

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return self.responses.pop(0)


class FakeChild:
    def __init__(self):
        self.pid = 888
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -1

    def wait(self, timeout):
        return self.returncode


class PickerMatchingTests(unittest.TestCase):
    def test_driver_frame_is_used_as_desktop_geometry(self):
        data = snapshot(row(frame={"x": 10, "y": 50, "w": 100, "h": 60}))
        self.assertEqual(picker.match_picked_element(data, selected(), TARGET)["element_index"], 1)
        data["elements"][0]["frame"]["x"] = 800
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(data, selected(), TARGET)
        self.assertEqual(failure.exception.picker_diagnostic["reason"], "geometry_mismatch")

    def test_missing_projected_id_matches_exact_name_role_and_same_native_rectangle(self):
        data = snapshot(row(frame={"x": 10, "y": 50, "w": 100, "h": 60}),
                        accessibility_normalization={"status": "rejected", "reason": "invalid_id_atom"})
        evidence = {}
        found = picker.match_picked_element(data, selected(element=identity(automation_id="status [1]")), TARGET, diagnostics=evidence)
        self.assertEqual(found["expected_selector"], {"name": "신청 상태", "role": "ComboBox"})
        self.assertEqual(evidence["matching_method"], "exact_name_role_and_native_rectangle_missing_driver_id")
        self.assertEqual(evidence["normalization_reason"], "invalid_id_atom")
        self.assertNotIn("status [1]", json.dumps(evidence))
        self.assertNotIn("신청 상태", json.dumps(evidence, ensure_ascii=False))

    def test_real_driver_rendered_id_rejection_does_not_drop_confirmed_element(self):
        data = snapshot(row(depth=1, frame={"x": 10, "y": 50, "w": 100, "h": 60}),
            tree_markdown='- Window "Fixture"\n  - [1] ComboBox "신청 상태" [id=status/1 actions=[select]]\n')
        observed = picker._payload({"structuredContent": data})
        self.assertEqual(observed["accessibility_normalization"]["status"], "rejected")
        evidence = {}
        found = picker.match_picked_element(observed, selected(element=identity(automation_id="status/1")), TARGET, diagnostics=evidence)
        self.assertEqual(found["element_index"], 1)
        self.assertEqual(evidence["normalization_reason"], "invalid_id_atom")

    def test_missing_projected_id_needs_geometry_and_never_overrides_conflicting_id(self):
        for extra in ({}, {"automation_id": "wrong", "frame": {"x": 10, "y": 50, "w": 100, "h": 60}},
                      {"frame": {"x": 0, "y": 0, "w": 1000, "h": 1000}}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                picker.match_picked_element(snapshot(row(**extra)), selected(element=identity(automation_id="expected")), TARGET)

    def test_existing_exact_id_geometry_failure_cannot_fall_back_to_different_row(self):
        data = snapshot(row(automation_id="status", frame={"x": 600, "y": 50, "w": 100, "h": 60}),
                        row(2, frame={"x": 10, "y": 50, "w": 100, "h": 60}))
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(data, selected(element=identity(automation_id="status")), TARGET)
        self.assertEqual(failure.exception.picker_diagnostic["reason"], "geometry_mismatch")

    def test_window_move_uses_two_equal_size_observed_window_rectangles(self):
        before = {"x": 0, "y": 0, "width": 800, "height": 600}
        after = {"x": -1200, "y": 100, "width": 800, "height": 600}
        data = snapshot(row(frame={"x": -1190, "y": 150, "w": 100, "h": 60}), window_bounds=after)
        evidence = {}
        found = picker.match_picked_element(data, selected(window_bounds=before), TARGET, diagnostics=evidence)
        self.assertEqual(found["element_index"], 1)
        self.assertTrue(evidence["window_translation_applied"])
        data["window_bounds"]["width"] = 1600
        with self.assertRaises(OperationError):
            picker.match_picked_element(data, selected(window_bounds=before), TARGET)

    def test_native_custom_child_is_not_silently_substituted_with_parent_button(self):
        data = snapshot(row(label="실행", role="Button", frame={"x": 10, "y": 50, "w": 100, "h": 60}))
        choice = selected(element=identity("Custom", "실행", "painted-start"), ancestors=[identity("Button", "실행")])
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(data, choice, TARGET)
        evidence = failure.exception.picker_diagnostic
        self.assertEqual(evidence["selected_role"], "Custom")
        self.assertEqual(evidence["role_candidates"], 0)
        self.assertEqual(evidence["reason"], "role_not_projected")
        self.assertNotIn("painted-start", json.dumps(evidence))

    def test_rendered_canvas_with_no_projected_controls_has_explicit_unsupported_diagnosis(self):
        data = snapshot(accessibility_normalization={"status": "rejected", "reason": "element_size_or_type"})
        choice = selected(element={**identity("Pane", "rendered canvas"), "has_control_patterns": False})
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(data, choice, TARGET)
        self.assertEqual(failure.exception.code, "picker_controls_not_exposed")
        self.assertEqual(failure.exception.picker_diagnostic["projected_element_count"], 0)
        self.assertEqual(failure.exception.picker_diagnostic["reason"], "no_controls_projected")
        self.assertIn("이미지 기반 선택", str(failure.exception))

    def test_native_container_only_failure_does_not_advise_repeat_selection(self):
        with self.assertRaises(OperationError) as failure:
            picker._selection_result({"status": "controls_not_exposed", "stage": "element_observation"})
        self.assertEqual(failure.exception.code, "picker_controls_not_exposed")
        self.assertIn("UIA 정보를 제공하지 않고 화면 영역만", str(failure.exception))

    def test_geometry_without_stable_identity_never_teaches(self):
        data = snapshot(row(label="", role="Custom", frame={"x": 10, "y": 50, "w": 100, "h": 60}))
        with self.assertRaises(OperationError):
            picker.match_picked_element(data, selected(element=identity("Custom", "", "")), TARGET)

    def test_geometry_disambiguation_still_requires_reusable_selector(self):
        data = snapshot(row(frame={"x": 10, "y": 50, "w": 100, "h": 60}),
                        row(2, frame={"x": 500, "y": 50, "w": 100, "h": 60}))
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(data, selected(), TARGET)
        self.assertEqual(failure.exception.picker_diagnostic["reason"], "no_unique_reusable_selector")

    def test_identity_returns_only_index_and_stable_selector(self):
        found = picker.match_picked_element(snapshot(row(value="SECRET", help_text="PRIVATE")), selected(), TARGET)
        self.assertEqual(found, {"element_index": 1, "expected_selector": {"name": "신청 상태", "role": "ComboBox"}})
        self.assertNotIn("SECRET", json.dumps(found))

    def test_duplicate_controls_resolve_by_unique_native_ancestor(self):
        data = snapshot(row(0, "조회 조건", "Group", actions=[]), row(parent_index=0),
                        row(2, "수정 조건", "Group", actions=[]), row(3, parent_index=2))
        chosen = selected(ancestors=[identity("Group", "수정 조건")])
        result = picker.match_picked_element(data, chosen, TARGET)
        self.assertEqual(result["element_index"], 3)
        self.assertEqual(result["expected_selector"]["within"], {"name": "수정 조건", "role": "Group"})

    def test_duplicate_controls_without_scope_are_refused(self):
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(snapshot(row(), row(2)), selected(), TARGET)
        self.assertEqual(failure.exception.code, "ambiguous_selector")

    def test_unique_id_uses_role_and_safe_scope_without_value_name(self):
        data = snapshot(row(0, "검색", "Group", actions=[]),
                        row(1, "SECRET", "Edit", value="SECRET", automation_id="customer", parent_index=0))
        result = picker.match_picked_element(data, selected(element=identity("Edit", "SECRET", "customer")), TARGET)
        self.assertEqual(result["expected_selector"], {"automation_id": "customer", "role": "Edit", "within": {"name": "검색", "role": "Group"}})
        self.assertNotIn("SECRET", json.dumps(result))

    def test_password_marker_refused_on_native_or_driver_side(self):
        variants = [(snapshot(row()), selected(element={**identity(), "is_password": True})),
                    (snapshot(row(is_password=True)), selected()),
                    (snapshot(row()), selected(ancestors=[{**identity("Group", "protected"), "is_password": True}]))]
        for data, choice in variants:
            with self.subTest(data=data, choice=choice), self.assertRaises(OperationError) as failure:
                picker.match_picked_element(data, choice, TARGET)
            self.assertEqual(failure.exception.code, "protected_element")

    def test_native_target_snapshot_target_and_geometry_mismatch_refused(self):
        for data, choice, code in [(snapshot(row()), selected(pid=99), "target_mismatch"),
                                   (snapshot(row(), window_id=99), selected(), "target_mismatch"),
                                   (snapshot(row()), selected(point={"x": 1000, "y": 80}), "picker_geometry_changed"),
                                   (snapshot(row()), selected(bounds={"x": float("nan"), "y": 0, "width": 100, "height": 100}), "picker_geometry_changed")]:
            with self.subTest(code=code), self.assertRaises(OperationError) as failure:
                picker.match_picked_element(data, choice, TARGET)
            self.assertEqual(failure.exception.code, code)

    def test_driver_geometry_is_checked_when_present(self):
        with self.assertRaises(OperationError) as failure:
            picker.match_picked_element(snapshot(row(bounds={"x": 600, "y": 0, "width": 50, "height": 50})), selected(), TARGET)
        self.assertEqual(failure.exception.code, "picker_not_found")

    def test_changed_role_and_dynamic_name_do_not_match(self):
        for data in (snapshot(row(role="Edit")), snapshot(row(label="다른 상태"))):
            with self.assertRaises(OperationError) as failure:
                picker.match_picked_element(data, selected(), TARGET)
            self.assertEqual(failure.exception.code, "picker_not_found")


class PickerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = Runtime(self.folder.name)

    def spawn_reply(self, status, *, stop=False):
        child = FakeChild()
        def spawn(args, **kwargs):
            self.assertEqual(args[1], "--pick")
            self.assertEqual(Path(args[0]).name, picker.HELPER_NAME)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            response = selected() if status == "selected" else {"status": status}
            response["nonce"] = request["nonce"]
            Path(args[3]).write_text(json.dumps(response), encoding="utf-8")
            if stop:
                self.runtime.stop_event.set()
            return child
        return child, spawn

    def test_cancel_terminates_only_owned_helper_and_removes_exchange(self):
        child, spawn = self.spawn_reply("cancelled")
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10)
        self.assertEqual(failure.exception.code, "picker_cancelled")
        self.assertTrue(child.terminated)
        self.assertEqual(list((self.runtime.run_dir / "learning").iterdir()), [])

    def test_long_install_directory_keeps_native_exchange_below_max_path_with_full_nonce(self):
        suffix = "d" * (195-len(str(self.runtime.run_dir))-1)
        self.runtime.run_dir = self.runtime.run_dir / suffix
        child, original_spawn = self.spawn_reply("cancelled")
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            self.assertEqual(len(request["nonce"]), 64)
            self.assertLess(len(args[3] + ".ready.json.tmp"), 260)
            return original_spawn(args, **kwargs)
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10)
        self.assertEqual(failure.exception.code, "picker_cancelled")
        self.assertTrue(child.terminated)
        self.assertEqual(list((self.runtime.run_dir / "learning").iterdir()), [])

    def test_late_selection_cannot_override_runtime_stop(self):
        child, spawn = self.spawn_reply("selected", stop=True)
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10)
        self.assertEqual(failure.exception.code, "picker_cancelled")
        self.assertTrue(child.terminated)

    def test_native_read_timeout_is_distinct_and_does_not_retry(self):
        child, spawn = self.spawn_reply("read_timeout")
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn) as process, self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10)
        self.assertEqual(failure.exception.code, "picker_read_timeout")
        self.assertEqual(process.call_count, 1)
        self.assertTrue(child.terminated)

    def test_unresponsive_helper_is_bounded_and_terminated(self):
        child = FakeChild()
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", return_value=child), patch.object(picker.time, "monotonic", side_effect=[0, 19]), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10)
        self.assertEqual(failure.exception.code, "picker_timeout")
        self.assertTrue(child.terminated)

    def test_fresh_guarded_observation_before_and_after_only(self):
        with patch.object(picker, "_run_helper", return_value=selected()):
            result = picker.pick_element(self.runtime, TARGET, "상태")
        self.assertEqual(result["element_index"], 1)
        self.assertEqual([name for name, _ in self.runtime.calls], ["get_window_state", "get_window_state"])
        self.assertTrue(all(not args["include_screenshot"] for _, args in self.runtime.calls))

    def test_foreign_or_denied_preflight_never_opens_helper(self):
        for answer in (snapshot(row(), pid=99), {"isError": True}):
            self.runtime.responses = [answer]
            with patch.object(picker, "_run_helper") as helper, self.assertRaises(OperationError):
                picker.pick_element(self.runtime, TARGET, "상태")
            helper.assert_not_called()

    def test_changed_post_selection_snapshot_is_not_saved(self):
        self.runtime.responses = [snapshot(row()), snapshot(row(role="Edit"))]
        with patch.object(picker, "_run_helper", return_value=selected()), self.assertRaises(OperationError) as failure:
            picker.pick_element(self.runtime, TARGET, "상태")
        self.assertEqual(failure.exception.code, "picker_not_found")

    def test_async_ready_requires_actual_owned_visible_window(self):
        child = FakeChild()
        notified = []
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            ready = {"nonce": request["nonce"], "status": "ready", **TARGET,
                     "helper_pid": child.pid, "helper_window_id": 999}
            Path(args[3] + ".ready.json").write_text(json.dumps(ready), encoding="utf-8")
            Path(args[3]).write_text(json.dumps({**selected(), "nonce": request["nonce"]}), encoding="utf-8")
            return child
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), \
                patch.object(picker, "_helper_visible", return_value=True) as visible:
            result = picker._run_helper(self.runtime, TARGET, "상태", 10, on_ready=notified.append)
        visible.assert_called_once_with(888, 999)
        self.assertEqual(notified, [{"helper_pid": 888, "helper_window_id": 999}])
        self.assertEqual(result["status"], "selected")
        self.assertEqual(list((self.runtime.run_dir / "learning").iterdir()), [])

    def test_async_ready_file_cannot_claim_hidden_or_foreign_helper_visible(self):
        for visible_result, helper_pid in ((False, 888), (True, 777)):
            child = FakeChild()
            notified = []
            def spawn(args, **kwargs):
                request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
                Path(args[3] + ".ready.json").write_text(json.dumps({"nonce": request["nonce"], "status": "ready",
                    **TARGET, "helper_pid": helper_pid, "helper_window_id": 999}), encoding="utf-8")
                return child
            with self.subTest(visible=visible_result, pid=helper_pid), patch.object(Path, "is_file", return_value=True), \
                    patch.object(picker.subprocess, "Popen", side_effect=spawn), \
                    patch.object(picker, "_helper_visible", return_value=visible_result), self.assertRaises(OperationError) as failure:
                picker._run_helper(self.runtime, TARGET, "상태", 10, on_ready=notified.append)
            self.assertEqual(failure.exception.code, "picker_not_visible")
            self.assertEqual(notified, [])
            self.assertTrue(child.terminated)

    def test_async_final_selection_without_ready_cannot_be_saved(self):
        child, spawn = self.spawn_reply("selected")
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10, on_ready=lambda value: None)
        self.assertEqual(failure.exception.code, "picker_not_visible")

    def test_native_startup_diagnostic_is_preserved(self):
        child = FakeChild()
        def spawn(args, **kwargs):
            request = json.loads(Path(args[2]).read_text(encoding="utf-8"))
            Path(args[3]).write_text(json.dumps({"nonce": request["nonce"], "status": "startup_failed",
                "code": "desktop_unavailable", "stage": "show", "error_type": "Win32Exception"}), encoding="utf-8")
            return child
        with patch.object(Path, "is_file", return_value=True), patch.object(picker.subprocess, "Popen", side_effect=spawn), self.assertRaises(OperationError) as failure:
            picker._run_helper(self.runtime, TARGET, "상태", 10, on_ready=lambda value: None)
        self.assertEqual(failure.exception.code, "picker_startup_failed")
        self.assertEqual(failure.exception.picker_diagnostic["code"], "desktop_unavailable")


if __name__ == "__main__":
    unittest.main()
