"""Release copying rejects reparse components, traversal, and changed bytes."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.prepare_image_backend import ARCHIVES, _checked_path, copy_runtime


class BackendCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.source = self.root/"source"
        (self.source/"pkg").mkdir(parents=True)
        self.payload = self.source/"pkg/module.py"
        self.payload.write_bytes(b"version = 1\n")
        self.manifest = {"format": "computer-image-backend/v1", "archives": ARCHIVES,
                         "files": {"pkg/module.py": hashlib.sha256(self.payload.read_bytes()).hexdigest()}}
        self.write_manifest()
        self.destination = self.root/"output"

    def write_manifest(self):
        (self.source/"BACKEND-MANIFEST.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_verified_regular_files_copy_byte_for_byte(self):
        result = copy_runtime(self.source, self.destination)
        self.assertEqual(result["files"], 1)
        self.assertEqual((self.destination/"pkg/module.py").read_bytes(), self.payload.read_bytes())

    def test_changed_payload_is_rejected_before_output_creation(self):
        self.payload.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            copy_runtime(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_absolute_traversal_and_noncanonical_manifest_names_are_rejected(self):
        for name in ("../outside", "/absolute", "C:/absolute", "dir//file", "dir/./file", "dir/../file", "dir\\file", "file:stream", "dir/file.", "dir/file ", "."):
            with self.subTest(name=name):
                self.manifest["files"] = {name: "0"*64}
                self.write_manifest()
                with self.assertRaises(ValueError):
                    copy_runtime(self.source, self.destination)
                self.assertFalse(self.destination.exists())

    def test_reparse_on_root_child_or_destination_ancestor_is_rejected_before_resolve(self):
        original = Path.lstat
        for suspect in (self.source, self.source/"pkg", self.root):
            def lstat(path, *, _suspect=suspect):
                if path == _suspect:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return original(path)
            with self.subTest(suspect=suspect), mock.patch.object(Path, "lstat", lstat):
                with self.assertRaisesRegex(ValueError, "links or junctions"):
                    copy_runtime(self.source, self.destination)

    def test_same_tree_payload_symlink_is_rejected_when_platform_allows_links(self):
        target = self.source/"same-content.py"
        target.write_bytes(self.payload.read_bytes())
        self.payload.unlink()
        try:
            os.symlink(target, self.payload)
        except OSError as error:
            self.skipTest(f"Creating symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "links or junctions"):
            copy_runtime(self.source, self.destination)


if __name__ == "__main__":
    unittest.main()
