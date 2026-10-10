"""Offline ZIP/source audit with synthetic Driver bytes; no executable launches."""
from __future__ import annotations

import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import zipfile

import build_portable
import bundle_driver
from settings import VERSION


class PackageSourceTests(unittest.TestCase):
    def test_sources_include_new_modules_all_tests_and_no_private_artifacts(self):
        files = set(build_portable.archive_source_files())
        required = {"easy_api.py", "interaction.py", "image_pixels.py", "task_inputs.py", "task_revision.py",
            "visual_review.py", "visual_targets.py", "builtin_programs.py", "privileges.py", "REDESIGN_PLAN.html", "CHANGES_0.16.0.html"}
        self.assertTrue(required <= set(build_portable.source_files()))
        self.assertFalse({p.name for p in build_portable.SOURCE.glob("test_*.py")} - files)
        for name in files:
            self.assertNotIn(".data", Path(name).parts)
            self.assertNotIn(".git", Path(name).parts)
            self.assertNotIn("__pycache__", Path(name).parts)
            self.assertNotIn(Path(name).name, {"config.json", ".env", "tasks.json", "elements.json"})
            self.assertTrue((build_portable.SOURCE / name).is_file(), name)

    def test_local_python_imports_resolve_inside_source_archive(self):
        files = set(build_portable.archive_source_files())
        modules = {p.stem: p.name for p in build_portable.SOURCE.glob("*.py")}
        modules.update({"vendor." + p.stem: "vendor/" + p.name for p in (build_portable.SOURCE / "vendor").glob("*.py")})
        for name in files:
            if not name.endswith(".py"): continue
            tree = ast.parse((build_portable.SOURCE / name).read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                references = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
                for module in references:
                    if module in modules: self.assertIn(modules[module], files, f"{name} imports {module}")

    def test_runtime_document_links_are_packaged(self):
        files = set(build_portable.source_files())
        for name in files:
            if not name.endswith((".html", ".md")): continue
            source = (build_portable.SOURCE / name).read_text(encoding="utf-8-sig")
            links = []
            if name.endswith(".html"):
                class Links(HTMLParser):
                    def handle_starttag(self, tag, attrs):
                        links.extend(v for k, v in attrs if k in {"href", "src"} and v)
                Links().feed(source)
            else: links = re.findall(r"\]\(([^)]+)\)", source)
            for link in links:
                if re.match(r"^[a-z]+:", link, re.I) or link.startswith("#"): continue
                target = link.split("#")[0].split("?")[0]
                if target: self.assertIn(target, files, f"{name} links {target}")


class DriverBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.driver_zip, self.license, self.output = self.root / "driver.zip", self.root / "LICENSE.md", self.root / "combined.zip"
        self.license.write_bytes(b"Synthetic test-only license")
        self.driver = {bundle_driver.DRIVER_PREFIX + name: ("synthetic test-only " + name).encode() for name in bundle_driver.DRIVER_FILES}
        self.write_zip(self.driver_zip, self.driver)
        for name, value in {"DRIVER_ARCHIVE_SHA256": bundle_driver.digest(self.driver_zip.read_bytes()),
            "DRIVER_EXE_SHA256": bundle_driver.digest(self.driver[bundle_driver.DRIVER_PREFIX + "cua-driver.exe"]),
            "LICENSE_SHA256": bundle_driver.digest(self.license.read_bytes())}.items():
            patch = mock.patch.object(bundle_driver, name, value)
            patch.start(); self.addCleanup(patch.stop)

    @staticmethod
    def write_zip(path, members):
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in members.items(): archive.writestr(name, body)

    def base(self, version=VERSION):
        raw = {"payload.py": b"# synthetic source\n", "CUA-DRIVER-LICENSE.md": self.license.read_bytes()}
        raw.update({name: Path(bundle_driver.__file__).with_name(name).read_bytes() for name in ("README.md", "DRIVER-BUNDLE.md")})
        raw["BUILD-MANIFEST.json"] = json.dumps({"product": "Computer-Use-MCP", "version": version,
            "source_files": {name: bundle_driver.digest(body) for name, body in raw.items()}}).encode()
        raw["SHA256SUMS.txt"] = "".join(bundle_driver.digest(body) + "  " + name + "\n" for name, body in raw.items()).encode()
        path = self.root / "base.zip"
        self.write_zip(path, {bundle_driver.BASE_PREFIX + name: body for name, body in raw.items()})
        return path

    def test_current_version_adds_all_driver_files_without_changing_mcp(self):
        base = self.base()
        original = bundle_driver.read_archive(base)
        result = bundle_driver.create_bundle(base, self.driver_zip, self.license, self.output)
        combined = bundle_driver.read_archive(self.output)
        self.assertTrue(result["passed"])
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["driver_components"], 6)
        self.assertEqual({key: combined[key] for key in original}, original)
        manifest = json.loads(combined[bundle_driver.BASE_PREFIX + "DRIVER-BUNDLE-MANIFEST.json"])
        self.assertTrue(manifest["mcp_base_files_unchanged"])
        self.assertFalse(manifest["downloads_performed_by_builder"])
        for source, body in self.driver.items():
            self.assertEqual(combined[bundle_driver.BASE_PREFIX + "driver/" + source.removeprefix(bundle_driver.DRIVER_PREFIX)], body)

    def test_unknown_version_and_modified_payload_are_rejected(self):
        base = self.base("99.0.0")
        with self.assertRaisesRegex(ValueError, "build identity"):
            bundle_driver.create_bundle(base, self.driver_zip, self.license, self.output)
        base = self.base()
        changed = bundle_driver.read_archive(base)
        changed[bundle_driver.BASE_PREFIX + "payload.py"] = b"changed after verification"
        self.write_zip(base, changed)
        with self.assertRaisesRegex(ValueError, "integrity mismatch"):
            bundle_driver.create_bundle(base, self.driver_zip, self.license, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__": unittest.main()
