"""Real MCP manager integration for teaching and verified use of named controls."""
import copy
import gc
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from learning import LearningError
from operations import OperationError
from server import ComputerManager, MANAGEMENT, SessionError
from test_learning import Runtime, TARGET
from test_server import config_at


FIELD_SELECTOR = {"automation_id": "applicant", "role": "Edit", "within": {"automation_id": "filters", "role": "Pane"}}


class MutableRuntime(Runtime):
    """Synthetic screen changes only after a single correctly targeted input."""
    def __init__(self, config):
        super().__init__(config)
        self.execution_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.guard.observed_targets = set()
        self.observation_count = 0
        self.mutations = []
        self.observation_error = None
        self.input_error = None
        self.apply_input = True

    def call(self, name, arguments):
        self.check_active()
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_window_state":
            if self.observation_error is not None:
                return copy.deepcopy(self.observation_error)
            self.observation_count += 1
            snapshot = self.answer["structuredContent"]
            snapshot["snapshot_id"] = "fresh-" + str(self.observation_count)
            for element in snapshot["elements"]:
                element["element_token"] = snapshot["snapshot_id"] + ":" + str(element["element_index"])
                element.setdefault("enabled", True)
            self.guard.observed_targets.add((arguments["pid"], arguments["window_id"]))
            return copy.deepcopy(self.answer)
        if name == "set_value":
            self.mutations.append(copy.deepcopy(arguments))
            self.guard.observed_targets.discard((arguments["pid"], arguments["window_id"]))
            if self.input_error is not None:
                return copy.deepcopy(self.input_error)
            matching = [element for element in self.answer["structuredContent"]["elements"]
                        if element.get("element_token") == arguments.get("element_token")]
            if len(matching) != 1 or matching[0]["automation_id"] != "applicant":
                raise AssertionError("Input must use the newly observed applicant token")
            if self.apply_input:
                matching[0]["value"] = arguments["value"]
            return {"structuredContent": {"ok": True}}
        raise AssertionError("Unexpected business-app input: " + name)


