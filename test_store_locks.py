"""Real process contention on empty lock files and atomic concurrent stores."""
import json
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import time
import unittest

from learning import ElementLibrary
from server import TaskStore
from workflows import WorkflowError, WorkflowRunner


def _config(folder):
    return {"state_dir": str(folder), "programs": [{"id": "forms", "name": "Forms",
             "exe": str(Path(folder) / "Forms.exe"), "control_exes": [], "enabled": True}]}


def _store(kind, folder):
    return TaskStore(folder, _config(folder)) if kind == "tasks" else ElementLibrary(folder, _config(folder))


def _save(store, kind, worker, index):
    if kind == "tasks":
        store.save({"id": f"worker{worker}_{index}", "name": "Concurrent task", "instructions": "Review",
                    "expected": "Verified", "program_ids": ["forms"]})
    else:
        from test_learning import Runtime, TARGET
        config = _config(store.path.parent)
        store.teach(Runtime(config), TARGET, "forms", 2, f"worker{worker}_{index}",
                    expected_selector={"automation_id": "status", "role": "ComboBox", "within": {"automation_id": "filters"}})


def _store_worker(kind, folder, worker, count, ready, start, done):
    try:
        store = _store(kind, folder)
        ready.put(worker)
        if not start.wait(15):
            raise RuntimeError("start gate timed out")
        for index in range(count):
            _save(store, kind, worker, index)
        done.put((worker, "saved", None))
    except Exception as error:
        done.put((worker, type(error).__name__, repr(error.__cause__ or error)))


def _workflow_worker(folder, worker, count, retry, ready, start, done):
    try:
        runner = WorkflowRunner(folder)
        path = runner._path("a"*32)
        ready.put(worker)
        if not start.wait(15):
            raise RuntimeError("start gate timed out")
        for _ in range(count):
            deadline = time.monotonic()+10
            while True:
                try:
                    with runner._lock(path):
                        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else 0
                        time.sleep(.002)  # Widen read/modify/write overlap; the real lock must exclude it.
                        path.write_text(json.dumps(value+1), encoding="utf-8")
                    break
                except WorkflowError:
                    if not retry or time.monotonic() >= deadline:
                        raise
                    time.sleep(.005)
        done.put((worker, "saved", None))
    except Exception as error:
        done.put((worker, type(error).__name__, repr(error)))


class StoreLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = multiprocessing.get_context("spawn")

    def launch(self, kind, folder, workers=1, count=1, retry=True):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Forms.exe").touch()
        ready, done = self.context.Queue(), self.context.Queue()
        start = self.context.Event()
        processes = []
        for worker in range(workers):
            target = _workflow_worker if kind == "workflow" else _store_worker
            args = ((str(folder), worker, count, retry, ready, start, done) if kind == "workflow" else
                    (kind, str(folder), worker, count, ready, start, done))
            process = self.context.Process(target=target, args=args)
            process.start()
            processes.append(process)
        def cleanup():
            start.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            for channel in (ready, done):
                channel.close()
                channel.join_thread()
        self.addCleanup(cleanup)
        self.assertEqual({ready.get(timeout=15) for _ in processes}, set(range(workers)))
        return start, done, processes

    @unittest.skipUnless(os.name == "nt", "Windows mandatory byte-range locking regression")
    def test_empty_locked_task_and_element_stores_wait_without_writing_before_lock(self):
        import msvcrt
        for kind in ("tasks", "elements"):
            with self.subTest(kind=kind):
                folder = self.root / kind
                folder.mkdir()
                path = _store(kind, folder).path.with_suffix(".lock")
                with path.open("w+b") as owner:
                    msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                    try:
                        start, done, processes = self.launch(kind, folder)
                        start.set()
                        # The old initialization write fails immediately with
                        # PermissionError against this already locked empty file.
                        with self.assertRaises(queue.Empty):
                            done.get(timeout=.4)
                        self.assertEqual(path.stat().st_size, 0)
                    finally:
                        owner.seek(0)
                        msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
                self.assertEqual(done.get(timeout=15), (0, "saved", None))
                processes[0].join(5)
                self.assertEqual(processes[0].exitcode, 0)
                self.assertEqual(len(_store(kind, folder).all()), 1)
                self.assertEqual(path.read_bytes(), b"0")

    @unittest.skipUnless(os.name == "nt", "Windows mandatory byte-range locking regression")
    def test_empty_locked_workflow_reports_busy_without_initialization_write(self):
        import msvcrt
        folder = self.root / "workflow-busy"
        runner = WorkflowRunner(folder)
        path = runner._path("a"*32)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("w+b") as owner:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                start, done, processes = self.launch("workflow", folder, retry=False)
                start.set()
                answer = done.get(timeout=15)
                self.assertEqual(answer[1], "WorkflowError", answer)
                self.assertEqual(lock_path.stat().st_size, 0)
                self.assertFalse(path.exists())
            finally:
                owner.seek(0)
                msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        processes[0].join(5)
        self.assertEqual(processes[0].exitcode, 0)

    def test_fresh_store_multiprocess_burst_preserves_every_save_and_single_init_byte(self):
        for kind in ("tasks", "elements"):
            for round_number in range(2):
                with self.subTest(kind=kind, round=round_number):
                    folder = self.root / f"{kind}-{round_number}"
                    start, done, processes = self.launch(kind, folder, workers=6, count=4)
                    start.set()
                    answers = [done.get(timeout=20) for _ in processes]
                    self.assertEqual({(worker, status) for worker, status, _ in answers}, {(i, "saved") for i in range(6)}, answers)
                    for process in processes:
                        process.join(5)
                        self.assertEqual(process.exitcode, 0)
                    store = _store(kind, folder)
                    entries = store.all()
                    key = "id" if kind == "tasks" else "label"
                    self.assertEqual({item[key] for item in entries}, {f"worker{i}_{j}" for i in range(6) for j in range(4)})
                    self.assertEqual(store.path.with_suffix(".lock").read_bytes(), b"0")

    def test_fresh_workflow_lock_excludes_overlapping_read_modify_write(self):
        folder = self.root / "workflow-counter"
        start, done, processes = self.launch("workflow", folder, workers=6, count=4)
        start.set()
        answers = [done.get(timeout=20) for _ in processes]
        self.assertEqual({(worker, status) for worker, status, _ in answers}, {(i, "saved") for i in range(6)}, answers)
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        path = WorkflowRunner(folder)._path("a"*32)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), 24)
        self.assertEqual(path.with_suffix(".lock").read_bytes(), b"0")


if __name__ == "__main__":
    unittest.main()
