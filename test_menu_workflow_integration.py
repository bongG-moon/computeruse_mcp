"""Simulated recipe lifecycle with real persistence/runner/file verification.

OS input, image matching and visual assessment are deterministic provider doubles.
This is not a corporate-app, native recorder, theme, monitor or DPI acceptance test.
"""
import copy
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from operations import OperationError
from process_editor import ProcessDraft
from result_files import ResultFiles, ResultFileError
from server import TaskStore
from test_image_steps import png
from test_recorded_menu_expansion import recording
from test_result_files import workbook_bytes
from workflows import WorkflowRunner


CLASS = "WindowsForms10.Window.20808.app.0.abc_r10_ad1"
PICTURES = {"main": png(24, 16), "menu": png(16, 16), "submenu": png(20, 16)}


def imported_draft():
    events, windows = recording()
    main_id = windows[0]["owner_window_id"]
    pid = windows[0]["pid"]
    for event in events:
        ref = event["window_ref"]
        event.update(template_png=PICTURES[ref], width={"main": 24, "menu": 16, "submenu": 20}[ref], height=16)
    draft = ProcessDraft([{"program_id": "editor", "pid": pid, "window_id": main_id, "label": "Simulated export fixture"}])
    draft.recorded(events, windows=windows, auto_popup=True)
    draft.validate()
    return draft


class Probe:
    def __init__(self, runtime, target):
        self.runtime, self.target = runtime, target

    def snapshot(self):
        self.runtime.native_reads += 1
        return {"creation_time": self.runtime.generation, "executable": self.runtime.exe,
                "process_exited": False, "target_present": self.target["window_id"] in self.runtime.inventory,
                "windows": copy.deepcopy(list(self.runtime.inventory.values()))}

    capture = transition_snapshot = snapshot

    def close(self):
        pass


class SimulatedApplication:
    mode = "uia"

    def __init__(self, folder, generation=1, *, write_export=True, uncertain_export=False):
        self.folder = Path(folder)
        self.generation = generation
        self.id = f"simulated-session-{generation}"
        self.pid = 100 + generation
        self.main, self.menu, self.submenu = 1000 + generation * 10, 1001 + generation * 10, 1002 + generation * 10
        self.exe = str(self.folder / "Fixture.exe")
        self.programs = [{"id": "editor", "exe": self.exe, "control_exes": []}]
        self.execution_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.inventory = {self.main: self.row(self.main, "main")}
        self.guard = SimpleNamespace(process_resolver=lambda pid: self.exe if pid == self.pid else None,
            window_resolver=lambda hwnd: self.pid if hwnd in self.inventory else None,
            observed_targets=set())
        self.calls, self.inputs = [], []
        self.capture_count = self.native_reads = self.image_compares = 0
        self.export_status = "Ready"
        self.write_export, self.uncertain_export = write_export, uncertain_export
        self.output_name = f"export-{generation}.xlsx"
        # The same semantic command occurs at different simulated positions
        # after restart. No index or HWND is stored in the recipe.
        self.menu_order = ["Active sheet", "All sheet"] if generation % 2 else ["All sheet", "Active sheet"]

    def row(self, hwnd, kind):
        return {"pid": self.pid, "window_id": hwnd, "class_name": "Main" if kind == "main" else CLASS,
                "title": "Fixture" if kind == "main" else "", "visible": True,
                "thread_id": 200 + self.generation, "owner_window_id": 0 if kind == "main" else self.main,
                "root_owner_window_id": 0 if kind == "main" else self.main,
                "bounds": [0, 0, 640, 480] if kind == "main" else [-180 if self.generation % 2 == 0 else 440, 40, 640, 180]}

    def check_active(self):
        if self.stop_event.is_set():
            raise OperationError("fixture cancelled", "session_stopped")

    def target(self, hwnd):
        return {"pid": self.pid, "window_id": hwnd}

    def create_transition_probe(self, target):
        return Probe(self, target)

    def picture(self, target):
        if target["pid"] != self.pid or target["window_id"] not in self.inventory:
            raise OperationError("fixture window missing", "target_unavailable")
        return PICTURES[{self.main: "main", self.menu: "menu", self.submenu: "submenu"}[target["window_id"]]]

    def capture_checkpoint(self, target):
        self.capture_count += 1
        return {"content": [{"type": "image", "mimeType": "image/png", "data": self.picture(target)}]}

    def match(self, image, frame, **unused):
        self.image_compares += 1
        found = image["template_png"] == frame
        return {"status": "matched" if found else "not_found", "score": 1.0 if found else .2,
                "candidate_count": 1 if found else 0}

    def image_action(self, request, target):
        self.check_active()
        operation = request["operation"]
        self.calls.append((operation, copy.deepcopy(target)))
        matched = self.match(request["image_target"], self.picture(target))
        if matched["status"] != "matched":
            return {"task_verified": False, "input_dispatched": False, "diagnostic": {"code": "image_not_found", "image_match": matched}}
        if operation == "wait_for_image":
            return {"task_verified": True, "input_dispatched": False, "verification_scope": "image_presence_only"}
        if operation == "image_right_click" and target == self.target(self.main):
            self.inputs.append(("root_context", target["pid"], target["window_id"]))
            self.inventory[self.menu] = self.row(self.menu, "menu")
        elif operation == "image_click" and target == self.target(self.submenu):
            self.inputs.append(("Active sheet", target["pid"], target["window_id"]))
            self.inventory.pop(self.submenu)
            self.inventory.pop(self.menu)
            self.export_status = "Export complete"
            if self.write_export:
                (self.folder / self.output_name).write_bytes(workbook_bytes([
                    ["Family", "Generation"], ["Hybrid", str(self.generation)], ["Hybrid", str(self.generation)]]))
            if self.uncertain_export:
                return {"task_verified": False, "input_dispatched": None,
                        "diagnostic": {"code": "popup_input_unknown"}}
        else:
            raise AssertionError(f"unexpected fixture input {operation} {target}")
        return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}

    def expand_menu_once(self, target, request):
        if target != self.target(self.menu) or self.match(request["image_target"], self.picture(target))["status"] != "matched":
            raise AssertionError("expansion must freshly identify the live parent")
        self.inputs.append(("expand_parent", target["pid"], target["window_id"]))
        self.inventory[self.submenu] = self.row(self.submenu, "submenu")
        return {"task_verified": False, "input_dispatched": True, "verification_deferred": True}

    def call(self, name, arguments):
        self.check_active()
        self.calls.append((name, copy.deepcopy(arguments)))
        if name != "get_window_state" or arguments["window_id"] != self.main:
            raise AssertionError("fixture only permits current main result read")
        return {"structuredContent": {**self.target(self.main), "snapshot_id": f"fresh-{len(self.calls)}", "elements": [
            {"element_index": 4, "name": "Export state", "role": "Text", "value": self.export_status,
             "enabled": True, "is_offscreen": False, "actions": []}]}}


