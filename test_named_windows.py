"""Saved workflows bind exact current windows without saving HWNDs or guessing."""
import copy
import unittest
from unittest import mock
from test_workflows import WorkflowTests, ScriptedOperations
from workflows import WorkflowError, validate_recipe


class NamedWindowTests(WorkflowTests):
    # Reuse fixtures/helpers without duplicating the inherited test methods.
    def named_task(self):
        task = copy.deepcopy(self.task)
        task["steps"][0]["window_ref"] = "first"
        task["steps"][1]["window_ref"] = "second"
        return task
    def named_targets(self):
        return [{"program_id": "editor", "window_ref": name, "pid": 100, "window_id": hwnd}
                for name, hwnd in (("first", 201), ("second", 202))]
    def test_same_program_two_windows_use_exact_distinct_bindings(self):
        engine = ScriptedOperations()
        result = self.run_recipe(engine, task=self.named_task(), targets=self.named_targets())
        self.assertTrue(result["task_verified"])
        self.assertEqual([c[1]["window_id"] for c in engine.calls], [201, 202])
        self.assertTrue(all("window_ref" not in c[0] for c in engine.calls))
    def test_named_bindings_are_required_unique_and_not_variable_templates(self):
        for targets in (self.named_targets()[:1], self.named_targets() * 2,
                        [{**t, "window_title": "doc"} for t in self.named_targets()]):
            with self.assertRaises(WorkflowError):
                self.run_recipe(ScriptedOperations(), task=self.named_task(), targets=targets)
        task = self.named_task()
        for bad in ("${value}", "../../file", "", 3):
            task["steps"][0]["window_ref"] = bad
            with self.assertRaises(WorkflowError):
                validate_recipe(task["steps"], task["variables"], task["program_ids"])
    def test_late_title_is_resolved_per_step_not_cached(self):
        responses = [201, 202]
        def windows(name, args):
            self.assertEqual(name, "list_windows")
            self.assertEqual(args, {"pid": 100, "on_screen_only": True})
            return {"structuredContent": {"windows": [{"pid": 100, "window_id": responses.pop(0), "title": "文書"}]}}
        self.runtime.call = windows
        engine = ScriptedOperations()
        result = self.run_recipe(engine, targets=[{"program_id": "editor", "pid": 100, "window_title": "文書"}])
        self.assertTrue(result["task_verified"])
        self.assertEqual([c[1]["window_id"] for c in engine.calls], [201, 202])
    def test_missing_or_ambiguous_window_records_no_pending_input(self):
        for windows in ([], [{"pid": 101, "window_id": 2, "title": "Document"}],
                        [{"pid": 100, "window_id": i, "title": "Document"} for i in (2, 3)]):
            self.runtime.call = lambda *_: {"structuredContent": {"windows": windows}}
            engine = ScriptedOperations()
            answer = self.run_recipe(engine, targets=[{"program_id": "editor", "pid": 100, "window_title": "Document"}])
            self.assertEqual(answer["status"], "needs_binding")
            self.assertIsNone(answer["pending_step"])
            self.assertEqual(engine.calls, [])
            self.assertEqual(self.checkpoint(answer["run_id"])["status"], "needs_binding")
    def test_rebinding_after_missing_window_runs_unattempted_step_once(self):
        self.runtime.call = lambda *_: {"structuredContent": {"windows": []}}
        first = self.run_recipe(ScriptedOperations(), targets=[{"program_id": "editor", "pid": 100, "window_title": "Later"}])
        engine = ScriptedOperations()
        resumed = self.run_recipe(engine, resume=first["run_id"])
        self.assertTrue(resumed["task_verified"])
        self.assertEqual([c[0]["operation"] for c in engine.calls], ["set_value", "set_value"])
    def test_uncertain_step_in_named_window_is_rechecked_only(self):
        first = self.run_recipe(ScriptedOperations([{"task_verified": True}, {"task_verified": False}]), task=self.named_task(), targets=self.named_targets())
        engine = ScriptedOperations()
        new_targets = [{**t, "window_id": t["window_id"] + 100} for t in self.named_targets()]
        resumed = self.run_recipe(engine, task=self.named_task(), targets=new_targets, resume=first["run_id"])
        self.assertTrue(resumed["task_verified"])
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0][0]["operation"], "assert")
        self.assertEqual(engine.calls[0][1]["window_id"], 302)
    def test_process_identity_is_rechecked_between_steps(self):
        original = self.runtime.guard.process_resolver
        def changed():
            self.runtime.guard.process_resolver = lambda _: self.runtime.programs[0]["exe"] + ".different.exe"
            return {"task_verified": True}
        engine = ScriptedOperations([changed])
        result = self.run_recipe(engine)
        self.assertEqual(result["status"], "needs_binding")
        self.assertEqual(result["completed_steps"], 1)
        self.assertIsNone(result["pending_step"])
        self.assertEqual(len(engine.calls), 1)
    def test_many_programs_and_windows_are_independent(self):
        task = self.named_task()
        task["program_ids"].append("other")
        task["steps"][1]["program_id"] = "other"
        self.runtime.programs.append({"id": "other", "exe": self.runtime.programs[0]["exe"] + ".other.exe", "control_exes": []})
        self.runtime.guard.process_resolver = lambda pid: self.runtime.programs[0 if pid == 100 else 1]["exe"]
        self.runtime.guard.window_resolver = lambda hwnd: 101 if hwnd == 202 else 100
        targets = self.named_targets()
        targets[1].update(program_id="other", pid=101)
        engine = ScriptedOperations()
        result = self.run_recipe(engine, task=task, targets=targets)
        self.assertTrue(result["task_verified"])
        self.assertEqual([c[1]["pid"] for c in engine.calls], [100, 101])
    def test_checkpoint_failure_before_input_can_resume_unattempted_step(self):
        from vendor.guard import atomic_json
        writes = []
        def once(path, value):
            writes.append(copy.deepcopy(value))
            if len(writes) == 1:
                raise PermissionError("PRIVATE file busy")
            atomic_json(path, value)
        engine = ScriptedOperations()
        with mock.patch("workflows.atomic_json", side_effect=once):
            first = self.run_recipe(engine)
        self.assertEqual(first["status"], "interrupted")
        self.assertIsNone(first["pending_step"])
        self.assertEqual(first["diagnostic"]["stage"], "checkpoint_before_input")
        self.assertEqual(first["diagnostic"]["error_type"], "PermissionError")
        self.assertNotIn("PRIVATE", str(first))
        self.assertEqual(engine.calls, [])
        resumed_engine = ScriptedOperations()
        resumed = self.run_recipe(resumed_engine, resume=first["run_id"])
        self.assertTrue(resumed["task_verified"])
        self.assertEqual([c[0]["operation"] for c in resumed_engine.calls], ["set_value", "set_value"])


# Keep just this module's cases; the shared base suite is discovered separately.
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(NamedWindowTests(name) for name in NamedWindowTests.__dict__ if name.startswith("test_"))
