"""Declarative workflow persistence and resumption; never controls a desktop."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from operations import OperationError
from server import ComputerManager, SessionError, TaskStore
from workflows import WorkflowError, WorkflowRunner, render_steps, validate_recipe


class FakeRuntime:
    def __init__(self, folder):
        self.mode = "uia"
        self.id = "synthetic-session"
        self.active = True
        self.programs = [{"id": "editor", "exe": str(Path(folder) / "Editor.exe"), "control_exes": []}]
        self.guard = SimpleNamespace(process_resolver=lambda pid: self.programs[0]["exe"],
                                     window_resolver=lambda hwnd: 100)

    def check_active(self):
        if not self.active:
            raise SessionError("synthetic cancellation")


class ScriptedOperations:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.reuse_flags = []

    def execute(self, step, target, delivery_mode="background", reuse_verified=False):
        self.calls.append((copy.deepcopy(step), copy.deepcopy(target), delivery_mode))
        self.reuse_flags.append(reuse_verified)
        response = self.responses.pop(0) if self.responses else {"task_verified": True}
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response()
        return copy.deepcopy(response)


class GuardedUIRuntime(FakeRuntime):
    """A small stateful UI double with consumed-observation/token enforcement."""
    def __init__(self, folder):
        super().__init__(folder)
        self.execution_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.guard.observed_targets = set()
        self.calls = []
        self.values = {}
        self.tokens = {}
        self.sequence = 0

    def call(self, name, arguments):
        with self.execution_lock:
            self.check_active()
            self.calls.append((name, copy.deepcopy(arguments)))
            target = arguments["pid"], arguments["window_id"]
            values = self.values.setdefault(target, {"첫 입력칸": "이전", "둘째 입력칸": "이전"})
            if name == "get_window_state":
                self.sequence += 1
                tokens = {f"snapshot{self.sequence}:{index}": label for index, label in enumerate(values)}
                self.tokens[target] = tokens
                self.guard.observed_targets.add(target)
                return {"structuredContent": {"pid": target[0], "window_id": target[1], "elements": [
                    {"label": label, "role": "Edit", "value": values[label], "element_token": token,
                     "actions": ["set_value"]} for token, label in tokens.items()]}}
            if name != "set_value" or target not in self.guard.observed_targets:
                raise AssertionError("Mutation requires a fresh observation for its exact window")
            token = arguments["element_token"]
            if token not in self.tokens[target]:
                raise AssertionError("Stale or foreign element token")
            self.guard.observed_targets.remove(target)
            values[self.tokens[target][token]] = arguments["value"]
            return {"structuredContent": {"effect": "confirmed"}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = FakeRuntime(self.tmp.name)
        self.runner = WorkflowRunner(self.tmp.name)
        self.target = [{"program_id": "editor", "pid": 100, "window_id": 200}]
        self.task = {"id": "example", "revision": 1, "name": "입력 시험", "instructions": "확인된 단계만 실행",
                     "expected": "두 입력칸 값 확인", "program_ids": ["editor"],
                     "variables": {"value": {"description": "입력할 값", "default": "기본값"}},
                     "steps": [{"program_id": "editor", "operation": "set_value",
                                "selector": {"name": "첫 입력칸", "role": "Edit"}, "value": "${value}"},
                               {"program_id": "editor", "operation": "set_value",
                                "selector": {"name": "둘째 입력칸", "role": "Edit"}, "value": "완료"}]}

    def run_recipe(self, engine, *, task=None, inputs=None, targets=None, resume=None, delivery="background"):
        with mock.patch("operations.Operations", return_value=engine):
            return self.runner.run(self.runtime, self.task if task is None else task,
                                   {} if inputs is None else inputs, self.target if targets is None else targets,
                                   resume_run_id=resume, delivery_mode=delivery)

    def checkpoint(self, run_id):
        return self.runner.progress(run_id)

    def test_defaults_overrides_and_substitution_are_literal_and_do_not_mutate_task(self):
        original = copy.deepcopy(self.task)
        rendered, values = render_steps(self.task, {})
        self.assertEqual(rendered[0]["value"], "기본값")
        self.assertEqual(values, {"value": "기본값"})
        literal = '한글 "quote" ${not_another_template} <tag>\\path'
        rendered, values = render_steps(self.task, {"value": literal})
        self.assertEqual(rendered[0]["value"], literal)
        self.assertEqual(values["value"], literal)
        self.assertEqual(self.task, original)

    def test_invalid_or_missing_inputs_and_undefined_templates_are_rejected(self):
        task = copy.deepcopy(self.task)
        del task["variables"]["value"]["default"]
        for inputs in ({}, {"other": "x"}, {"value": 1}, {"value": "x" * 4001}):
            with self.subTest(inputs=list(inputs)), self.assertRaises(WorkflowError):
                render_steps(task, inputs)
        task["steps"][0]["value"] = "${missing}"
        with self.assertRaises(WorkflowError):
            validate_recipe(task["steps"], task["variables"], task["program_ids"])

    def test_saved_steps_cannot_contain_code_handles_or_unverified_click(self):
        base = self.task["steps"][0]
        candidates = [{**base, "element_token": "old"}, {**base, "snapshot_id": "old"},
                      {**base, "operation": "shell", "value": "ignored"},
                      {"program_id": "editor", "operation": "click", "selector": {"name": "제출"}}]
        for step in candidates:
            with self.subTest(step=step), self.assertRaises((WorkflowError, OperationError)):
                validate_recipe([step], {}, ["editor"])

    def test_each_verified_step_advances_and_preserves_exact_bound_target(self):
        engine = ScriptedOperations()
        answer = self.run_recipe(engine, delivery="foreground")
        self.assertEqual(answer["status"], "verified")
        self.assertTrue(answer["task_verified"])
        self.assertEqual(answer["completed_steps"], 2)
        self.assertIsNone(answer["pending_step"])
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["set_value", "set_value"])
        self.assertTrue(all(target == {"pid": 100, "window_id": 200} for _, target, _ in engine.calls))
        self.assertTrue(all(delivery == "foreground" for _, _, delivery in engine.calls))
        self.assertEqual(engine.reuse_flags, [True, True])
        self.assertEqual(self.checkpoint(answer["run_id"])["completed_steps"], 2)

    def test_delivery_or_truthy_text_is_not_a_verified_postcondition(self):
        for outcome in ({"isError": False, "effect": "delivered"}, {"task_verified": False},
                        {"task_verified": "true"}):
            with self.subTest(outcome=outcome):
                engine = ScriptedOperations([outcome])
                answer = self.run_recipe(engine)
                self.assertEqual(answer["status"], "needs_review")
                self.assertFalse(answer["task_verified"])
                self.assertEqual(answer["completed_steps"], 0)
                self.assertEqual(answer["pending_step"], 0)
                self.assertEqual(len(engine.calls), 1)

    def test_resume_verifies_uncertain_step_without_replaying_it(self):
        initial = self.run_recipe(ScriptedOperations([{"task_verified": False}]))
        engine = ScriptedOperations()
        answer = self.run_recipe(engine, resume=initial["run_id"])
        self.assertTrue(answer["task_verified"])
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["assert", "set_value"])
        self.assertEqual(engine.calls[0][0]["expect"][0]["equals"], "기본값")
        self.assertEqual(engine.calls[1][0]["selector"]["name"], "둘째 입력칸")
        self.assertEqual(engine.reuse_flags, [False, True])

    def test_resume_with_unconfirmed_pending_result_stays_paused_without_writes(self):
        initial = self.run_recipe(ScriptedOperations([{"task_verified": False}]))
        engine = ScriptedOperations([{"task_verified": False}])
        answer = self.run_recipe(engine, resume=initial["run_id"])
        self.assertEqual(answer["status"], "needs_review")
        self.assertEqual(answer["completed_steps"], 0)
        self.assertEqual(answer["pending_step"], 0)
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["assert"])
        self.assertEqual(engine.reuse_flags, [False])

    def test_completed_run_resume_rechecks_last_result_and_never_replays_writes(self):
        initial = self.run_recipe(ScriptedOperations())
        engine = ScriptedOperations([{"task_verified": False}])
        answer = self.run_recipe(engine, resume=initial["run_id"])
        self.assertEqual(answer["status"], "needs_review")
        self.assertFalse(answer["task_verified"])
        self.assertEqual(answer["completed_steps"], 2)
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["assert"])
        self.assertEqual(engine.reuse_flags, [False])

    def test_real_operations_reuse_verified_observation_under_reentrant_runtime_lock(self):
        self.runtime = GuardedUIRuntime(self.tmp.name)
        answer = self.runner.run(self.runtime, self.task, {}, self.target)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([name for name, _ in self.runtime.calls],
                         ["get_window_state", "set_value", "get_window_state", "set_value", "get_window_state"])
        self.assertEqual(answer["last_result"]["metrics"]["reused_observations"], 1)
        self.assertEqual(self.runtime.values[(100, 200)], {"첫 입력칸": "기본값", "둘째 입력칸": "완료"})
        before = len(self.runtime.calls)
        resumed = self.runner.run(self.runtime, self.task, {}, self.target, resume_run_id=answer["run_id"])
        self.assertTrue(resumed["task_verified"])
        self.assertEqual([name for name, _ in self.runtime.calls[before:]], ["get_window_state"])
        self.assertEqual(resumed["last_result"]["metrics"]["reused_observations"], 0)

    def test_real_operations_cannot_reuse_observation_across_bound_windows(self):
        self.runtime = GuardedUIRuntime(self.tmp.name)
        task = copy.deepcopy(self.task)
        task["program_ids"].append("viewer")
        task["steps"][1]["program_id"] = "viewer"
        self.runtime.programs.append({"id": "viewer", "exe": str(Path(self.tmp.name) / "Viewer.exe")})
        self.runtime.guard.process_resolver = lambda pid: self.runtime.programs[0 if pid == 100 else 1]["exe"]
        self.runtime.guard.window_resolver = lambda hwnd: 300 if hwnd == 400 else 100
        targets = self.target + [{"program_id": "viewer", "pid": 300, "window_id": 400}]
        answer = self.runner.run(self.runtime, task, {}, targets)
        self.assertTrue(answer["task_verified"])
        self.assertEqual(answer["last_result"]["metrics"]["reused_observations"], 0)
        self.assertEqual([name for name, _ in self.runtime.calls].count("get_window_state"), 4)
        writes = [(args["pid"], args["window_id"]) for name, args in self.runtime.calls if name == "set_value"]
        self.assertEqual(writes, [(100, 200), (300, 400)])

    def test_changed_input_or_recipe_cannot_resume_previous_checkpoint(self):
        initial = self.run_recipe(ScriptedOperations([{"task_verified": False}]))
        changed = copy.deepcopy(self.task)
        changed["revision"] = 2
        for kwargs in ({"inputs": {"value": "다른 값"}}, {"task": changed}):
            engine = ScriptedOperations()
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkflowError):
                self.run_recipe(engine, resume=initial["run_id"], **kwargs)
            self.assertFalse(engine.calls)

    def test_window_bindings_require_exact_approved_program_and_positive_identifiers(self):
        invalid = [[], self.target * 2, [{"program_id": "unknown", "pid": 100, "window_id": 200}],
                   [{"program_id": "editor", "pid": True, "window_id": 200}],
                   [{"program_id": "editor", "pid": 100, "window_id": 0}]]
        for targets in invalid:
            engine = ScriptedOperations()
            with self.subTest(targets=targets), self.assertRaises(WorkflowError):
                self.run_recipe(engine, targets=targets)
            self.assertFalse(engine.calls)
        self.runtime.guard.process_resolver = lambda pid: str(Path(self.tmp.name) / "Other.exe")
        engine = ScriptedOperations()
        with self.assertRaises(WorkflowError):
            self.run_recipe(engine)
        self.assertFalse(engine.calls)

    def test_visual_and_inactive_sessions_never_dispatch_a_workflow(self):
        self.runtime.mode = "visual"
        engine = ScriptedOperations()
        with self.assertRaises(WorkflowError):
            self.run_recipe(engine)
        self.runtime.mode = "uia"
        self.runtime.active = False
        with self.assertRaises(SessionError):
            self.run_recipe(engine)
        self.assertFalse(engine.calls)

    def test_multiple_programs_require_session_approval_and_keep_separate_window_bindings(self):
        task = copy.deepcopy(self.task)
        task["program_ids"].append("viewer")
        task["steps"][1]["program_id"] = "viewer"
        targets = self.target + [{"program_id": "viewer", "pid": 300, "window_id": 400}]
        engine = ScriptedOperations()
        with self.assertRaises(WorkflowError):
            self.run_recipe(engine, task=task, targets=targets)
        self.assertFalse(engine.calls)
        self.runtime.programs.append({"id": "viewer", "exe": str(Path(self.tmp.name) / "Viewer.exe")})
        self.runtime.guard.process_resolver = lambda pid: self.runtime.programs[0 if pid == 100 else 1]["exe"]
        self.runtime.guard.window_resolver = lambda hwnd: 300 if hwnd == 400 else 100
        answer = self.run_recipe(engine, task=task, targets=targets)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([target for _, target, _ in engine.calls],
                         [{"pid": 100, "window_id": 200}, {"pid": 300, "window_id": 400}])

    def test_cancellation_between_steps_persists_verified_boundary(self):
        def finish_and_cancel():
            self.runtime.active = False
            return {"task_verified": True}
        engine = ScriptedOperations([finish_and_cancel])
        answer = self.run_recipe(engine)
        self.assertEqual(answer["status"], "interrupted")
        self.assertFalse(answer["task_verified"])
        self.assertTrue(answer["next_step"])
        files = list(self.runner.root.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(answer["run_id"], files[0].stem)
        checkpoint = self.checkpoint(files[0].stem)
        self.assertEqual(checkpoint["status"], "interrupted")
        self.assertEqual(checkpoint["completed_steps"], 1)
        self.assertIsNone(checkpoint["pending_step"])
        self.assertEqual(len(engine.calls), 1)

    def test_driver_error_retains_uncertain_boundary_without_private_checkpoint_content(self):
        engine = ScriptedOperations([RuntimeError("PRIVATE DRIVER DETAIL")])
        answer = self.run_recipe(engine, inputs={"value": "PRIVATE INPUT"})
        self.assertEqual(answer["status"], "interrupted")
        self.assertFalse(answer["task_verified"])
        self.assertTrue(answer["next_step"])
        self.assertNotIn("PRIVATE", json.dumps(answer))
        path = next(self.runner.root.glob("*.json"))
        self.assertEqual(answer["run_id"], path.stem)
        record = self.checkpoint(path.stem)
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["completed_steps"], 0)
        self.assertEqual(record["pending_step"], 0)
        self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))
        self.assertNotIn("steps", record)
        self.assertNotIn("inputs", record)
        self.assertNotIn("targets", record)

    def test_unverified_screen_evidence_returned_but_never_saved_to_checkpoint(self):
        answer = self.run_recipe(ScriptedOperations([{"task_verified": False, "screen": "PRIVATE SCREEN"}]),
                                 inputs={"value": "PRIVATE INPUT"})
        self.assertEqual(answer["last_result"]["screen"], "PRIVATE SCREEN")
        path = self.runner.root / (answer["run_id"] + ".json")
        self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))
        self.assertNotIn("last_result", self.checkpoint(answer["run_id"]))

    def test_resume_read_error_is_recorded_as_interrupted_and_does_not_replay(self):
        initial = self.run_recipe(ScriptedOperations([{"task_verified": False}]))
        engine = ScriptedOperations([RuntimeError("PRIVATE RESUME FAILURE")])
        answer = self.run_recipe(engine, resume=initial["run_id"])
        self.assertEqual(answer["run_id"], initial["run_id"])
        self.assertEqual(answer["status"], "interrupted")
        self.assertTrue(answer["next_step"])
        self.assertNotIn("PRIVATE", json.dumps(answer))
        record = self.checkpoint(initial["run_id"])
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["pending_step"], 0)
        self.assertEqual(record["completed_steps"], 0)
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["assert"])

    def test_progress_rejects_bad_paths_and_corrupt_records_without_overwriting(self):
        for run_id in ("../private", "x" * 32, 123, ""):
            with self.subTest(run_id=run_id), self.assertRaises(WorkflowError):
                self.runner.progress(run_id)
        initial = self.run_recipe(ScriptedOperations())
        path = self.runner.root / (initial["run_id"] + ".json")
        path.write_text('{"broken": true}', encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.runner.progress(initial["run_id"])
        self.assertEqual(path.read_text(encoding="utf-8"), '{"broken": true}')

    def test_progress_rejects_extra_fields_missing_fields_and_invalid_metadata_types(self):
        answer = self.run_recipe(ScriptedOperations())
        run_id = answer["run_id"]
        original = self.checkpoint(run_id)
        path = self.runner.root / (run_id + ".json")
        cases = [{**original, "inputs": {"text": "PRIVATE INJECTED INPUT"}},
                 {key: value for key, value in original.items() if key != "session_id"}]
        for key, value in (("completed_steps", True), ("total_steps", 2.0),
                           ("pending_step", True), ("pending_step", 2),
                           ("revision", True), ("revision", 0),
                           ("task_verified", "true"), ("task_verified", False),
                           ("status", "unknown"), ("status", []),
                           ("recipe_hash", "bad-hash"), ("inputs_hash", 123),
                           ("task_id", ""), ("session_id", {}), ("created_at", 123),
                           ("duration_ms", True), ("duration_ms", -1),
                           ("duration_ms", float("nan")), ("duration_ms", float("inf"))):
            cases.append({**original, key: value})
        for index, invalid in enumerate(cases):
            with self.subTest(index=index):
                contents = json.dumps(invalid, ensure_ascii=False)
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(WorkflowError):
                    self.runner.progress(run_id)
                self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_server_workflow_failure_exposes_checkpoint_and_resume_verification(self):
        config = {"state_dir": self.tmp.name, "programs": self.runtime.programs}
        manager = ComputerManager(config)
        manager.tasks.save(self.task_without_revision())
        request = {"task_id": self.task["id"], "targets": self.target}
        engine = ScriptedOperations([{"task_verified": False}])
        with mock.patch("operations.Operations", return_value=engine):
            with self.assertRaises(SessionError):
                manager.call("computer_run_task", request)
            manager.session = self.runtime
            first = manager.call("computer_run_task", request)
        self.assertTrue(first["isError"])
        run_id = first["structuredContent"]["run_id"]
        progress = manager.call("computer_task_progress", {"run_id": run_id})["structuredContent"]
        self.assertEqual(progress["status"], "needs_review")
        engine = ScriptedOperations()
        with mock.patch("operations.Operations", return_value=engine):
            resumed = manager.call("computer_run_task", {**request, "resume_run_id": run_id})
        self.assertFalse(resumed["isError"])
        self.assertEqual([entry[0]["operation"] for entry in engine.calls], ["assert", "set_value"])

    def task_without_revision(self):
        return {key: copy.deepcopy(value) for key, value in self.task.items() if key != "revision"}

    def test_server_interrupted_run_returns_recoverable_id_and_progress_without_exception_text(self):
        manager = ComputerManager({"state_dir": self.tmp.name, "programs": self.runtime.programs})
        manager.tasks.save(self.task_without_revision())
        manager.session = self.runtime
        engine = ScriptedOperations([RuntimeError("PRIVATE DRIVER ERROR")])
        with mock.patch("operations.Operations", return_value=engine):
            answer = manager.call("computer_run_task", {"task_id": self.task["id"], "targets": self.target})
        self.assertTrue(answer["isError"])
        result = answer["structuredContent"]
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["next_step"])
        self.assertNotIn("PRIVATE", json.dumps(answer))
        progress = manager.call("computer_task_progress", {"run_id": result["run_id"]})["structuredContent"]
        self.assertEqual(progress["pending_step"], 0)
        self.assertEqual(progress["completed_steps"], 0)
        self.assertEqual(progress["status"], "interrupted")

    def test_reconnected_manager_discovers_interrupted_run_without_run_id(self):
        answer = self.run_recipe(ScriptedOperations([RuntimeError("PRIVATE FAILURE")]),
                                 inputs={"value": "PRIVATE INPUT"})
        manager = ComputerManager({"state_dir": self.tmp.name, "programs": self.runtime.programs})
        listed = manager.call("computer_task_progress", {})["structuredContent"]
        self.assertEqual([item["run_id"] for item in listed["runs"]], [answer["run_id"]])
        self.assertEqual(listed["runs"][0]["status"], "interrupted")
        self.assertEqual(listed["runs"][0]["pending_step"], 0)
        self.assertEqual(listed["invalid_records"], 0)
        self.assertNotIn("PRIVATE", json.dumps(listed))
        self.assertIsNone(manager.session)
        exact = manager.call("computer_task_progress", {"run_id": answer["run_id"]})["structuredContent"]
        self.assertEqual(exact["run_id"], answer["run_id"])
        with self.assertRaises(SessionError):
            manager.call("computer_task_progress", {"run_id": answer["run_id"], "task_id": self.task["id"]})

    def test_recent_records_filter_latest_ten_and_report_skipped_corruption(self):
        first = self.run_recipe(ScriptedOperations([{"task_verified": False}]))
        source = self.checkpoint(first["run_id"])
        stamp = time.time_ns()
        os.utime(self.runner.root / (first["run_id"] + ".json"), ns=(stamp - 1_000_000_000, stamp - 1_000_000_000))
        for index in range(12):
            run_id = f"{index:032x}"
            item = {**source, "run_id": run_id, "task_id": "match" if index % 2 == 0 else "other"}
            path = self.runner.root / (run_id + ".json")
            path.write_text(json.dumps(item), encoding="utf-8")
            modified = stamp + index * 1_000_000
            os.utime(path, ns=(modified, modified))
        invalid = [(f"{255:032x}", "{broken"),
                   (f"{256:032x}", json.dumps({**source, "run_id": f"{256:032x}", "inputs": "PRIVATE INJECTION"}))]
        for index, (run_id, contents) in enumerate(invalid):
            path = self.runner.root / (run_id + ".json")
            path.write_text(contents, encoding="utf-8")
            modified = stamp + (100 + index) * 1_000_000
            os.utime(path, ns=(modified, modified))
        manager = ComputerManager({"state_dir": self.tmp.name, "programs": self.runtime.programs})
        recent = manager.call("computer_task_progress", {})["structuredContent"]
        self.assertEqual([item["run_id"] for item in recent["runs"]], [f"{i:032x}" for i in range(11, 1, -1)])
        self.assertEqual(recent["invalid_records"], 2)
        self.assertFalse(recent["scan_limited"])
        filtered = manager.call("computer_task_progress", {"task_id": "match"})["structuredContent"]
        self.assertEqual([item["run_id"] for item in filtered["runs"]], [f"{i:032x}" for i in range(10, -1, -2)])
        self.assertEqual(filtered["invalid_records"], 2)
        self.assertNotIn("PRIVATE", json.dumps(filtered))
        for item in filtered["runs"]:
            self.assertFalse({"inputs", "steps", "targets", "last_result"} & item.keys())
        for run_id, contents in invalid:
            self.assertEqual((self.runner.root / (run_id + ".json")).read_text(encoding="utf-8"), contents)
        self.assertEqual(manager.call("computer_task_progress", {"task_id": "absent"})["structuredContent"]["runs"], [])

    def test_recent_on_empty_store_and_invalid_filter_do_not_start_a_session(self):
        manager = ComputerManager({"state_dir": self.tmp.name, "programs": self.runtime.programs})
        self.assertEqual(manager.call("computer_task_progress", {})["structuredContent"],
                         {"runs": [], "invalid_records": 0, "scan_limited": False})
        with self.assertRaises(WorkflowError):
            self.runner.recent("../private")
        self.assertIsNone(manager.session)


class WorkflowTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"state_dir": self.tmp.name, "programs": [{"id": "editor"}]}
        self.store = TaskStore(self.tmp.name, self.config)
        self.text = {"id": "first", "name": "반복 입력", "instructions": "PRIVATE INSTRUCTION",
                     "expected": "PRIVATE EXPECTATION", "program_ids": ["editor"]}
        self.recipe = {**self.text, "variables": {"text": {"default": "기본값", "description": "입력값"}},
                       "steps": [{"program_id": "editor", "operation": "set_value",
                                  "selector": {"name": "입력칸"}, "value": "${text}"}]}

    def test_text_only_legacy_task_stays_readable_and_savable(self):
        old = {**self.text, "updated_at": "2026-01-01T00:00:00Z"}
        self.store.path.write_text(json.dumps({"version": 1, "tasks": [old]}), encoding="utf-8")
        self.assertEqual(self.store.get("first"), old)
        new = self.store.save({**self.text, "name": "새 이름"})
        self.assertEqual(new["revision"], 1)
        self.assertNotIn("steps", new)
        self.assertNotIn("variables", new)

    def test_text_editor_updates_preserve_saved_recipe_and_increment_revision(self):
        first = self.store.save(self.recipe)
        edited = self.store.save({**self.text, "name": "수정한 이름"})
        self.assertEqual(edited["steps"], first["steps"])
        self.assertEqual(edited["variables"], first["variables"])
        self.assertEqual(edited["revision"], first["revision"] + 1)
        self.assertEqual(TaskStore(self.tmp.name, self.config).get("first"), edited)

    def test_invalid_recipe_save_preserves_existing_bytes(self):
        self.store.save(self.recipe)
        before = self.store.path.read_bytes()
        with self.assertRaises(SessionError):
            self.store.save({**self.recipe, "steps": [{"program_id": "editor", "operation": "shell"}]})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_server_task_list_is_paginated_and_omits_private_details(self):
        self.store.save(self.recipe)
        self.store.save({**self.text, "id": "second", "name": "OTHER Example"})
        self.store.save({**self.text, "id": "third", "name": "마지막"})
        manager = ComputerManager(self.config)
        first = manager.call("computer_tasks", {"limit": 2})["structuredContent"]
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(len(first["tasks"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(first))
        self.assertTrue(first["tasks"][0]["runnable"])
        self.assertEqual(first["tasks"][0]["step_count"], 1)
        for entry in first["tasks"]:
            self.assertFalse({"instructions", "expected", "steps", "variables"} & entry.keys())
        last = manager.call("computer_tasks", {"offset": 2, "limit": 2})["structuredContent"]
        self.assertEqual([t["id"] for t in last["tasks"]], ["third"])
        self.assertIsNone(last["next_offset"])
        found = manager.call("computer_tasks", {"query": "other example"})["structuredContent"]
        self.assertEqual([t["id"] for t in found["tasks"]], ["second"])
        self.assertEqual(found["total"], 1)
        for args in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": True}):
            with self.subTest(args=args), self.assertRaises(SessionError):
                manager.call("computer_tasks", args)


if __name__ == "__main__":
    unittest.main()
