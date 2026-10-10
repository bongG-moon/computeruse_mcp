"""Do not bind a named business control to a per-launch numeric UIA ID."""
import copy
import tempfile
from pathlib import Path
import unittest
from learning import ElementLibrary, LearningError, stable_selector
from operations import _unique, OperationError

class SelectorReuseTests(unittest.TestCase):
    def test_name_survives_changed_numeric_control_and_parent_ids(self):
        controls = [{"element_index": 0, "role": "Pane", "automation_id": "56227", "actions": []},
                    {"element_index": 1, "parent_index": 0, "role": "Button", "automation_id": "124556", "name": "Query", "actions": ["click"]}]
        selected = stable_selector({"elements": controls}, controls[1])
        self.assertEqual(selected, {"name": "Query", "role": "Button"})
        next_controls = copy.deepcopy(controls)
        next_controls[0]["automation_id"] = "77882"
        next_controls[1]["automation_id"] = "89422"
        self.assertIs(_unique({"elements": next_controls}, selected), next_controls[1])

    def test_duplicate_buttons_need_named_parent_and_remain_unique(self):
        controls = [{"element_index": 0, "role": "Pane", "name": "Search", "automation_id": "11555", "actions": []},
                    {"element_index": 1, "parent_index": 0, "role": "Button", "automation_id": "22117", "name": "Query", "actions": ["click"]},
                    {"element_index": 2, "role": "Pane", "name": "Export", "actions": []},
                    {"element_index": 3, "parent_index": 2, "role": "Button", "automation_id": "33882", "name": "Query", "actions": ["click"]}]
        selected = stable_selector({"elements": controls}, controls[1])
        self.assertEqual(selected["within"], {"name": "Search", "role": "Pane"})
        self.assertNotIn("automation_id", selected)
        self.assertIs(_unique({"elements": controls}, selected), controls[1])
        controls.append({**controls[1], "element_index": 4})
        with self.assertRaises(OperationError):
            _unique({"elements": controls}, selected)

    def test_numeric_resource_with_no_name_remains_exact_not_guessed(self):
        item = {"element_index": 0, "role": "Button", "automation_id": "1001", "actions": ["click"]}
        self.assertEqual(stable_selector({"elements": [item]}, item), {"automation_id": "1001", "role": "Button"})

    def test_saved_learning_survives_restart_only_after_new_driver_identity_checks(self):
        from test_learning import Runtime, TARGET
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Business.exe"
            executable.touch()
            config = {"state_dir": str(root / "state"), "programs": [{"id": "forms", "name": "Business",
                "exe": str(executable), "control_exes": [], "enabled": True}]}
            elements = [{"element_index": 0, "role": "Pane", "automation_id": "1211", "name": "Search", "actions": []},
                {"element_index": 1, "parent_index": 0, "role": "Button", "name": "Query", "automation_id": "2412",
                 "actions": ["click"], "element_token": "old-process-token"}]
            first = Runtime(config, elements)
            library = ElementLibrary(config["state_dir"], config)
            entry = library.teach(first, TARGET, "forms", 1, "Run query", expected_selector={"automation_id": "2412"})
            saved = library.path.read_text(encoding="utf-8")
            self.assertNotIn("automation_id", entry["selector"])
            self.assertNotIn("automation_id", entry["selector"].get("within", {}))
            self.assertNotIn("old-process-token", saved)
            # A new library and runtime represent reload/restart. Neither a
            # factory promise nor an old cached token authorizes the new target.
            restarted = Runtime(config, copy.deepcopy(elements))
            restarted.answer["structuredContent"].update(pid=311, window_id=522)
            current = restarted.answer["structuredContent"]["elements"]
            current[0].update(element_index=8, automation_id="8811")
            current[1].update(element_index=9, parent_index=8, automation_id="9822", element_token="new-process-token")
            reopened = ElementLibrary(config["state_dir"], config)
            result = reopened.resolve(restarted, {"pid": 311, "window_id": 522}, entry["id"])
            self.assertIn("click", result["suggested_operations"])
            self.assertFalse(result["input_dispatched"])
            self.assertEqual([name for name, _ in restarted.calls], ["get_window_state"])
            self.assertEqual(restarted.calls[0][1]["window_id"], 522)
            # A duplicate in the same named section cannot borrow the previous
            # successful identity; current Driver ambiguity blocks reuse.
            current.append({**current[1], "element_index": 10, "automation_id": "9823"})
            with self.assertRaises(OperationError):
                reopened.resolve(restarted, {"pid": 311, "window_id": 522}, entry["id"])
            current.pop()
            current[1]["actions"] = []
            with self.assertRaises(LearningError) as changed:
                reopened.resolve(restarted, {"pid": 311, "window_id": 522}, entry["id"])
            self.assertEqual(changed.exception.code, "control_changed")
            self.assertTrue(all(name == "get_window_state" for name, _ in restarted.calls))

if __name__ == "__main__": unittest.main()
