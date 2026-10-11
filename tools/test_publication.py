"""Publication contract tests using tiny, synthetic archives and local Git only.

No released executable, private workspace state, network, or installed program is
used. The source fixture is intentionally small: inventory is never a fixed count.
"""
from __future__ import annotations

import hashlib
from contextlib import chdir
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import publication


VERSION = "0.16.0"
PRODUCT = "Computer-Use-MCP"
METADATA = ("BUILD-MANIFEST.json", "DRIVER-BUNDLE-MANIFEST.json", "PUBLICATION-MANIFEST.json")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def checksums(contents):
    return "".join(digest(data) + "  " + name + "\n" for name, data in sorted(contents.items())).encode("utf-8")


def archive(path, prefix, contents):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(contents.items()):
            member = zipfile.ZipInfo(prefix + name, date_time=(2026, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            output.writestr(member, data)


@unittest.skipUnless(shutil.which("git"), "Git is needed for publication inventory checks")
class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.assets = self.root / "assets"
        self.repo.mkdir()
        self.assets.mkdir()
        self.source = {
            "settings.py": b'VERSION = "0.16.0"\n',
            "server.py": b'"""Synthetic publication fixture; no application is run."""\n',
            "CUA-DRIVER-LICENSE.md": b"Synthetic license fixture, not a redistributed Driver.\n",
        }
        self.runtime = {"runtime/python.exe": b"synthetic-runtime-not-executable"}
        self.driver = {"driver/cua-driver.exe": b"synthetic-driver-not-executable",
                       "driver/LICENSE.md": self.source["CUA-DRIVER-LICENSE.md"]}
        for name, data in self.source.items():
            self.write_repo(name, data)
        for name in publication.SUPPORT_FILES:
            self.write_repo(name, ("Synthetic publication support fixture: " + name + "\n").encode())
        for name in METADATA:
            self.write_repo(name, encoded({"format": 1, "version": "0.15.0", "fixture": "stale"}))
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.name", "Publication Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "--all")
        self.make_assets()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT)

    def write_repo(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def make_assets(self, *, build_version=VERSION, driver_version=VERSION):
        hashes = {name: digest(data) for name, data in sorted(self.source.items())}
        self.build = {
            "format": 1, "product": PRODUCT, "version": build_version,
            "source_files": hashes,
            "source_snapshot_sha256": digest(json.dumps(hashes, sort_keys=True).encode()),
            "runtime_files": {name: digest(data) for name, data in self.runtime.items()},
        }
        self.build_bytes = encoded(self.build)
        source_members = {**self.source, "BUILD-MANIFEST.json": self.build_bytes}
        archive(self.assets / (PRODUCT + "-" + VERSION + "-source.zip"), PRODUCT + "-source/", source_members)
        self.base = {**self.source, **self.runtime, "BUILD-MANIFEST.json": self.build_bytes}
        self.base["SHA256SUMS.txt"] = checksums(self.base)
        base_path = self.assets / (PRODUCT + "-" + VERSION + ".zip")
        archive(base_path, PRODUCT + "/", self.base)
        self.driver_manifest = {
            "format": 1, "mcp_version": driver_version,
            "mcp_base_zip_sha256": digest(base_path.read_bytes()), "mcp_base_files_unchanged": True,
            "driver": {"version": "0.28.2", "platform": "windows-x86_64", "license": "MIT",
                       "license_file": "driver/LICENSE.md", "license_sha256": digest(self.driver["driver/LICENSE.md"]),
                       "files": {name: digest(data) for name, data in self.driver.items()}},
        }
        self.driver_bytes = encoded(self.driver_manifest)
        self.bundle = {**self.base, **self.driver, "DRIVER-BUNDLE-MANIFEST.json": self.driver_bytes}
        self.write_bundle()
        (self.assets / "DRIVER-BUNDLE-MANIFEST.json").write_bytes(self.driver_bytes)
        (self.assets / "cua-driver.exe").write_bytes(self.driver["driver/cua-driver.exe"])
        (self.assets / "CUA-DRIVER-LICENSE.md").write_bytes(self.source["CUA-DRIVER-LICENSE.md"])
        self.write_asset_sums()

    def write_bundle(self):
        self.bundle.pop("BUNDLE-SHA256SUMS.txt", None)
        self.bundle["BUNDLE-SHA256SUMS.txt"] = checksums(self.bundle)
        archive(self.assets / (PRODUCT + "-" + VERSION + "-with-driver.zip"), PRODUCT + "/", self.bundle)

    def write_asset_sums(self):
        contents = {path.name: path.read_bytes() for path in self.assets.iterdir() if path.name != "SHA256SUMS.txt"}
        (self.assets / "SHA256SUMS.txt").write_bytes(checksums(contents))

    def snapshot(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(root).parts}

    def prepare(self):
        return publication.prepare(self.repo, self.assets)

    def commit_fixture(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "synthetic publication fixture")
        return self.git("rev-parse", "HEAD").decode().strip()

    def remote_fixture(self):
        manifest = json.loads((self.repo / "PUBLICATION-MANIFEST.json").read_bytes())
        return {"tag_name": "v" + VERSION, "draft": False, "prerelease": False,
                "assets": [{"name": row["name"], "size": row["size"], "digest": "sha256:" + row["sha256"]}
                           for row in manifest["release_assets"]]}

    def test_prepare_replaces_stale_metadata_deterministically_and_excludes_self_hash(self):
        self.prepare()
        manifest = json.loads((self.repo / "PUBLICATION-MANIFEST.json").read_bytes())
        self.assertEqual(manifest["format"], 2)
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(manifest["release_status"], "release")
        self.assertNotIn("PUBLICATION-MANIFEST.json", manifest["source_files"])
        self.assertEqual(set(manifest["publication_support_files"]), set(publication.SUPPORT_FILES))
        self.assertEqual((self.repo / "BUILD-MANIFEST.json").read_bytes(), self.build_bytes)
        self.assertEqual((self.repo / "DRIVER-BUNDLE-MANIFEST.json").read_bytes(), self.driver_bytes)
        before = self.snapshot(self.repo)
        self.prepare()
        self.assertEqual(self.snapshot(self.repo), before)
        result = publication.audit_assets(self.assets, VERSION)
        self.assertEqual(result[:3], (self.build_bytes, self.driver_bytes, self.source))
        self.assertEqual(len(result[3]), 7)

    def test_check_is_read_only_and_detects_old_publication_or_driver_manifest(self):
        self.prepare()
        before, assets_before = self.snapshot(self.repo), self.snapshot(self.assets)
        self.assertIsInstance(publication.check(self.repo, self.assets), dict)
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.snapshot(self.assets), assets_before)
        remote = self.remote_fixture()
        self.assertIsInstance(publication.check(self.repo, self.assets, release_json=remote), dict)
        for patch in ({"tag_name": "v0.15.0"}, {"draft": True}, {"prerelease": True},
                      {"assets": remote["assets"][:-1]},
                      {"assets": [{**remote["assets"][0], "digest": "sha256:" + "0" * 64}, *remote["assets"][1:]]}):
            with self.subTest(remote=patch), self.assertRaises(ValueError):
                publication.check(self.repo, self.assets, release_json={**remote, **patch})
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.snapshot(self.assets), assets_before)
        for name, field in (("PUBLICATION-MANIFEST.json", "version"), ("DRIVER-BUNDLE-MANIFEST.json", "mcp_version")):
            with self.subTest(name=name):
                path = self.repo / name
                original = path.read_bytes()
                stale = json.loads(original); stale[field] = "0.15.0"
                path.write_bytes(encoded(stale))
                changed = self.snapshot(self.repo)
                with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
                self.assertEqual(self.snapshot(self.repo), changed)
                path.write_bytes(original)

    def test_asset_mutation_rejected_even_if_outer_checksum_is_recomputed(self):
        self.prepare()
        path = self.assets / "cua-driver.exe"
        path.write_bytes(b"changed driver bytes")
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        self.write_asset_sums()
        with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)

    def test_source_byte_mutation_is_not_rewritten_or_blessed_by_prepare(self):
        self.prepare()
        self.write_repo("server.py", self.source["server.py"] + b"# untested change\n")
        before = self.snapshot(self.repo)
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.snapshot(self.repo), before)

    def test_inventory_rejects_missing_source_or_added_tracked_private_state(self):
        self.prepare()
        path = self.repo / "server.py"
        path.unlink()
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        path.write_bytes(self.source["server.py"])
        self.write_repo("state/recording.json", b'{"private_fixture":true}\n')
        self.git("add", "--", "state/recording.json")
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        before = self.snapshot(self.repo)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.snapshot(self.repo), before)

    def test_support_files_are_hashed_and_must_be_tracked_for_check(self):
        self.prepare()
        path = self.repo / publication.SUPPORT_FILES[0]
        original = path.read_bytes()
        path.write_bytes(original + b"changed\n")
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        path.write_bytes(original)
        self.git("rm", "--cached", "--", publication.SUPPORT_FILES[0])
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        self.prepare()  # A newly added publication helper is allowed before staging.
        self.git("add", "--", publication.SUPPORT_FILES[0])
        self.assertIsInstance(publication.check(self.repo, self.assets), dict)

    def test_asset_inventory_rejects_missing_and_unexpected_files(self):
        path = self.assets / "cua-driver.exe"
        original = path.read_bytes(); path.unlink()
        with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)
        path.write_bytes(original)
        (self.assets / "unexpected.txt").write_bytes(b"must not publish")
        self.write_asset_sums()
        with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)

    def test_embedded_version_disagreement_cannot_regenerate_metadata(self):
        before = self.snapshot(self.repo)
        for patch in ({"build_version": "0.15.0"}, {"driver_version": "0.15.0"}):
            with self.subTest(patch=patch):
                self.make_assets(**patch)
                with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)
                with self.assertRaises(ValueError): self.prepare()
                self.assertEqual(self.snapshot(self.repo), before)

    def test_driver_bundle_cannot_replace_base_bytes_despite_valid_bundle_hashes(self):
        self.bundle["server.py"] = b"different program, with recomputed checksums\n"
        self.write_bundle()
        self.write_asset_sums()
        with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)

    def test_zip_traversal_and_duplicate_members_are_rejected_without_extraction(self):
        source_zip = self.assets / (PRODUCT + "-" + VERSION + "-source.zip")
        original = source_zip.read_bytes()
        for name in (PRODUCT + "-source/../outside.txt", PRODUCT + "-source/settings.py"):
            with self.subTest(name=name):
                source_zip.write_bytes(original)
                import warnings
                with warnings.catch_warnings(), zipfile.ZipFile(source_zip, "a") as output:
                    warnings.simplefilter("ignore", UserWarning)
                    output.writestr(name, b"untrusted zip member")
                self.write_asset_sums()
                with self.assertRaises(ValueError): publication.audit_assets(self.assets, VERSION)
                self.assertFalse((self.root / "outside.txt").exists())

    def test_git_ref_checks_committed_bytes_even_if_worktree_was_modified(self):
        self.prepare()
        revision = self.commit_fixture()
        self.write_repo("server.py", b"worktree-only untested change\n")
        before = self.snapshot(self.repo)
        self.assertIsInstance(publication.check(self.repo, self.assets, git_ref=revision), dict)
        self.assertIsInstance(publication.check(self.repo, self.assets, git_ref="HEAD"), dict)
        with self.assertRaises(ValueError): publication.check(self.repo, self.assets)
        self.assertEqual(self.snapshot(self.repo), before)

    def test_publish_stale_manifest_stops_before_any_remote_or_create_command(self):
        self.prepare()
        path = self.repo / "PUBLICATION-MANIFEST.json"
        stale = json.loads(path.read_bytes()); stale["version"] = "0.15.0"
        path.write_bytes(encoded(stale))
        self.commit_fixture()
        notes = self.root / "notes.md"; notes.write_bytes(b"Synthetic notes\n")
        actual_run = subprocess.run
        create_calls = []

        def run(command, *args, **kwargs):
            if command[0] == "gh":
                create_calls.append(command)
                self.fail("Invalid metadata must not reach GitHub")
            return actual_run(command, *args, **kwargs)

        with mock.patch.object(publication, "remote_tag") as remote, \
             mock.patch.object(publication.subprocess, "run", side_effect=run):
            with self.assertRaises(ValueError):
                publication.publish(self.repo, self.assets, notes, "fixture/example", "release")
            remote.assert_not_called()
        self.assertEqual(create_calls, [])

    def test_publish_existing_remote_tag_is_never_reused(self):
        self.prepare()
        revision = self.commit_fixture()
        notes = self.root / "notes.md"; notes.write_bytes(b"Synthetic notes\n")
        actual_run = subprocess.run
        create_calls = []

        def run(command, *args, **kwargs):
            if command[0] == "gh":
                create_calls.append(command)
                self.fail("Existing tags must never reach release creation")
            return actual_run(command, *args, **kwargs)

        with mock.patch.object(publication, "remote_tag", return_value=revision) as remote, \
             mock.patch.object(publication.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "tag already exists"):
                publication.publish(self.repo, self.assets, notes, "fixture/example", "release")
            remote.assert_called_once_with("fixture/example", "v" + VERSION)
        self.assertEqual(create_calls, [])

    def test_publish_freezes_absolute_uploads_and_commit_from_relative_caller_paths(self):
        self.prepare()
        revision = self.commit_fixture()
        notes = self.root / "notes.md"; notes.write_bytes(b"Reviewed original notes\n")
        original_assets = self.snapshot(self.assets)
        remote_release = self.remote_fixture()
        actual_run, actual_output = subprocess.run, subprocess.check_output
        creates, tag_checks = [], []

        def remote_tag(repository, tag):
            self.assertEqual((repository, tag), ("fixture/example", "v" + VERSION))
            tag_checks.append(tag)
            if len(tag_checks) == 1:
                # Caller files and HEAD change after validation but before upload.
                self.write_repo("server.py", b"Later unreviewed code\n")
                self.commit_fixture()
                (self.assets / "cua-driver.exe").write_bytes(b"Later different asset")
                notes.write_bytes(b"Later different notes")
                return None
            return revision

        def run(command, *args, **kwargs):
            if command[0] != "gh":
                return actual_run(command, *args, **kwargs)
            self.assertEqual(command[:3], ["gh", "release", "create"])
            creates.append(command)
            self.assertEqual(command[command.index("--target") + 1], revision)
            self.assertEqual(kwargs["cwd"], self.repo.resolve())
            frozen_notes = Path(command[command.index("--notes-file") + 1])
            self.assertTrue(frozen_notes.is_absolute())
            self.assertNotEqual(frozen_notes, notes)
            self.assertEqual(frozen_notes.read_bytes(), b"Reviewed original notes\n")
            uploads = [Path(value) for value in command[command.index("--latest") + 1:]]
            self.assertEqual({path.name for path in uploads}, set(original_assets))
            for path in uploads:
                self.assertTrue(path.is_absolute())
                self.assertNotEqual(path.parent, self.assets.resolve())
                self.assertEqual(path.read_bytes(), original_assets[path.name])
            return subprocess.CompletedProcess(command, 0)

        def output(command, *args, **kwargs):
            if command[0] == "gh":
                self.assertEqual(command, ["gh", "api", "repos/fixture/example/releases/tags/v" + VERSION])
                return encoded(remote_release)
            return actual_output(command, *args, **kwargs)

        with chdir(self.root), mock.patch.object(publication, "remote_tag", side_effect=remote_tag), \
             mock.patch.object(publication.subprocess, "run", side_effect=run), \
             mock.patch.object(publication.subprocess, "check_output", side_effect=output):
            result = publication.publish(Path("repo"), Path("assets"), Path("notes.md"), "fixture/example", "release")
        self.assertTrue(result["published"])
        self.assertEqual(result["git_ref"], revision)
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(tag_checks), 2)
        self.assertNotEqual(self.git("rev-parse", "HEAD").decode().strip(), revision)

    def test_version_is_read_as_literal_without_executing_source(self):
        source = b'raise RuntimeError("must never execute")\nVERSION = "0.16.0"\n'
        self.assertEqual(publication.version_from(source), VERSION)
        for invalid in (b'VERSION = "0.16." + "0"\n', b'VERSION = get_version()\n',
                        b'VERSION = 16\n', b'OTHER = "0.16.0"\n',
                        b'VERSION = "0.15.0"\nVERSION = "0.16.0"\n'):
            with self.subTest(source=invalid), self.assertRaises(ValueError):
                publication.version_from(invalid)


if __name__ == "__main__":
    unittest.main()
