"""Recording event evidence, actionable buttons and non-destructive row repair."""
import copy
import json
import unittest

from operations import OperationError
from process_editor import ProcessDraft
import test_process_editor as fixtures


class RecordingReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.source = fixtures.RecordingSemanticTests()
        self.source.setUp()
        self.program = self.source.program
        self.draft = self.source.draft

    def evidence(self, event, identity="42,7,9"):
        event["event_evidence"] = {"source": "uia_recording", "identity": identity, "after_input": True}
        return event

    def test_w_then_n_keep_both_event_time_values_and_expected_results(self):
        first, _ = self.source.event("select_option", "W")
        last, snapshots = self.source.event("select_option", "N")
        self.draft.recorded([self.evidence(first), self.evidence(last)], snapshots=snapshots)
        self.assertEqual([s["value"] for s in self.draft.steps], ["W", "N"])
        self.assertEqual([s["expect"][0]["equals"] for s in self.draft.steps], ["W", "N"])
        self.assertEqual(self.draft.recording_warnings, [])
        self.assertNotIn("event_evidence", json.dumps(self.draft.steps))
        self.assertNotIn("native_target", json.dumps(self.draft.steps))

    def test_historical_value_requires_same_identity_and_verified_terminal_value(self):
        for failure in ("identity", "terminal_value", "protected", "unsupported", "unproven"):
            first, _ = self.source.event("select_option", "W")
            last, snapshots = self.source.event("select_option", "N")
            self.evidence(first); self.evidence(last)
            row = snapshots[("editor", "main")]["elements"][0]
            if failure == "identity": last["event_evidence"]["identity"] = "different"
            elif failure == "terminal_value": row["value"] = "P"
            elif failure == "protected": row["is_password"] = True
            elif failure == "unsupported": row["actions"] = []
            elif failure == "unproven": first["event_evidence"]["after_input"] = False
            draft = ProcessDraft([self.program])
            with self.subTest(failure=failure):
                draft.recorded([first, last], snapshots=snapshots)
                self.assertEqual(draft.steps[0]["operation"], "manual_entry")
                self.assertNotIn("value", draft.steps[0])

    def button(self):
        event, snapshots = self.source.event()
        event.update(operation="click"); event.pop("after"); event.pop("value")
        event["native_target"]["element"]["role"] = "Button"
        row = snapshots[("editor", "main")]["elements"][0]
        row.update(role="Button", actions=["click"]); row.pop("value")
        return event, snapshots

    def test_button_identity_is_driver_verified_and_no_business_result_is_invented(self):
        event, snapshots = self.button()
        self.draft.recorded([event], snapshots=snapshots)
        self.assertEqual([s["operation"] for s in self.draft.steps], ["click", "checkpoint"])
        self.assertEqual(self.draft.steps[0]["completion_mode"], "human")
        self.assertNotIn("expect", self.draft.steps[0])
        self.assertNotIn("image_target", self.draft.steps[0])
        self.draft.validate()

    def test_native_button_missing_from_driver_keeps_real_image_click(self):
        event, _ = self.button()
        self.draft.recorded([event], snapshots={})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["image_click", "checkpoint"])
        self.assertNotIn("selector", self.draft.steps[0])

    def test_driver_verified_button_needs_no_image_template(self):
        event, snapshots = self.button()
        for field in ("template_png", "width", "height", "anchor", "capture_window"): event.pop(field)
        self.draft.recorded([event], snapshots=snapshots)
        self.assertEqual([s["operation"] for s in self.draft.steps], ["click", "checkpoint"])

    def test_new_button_without_own_capture_never_replays_previous_image(self):
        previous, _ = self.button()
        later, _ = self.button()
        later["native_target"]["element"]["name"] = "another button"
        for field in ("template_png", "width", "height", "anchor", "capture_window"):
            later.pop(field)
        self.draft.recorded([previous, later], snapshots={})
        self.assertEqual(self.draft.steps[0]["operation"], "image_click")
        self.assertEqual(self.draft.steps[2]["operation"], "manual_entry")
        self.assertNotIn("image_target", self.draft.steps[2])
        self.assertNotIn("selector", self.draft.steps[2])
        with self.assertRaises(OperationError): self.draft.validate()

    def test_button_and_checkpoint_move_and_delete_together(self):
        event, snapshots = self.button()
        self.draft.recorded([event], snapshots=snapshots)
        self.draft.steps.append({"operation": "delay", "program_id": "editor", "window_ref": "main", "duration_ms": 1})
        self.draft.labels.append("wait")
        self.draft.change("move_step", {"index": 0, "direction": 1})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["delay", "click", "checkpoint"])
        self.draft.change("remove_step", {"index": 2})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["delay"])

    def test_direct_button_authoring_can_use_explicit_human_review(self):
        event, snapshots = self.button()
        selected = self.draft.remember(self.program, snapshots[("editor", "main")], event["native_target"])
        self.draft.add({**fixtures.TARGET, "program_id": "editor", "action": "click", "selection_id": selected["selection_id"], "completion_mode": "human"})
        self.assertEqual([s["operation"] for s in self.draft.steps], ["click", "checkpoint"])

    def test_missing_image_row_repaired_in_place_without_losing_checkpoint(self):
        self.draft.recorded([{"program_id": "editor", "operation": "manual_entry", "reason": "image_occluded_requires_selection", "requested_operation": "right_click"}])
        before = copy.deepcopy(self.draft.steps[1])
        selected = self.draft.remember_image(self.program, fixtures.image_choice())
        self.draft.repair(0, selected)
        self.assertEqual(self.draft.steps[0]["operation"], "image_right_click")
        self.assertEqual(self.draft.steps[1], before)
        self.assertEqual(self.draft.steps[0]["program_id"], "editor")
        self.draft.validate()

    def test_input_repair_requires_user_value_and_protected_input_cannot_be_repaired(self):
        for reason in ("protected_input", "unknown_input"):
            draft = ProcessDraft([self.program])
            draft.recorded([{"program_id": "editor", "operation": "manual_entry", "reason": reason}])
            selected = draft.remember_image(self.program, fixtures.image_choice())
            with self.assertRaises(OperationError): draft.repair(0, selected, action="set_value")
            if reason == "protected_input":
                with self.assertRaises(OperationError): draft.repair(0, selected, action="set_value", value="never store")
                self.assertFalse(draft.summaries()[0]["repairable"])
            else:
                draft.repair(0, selected, action="set_value", value="explicitly supplied")
                self.assertEqual(draft.steps[0]["value"], "explicitly supplied")

    def test_popup_row_repair_preserves_owner_binding_and_other_steps(self):
        root = self.program
        window = {"program_id": "editor", "window_ref": "popup_1", "owner_ref": "main", "pid": root["pid"],
                  "window_id": root["window_id"] + 1, "owner_window_id": root["window_id"],
                  "title": "Choose item", "class_name": "OwnedDialog", "owner_verified": True}
        self.draft.recorded([
            {**fixtures.image_choice(), "program_id": "editor", "operation": "click"},
            {"program_id": "editor", "window_ref": "popup_1", "operation": "manual_entry",
             "reason": "image_occluded_requires_selection", "requested_operation": "click"},
        ], windows=[window])
        index = next(i for i, step in enumerate(self.draft.steps) if step["operation"] == "manual_entry")
        prior = copy.deepcopy(self.draft.steps)
        target = self.draft.targets[("editor", "popup_1")]
        selected = self.draft.remember_image(target, fixtures.image_choice(window_id=target["window_id"]))
        self.draft.repair(index, selected)
        self.assertEqual(self.draft.steps[index]["window_ref"], "popup_1")
        self.assertEqual(self.draft.steps[:index], prior[:index])
        self.assertEqual(self.draft.steps[index+1:], prior[index+1:])
        self.draft.validate()


if __name__ == "__main__":
    unittest.main()