class SimulatedVisualReview:
    """Explicit test predicate, not a fabricated user approval or real model call."""
    def open(self, runtime, record, step, target):
        if target != runtime.target(runtime.main):
            raise AssertionError("result review must return to the current main window")
        return {"review_id": "simulated-review", "state": "awaiting_review", "image_content": []}

    def verify(self, runtime, record, step, target, response):
        passed = response == {"review_id": "simulated-review"} and runtime.export_status == "Export complete" and len(runtime.inventory) == 1
        return {"task_verified": passed, "input_dispatched": False, "verification_scope": "simulated_menu_and_status_predicate"}


class MenuWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="simulated-menu-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = {"data_policy": "connected", "programs": [{"id": "editor"}]}
        self.store = TaskStore(self.folder, self.config)
        self.addCleanup(mock.patch.stopall)
        mock.patch("transient_popup.register_popup", return_value=True).start()

    def save_task(self):
        draft = imported_draft()
        text = {"id": "simulated-export", "name": "Simulated menu lifecycle", "instructions": "Synthetic fixture only",
                "expected": "Explicit status check and separately verified fresh XLSX", "program_ids": ["editor"], "variables": {}}
        first = self.store.save({**text, "steps": draft.steps})
        # An explicit authored policy edit creates a revision; no runtime
        # snapshot or historical human acknowledgement is rewritten.
        draft.edit({"index": len(draft.steps)-1, "review_mode": "visual", "value": "Verify export status; menu closure alone does not prove a file."})
        draft.steps.append({"operation": "assert", "program_id": "editor", "window_ref": "main",
            "expect": [{"selector": {"name": "Export state", "role": "Text"}, "property": "value", "equals": "Export complete"}],
            "verification_timeout_ms": 0})
        second = self.store.save({**text, "steps": draft.steps})
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        self.assertTrue(second["revision_history"])
        loaded = TaskStore(self.folder, self.config).get(second["id"])
        self.assertEqual(loaded["steps"], second["steps"])
        for ephemeral in ('"pid":', '"window_id":', '"element_index":', '"runtime_id":'):
            self.assertNotIn(ephemeral, json.dumps(loaded["steps"]))
        return loaded

    def execute(self, task, runtime, *, resume=None):
        runner = WorkflowRunner(self.folder)
        runner.visual_reviews = SimulatedVisualReview()
        targets = [{"program_id": "editor", **runtime.target(runtime.main)}]
        with mock.patch("image_targets.match_image", side_effect=lambda unused, image, frame, **options: runtime.match(image, frame, **options)), \
             mock.patch("transient_popup.proven_popup", side_effect=lambda target: target["window_id"] in (runtime.menu, runtime.submenu) and target["window_id"] in runtime.inventory), \
             mock.patch("transient_popup.popup_owner", return_value=runtime.target(runtime.main)):
            first = runner.run(runtime, task, {}, targets, resume_run_id=resume, image_delivery_enabled=True)
            if first.get("state") == "awaiting_review":
                self.assertFalse(first["task_verified"])
                final = runner.run(runtime, task, {}, targets, resume_run_id=first["run_id"], image_delivery_enabled=True,
                    observation_review={"review_id": "simulated-review"})
                self.assertEqual(first["run_id"], final["run_id"])
                return final
            return first

    def test_import_save_reload_restart_replay_and_separate_xlsx_evidence(self):
        task = self.save_task()
        first_ids = None
        for generation in (1, 2):
            runtime = SimulatedApplication(self.folder, generation)
            verifier = ResultFiles(runtime)
            ticket = verifier.prepare(str(self.folder), "*.xlsx")["ticket_id"]
            result = self.execute(task, runtime)
            self.assertTrue(result["task_verified"], result)
            self.assertEqual(result["completed_steps"], len(task["steps"]))
            self.assertEqual([row[0] for row in runtime.inputs], ["root_context", "expand_parent", "Active sheet"])
            identifiers = {row[2] for row in runtime.inputs}
            if first_ids is not None:
                self.assertTrue(identifiers.isdisjoint(first_ids))
                self.assertEqual(runtime.menu_order, ["All sheet", "Active sheet"])
            first_ids = identifiers
            file_result = verifier.verify(ticket, runtime.output_name, "Family", "Hybrid", sheet_name="Data")
            self.assertTrue(file_result["file_verified"])
            self.assertTrue(file_result["content_verified"])
            self.assertEqual(file_result["data_rows"], 2)
            self.assertFalse(file_result["task_verified"])

    def test_uncertain_export_never_repeats_on_same_run_resume(self):
        task = self.save_task()
        runtime = SimulatedApplication(self.folder, uncertain_export=True)
        first = self.execute(task, runtime)
        self.assertFalse(first["task_verified"])
        self.assertEqual(first["step_delivery"]["state"], "unknown")
        self.assertEqual([row[0] for row in runtime.inputs].count("Active sheet"), 1)
        again = self.execute(task, runtime, resume=first["run_id"])
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertFalse(again["task_verified"])
        self.assertEqual([row[0] for row in runtime.inputs].count("Active sheet"), 1)
        self.assertTrue((self.folder / runtime.output_name).is_file())

    def test_menu_closure_and_status_cannot_create_file_evidence(self):
        task = self.save_task()
        runtime = SimulatedApplication(self.folder, write_export=False)
        verifier = ResultFiles(runtime)
        ticket = verifier.prepare(str(self.folder), "*.xlsx")["ticket_id"]
        result = self.execute(task, runtime)
        self.assertTrue(result["task_verified"], result)  # Only the authored UI predicates.
        self.assertNotIn("file_verified", result)
        self.assertEqual(len(runtime.inventory), 1)
        with self.assertRaises(ResultFileError):
            verifier.verify(ticket, runtime.output_name, "Family", "Hybrid", sheet_name="Data")

    def test_ten_simulated_runs_report_observation_counts_and_local_latency(self):
        task = self.save_task()
        samples = []
        for generation in range(1, 11):
            runtime = SimulatedApplication(self.folder, generation)
            began = time.perf_counter()
            result = self.execute(task, runtime)
            self.assertTrue(result["task_verified"], result)
            samples.append({"elapsed_ms": (time.perf_counter()-began)*1000,
                "calls": len(runtime.calls), "captures": runtime.capture_count,
                "image_compares": runtime.image_compares, "native_reads": runtime.native_reads,
                "inputs": len(runtime.inputs)})
        self.assertEqual({s["inputs"] for s in samples}, {3})
        for key in ("calls", "captures", "image_compares", "native_reads"):
            self.assertEqual(len({s[key] for s in samples}), 1, key)
        values = sorted(s["elapsed_ms"] for s in samples)
        report = {"scope": "simulated-provider-local-recipe-only", "runs": len(samples),
            "p50_ms": round(statistics.median(values), 3), "p95_ms": round(values[-1], 3),
            "p95_method": "nearest_rank", "counts_per_run": {key: samples[0][key] for key in samples[0] if key != "elapsed_ms"},
            "setup_before_timing": ["TaskStore revision save/reload"],
            "includes": ["recipe snapshots", "WorkflowRunner", "simulated review resume", "synthetic XLSX write"],
            "excludes": ["real OS input", "real image matching", "LLM latency", "DPI", "production application"]}
        print("SIMULATED_MENU_BENCHMARK " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
