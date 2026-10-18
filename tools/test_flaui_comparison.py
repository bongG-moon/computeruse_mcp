"""Guardrails for optional developer-only engine comparison tooling."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import prepare_flaui


class FlaUIPreparationTests(unittest.TestCase):
    def test_packages_are_version_and_hash_pinned(self):
        self.assertEqual(len(prepare_flaui.PACKAGES), 5)
        for name, version, digest, member in prepare_flaui.PACKAGES:
            self.assertNotIn("*", version)
            self.assertRegex(digest, r"^[a-f0-9]{64}$")
            self.assertTrue(name.islower())
            if member:
                self.assertNotIn("..", Path(member).parts)

    def test_missing_package_does_not_contact_network_without_download_flag(self):
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen") as network:
            root = Path(temporary)
            with self.assertRaisesRegex(FileNotFoundError, "Missing pinned package"):
                prepare_flaui.build(root / "runtime", root / "cache")
            network.assert_not_called()

    def test_changed_cached_package_is_rejected_before_compile_or_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen") as network, patch("subprocess.run") as compiler:
            root = Path(temporary); cache = root / "cache"; cache.mkdir()
            name, version, _, _ = prepare_flaui.PACKAGES[0]
            (cache / f"{name}.{version}.nupkg").write_bytes(b"not the pinned NuGet package")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                prepare_flaui.build(root / "runtime", cache)
            network.assert_not_called(); compiler.assert_not_called()


PROBE = prepare_flaui.ROOT / ".data/flaui018/runtime" / prepare_flaui.HELPER_NAME


@unittest.skipUnless(os.name == "nt" and PROBE.is_file(), "optional compiled comparison helper not prepared")
class FlaUIProtocolTests(unittest.TestCase):
    def call(self, request, backend="uia3"):
        result = subprocess.run([str(PROBE), backend], input=json.dumps({"id": "test", **request}) + "\n",
                                text=True, encoding="utf-8", capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_missing_window_rejected(self):
        for backend in ("uia2", "uia3"):
            result = self.call({"pid": 123, "window_id": 1, "operation": "dump"}, backend)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "target_mismatch")
            self.assertNotIn("data", result)

    def test_unbounded_depth_rejected_before_desktop_read(self):
        result = self.call({"pid":123, "window_id":1, "operation":"dump", "max_depth":1000})
        self.assertEqual(result["code"], "invalid_inspection_limits")

    def test_unbounded_timeout_rejected(self):
        result = self.call({"pid":123, "window_id":1, "operation":"dump", "timeout_ms":999999})
        self.assertEqual(result["code"], "invalid_inspection_limits")

    def test_invalid_backend_does_not_start_uia(self):
        result = subprocess.run([str(PROBE), "automatic-input"], capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
