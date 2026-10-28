"""Recorded pointer candidates must remain fresh, unique, actionable identities.

These fixtures exercise recipe import only; they do not prove vendor UIA
support or dispatch input to an application.
"""
import copy
import json
import unittest

from operations import _unique
from process_editor import ProcessDraft


TARGET = {"pid": 11, "window_id": 22}


def recorded_pointer(role="MenuItem", *, scope="Export menu"):
    bounds = {"x": 20, "y": 30, "width": 120, "height": 24}
    native = {"status": "selected", **TARGET,
              "element": {"role": role, "name": "Active sheet", "automation_id": "9876", "is_password": False},
              "ancestors": [{"role": "ToolBar", "name": scope, "is_password": False}],
              "point": {"x": 30, "y": 40}, "bounds": bounds}
    event = {"program_id": "editor", "operation": "click", "native_target": native}
    snapshot = {**TARGET, "elements": [
        {"element_index": 0, "role": "ToolBar", "name": scope, "actions": []},
        {"element_index": 1, "parent_index": 0, "role": role, "name": "Active sheet",
         "automation_id": "9876", "actions": ["invoke"], "bounds": bounds}]}
    return event, {("editor", "main"): snapshot}


class RecordedMenuSemanticsTests(unittest.TestCase):
    def draft(self):
        return ProcessDraft([{"program_id": "editor", **TARGET, "label": "Fixture"}])

    def test_observed_menu_click_preserves_explicit_result_review(self):
        for review_mode in ("human", "visual"):
            with self.subTest(review_mode=review_mode):
                draft = self.draft()
                event, snapshots = recorded_pointer()
                draft.recorded([event], snapshots=snapshots, review_mode=review_mode)
                self.assertEqual([step["operation"] for step in draft.steps], ["click", "checkpoint"])
                self.assertEqual(draft.steps[0]["completion_mode"], "human")
                self.assertEqual(draft.steps[1].get("review_mode", "human"), review_mode)
                self.assertNotIn("expect", draft.steps[0])
                self.assertEqual(draft.recording_import_diagnostics, [])
                draft.validate()

    def test_each_native_pointer_role_uses_fresh_stable_selector_after_recreation(self):
        for role in ("Button", "MenuItem", "ListItem", "DataItem"):
            with self.subTest(role=role):
                draft = self.draft()
                event, snapshots = recorded_pointer(role)
                draft.recorded([event], snapshots=snapshots)
                selector = draft.steps[0]["selector"]
                self.assertNotIn("9876", json.dumps(selector))
                current = copy.deepcopy(snapshots[("editor", "main")])
                current["elements"][1]["automation_id"] = "2468"
                current["elements"][1]["element_index"] = 41
                self.assertIs(_unique(current, selector), current["elements"][1])
                for private in ("native_target", "point", "pid", "window_id", "element_index"):
                    self.assertNotIn(json.dumps(private) + ":", json.dumps(draft.steps))

    def test_same_menu_label_in_another_observed_scope_is_not_confused(self):
        draft = self.draft()
        event, snapshots = recorded_pointer()
        elements = snapshots[("editor", "main")]["elements"]
        elements.extend([
            {"element_index": 3, "role": "ToolBar", "name": "Other menu", "actions": []},
            {**elements[1], "element_index": 4, "parent_index": 3}])
        draft.recorded([event], snapshots=snapshots)
        self.assertEqual(draft.steps[0]["operation"], "click")
        self.assertEqual(draft.steps[0]["selector"]["within"]["name"], "Export menu")

    def test_unproven_menu_candidates_are_not_promoted(self):
        for failure in ("missing_snapshot", "stale_identity", "foreign_window", "moved_geometry",
                        "ambiguous", "unsupported", "synthetic", "protected", "unsupported_role"):
            with self.subTest(failure=failure):
                draft = self.draft()
                event, snapshots = recorded_pointer()
                snapshot = snapshots[("editor", "main")]
                element = snapshot["elements"][1]
                if failure == "missing_snapshot":
                    snapshots = {}
                elif failure == "stale_identity":
                    element["automation_id"] = "different-command"
                elif failure == "foreign_window":
                    snapshot["window_id"] = 99
                elif failure == "moved_geometry":
                    element["bounds"] = {**element["bounds"], "x": 500}
                elif failure == "ambiguous":
                    snapshot["elements"].append({**element, "element_index": 2})
                elif failure == "unsupported":
                    element["actions"] = []
                elif failure == "synthetic":
                    element["synthetic_ancestor"] = True
                elif failure == "protected":
                    element["is_password"] = True
                elif failure == "unsupported_role":
                    event["native_target"]["element"]["role"] = element["role"] = "Custom"
                draft.recorded([event], snapshots=snapshots)
                self.assertEqual(draft.steps[0]["operation"], "manual_entry")
                self.assertTrue(draft.recording_import_diagnostics)
                self.assertNotIn("selector", draft.steps[0])


if __name__ == "__main__":
    unittest.main()