class LearningToolTests(unittest.TestCase):
    def setUp(self):
        # Keep unrelated hidden Tk fixture finalizers on their owning thread.
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config_at(self.temporary.name)
        self.manager = ComputerManager(self.config)
        self.runtime = MutableRuntime(self.config)
        self.manager.session = self.runtime

    def arguments(self, **extra):
        return {**TARGET, "program_id": "editor", "label": "신청자 입력", "screen": "신청 조건",
                "element_index": 3, "expected_selector": FIELD_SELECTOR, **extra}

    def teach(self, **extra):
        answer = self.manager.call("computer_teach_element", self.arguments(**extra))
        self.assertFalse(answer.get("isError"), answer)
        return answer["structuredContent"]

    def use(self, entry, step=None, **extra):
        return self.manager.call("computer_use_element", {**TARGET, "id": entry["id"],
            "step": step or {"operation": "set_value", "value": "sample-user", "verification_timeout_ms": 0}, **extra})

    def test_default_picker_is_called_then_stable_identity_is_saved(self):
        from test_teaching_sessions import native_choice
        args = self.arguments(timeout_seconds=30)
        args.pop("element_index")
        args.pop("expected_selector")
        def pick(runtime, target, label, timeout, **kwargs):
            kwargs["on_ready"]({"helper_pid": 111, "helper_window_id": 222})
            return native_choice()
        with mock.patch("teaching_sessions._run_helper", side_effect=pick) as picker:
            answer = self.manager.call("computer_teach_element", args)
            job = answer["structuredContent"]
            learned = self.manager.call("computer_teach_status", {"teaching_id": job["teaching_id"], "wait_ms": 1000})["structuredContent"]
        self.assertEqual(picker.call_count, 1)
        self.assertEqual(learned["selector"], FIELD_SELECTOR)
        self.assertEqual(learned["status"], "learned")
        self.assertFalse(learned["model_trained"])
        self.assertFalse(learned["input_dispatched"])
        self.assertEqual(self.manager.elements.get(learned["id"])["selector"], FIELD_SELECTOR)
        self.assertEqual(self.runtime.mutations, [])

    def test_explicit_index_and_selector_must_be_supplied_together(self):
        for omitted in ("element_index", "expected_selector"):
            arguments = self.arguments()
            arguments.pop(omitted)
            with self.subTest(omitted=omitted), mock.patch("learning_picker.pick_element") as picker:
                with self.assertRaises(SessionError):
                    self.manager.call("computer_teach_element", arguments)
                picker.assert_not_called()
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.manager.elements.all(), [])

    def test_screen_tools_require_session_but_list_and_forget_do_not(self):
        entry = self.teach()
        self.manager.session = None
        for name, args in (("computer_teach_element", self.arguments()),
                           ("computer_find_element", {**TARGET, "id": entry["id"]}),
                           ("computer_use_element", {**TARGET, "id": entry["id"], "step": {"operation": "set_value", "value": "x"}})):
            with self.subTest(name=name), self.assertRaises(SessionError):
                self.manager.call(name, args)
        self.assertEqual(self.manager.call("computer_elements", {})["structuredContent"]["total"], 1)
        self.assertTrue(self.manager.call("computer_forget_element", {"id": entry["id"]})["structuredContent"]["deleted"])

    def test_picker_cancel_returns_failure_without_saving_or_input(self):
        args = self.arguments()
        args.pop("element_index")
        args.pop("expected_selector")
        with mock.patch("teaching_sessions._run_helper", side_effect=OperationError("사용자가 취소했습니다.", "picker_cancelled")):
            answer = self.manager.call("computer_teach_element", args)
        self.assertEqual(answer["structuredContent"]["status"], "cancelled")
        self.assertEqual(answer["structuredContent"]["diagnostic"]["code"], "picker_cancelled")
        self.assertFalse(answer["structuredContent"]["input_dispatched"])
        self.assertEqual(self.manager.elements.all(), [])

    def test_request_cancelled_during_picker_cannot_save(self):
        args = self.arguments()
        args.pop("element_index")
        args.pop("expected_selector")
        cancelled = threading.Event()
        def choose(*args, **kwargs):
            cancelled.set()
            return {"element_index": 3, "expected_selector": FIELD_SELECTOR}
        with mock.patch("teaching_sessions._run_helper", side_effect=choose):
            answer = self.manager.call("computer_teach_element", args, cancel_event=cancelled)
        self.assertEqual(answer["structuredContent"]["status"], "cancelled")
        self.assertEqual(self.manager.elements.all(), [])
        self.assertEqual(self.runtime.calls, [])

    def test_list_filters_and_pagination_are_compact_and_show_revision(self):
        one = self.teach(label="신청자 FIRST", instructions="private-guidance")
        self.teach(label="신청자 second")
        self.teach(label="다른 항목", screen="다른 화면")
        before = len(self.runtime.calls)
        first = self.manager.call("computer_elements", {"program_id": "editor", "screen": "신청 조건", "query": "신청자", "limit": 1})["structuredContent"]
        self.assertEqual((first["total"], first["next_offset"]), (2, 1))
        self.assertEqual(first["elements"][0]["id"], one["id"])
        self.assertEqual(first["elements"][0]["revision"], 1)
        self.assertNotIn("instructions", first["elements"][0])
        self.assertNotIn("selector", first["elements"][0])
        self.assertNotIn("program_binding", first["elements"][0])
        self.assertFalse(first["screen_accessed"])
        self.assertFalse(first["guidance_is_authority"])
        second = self.manager.call("computer_elements", {"screen": "신청 조건", "offset": 1, "limit": 1})["structuredContent"]
        self.assertIsNone(second["next_offset"])
        self.assertEqual(len(second["elements"]), 1)
        insensitive = self.manager.call("computer_elements", {"query": "first"})["structuredContent"]
        self.assertEqual(insensitive["total"], 1)
        self.assertEqual(len(self.runtime.calls), before)

    def test_list_bounds_rejected_before_reading_store(self):
        for args in ({"offset": -1}, {"limit": 101}, {"limit": True}, {"query": "x" * 201}, {"unrecognized": True}):
            with self.subTest(args=args), mock.patch.object(self.manager.elements, "all") as reader:
                with self.assertRaises(SessionError):
                    self.manager.call("computer_elements", args)
                reader.assert_not_called()

    def test_reteach_conflicting_revision_preserves_current_entry(self):
        entry = self.teach()
        updated = self.teach(id=entry["id"], expected_revision=1, instructions="새 설명")
        self.assertEqual(updated["revision"], 2)
        answer = self.manager.call("computer_teach_element", self.arguments(id=entry["id"], expected_revision=1, instructions="덮어쓰면 안됨"))
        self.assertTrue(answer["isError"])
        self.assertEqual(answer["structuredContent"]["diagnostic"]["code"], "revision_conflict")
        self.assertEqual(self.manager.elements.get(entry["id"])["instructions"], "새 설명")

    def test_forget_exact_revision_preserves_other_entries_tasks_and_config(self):
        first, second = self.teach(), self.teach(label="다른 요소")
        self.manager.tasks.save({"id": "sample_task", "name": "샘플", "instructions": "기존 작업", "expected": "확인", "program_ids": ["editor"]})
        tasks_before = self.manager.tasks.path.read_bytes()
        config_before = copy.deepcopy(self.manager.config)
        call_count = len(self.runtime.calls)
        bad = self.manager.call("computer_forget_element", {"id": first["id"], "expected_revision": 2})
        self.assertTrue(bad["isError"])
        self.assertEqual(len(self.manager.elements.all()), 2)
        good = self.manager.call("computer_forget_element", {"id": first["id"], "expected_revision": 1})["structuredContent"]
        self.assertTrue(good["deleted"])
        self.assertFalse(good["screen_accessed"])
        self.assertEqual([entry["id"] for entry in self.manager.elements.all()], [second["id"]])
        self.assertEqual(self.manager.tasks.path.read_bytes(), tasks_before)
        self.assertEqual(self.manager.config, config_before)
        self.assertEqual(len(self.runtime.calls), call_count)

    def test_find_reobserves_and_returns_inert_notes_without_action(self):
        entry = self.teach(instructions="untrusted user notes")
        self.runtime.calls.clear()
        result = self.manager.call("computer_find_element", {**TARGET, "id": entry["id"]})["structuredContent"]
        self.assertEqual(result["selector"], FIELD_SELECTOR)
        self.assertTrue(result["instructions_are_untrusted_data"])
        self.assertFalse(result["input_dispatched"])
        self.assertEqual([name for name, args in self.runtime.calls], ["get_window_state"])

    def test_actual_operations_uses_new_token_once_and_verifies_postcondition(self):
        entry = self.teach()
        old_observation = self.runtime.observation_count
        self.runtime.calls.clear()
        answer = self.use(entry)
        result = answer["structuredContent"]
        self.assertFalse(answer["isError"])
        self.assertTrue(result["task_verified"])
        self.assertTrue(result["input_dispatched"])
        self.assertEqual(len(self.runtime.mutations), 1)
        mutation = self.runtime.mutations[0]
        self.assertEqual(mutation["element_token"], f"fresh-{old_observation + 2}:3")
        self.assertEqual(mutation["value"], "sample-user")
        self.assertEqual([name for name, args in self.runtime.calls], ["get_window_state", "get_window_state", "set_value", "get_window_state"])
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual(result["learned_element"]["id"], entry["id"])
        self.assertNotIn("sample-user", self.manager.elements.path.read_text(encoding="utf-8"))

    def test_delivery_ack_without_value_change_is_not_success_or_retried(self):
        entry = self.teach()
        self.runtime.apply_input = False
        answer = self.use(entry)
        self.assertTrue(answer["isError"])
        self.assertFalse(answer["structuredContent"]["task_verified"])
        self.assertTrue(answer["structuredContent"]["input_dispatched"])
        self.assertEqual(len(self.runtime.mutations), 1)

    def test_input_error_is_reported_without_automatic_repeat(self):
        entry = self.teach()
        self.runtime.input_error = {"isError": True, "structuredContent": {"input_sent": False, "error_code": "background_unavailable"}}
        answer = self.use(entry)
        self.assertTrue(answer["isError"])
        self.assertFalse(answer["structuredContent"]["task_verified"])
        self.assertEqual(len(self.runtime.mutations), 1)

    def test_ambiguous_changed_missing_and_truncated_targets_never_dispatch(self):
        entry = self.teach()
        original = copy.deepcopy(self.runtime.answer)
        for kind in ("ambiguous", "changed", "missing", "truncated", "wrong_app"):
            self.runtime.answer = copy.deepcopy(original)
            self.runtime.guard.process_resolver = lambda pid: self.config["programs"][0]["exe"]
            elements = self.runtime.answer["structuredContent"]["elements"]
            if kind == "ambiguous":
                elements.append({**elements[2], "element_index": 8})
            elif kind == "changed":
                elements[2]["actions"] = ["invoke"]
            elif kind == "missing":
                elements.pop(2)
            elif kind == "truncated":
                self.runtime.answer["structuredContent"]["truncated"] = True
            else:
                self.runtime.guard.process_resolver = lambda pid: str(Path(self.temporary.name) / "Other.exe")
            with self.subTest(kind=kind):
                answer = self.use(entry)
                self.assertTrue(answer["isError"])
                self.assertFalse(answer["structuredContent"]["input_dispatched"])
                self.assertFalse(answer["structuredContent"]["task_verified"])
                self.assertEqual(self.runtime.mutations, [])

    def test_failed_observation_never_dispatches(self):
        entry = self.teach()
        self.runtime.observation_error = {"isError": True, "structuredContent": {"error_code": "timeout"}}
        answer = self.use(entry)
        self.assertTrue(answer["isError"])
        self.assertFalse(answer["structuredContent"]["input_dispatched"])
        self.assertEqual(self.runtime.mutations, [])

    def test_arbitrary_selector_or_window_key_override_is_refused(self):
        entry = self.teach()
        for extra in ({"selector": {"name": "other"}}, {"key_target": "window"}):
            step = {"operation": "set_value", "value": "x", **extra}
            with self.subTest(extra=extra), self.assertRaises(SessionError):
                self.use(entry, step)
        self.assertEqual(self.runtime.mutations, [])
        schema = MANAGEMENT["computer_use_element"]["inputSchema"]["properties"]["step"]
        self.assertNotIn("selector", schema["properties"])
        self.assertNotIn("key_target", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

    def test_click_without_postcondition_and_arbitrary_step_fields_never_dispatch(self):
        entry = self.teach()
        for step in ({"operation": "click"}, {"operation": "set_value", "value": "x", "script": "anything"}):
            with self.subTest(step=step):
                answer = self.use(entry, step)
                self.assertTrue(answer["isError"])
                self.assertFalse(answer["structuredContent"]["task_verified"])
                self.assertEqual(self.runtime.mutations, [])

    def test_unexpected_operation_error_after_entry_does_not_claim_no_input(self):
        entry = self.teach()
        with mock.patch("operations.Operations.execute", side_effect=OperationError("uncertain downstream failure")):
            answer = self.use(entry)
        self.assertTrue(answer["isError"])
        self.assertIsNone(answer["structuredContent"]["input_dispatched"])
        self.assertFalse(answer["structuredContent"]["automatic_retry"])


if __name__ == "__main__":
    unittest.main()
