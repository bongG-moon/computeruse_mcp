import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from repeat_profiles import RepeatProfiles
from workflows import WorkflowError, WorkflowRunner
from test_workflows import FakeRuntime, ScriptedOperations


class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runner = WorkflowRunner(self.temp.name)
        self.task = {"id": "repeat", "revision": 1, "program_ids": ["editor"], "steps": [
            {"program_id": "editor", "operation": "set_value", "selector": {"name": "Field", "role": "Edit"}, "value": "${input}"}],
            "variables": {"input": {"description": "value", "default": "alpha"}}}
        self.targets = [{"program_id": "editor", "pid": 100, "window_id": 200}]

    def run_task(self, mode="auto", *, task=None, inputs=None, verified=True):
        engine = ScriptedOperations([{"task_verified": verified}]); flags = []
        original = engine.execute
        def run(*args, **kwargs):
            flags.append(self.runtime.fast_verification_enabled)
            return original(*args, **kwargs)
        engine.execute = run
        with mock.patch("operations.Operations", return_value=engine):
            result = self.runner.run(self.runtime, task or self.task, inputs or {}, self.targets, execution_mode=mode)
        self.assertFalse(self.runtime.fast_verification_enabled)
        return result, flags

    def test_first_verified_then_fast_with_new_input(self):
        first, flags = self.run_task("fast")
        self.assertEqual(first["execution"]["mode"], "standard"); self.assertEqual(flags, [False])
        second, flags = self.run_task(inputs={"input": "beta"})
        self.assertTrue(second["task_verified"]); self.assertEqual(flags, [True])
        self.assertEqual(second["execution"]["mode"], "fast")
        self.assertFalse(second["execution"]["checks_skipped"])
        content = next((Path(self.temp.name) / "repeat-profiles").glob("*.json")).read_text()
        self.assertNotIn("alpha", content); self.assertNotIn("beta", content)

    def test_recipe_program_delivery_changes_need_standard(self):
        self.run_task()
        changed = copy.deepcopy(self.task); changed["revision"] = 2
        self.assertEqual(self.run_task(task=changed)[1], [False])
        self.runtime.programs[0]["control_exes"] = [str(Path(self.temp.name) / "Other.exe")]
        self.assertEqual(self.run_task()[1], [False])

    def test_failed_repeat_disables_next_fast_attempt(self):
        self.run_task(); failed, flags = self.run_task(verified=False)
        self.assertEqual(flags, [True]); self.assertFalse(failed["task_verified"])
        self.assertEqual(self.run_task()[1], [False])

    def test_rejected_request_before_execution_invalidates_profile(self):
        self.run_task()
        with self.assertRaises(WorkflowError):
            self.run_task(inputs={"unknown": "private-value"})
        self.assertFalse(self.runtime.fast_verification_enabled)
        content = next((Path(self.temp.name) / "repeat-profiles").glob("*.json")).read_text()
        self.assertNotIn("private-value", content)
        self.assertFalse(json.loads(content)["eligible"])
        self.assertEqual(self.run_task()[1], [False])

    def test_standard_override_and_corrupt_cache(self):
        self.run_task(); self.assertEqual(self.run_task("standard")[1], [False])
        path = next((Path(self.temp.name) / "repeat-profiles").glob("*.json")); path.write_text("broken")
        self.assertEqual(self.run_task()[1], [False])

    def test_signature_is_not_current_window_or_permission(self):
        profile = RepeatProfiles(self.temp.name)
        one = profile.signature(self.task, self.runtime.programs, "background")
        two = profile.signature(self.task, self.runtime.programs, "foreground")
        self.assertNotEqual(one, two)
        self.assertIsNone(profile.read(one))

    def test_same_executable_path_updated_on_disk_requires_standard_again(self):
        executable = Path(self.runtime.programs[0]["exe"])
        executable.write_bytes(b"synthetic version one")
        self.run_task()
        self.assertEqual(self.run_task()[1], [True])
        executable.write_bytes(b"synthetic version two with changed file size")
        updated, flags = self.run_task()
        self.assertEqual(flags, [False])
        self.assertEqual(updated["execution"]["mode"], "standard")
        self.assertEqual(self.run_task()[1], [True])

    def test_control_executable_update_invalidates_identical_recipe(self):
        executable = Path(self.temp.name) / "Controlled.exe"
        executable.write_bytes(b"v1")
        self.runtime.programs[0]["control_exes"] = [str(executable)]
        self.run_task()
        self.assertEqual(self.run_task()[1], [True])
        executable.write_bytes(b"v2 updated")
        self.assertEqual(self.run_task()[1], [False])

    def test_recipe_content_and_mcp_version_changes_invalidate_profile(self):
        self.run_task()
        changed = copy.deepcopy(self.task)
        changed["steps"][0]["selector"]["name"] = "Changed field"
        self.assertEqual(self.run_task(task=changed)[1], [False])
        with mock.patch("repeat_profiles.VERSION", "future-test-version"):
            self.assertEqual(self.run_task()[1], [False])

    def test_standard_override_still_executes_verification_and_restores_flag(self):
        self.run_task()
        self.runtime.fast_verification_enabled = True
        engine = ScriptedOperations([{"task_verified": False}])
        flags = []
        original = engine.execute
        def run(*args, **kwargs):
            flags.append(self.runtime.fast_verification_enabled)
            return original(*args, **kwargs)
        engine.execute = run
        with mock.patch("operations.Operations", return_value=engine):
            result = self.runner.run(self.runtime, self.task, {}, self.targets, execution_mode="standard")
        self.assertEqual(flags, [False])
        self.assertTrue(self.runtime.fast_verification_enabled)
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["execution"]["mode"], "standard")

    def test_forged_profile_cannot_turn_failed_execution_into_success(self):
        self.run_task()
        path = next((Path(self.temp.name) / "repeat-profiles").glob("*.json"))
        value = json.loads(path.read_text())
        value.update(successes=99999, eligible=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        result, flags = self.run_task(verified=False)
        self.assertEqual(flags, [True])
        self.assertFalse(result["task_verified"])
        self.assertFalse(result["execution"]["checks_skipped"])
        self.assertFalse(json.loads(path.read_text())["eligible"])
        self.assertEqual(self.run_task()[1], [False])


if __name__ == "__main__": unittest.main()
