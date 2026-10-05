"""Native consent layout checks; all windows stay withdrawn and no input is sent."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import consent


class ConsentLayoutTests(unittest.TestCase):
    def test_window_fits_small_and_scaled_screens(self):
        for screen_width, screen_height, line_height in [
            (1024, 768, 20), (1366, 768, 26), (1920, 1080, 40), (3840, 2160, 48),
        ]:
            width, height, minimum_width, minimum_height = consent._dialog_size(
                screen_width, screen_height, line_height)
            self.assertLess(width, screen_width)
            self.assertLess(height, screen_height)
            self.assertLessEqual(minimum_width, width)
            self.assertLessEqual(minimum_height, height)

    def test_countdown_is_clear_at_minute_boundary_and_expiry(self):
        self.assertEqual(consent._countdown_text(300), "자동 취소까지 05:00")
        self.assertEqual(consent._countdown_text(59), "자동 취소까지 00:59")
        self.assertEqual(consent._countdown_text(-1), "자동 취소까지 00:00")


class ConsentWidgetTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tk = tk
        self.real_tk = tk.Tk
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Native Tk display unavailable: {exc}")

    def tearDown(self):
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def test_complete_long_details_remain_readable_and_readonly(self):
        title = "한글 경로를 포함한 요청 확인 " * 40
        details = "프로그램: 메모장\n경로: C:\\시험 폴더\\문서.txt\n" * 500
        responses = []
        view = consent._build_consent_dialog(self.root, {"title": title, "details": details}, responses.append)
        self.root.update_idletasks()
        self.assertEqual(view["details"].get("1.0", "end-1c"), title + "\n\n" + details)
        self.assertEqual(view["details"]["state"], "disabled")
        self.assertTrue(view["details"]["yscrollcommand"])
        self.assertTrue(self.root.bind("<Return>"))
        self.assertTrue(self.root.bind("<KP_Enter>"))
        self.assertTrue(view["allow"].bind("<Return>"))
        self.assertTrue(view["allow"].bind("<KP_Enter>"))
        self.assertEqual(responses, [])

    def test_close_control_and_cancel_button_deny(self):
        responses = []
        view = consent._build_consent_dialog(self.root, {"kind": "action"}, responses.append)
        self.assertEqual(view["allow"]["text"], "이 조작 허용")
        self.root.tk.call(self.root.protocol("WM_DELETE_WINDOW"))
        view["cancel"].invoke()
        self.assertEqual(responses, [False, False])

    def test_native_timeout_writes_denial_with_original_nonce(self):
        self.root.destroy()
        with tempfile.TemporaryDirectory(prefix="consent-ui-test-") as folder:
            request, response = Path(folder) / "request.json", Path(folder) / "response.json"
            nonce = "a" * 64
            request.write_text(json.dumps({"nonce": nonce, "title": "합성 테스트", "details": "내용 확인",
                                           "timeout_seconds": 0.025}), encoding="utf-8")

            def withdrawn_root():
                self.root = self.real_tk()
                self.root.withdraw()
                return self.root

            with patch.object(self.tk, "Tk", side_effect=withdrawn_root):
                self.assertEqual(consent._dialog_main(request, response), 0)
            self.assertEqual(json.loads(response.read_text(encoding="utf-8")),
                             {"nonce": nonce, "approved": False})


if __name__ == "__main__":
    unittest.main()
