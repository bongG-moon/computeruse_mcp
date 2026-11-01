"""Real offscreen UIA provider transport, not the SmartFX/FarPoint provider."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from scoped_controls import ScopedControls
import test_scoped_controls
from test_grid_intent import contract


class NativeGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "nt": raise unittest.SkipTest("Windows UIA provider fixture")
        from build_portable import COMPILER, compile_scoped_controls
        if not COMPILER.is_file(): raise unittest.SkipTest("C# compiler unavailable")
        cls.folder = tempfile.TemporaryDirectory(prefix="grid-provider-")
        root = Path(cls.folder.name)
        cls.helper = root / "ScopedControls.exe"
        compile_scoped_controls(cls.helper)
        cls.fixture = root / "GridIntentFixture.exe"
        references = ["System.dll", "System.Core.dll", "System.Web.Extensions.dll", "System.Xaml.dll"]
        references.extend(str(COMPILER.parent / "WPF" / name) for name in
            ("WindowsBase.dll", "PresentationCore.dll", "PresentationFramework.dll", "UIAutomationProvider.dll", "UIAutomationTypes.dll"))
        source = Path(__file__).parent / "fixtures" / "GridIntentFixture.cs"
        compiled = subprocess.run([str(COMPILER), "/nologo", "/target:exe", "/platform:anycpu", "/out:" + str(cls.fixture),
            *["/reference:" + name for name in references], str(source)], capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if compiled.returncode: raise RuntimeError(compiled.stdout + compiled.stderr)

    @classmethod
    def tearDownClass(cls): cls.folder.cleanup()

    def setUp(self):
        self.fixture_process = subprocess.Popen([str(self.fixture)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines = queue.Queue()
        def reader():
            for line in self.fixture_process.stdout: self.lines.put(line)
        self.thread = threading.Thread(target=reader, daemon=True); self.thread.start()
        ready = self.read()
        self.target = {key: ready[key] for key in ("pid", "window_id")}
        self.reader = ScopedControls(test_scoped_controls.ScopedTests.runtime(self))
        # This isolated fixture launcher is the only substitution. Query transport,
        # helper process, UIA properties and provider calls below are actual code.
        self.reader.factory = lambda args, **kwargs: subprocess.Popen([str(self.helper)], **kwargs)
        self.guard = mock.patch("scoped_controls.validate_arguments"); self.guard.start()

    def read(self):
        try: return json.loads(self.lines.get(timeout=10))
        except queue.Empty: raise AssertionError("fixture response timeout")

    def change(self, command):
        self.fixture_process.stdin.write(command + "\n"); self.fixture_process.stdin.flush()
        self.assertTrue(self.read()["done"])

    def inspect(self, **kwargs):
        intent = contract()
        intent["active_sheet"]["selector"]["automation_id"] = "activeSheet"
        intent["grid_selector"]["within"] = {"automation_id": "businessPanel"}
        return self.reader.observe_grid(self.target, intent, timeout_ms=5000, **kwargs)["structuredContent"]

    def tearDown(self):
        try:
            self.reader.close()
        finally:
            self.guard.stop()
            if self.fixture_process.poll() is None:
                self.fixture_process.stdin.write("close\n"); self.fixture_process.stdin.flush()
                try: self.fixture_process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.fixture_process.kill(); self.fixture_process.wait(timeout=5)
            self.thread.join(timeout=1)
            for stream in (self.fixture_process.stdin, self.fixture_process.stdout, self.fixture_process.stderr): stream.close()

    def test_actual_grid_griditem_selection_and_external_conditions(self):
        data = self.inspect()
        self.assertEqual(data["target"], self.target)
        self.assertTrue(data["grid_evidence_read_only"])
        self.assertEqual(data["elements"][0]["patterns"]["Grid"], {"status": "supported", "row_count": 2, "column_count": 1})
        selection = data["grid_selection"]
        self.assertTrue(selection["complete"])
        self.assertEqual(len(selection["selected_items"]), 1)
        selected = next(element for element in data["elements"] if element["element_index"] == selection["selected_items"][0])
        self.assertEqual(selected["grid_item"]["containing_grid"], 0)
        self.assertEqual(selected["grid_item"]["row"], 0)
        self.assertEqual(selected["coordinate_space"], "desktop_physical")
        self.assertTrue(all(element["verification_only"] for element in data["elements"]))
        self.assertEqual(len({element["element_index"] for element in data["elements"]}), len(data["elements"]))
        self.assertEqual(next(e for e in data["elements"] if e["automation_id"] == "filter")["value"], "All")
        self.assertEqual(next(e for e in data["elements"] if e["automation_id"] == "status")["name"], "Ready")
        parent = next(e for e in data["elements"] if e["automation_id"] == "businessPanel")
        self.assertEqual(data["elements"][0]["parent_index"], parent["element_index"])

    def test_complete_multiple_and_empty_selection_are_distinct_from_unsupported(self):
        self.change("many"); data = self.inspect()
        self.assertEqual(len(data["grid_selection"]["selected_items"]), 2)
        self.assertTrue(data["grid_selection"]["complete"])
        self.change("none"); data = self.inspect()
        self.assertEqual(data["grid_selection"]["selected_items"], [])
        self.assertTrue(data["grid_selection"]["complete"])
        self.change("unsupported"); data = self.inspect()
        self.assertEqual(data["grid_selection"]["status"], "unsupported")
        self.assertFalse(data["grid_selection"]["complete"])

    def test_truncated_selected_scope_stays_incomplete(self):
        self.change("many")
        data = self.inspect(max_elements=2)
        self.assertTrue(data["truncated"])
        self.assertFalse(data["grid_selection"]["complete"])

    def test_current_values_and_ready_properties_are_not_cached(self):
        before = self.inspect()
        self.change("changed"); self.change("processing")
        after = self.inspect()
        self.assertEqual(before["elements"][0]["patterns"]["Grid"]["row_count"], 2)
        self.assertEqual(after["elements"][0]["patterns"]["Grid"]["row_count"], 10)
        self.assertEqual(next(e for e in after["elements"] if e["automation_id"] == "status")["name"], "Processing")
        self.assertIn("Different result", [e["name"] for e in after["elements"]])


if __name__ == "__main__": unittest.main()
