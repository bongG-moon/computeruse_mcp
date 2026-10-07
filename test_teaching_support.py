"""Teaching package diagnostics never imply a successful native launch."""
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import teaching_support
from settings import VERSION


class TeachingSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_helpers_do_not_mean_unsupported_uia(self):
        result = teaching_support.teaching_capabilities(self.root)
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["diagnostics"]["code"], "helper_files_missing")
        self.assertEqual(result["diagnostics"]["missing_files"], list(teaching_support.TEACHING_HELPER_FILES))
        direct = result["direct_picker"]
        self.assertTrue(direct["supported"])
        self.assertEqual(direct["supported_modes"], ["uia"])
        self.assertEqual(direct["status_tool"], "computer_teach_status")
        self.assertFalse(direct["helper"]["file_present"])
        self.assertIn("미지원이 아닙니다", result["diagnostics"]["message"])

    def test_all_present_files_are_still_not_tested_or_launch_verified(self):
        for name in teaching_support.TEACHING_HELPER_FILES:
            (self.root / name).write_bytes(b"")
        result = teaching_support.teaching_capabilities(self.root)
        self.assertEqual(result["diagnostics"]["code"], "helper_files_present_unverified")
        self.assertEqual(result["probe_scope"], "files_only")
        self.assertEqual(result["readiness"], "not_tested")
        self.assertFalse(result["screen_accessed"])
        for feature in (result["direct_picker"], result["image_process"]):
            self.assertFalse(feature["launch_verified"])
            self.assertEqual(feature["readiness"], "not_tested")
        files = [result["direct_picker"]["helper"], *result["image_process"]["helpers"]]
        self.assertTrue(all(item["file_present"] for item in files))
        self.assertEqual(result["image_process"]["tool"], "computer_process_editor")
        self.assertEqual(result["identity"]["server_path"], str(self.root / "server.py"))

    def test_direct_picker_and_process_files_are_reported_independently(self):
        (self.root / teaching_support.ELEMENT_PICKER_FILE).touch()
        (self.root / teaching_support.PROCESS_EDITOR_FILE).mkdir()
        result = teaching_support.teaching_capabilities(self.root)
        self.assertTrue(result["direct_picker"]["helper"]["file_present"])
        self.assertEqual(result["diagnostics"]["missing_files"],
                         [teaching_support.PROCESS_EDITOR_FILE, teaching_support.VISUAL_HELPER_FILE])
        self.assertTrue(result["image_process"]["supported"])

    def test_unreadable_file_is_distinct_from_missing_or_unsupported(self):
        with mock.patch.object(Path, "is_file", side_effect=PermissionError("private detail")):
            result = teaching_support.teaching_capabilities(self.root)
        self.assertEqual(result["diagnostics"]["code"], "helper_files_unavailable")
        self.assertEqual(result["diagnostics"]["missing_files"], [])
        self.assertEqual(result["diagnostics"]["unavailable_files"], list(teaching_support.TEACHING_HELPER_FILES))
        self.assertNotIn("private detail", repr(result))

    def test_import_and_probe_do_not_start_processes_or_change_files(self):
        marker = self.root / "unrelated.txt"
        marker.write_bytes(b"unchanged")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no helper launch")), \
             mock.patch("subprocess.run", side_effect=AssertionError("no process")), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("no writes")), \
             mock.patch.object(Path, "write_bytes", side_effect=AssertionError("no writes")):
            importlib.reload(teaching_support)
            teaching_support.teaching_capabilities(self.root)
        after = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
