"""Browser selection tests with fake files; no browser or desktop process."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import settings
from vendor import windows


class ChromeDefaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chrome-default-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pf = self.root / "Program Files"
        self.pf86 = self.root / "Program Files x86"
        self.local = self.root / "Local"
        environment = {"ProgramFiles": str(self.pf), "ProgramFiles(x86)": str(self.pf86),
                       "LOCALAPPDATA": str(self.local), "WINDIR": str(self.root / "Windows")}
        patches = [patch.dict(os.environ, environment),
                   patch.object(windows, "_app_path", return_value=""),
                   patch.object(windows, "native_claude", return_value=""),
                   patch.object(windows.subprocess, "run", side_effect=AssertionError("No process may run"))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path.resolve()

    def browser(self):
        return next(item for item in windows.discover()["apps"] if item["id"] == "browser")

    def test_chrome_selected_even_when_edge_is_installed(self):
        chrome = self.file(self.pf / "Google/Chrome/Application/chrome.exe")
        self.file(self.pf86 / "Microsoft/Edge/Application/msedge.exe")
        result = self.browser()
        self.assertEqual(result["exe"], str(chrome))
        self.assertEqual(result["name"], "Google Chrome")
        self.assertTrue(result["available"])
        self.assertNotIn("msedge.exe", [call.args[0] for call in windows._app_path.call_args_list])

    def test_edge_only_keeps_browser_unavailable_and_disabled(self):
        self.file(self.pf86 / "Microsoft/Edge/Application/msedge.exe")
        self.assertEqual(self.browser()["exe"], "")
        self.assertFalse(self.browser()["available"])
        value = settings.default_config(self.root / "config.json")
        browser = next(item for item in value["programs"] if item["id"] == "browser")
        self.assertFalse(browser["enabled"])
        self.assertIn("Edge로 자동 대체하지 않습니다", browser["hints"])

    def test_32_bit_chrome_is_supported(self):
        chrome = self.file(self.pf86 / "Google/Chrome/Application/chrome.exe")
        self.assertEqual(self.browser()["exe"], str(chrome))

    def test_per_user_chrome_is_supported(self):
        chrome = self.file(self.local / "Google/Chrome/Application/chrome.exe")
        self.assertEqual(self.browser()["exe"], str(chrome))

    def test_registered_chrome_path_has_priority(self):
        chrome = self.file(self.root / "Approved Chrome/chrome.exe")
        self.file(self.pf / "Google/Chrome/Application/chrome.exe")
        windows._app_path.side_effect = lambda name: str(chrome) if name == "chrome.exe" else ""
        self.assertEqual(self.browser()["exe"], str(chrome))

    def test_explicitly_saved_edge_is_not_silently_migrated(self):
        edge = self.file(self.pf86 / "Microsoft/Edge/Application/msedge.exe")
        value = settings.default_config(self.root / "config.json")
        browser = next(item for item in value["programs"] if item["id"] == "browser")
        browser.update(name="Chosen Edge", exe=str(edge), enabled=True)
        path = self.root / "existing.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        before = path.read_bytes()
        loaded = settings.load_config(path)
        self.assertEqual(loaded["programs"][0]["exe"], str(edge))
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
