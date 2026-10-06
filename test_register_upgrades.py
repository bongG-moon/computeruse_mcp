"""Trusted release provenance only; no Claude process or personal profile access."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import register


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcp-upgrade-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = self.root / "previous-bundle"
        (self.base / "runtime").mkdir(parents=True)
        self.python = self.base / "runtime" / "python.exe"
        self.server = self.base / "server.py"
        self.python.write_bytes(b"synthetic runtime - never execute")
        self.server.write_bytes(b"# synthetic server - never execute\n")
        self.config = self.root / "kept-config.json"
        self.config.write_text('{"keep":true}', encoding="utf-8")
        self.entry = register._entry_for_paths(self.python, self.server, self.config)

    def manifest(self, version):
        (self.base / "BUILD-MANIFEST.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        lines = [f"{hashlib.sha256((self.base/name).read_bytes()).hexdigest()}  {name}"
                 for name in ("runtime/python.exe", "server.py", "BUILD-MANIFEST.json")]
        data = ("\n".join(lines) + "\n").encode()
        (self.base / "SHA256SUMS.txt").write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_known_exact_manifest_accepts_each_supported_release(self):
        for version in ("0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.4.0", "0.5.0", "0.6.0", "0.7.0", "0.7.1", "0.8.0"):
            with self.subTest(version=version):
                digest = self.manifest(version)
                with patch.dict(register.PREVIOUS_MANIFESTS, {digest: version}):
                    proof = register._previous_entry(self.entry)
                self.assertEqual(proof["version"], version)
                self.assertEqual(proof["previous_config"], str(self.config))
                self.assertEqual(self.config.read_text(), '{"keep":true}')

    def test_a_supported_version_label_without_exact_provenance_is_rejected(self):
        self.manifest("0.3.3")
        self.assertIsNone(register._previous_entry(self.entry))

    def test_proven_administrator_bridge_entry_is_supported_without_extra_args_or_env(self):
        bridge=self.base/'Computer Use MCP 관리자 연결.exe'
        bridge.write_bytes(b'synthetic known bridge - never execute')
        self.manifest('0.8.0')
        path=self.base/'SHA256SUMS.txt'
        data=path.read_bytes()+(hashlib.sha256(bridge.read_bytes()).hexdigest()+'  '+bridge.name+'\n').encode('utf-8')
        path.write_bytes(data)
        digest=hashlib.sha256(data).hexdigest()
        entry={**copy.deepcopy(self.entry),'command':str(bridge),'args':['--config',str(self.config)]}
        with patch.dict(register.PREVIOUS_MANIFESTS,{digest:'0.8.0'}):
            self.assertEqual(register._previous_entry(entry)['version'],'0.8.0')
            modified=copy.deepcopy(entry);modified['args'].append('--normal')
            self.assertIsNone(register._previous_entry(modified))
            modified=copy.deepcopy(entry);modified['env']['PYTHONPATH']='foreign'
            self.assertIsNone(register._previous_entry(modified))
            bridge.write_bytes(b'changed bridge')
            self.assertIsNone(register._previous_entry(entry))

    def test_known_manifest_cannot_hide_modified_server_or_command(self):
        digest = self.manifest("0.3.3")
        with patch.dict(register.PREVIOUS_MANIFESTS, {digest: "0.3.3"}):
            modified = copy.deepcopy(self.entry)
            modified["env"]["PYTHONPATH"] = "foreign-location"
            self.assertIsNone(register._previous_entry(modified))
            modified = copy.deepcopy(self.entry)
            modified["args"].append("--foreign")
            self.assertIsNone(register._previous_entry(modified))
            self.server.write_bytes(b"# modified after release")
            self.assertIsNone(register._previous_entry(self.entry))

    def test_release_manifest_pins_match_actual_archives_when_available(self):
        releases = Path(__file__).resolve().parent / "release"
        found = 0
        for version in ("0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.4.0", "0.5.0", "0.6.0", "0.7.0", "0.7.1", "0.8.0"):
            path = releases / f"Computer-Use-MCP-{version}.zip"
            if not path.is_file():
                continue
            found += 1
            with self.subTest(version=version), zipfile.ZipFile(path) as archive:
                prefix = "Computer-Use-MCP/"
                manifest = archive.read(prefix + "SHA256SUMS.txt")
                self.assertEqual(register.PREVIOUS_MANIFESTS.get(hashlib.sha256(manifest).hexdigest()), version)
                self.assertEqual(json.loads(archive.read(prefix + "BUILD-MANIFEST.json"))["version"], version)
                for line in manifest.decode().splitlines():
                    expected, name = line.split("  ", 1)
                    self.assertEqual(hashlib.sha256(archive.read(prefix + name)).hexdigest(), expected, name)
        if not found:
            self.skipTest("Historical release archives are not included in the source-only package")


if __name__ == "__main__":
    unittest.main()
