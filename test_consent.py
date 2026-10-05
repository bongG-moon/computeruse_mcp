"""Consent tests use fake dialogs; no real desktop input or windows are created."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import consent


class FakeChild:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def kill(self):
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        return self.exit_code


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="consent-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def dialog_factory(self, approved=True, valid_nonce=True):
        child = FakeChild()

        def spawn(request, response):
            data = json.loads(request.read_text(encoding="utf-8"))
            consent._atomic_json(response, {"nonce": data["nonce"] if valid_nonce else "different-request", "approved": approved})
            return child
        return child, spawn

    def test_valid_dialog_response_accepts_and_ends_only_own_child(self):
        broker = consent.ConsentBroker({}, self.root)
        child, factory = self.dialog_factory()
        unrelated = FakeChild()
        with patch.object(broker, "_spawn_dialog", side_effect=factory):
            self.assertTrue(broker.confirm("scope", "title", "details", threading.Event()))
        self.assertTrue(child.terminated)
        self.assertFalse(unrelated.terminated)
        broker.close()

    def test_resolved_exchange_erased_and_audit_excludes_content(self):
        for approved in (True, False):
            broker = consent.ConsentBroker({}, self.root)
            _, factory = self.dialog_factory(approved)
            with patch.object(broker, "_spawn_dialog", side_effect=factory):
                self.assertEqual(broker.confirm("action", "PRIVATE TITLE", "PRIVATE DETAILS", threading.Event()), approved)
            self.assertEqual(list((self.root / "consent").glob("*.request.json")), [])
            self.assertEqual(list((self.root / "consent").glob("*.response.json")), [])
        for path in (self.root / "consent").glob("*.audit.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", text)
            self.assertTrue(json.loads(text)["exchange_removed"])

    def test_timeout_and_stop_erase_details_after_owned_child_exits(self):
        broker = consent.ConsentBroker({"approval_timeout_seconds": 0.02}, self.root)
        with patch.object(broker, "_spawn_dialog", return_value=FakeChild()):
            self.assertFalse(broker.confirm("scope", "title", "PRIVATE DETAILS", threading.Event()))
        self.assertEqual(list((self.root / "consent").glob("*.request.json")), [])
        audit = json.loads(next((self.root / "consent").glob("*.audit.json")).read_text())
        self.assertEqual(audit["outcome"], "timeout")

    def test_unconfirmed_dialog_exit_denies_and_close_reports_failure(self):
        class StuckChild(FakeChild):
            def terminate(self):
                raise OSError("cannot terminate")
        broker = consent.ConsentBroker({}, self.root)
        _, factory = self.dialog_factory()
        child = StuckChild()
        def spawn(request, response):
            factory(request, response)
            return child
        with patch.object(broker, "_spawn_dialog", side_effect=spawn):
            self.assertFalse(broker.confirm("scope", "title", "details", threading.Event()))
        with self.assertRaises(OSError):
            broker.close()
        self.assertIn(id(child), broker._children)
        child.exit_code = 0
        broker.close()
        self.assertEqual(list((self.root / "consent").glob("*.request.json")), [])
        self.assertEqual(list((self.root / "consent").glob("*.response.json")), [])

    def test_wrong_nonce_false_string_or_denial_never_accept(self):
        for approved, matches in [(True, False), (False, True), ("true", True), (1, True)]:
            with self.subTest(approved=approved, matches=matches):
                broker = consent.ConsentBroker({}, self.root)
                _, factory = self.dialog_factory(approved, matches)
                with patch.object(broker, "_spawn_dialog", side_effect=factory):
                    self.assertFalse(broker.confirm("action", "title", "details", threading.Event()))
                broker.close()

    def test_config_boolean_cannot_bypass_human_consent(self):
        broker = consent.ConsentBroker({"approved": True, "approval": "client"}, self.root)
        _, factory = self.dialog_factory(False)
        with patch.object(broker, "_spawn_dialog", side_effect=factory) as spawn:
            self.assertFalse(broker.confirm("scope", "title", "details", threading.Event()))
            spawn.assert_called_once()

    def test_timeout_denies_and_closes_dialog(self):
        broker = consent.ConsentBroker({"approval_timeout_seconds": 0.06}, self.root)
        child = FakeChild()
        with patch.object(broker, "_spawn_dialog", return_value=child):
            before = time.monotonic()
            self.assertFalse(broker.confirm("scope", "title", "details", threading.Event()))
            self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(child.terminated)
        self.assertTrue(broker.last_error)

    def test_closed_dialog_without_response_denies(self):
        broker = consent.ConsentBroker({}, self.root)
        with patch.object(broker, "_spawn_dialog", return_value=FakeChild(exit_code=0)):
            self.assertFalse(broker.confirm("scope", "title", "details", threading.Event()))

    def test_stopped_request_does_not_launch_dialog(self):
        broker = consent.ConsentBroker({}, self.root)
        stopped = threading.Event()
        stopped.set()
        with patch.object(broker, "_spawn_dialog") as spawn:
            self.assertFalse(broker.confirm("scope", "title", "details", stopped))
            spawn.assert_not_called()

    def test_stop_file_denies_even_valid_response(self):
        broker = consent.ConsentBroker({}, self.root)
        child, factory = self.dialog_factory()

        def spawn(request, response):
            factory(request, response)
            (self.root / "stop.flag").touch()
            return child
        with patch.object(broker, "_spawn_dialog", side_effect=spawn):
            self.assertFalse(broker.confirm("scope", "title", "details", threading.Event()))

    def test_concurrent_close_kills_only_owned_dialog_and_unblocks(self):
        broker = consent.ConsentBroker({}, self.root)
        child, unrelated = FakeChild(), FakeChild()
        started = threading.Event()
        results = []

        def spawn(*_):
            started.set()
            return child
        with patch.object(broker, "_spawn_dialog", side_effect=spawn):
            worker = threading.Thread(target=lambda: results.append(broker.confirm("scope", "title", "details", threading.Event())))
            worker.start()
            self.assertTrue(started.wait(1))
            broker.close()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertTrue(child.terminated)
        self.assertFalse(unrelated.terminated)
        self.assertEqual(list((self.root / "consent").glob("*.request.json")), [])
        self.assertEqual(list((self.root / "consent").glob("*.response.json")), [])

    def test_atomic_response_has_no_temporary_files(self):
        response = self.root / "response.json"
        consent._atomic_json(response, {"nonce": "n", "approved": True})
        self.assertTrue(consent._response_approved(response, "n"))
        self.assertEqual(list(self.root.iterdir()), [response])

    def test_malformed_or_oversized_response_denies(self):
        response = self.root / "response.json"
        for text in ["bad-json", "[]", "null", "x" * 16_385]:
            response.write_text(text, encoding="utf-8")
            self.assertFalse(consent._response_approved(response, "n"))

    def test_invalid_timeout_uses_default(self):
        for value in [None, "bad", 0, -1, float("nan"), float("inf")]:
            self.assertEqual(consent._timeout({"approval_timeout_seconds": value}), 300)


class DesktopLeaseTests(unittest.TestCase):
    def test_same_session_leases_contend_and_recover(self):
        with tempfile.TemporaryDirectory(prefix="lease-test-") as folder:
            one, two = consent.DesktopLease(Path(folder)), consent.DesktopLease(Path(folder))
            self.assertEqual(one.path, two.path)
            try:
                self.assertTrue(one.acquire())
                self.assertTrue(one.acquire())
                self.assertFalse(two.acquire())
                one.release()
                self.assertTrue(two.acquire())
            finally:
                one.release()
                two.release()
            self.assertTrue(one.path.is_file())

    def test_subprocess_owner_blocks_and_exit_releases(self):
        with tempfile.TemporaryDirectory(prefix="lease-child-test-") as folder:
            code = "from pathlib import Path; import sys; from consent import DesktopLease; lease=DesktopLease(Path(sys.argv[1])); print('ready' if lease.acquire() else 'busy',flush=True); sys.stdin.readline()"
            process = subprocess.Popen([sys.executable, "-u", "-c", code, folder], cwd=str(Path(__file__).resolve().parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            lease = consent.DesktopLease(Path(folder))
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                self.assertFalse(lease.acquire())
                process.communicate("\n", timeout=5)
                self.assertTrue(lease.acquire())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                lease.release()


class ActiveRunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="active-run-test-")
        self.root = Path(self.temp.name)
        self.registry = self.root / "shared-runtime" / "active"
        self.registry_patch = patch.object(consent, "_active_registry_dir", return_value=self.registry)
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()
        self.temp.cleanup()

    def make_run(self, config_name="config-a"):
        run = self.root / config_name / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        return consent._local_path(run)

    def entries(self):
        return list(self.registry.glob("*.json"))

    def test_common_registry_stops_runs_under_different_config_directories(self):
        first, second = self.make_run(), self.make_run("config-b")
        consent.register_active_run(first)
        consent.register_active_run(second)
        self.assertEqual(consent.stop_active_runs(), 2)
        self.assertTrue((first / "stop.flag").is_file())
        self.assertTrue((second / "stop.flag").is_file())

    def test_unregister_removes_only_requested_run(self):
        first, second = self.make_run(), self.make_run("config-b")
        consent.register_active_run(first)
        consent.register_active_run(second)
        consent.unregister_active_run(first)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(consent.stop_active_runs(), 1)
        self.assertFalse((first / "stop.flag").exists())
        self.assertTrue((second / "stop.flag").exists())

    def test_stale_process_identity_removes_reference_without_writing(self):
        run = self.make_run()
        consent.register_active_run(run)
        entry = self.entries()[0]
        record = json.loads(entry.read_text(encoding="utf-8"))
        record["birth"] = "old-process-creation-time"
        consent._atomic_json(entry, record)
        consent._atomic_json(run / ".computer-use-active.json", record)
        self.assertEqual(consent.stop_active_runs(), 0)
        self.assertFalse(entry.exists())
        self.assertFalse((run / "stop.flag").exists())

    def test_changed_path_cannot_redirect_stop_file(self):
        run, other = self.make_run(), self.make_run("other")
        consent.register_active_run(run)
        entry = self.entries()[0]
        record = json.loads(entry.read_text(encoding="utf-8"))
        record["run_dir"] = str(other)
        consent._atomic_json(entry, record)
        self.assertEqual(consent.stop_active_runs(), 0)
        self.assertFalse(entry.exists())
        self.assertFalse((other / "stop.flag").exists())

    def test_marker_mismatch_removes_reference(self):
        run = self.make_run()
        consent.register_active_run(run)
        marker = run / ".computer-use-active.json"
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["token"] = "0" * 64
        consent._atomic_json(marker, value)
        self.assertEqual(consent.stop_active_runs(), 0)
        self.assertEqual(self.entries(), [])
        self.assertFalse((run / "stop.flag").exists())

    def test_stopped_session_is_removed_but_stop_failed_is_still_stoppable(self):
        ended, failed = self.make_run(), self.make_run("failed")
        for run in (ended, failed):
            consent.register_active_run(run)
        consent._atomic_json(ended / "session.json", {"state": "stopped"})
        consent._atomic_json(failed / "session.json", {"state": "stop_failed"})
        self.assertEqual(consent.stop_active_runs(), 1)
        self.assertFalse((ended / "stop.flag").exists())
        self.assertTrue((failed / "stop.flag").exists())
        self.assertEqual(len(self.entries()), 1)

    def test_missing_run_is_cleaned(self):
        run = self.make_run()
        consent.register_active_run(run)
        (run / ".computer-use-active.json").unlink()
        run.rmdir()
        self.assertEqual(consent.stop_active_runs(), 0)
        self.assertEqual(self.entries(), [])

    def test_arbitrary_directory_cannot_be_registered(self):
        other = self.root / "ordinary-folder"
        other.mkdir()
        with self.assertRaises(ValueError):
            consent.register_active_run(other)
        self.assertFalse((other / ".computer-use-active.json").exists())

    def test_detected_reparse_stop_file_is_not_touched(self):
        run = self.make_run()
        consent.register_active_run(run)
        flag = run / "stop.flag"
        flag.write_text("unchanged", encoding="utf-8")
        before = flag.stat().st_mtime_ns
        original = consent._plain_chain

        def plain(path, **kwargs):
            return False if path == flag else original(path, **kwargs)
        with patch.object(consent, "_plain_chain", side_effect=plain):
            with self.assertRaises(RuntimeError):
                consent.stop_active_runs()
        self.assertEqual(flag.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(flag.stat().st_mtime_ns, before)

    def test_symlinked_run_is_rejected_without_writing_target(self):
        target = self.make_run("target")
        link = self.root / "linked" / "runs" / uuid.uuid4().hex
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating Windows symlinks is unavailable for this account")
        with self.assertRaises(ValueError):
            consent.register_active_run(link)
        self.assertFalse((target / ".computer-use-active.json").exists())

    def test_default_registry_uses_same_desktop_identity_as_lease(self):
        self.registry_patch.stop()
        lease = consent.DesktopLease()
        self.assertEqual(consent._active_registry_dir().name, lease.path.stem.removeprefix("desktop-"))
        self.assertEqual(consent._active_registry_dir().parent.parent, lease.path.parent)
        self.registry_patch.start()


class FakeEmergencyStop(consent.EmergencyStop):
    def __init__(self, folder, callback, register=True):
        super().__init__(folder, callback)
        self.register = register
        self.inject_key = threading.Event()
        self.unregistered = False

    def _register_hotkey(self):
        return self.register

    def _unregister_hotkey(self):
        self.unregistered = True

    def _poll_hotkey(self):
        return self.inject_key.is_set()


class EmergencyStopTests(unittest.TestCase):
    def test_injected_hotkey_marks_stop_and_calls_once(self):
        with tempfile.TemporaryDirectory(prefix="hotkey-test-") as folder:
            called = threading.Event()
            calls = []

            def callback():
                calls.append(True)
                called.set()
            stop = FakeEmergencyStop(Path(folder), callback)
            try:
                stop.start()
                self.assertTrue(stop.available)
                self.assertFalse((Path(folder) / "stop.flag").exists())
                stop.inject_key.set()
                self.assertTrue(called.wait(1))
                self.assertTrue((Path(folder) / "stop.flag").exists())
                time.sleep(0.08)
                self.assertEqual(len(calls), 1)
            finally:
                stop.close()
            self.assertFalse(stop.available)
            self.assertTrue(stop.unregistered)

    def test_existing_stop_flag_is_preserved_and_never_registers_hotkey(self):
        with tempfile.TemporaryDirectory(prefix="hotkey-existing-stop-") as folder:
            flag = Path(folder) / "stop.flag"
            flag.touch()
            called = threading.Event()
            calls = []

            def callback():
                calls.append(True)
                called.set()
            stop = FakeEmergencyStop(Path(folder), callback)
            try:
                with patch.object(stop, "_register_hotkey") as register:
                    stop.start()
                    self.assertTrue(called.wait(1))
                    stop.start()
                    self.assertTrue(flag.exists())
                    self.assertFalse(stop.available)
                    register.assert_not_called()
                self.assertEqual(calls, [True])
            finally:
                stop.close()

    def test_registration_failure_is_exposed(self):
        with tempfile.TemporaryDirectory(prefix="hotkey-fail-") as folder:
            stop = FakeEmergencyStop(Path(folder), lambda: None, register=False)
            stop.start()
            self.assertFalse(stop.available)
            stop.close()
            self.assertFalse(stop.unregistered)

    def test_callback_may_close_own_hotkey_thread(self):
        with tempfile.TemporaryDirectory(prefix="hotkey-close-") as folder:
            called = threading.Event()
            stop = None

            def callback():
                stop.close()
                called.set()
            stop = FakeEmergencyStop(Path(folder), callback)
            stop.start()
            stop.inject_key.set()
            self.assertTrue(called.wait(1))
            stop.close()
            self.assertFalse(stop.available)


if __name__ == "__main__":
    unittest.main()
