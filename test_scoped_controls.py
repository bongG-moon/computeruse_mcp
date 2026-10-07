import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from operations import OperationError
from scoped_controls import ScopedControls


ANCESTRY_HARNESS = r'''
using System;
using System.Collections.Generic;
using System.Reflection;
class AncestryTests {
    static MethodInfo Method;
    static int Parent(string child, string root, Dictionary<string,int> selected, Dictionary<string,string> edges) {
        Func<string,string> parentOf = delegate(string id) { string value; return edges.TryGetValue(id, out value) ? value : null; };
        return (int)Method.Invoke(null, new object[] { child, root, selected, parentOf });
    }
    static void Equal(int expected, int actual) { if (expected != actual) throw new Exception("incorrect parent"); }
    static void Refused(string code, string root, Dictionary<string,int> selected, Dictionary<string,string> edges) {
        try { Parent("leaf", root, selected, edges); throw new Exception("accepted invalid ancestry"); }
        catch (TargetInvocationException error) { if (error.InnerException.Message != code) throw; }
    }
    static int Main(string[] args) {
        Method = Assembly.LoadFrom(args[0]).GetType("ScopedControls").GetMethod("NearestSelectedAncestor", BindingFlags.Static|BindingFlags.NonPublic);
        var edges = new Dictionary<string,string> { {"leaf","skip"}, {"skip","inner"}, {"inner","outer"}, {"outer","root"} };
        foreach (var selected in new[] { new Dictionary<string,int> { {"inner",0}, {"leaf",1}, {"outer",2} },
                                        new Dictionary<string,int> { {"outer",0}, {"leaf",1}, {"inner",2} } }) {
            Equal(selected["inner"], Parent("leaf", "root", selected, edges));
            Equal(selected["outer"], Parent("inner", "root", selected, edges));
            Equal(-1, Parent("outer", "root", selected, edges));
            selected["root"] = 3;
            Equal(3, Parent("outer", "root", selected, edges));
            Equal(-1, Parent("root", "root", selected, new Dictionary<string,string>()));
        }
        var one = new Dictionary<string,int> { {"leaf",0}, {"inner",1} };
        Refused("scope_outside_target", "root", one, new Dictionary<string,string> { {"leaf","inner"} });
        Refused("scope_ancestry_cycle", "root", one, new Dictionary<string,string> { {"leaf","inner"}, {"inner","leaf"} });
        var deep = new Dictionary<string,string> { {"leaf","n1"} };
        for (int i=1; i<129; i++) deep["n"+i] = "n"+(i+1);
        Refused("scope_ancestry_limit", "n129", one, deep);
        Equal(-1, Parent("leaf", "n128", one, deep));
        Console.WriteLine("nested_order_independent,root_boundary,detached,cycle,depth_limit: PASS");
        return 0;
    }
}
'''


class NativeAncestryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows .NET helper")
    def test_compiled_ancestry_algorithm_without_uia_or_desktop_calls(self):
        from build_portable import COMPILER, compile_scoped_controls
        if not COMPILER.is_file():
            self.skipTest("Windows C# compiler unavailable")
        with tempfile.TemporaryDirectory(prefix="scoped-ancestry-") as directory:
            folder = Path(directory)
            helper = folder / "ScopedControls.exe"
            compile_scoped_controls(helper)
            source = folder / "AncestryTests.cs"
            source.write_text(ANCESTRY_HARNESS, encoding="utf-8")
            executable = folder / "AncestryTests.exe"
            subprocess.run([str(COMPILER), "/nologo", "/target:exe", "/reference:System.Core.dll",
                "/out:" + str(executable), str(source)], check=True, capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW)
            answer = subprocess.run([str(executable), str(helper)], check=True, capture_output=True, text=True,
                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertIn("PASS", answer.stdout)


class ScopedTests(unittest.TestCase):
    def runtime(self):
        return SimpleNamespace(check_active=mock.Mock(), guard=SimpleNamespace(policy={}, process_resolver=None, window_resolver=None))

    def reader(self, transform=lambda x: x):
        runtime = self.runtime(); reader = ScopedControls(runtime)
        class Stream(io.StringIO):
            def write(self, value):
                request = json.loads(value)
                data = {"pid": 10, "window_id": 20, "elements": [{"name": "field", "role": "Edit", "value": "done", "verification_only": True}],
                        "read_only": True, "scoped_observation": True, "scope_complete": True}
                reader.responses.put(json.dumps(transform({"id": request["id"], "ok": True, "data": data})))
                return len(value)
        process = SimpleNamespace(stdin=Stream(), stdout=io.StringIO(), poll=mock.Mock(return_value=None), kill=mock.Mock(), wait=mock.Mock())
        process.terminate = mock.Mock(side_effect=lambda: setattr(process.poll, "return_value", 0))
        reader.process = process
        return reader, process

    def test_read_is_bounded_readonly_and_identity_checked_twice(self):
        reader, process = self.reader()
        with mock.patch("scoped_controls.validate_arguments") as validate:
            answer = reader.observe({"pid": 10, "window_id": 20}, [{"name": "field"}])
        self.assertGreaterEqual(validate.call_count, 2)
        self.assertTrue(answer["structuredContent"]["read_only"])
        self.assertNotIn("element_token", answer["structuredContent"]["elements"][0])
        self.assertFalse(process.terminate.called)

    def test_foreign_target_or_input_handle_is_rejected(self):
        for transform in (lambda a: {**a, "data": {**a["data"], "pid": 999}},
                          lambda a: {**a, "data": {**a["data"], "elements": [{"element_token": "stale", "verification_only": True}]}},
                          lambda a: {**a, "data": {**a["data"], "scope_complete": False}}):
            reader, process = self.reader(transform)
            with mock.patch("scoped_controls.validate_arguments"), self.assertRaises(OperationError):
                reader.observe({"pid": 10, "window_id": 20}, [{"name": "field"}])
            self.assertTrue(process.terminate.called)

    def test_error_is_not_empty_success(self):
        reader, process = self.reader(lambda a: {"id": a["id"], "ok": False, "code": "ambiguous_selector"})
        with mock.patch("scoped_controls.validate_arguments"), self.assertRaises(OperationError) as raised:
            reader.observe({"pid": 10, "window_id": 20}, [{"name": "field"}])
        self.assertEqual(raised.exception.code, "ambiguous_selector")

    def test_timeout_kills_only_helper(self):
        reader, process = self.reader()
        process.stdin = io.StringIO()
        with mock.patch("scoped_controls.validate_arguments"), self.assertRaises(OperationError) as raised:
            reader.observe({"pid": 10, "window_id": 20}, [{"name": "field"}], timeout_ms=10)
        self.assertEqual(raised.exception.code, "scoped_observation_timeout")
        self.assertTrue(process.terminate.called)

    def test_disallowed_target_cannot_start_helper(self):
        reader = ScopedControls(self.runtime(), factory=mock.Mock())
        with mock.patch("scoped_controls.validate_arguments", side_effect=ValueError("not allowed")), self.assertRaises(ValueError):
            reader.observe({"pid": 10, "window_id": 20}, [{"name": "field"}])
        reader.factory.assert_not_called()

    def test_failed_helper_stop_keeps_process_reference(self):
        reader, process = self.reader()
        process.terminate = mock.Mock(side_effect=OSError("denied"))
        process.kill = mock.Mock(side_effect=OSError("denied"))
        with self.assertRaises(OperationError) as raised:
            reader.close()
        self.assertEqual(raised.exception.code, "scoped_cleanup_pending")
        self.assertIs(reader.process, process)
        self.assertFalse(process.stdout.closed)
        process.poll.return_value = 0
        reader.close()
        self.assertIsNone(reader.process)


class ScopedSubtreeTests(unittest.TestCase):
    runtime = ScopedTests.runtime
    reader = ScopedTests.reader

    def scoped_reader(self, transform=lambda value: value):
        within = {"name": "conditions"}
        def convert(answer):
            answer["data"].update(scoped_inspection=True, within=within, truncated=False)
            return transform(answer)
        return self.reader(convert)

    def test_subtree_request_is_bounded_and_keeps_no_input_handles(self):
        reader, process = self.scoped_reader()
        with mock.patch("scoped_controls.validate_arguments") as validate:
            answer = reader.inspect({"pid": 10, "window_id": 20}, within={"name": "conditions"}, max_elements=100, max_depth=3)
        self.assertGreaterEqual(validate.call_count, 2)
        self.assertEqual(answer["structuredContent"]["metrics"]["source"], "native_selected_subtree")
        self.assertEqual(answer["structuredContent"]["metrics"]["visited_controls"], 1)
        self.assertFalse(process.terminate.called)

    def test_subtree_response_boundary_and_truncation_must_be_explicit(self):
        for transform in (lambda a: {**a, "data": {**a["data"], "within": {"name": "wrong"}}},
                          lambda a: {**a, "data": {**a["data"], "scoped_inspection": False}},
                          lambda a: {**a, "data": {**a["data"], "truncated": "no"}}):
            reader, process = self.scoped_reader(transform)
            with mock.patch("scoped_controls.validate_arguments"), self.assertRaises(OperationError) as raised:
                reader.inspect({"pid": 10, "window_id": 20}, within={"name": "conditions"})
            self.assertEqual(raised.exception.code, "scoped_response_invalid")
            self.assertTrue(process.terminate.called)

    def test_subtree_validation_precedes_process_start(self):
        for kwargs in ({"within": {"role": "Group"}}, {"within": {"name": "a", "within": {"name": "b"}}},
                       {"within": {"name": "a"}, "max_depth": 33}, {"within": {"name": "a"}, "max_elements": 5001},
                       {"within": {"name": "a"}, "timeout_ms": 0}):
            reader = ScopedControls(self.runtime(), factory=mock.Mock())
            with self.assertRaises(OperationError):
                reader.inspect({"pid": 10, "window_id": 20}, **kwargs)
            reader.factory.assert_not_called()

    def test_subtree_truncated_data_stays_readonly_and_never_becomes_selector_query(self):
        reader, process = self.scoped_reader(lambda a: {**a, "data": {**a["data"], "truncated": True}})
        with mock.patch("scoped_controls.validate_arguments"):
            answer = reader.inspect({"pid": 10, "window_id": 20}, within={"name": "conditions"})
        self.assertTrue(answer["structuredContent"]["truncated"])
        self.assertTrue(answer["structuredContent"]["read_only"])
        self.assertTrue(answer["structuredContent"]["scoped_inspection"])


if __name__ == "__main__": unittest.main()
