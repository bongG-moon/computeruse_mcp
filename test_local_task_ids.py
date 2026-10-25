"""Legacy task aliases stay private and persist without modifying recipes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_task_ids import LocalTaskIdError, LocalTaskIds
from server import TaskStore


class LocalTaskIdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"programs": [{"id": "app", "name": "Private program"}]}
        self.store = TaskStore(self.root, self.config)
        self.identities = LocalTaskIds(self.store, self.root)
        self.legacy = "smartmmdm-PRIVATE-CUSTOMER-query"
        self.save(self.legacy)

    def save(self, ident):
        return self.store.save({"id": ident, "name": "PRIVATE NAME", "instructions": "private instructions",
                                "expected": "private expected", "program_ids": ["app"]})

    def assert_code(self, code, operation):
        with self.assertRaises(LocalTaskIdError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(self.legacy, str(caught.exception))

    def test_alias_omits_business_id_and_does_not_modify_recipe(self):
        before = self.store.path.read_bytes()
        with mock.patch("socket.socket", side_effect=AssertionError("no network")):
            alias = self.identities.alias(self.legacy)
            self.assertRegex(alias, r"^task-[a-f0-9]{32}$")
            self.assertNotIn("PRIVATE", alias)
            self.assertEqual(self.identities.resolve(alias), self.legacy)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_persists_across_instances_and_restarts(self):
        first = self.identities.alias(self.legacy)
        other_store = TaskStore(self.root, self.config)
        second = LocalTaskIds(other_store, self.root)
        self.assertEqual(second.alias(self.legacy), first)
        self.assertEqual(second.resolve(first), self.legacy)
        self.assertEqual(json.loads(second.path.read_text())["map"], {first: self.legacy})

    def test_repeated_filtering_does_not_allocate_another_alias(self):
        public = self.identities.alias(self.legacy)
        before = self.identities.path.read_bytes()
        self.assertEqual(self.identities.alias(public), public)
        self.assertEqual(self.identities.path.read_bytes(), before)

    def test_alias_locked_uses_existing_store_transaction_without_nested_lock(self):
        with self.store._locked():
            with mock.patch.object(self.store, "_locked", side_effect=AssertionError("nested lock")):
                public = self.identities.alias_locked(self.legacy)
                self.assertEqual(self.identities.alias_locked(public), public)
        self.assertEqual(self.identities.resolve(public), self.legacy)

    def test_unknown_and_raw_legacy_identifiers_are_rejected(self):
        self.assert_code("local_task_identity_invalid", lambda: self.identities.resolve(self.legacy))
        self.assert_code("local_task_identity_unknown", lambda: self.identities.resolve("task-" + "a" * 32))
        self.assert_code("local_task_identity_unknown", lambda: self.identities.resolve("b" * 32))
        self.assert_code("local_task_identity_unknown", lambda: self.identities.alias("missing"))
        self.assertFalse(self.identities.path.exists())

    def test_native_uuid_passes_through_without_io_but_resolution_checks_existence(self):
        ident = "428f10547e844b8f997ddc3af1170907"
        with mock.patch.object(self.store, "_locked", side_effect=AssertionError("unnecessary IO")):
            self.assertEqual(self.identities.alias(ident), ident)
        self.assert_code("local_task_identity_unknown", lambda: self.identities.resolve(ident))
        self.save(ident)
        self.assertEqual(self.identities.alias(ident), ident)
        self.assertEqual(self.identities.resolve(ident), ident)
        self.assertFalse(self.identities.path.exists())

    def test_unknown_alias_shaped_real_id_is_not_accepted_as_original(self):
        real = "task-" + "c" * 32
        self.save(real)
        self.assert_code("local_task_identity_corrupt", lambda: self.identities.resolve(real))
        public = self.identities.alias(real)
        self.assertNotEqual(public, real)
        self.assertEqual(self.identities.resolve(public), real)

    def test_deleted_target_is_not_resolved_or_reassigned(self):
        public = self.identities.alias(self.legacy)
        self.store.delete(self.legacy)
        self.assert_code("local_task_identity_unknown", lambda: self.identities.resolve(public))
        self.save("another-task")
        self.assertNotEqual(self.identities.alias("another-task"), public)
        self.save(self.legacy)
        self.assertEqual(self.identities.alias(self.legacy), public)

    def test_collision_with_new_user_defined_id_fails_closed(self):
        public = self.identities.alias(self.legacy)
        self.save(public)
        self.assert_code("local_task_identity_corrupt", lambda: self.identities.resolve(public))
        self.assert_code("local_task_identity_corrupt", lambda: self.identities.alias(self.legacy))

    def test_corrupt_or_tampered_registry_is_not_rewritten(self):
        alias = "task-" + "a" * 32
        documents = [b"broken", b'{"version":true,"map":{}}',
                     json.dumps({"version": 1, "map": {"PRIVATE": self.legacy}}).encode(),
                     json.dumps({"version": 1, "map": {alias: "../escape"}}).encode(),
                     json.dumps({"version": 1, "map": {alias: self.legacy, "task-" + "b" * 32: self.legacy}}).encode(),
                     ('{"version":1,"map":{"' + alias + '":"one","' + alias + '":"two"}}').encode()]
        for document in documents:
            with self.subTest(document=document):
                self.identities.path.write_bytes(document)
                self.assert_code("local_task_identity_corrupt", lambda: self.identities.alias(self.legacy))
                self.assertEqual(self.identities.path.read_bytes(), document)

    def test_tampered_mapping_to_missing_recipe_cannot_resolve(self):
        public = self.identities.alias(self.legacy)
        self.identities.path.write_text(json.dumps({"version": 1, "map": {public: "missing"}}))
        self.assert_code("local_task_identity_unknown", lambda: self.identities.resolve(public))

    def test_atomic_replace_failure_preserves_original_registry(self):
        self.identities.alias(self.legacy)
        before = self.identities.path.read_bytes()
        self.save("new-private-task")
        with mock.patch("local_task_ids.os.replace", side_effect=OSError("disk problem")):
            with self.assertRaises(Exception): self.identities.alias("new-private-task")
        self.assertEqual(self.identities.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_alias_limit_is_explicit_not_pruning_existing_aliases(self):
        self.identities.alias(self.legacy)
        self.save("second-task")
        before = self.identities.path.read_bytes()
        with mock.patch.object(self.identities, "MAX_ALIASES", 1):
            self.assert_code("local_task_identity_limit", lambda: self.identities.alias("second-task"))
        self.assertEqual(self.identities.path.read_bytes(), before)

    def test_concurrent_instances_share_single_alias_without_lost_updates(self):
        self.save("another-private-task")
        def run(index):
            registry = LocalTaskIds(TaskStore(self.root, self.config), self.root)
            real = self.legacy if index % 2 else "another-private-task"
            return real, registry.alias(real)
        with ThreadPoolExecutor(max_workers=6) as pool:
            output = list(pool.map(run, range(24)))
        self.assertEqual(len({public for _, public in output}), 2)
        for real, public in output:
            self.assertEqual(self.identities.resolve(public), real)
        self.assertEqual(len(json.loads(self.identities.path.read_text())["map"]), 2)

    def test_link_registry_is_rejected_before_read_or_write(self):
        outside = self.root / "outside.json"
        outside.write_text("untouched")
        try:
            self.identities.path.symlink_to(outside)
        except OSError:
            # Windows without symlink privilege: exercise the reparse-point
            # branch of the production TaskStore guard, not a no-op stub.
            from types import SimpleNamespace
            original = Path.lstat
            def lstat(path):
                if path == self.identities.path: return SimpleNamespace(st_file_attributes=0x400, st_mode=0)
                return original(path)
            with mock.patch.object(Path, "lstat", lstat):
                with self.assertRaises(Exception): self.identities.alias(self.legacy)
                with self.assertRaises(Exception): self.identities.resolve("task-" + "a" * 32)
        else:
            with self.assertRaises(Exception): self.identities.alias(self.legacy)
            with self.assertRaises(Exception): self.identities.resolve("task-" + "a" * 32)
        self.assertEqual(outside.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
