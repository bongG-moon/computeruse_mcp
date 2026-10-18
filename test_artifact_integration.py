"""Storage lifecycle wiring; disposable files only, no desktop input."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from checkpoint_review import CheckpointReviews
from server import ComputerManager
from session_runtime import SessionRuntime
from test_server import config_at, FakeLease, FakeBroker, FakeEmergency


class ArtifactIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-storage-integration-')
        self.addCleanup(self.temp.cleanup)
        self.config = config_at(self.temp.name)

    def manager(self):
        return SimpleNamespace(config=self.config, storage_cleanup=None,
            teachings=SimpleNamespace(pending=Mock(return_value=None)),
            process_editors=SimpleNamespace(pending=Mock(return_value=None)),
            checkpoint_reviews=SimpleNamespace(pending=Mock(return_value=None)))

    def test_session_metadata_created_in_its_own_run_without_starting_driver(self):
        runtime = SessionRuntime(self.config, [self.config['programs'][0]], 'uia',
            {'id':'daily-query','name':'일일 조회','instructions':'synthetic'},
            broker_factory=FakeBroker, lease_factory=FakeLease, emergency_factory=FakeEmergency)
        self.assertEqual(runtime.run_dir.parent, Path(self.config['state_dir']) / 'runs')
        marker = runtime.run_dir / '.artifacts.json'
        self.assertTrue(marker.is_file())
        self.assertIn('일일 조회', marker.read_text(encoding='utf-8'))
        self.assertIsNone(runtime.transport)
        self.assertIn('storage', runtime.status())

    def test_adhoc_task_uses_user_description_as_folder_metadata(self):
        runtime = SessionRuntime(self.config, [self.config['programs'][0]], 'uia',
            {'instructions':'오늘 자료 조회'}, broker_factory=FakeBroker,
            lease_factory=FakeLease, emergency_factory=FakeEmergency)
        self.assertEqual(runtime.status()['storage']['task']['name'], '오늘 자료 조회')

    def test_storage_metadata_failure_does_not_break_runtime_status(self):
        with patch('artifact_lifecycle.initialize_run_artifacts', side_effect=PermissionError('readonly')):
            runtime = SessionRuntime(self.config, [self.config['programs'][0]], 'uia',
                {'instructions':'synthetic'}, broker_factory=FakeBroker,
                lease_factory=FakeLease, emergency_factory=FakeEmergency)
        status = runtime.status()
        self.assertEqual(status['storage']['status'], 'unmanaged')
        self.assertTrue(status['storage']['records_preserved'])
        self.assertEqual(status['storage_notice'], 'readonly')

    def test_portable_package_carries_storage_module_and_focused_tests(self):
        from build_portable import source_files, archive_source_files
        self.assertIn('artifact_lifecycle.py', source_files())
        for name in ('test_artifact_lifecycle.py','test_artifact_integration.py','test_capture_temp_scope.py'):
            self.assertIn(name, archive_source_files())

    def test_cleanup_seal_requires_stopped_session_and_all_helper_owners_exited(self):
        manager = self.manager()
        runtime = SimpleNamespace(state='stopped', run_dir=Path(self.temp.name) / 'run', execution_lock=threading.RLock())
        with patch('artifact_lifecycle.mark_run_closed') as seal, \
                patch('artifact_lifecycle.cleanup_artifacts', return_value={'done':True}) as cleanup:
            for owner in ('teachings','process_editors','checkpoint_reviews'):
                getattr(manager, owner).pending.return_value = 'still-running'
                ComputerManager._cleanup_storage(manager, runtime)
                seal.assert_not_called()
                getattr(manager, owner).pending.return_value = None
            runtime.state = 'active'
            ComputerManager._cleanup_storage(manager, runtime)
            seal.assert_not_called()
            runtime.state = 'stopped'
            ComputerManager._cleanup_storage(manager, runtime)
            seal.assert_called_once_with(runtime.run_dir)
            self.assertEqual(manager.storage_cleanup, {'done':True})
            self.assertEqual(cleanup.call_count, 5)

    def test_running_matcher_finally_blocks_sealing_even_after_stop(self):
        manager = self.manager()
        runtime = SimpleNamespace(state='stopped', run_dir=Path(self.temp.name) / 'run', execution_lock=threading.RLock())
        held, release = threading.Event(), threading.Event()
        def matching_worker():
            with runtime.execution_lock:
                held.set()
                release.wait(3)
        worker = threading.Thread(target=matching_worker)
        worker.start()
        self.assertTrue(held.wait(1))
        try:
            with patch('artifact_lifecycle.mark_run_closed') as seal, \
                    patch('artifact_lifecycle.cleanup_artifacts'):
                ComputerManager._cleanup_storage(manager, runtime)
                seal.assert_not_called()
                release.set()
                worker.join(1)
                with runtime.execution_lock:
                    ComputerManager._cleanup_storage(manager, runtime)
                    seal.assert_not_called()
                ComputerManager._cleanup_storage(manager, runtime)
                seal.assert_called_once_with(runtime.run_dir)
        finally:
            release.set()
            worker.join(3)

    def test_cleanup_failure_is_reported_without_failing_session_stop(self):
        manager = self.manager()
        with patch('artifact_lifecycle.cleanup_artifacts', side_effect=PermissionError('locked')):
            result = ComputerManager._cleanup_storage(manager)
        self.assertEqual(result['status'], 'deferred')
        self.assertTrue(result['records_preserved'])

    def test_checkpoint_terminal_decision_does_not_mean_helper_process_exited(self):
        reviews = CheckpointReviews()
        runtime = object()
        process = SimpleNamespace(poll=Mock(return_value=None))
        reviews.jobs['review'] = {'runtime':runtime, 'status':'confirmed', 'process':process}
        self.assertEqual(reviews.pending(runtime), 'review')
        self.assertIsNone(reviews.pending(object()))
        process.poll.return_value = 0
        self.assertIsNone(reviews.pending(runtime))
        process.poll.side_effect = OSError('unavailable')
        self.assertEqual(reviews.pending(runtime), 'review')

    def test_manager_start_invokes_housekeeping_without_driving_apps(self):
        with patch('artifact_lifecycle.cleanup_artifacts', return_value={'checked':True}) as cleanup:
            manager = ComputerManager(self.config)
        cleanup.assert_called_once_with(self.config['state_dir'])
        self.assertIsNone(manager.session)
        self.assertEqual(manager.storage_cleanup, {'checked':True})

    def test_review_history_pruning_never_forgets_living_helper_ownership(self):
        reviews = CheckpointReviews()
        runtime = object()
        reviews.jobs['live'] = {'runtime':runtime, 'status':'cancelled',
                                'process':SimpleNamespace(poll=Mock(return_value=None))}
        reviews.jobs['unknown'] = {'runtime':runtime, 'status':'confirmed',
                                   'process':SimpleNamespace(poll=Mock(side_effect=OSError('unknown')))}
        for number in range(50):
            reviews.jobs[str(number)] = {'runtime':runtime, 'status':'confirmed',
                                        'process':SimpleNamespace(poll=Mock(return_value=0))}
        reviews._prune_completed()
        self.assertIn('live', reviews.jobs)
        self.assertIn('unknown', reviews.jobs)
        self.assertEqual(len(reviews.jobs), 41)
        self.assertEqual(reviews.pending(runtime), 'live')

    def editor(self):
        from test_process_editor import EditorIPCTests
        harness = EditorIPCTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.context()
        opened = harness.start()
        return harness, opened

    def test_only_committed_runnable_recording_is_marked_for_retention(self):
        from test_process_editor import image_choice
        from operations import OperationError
        harness, opened = self.editor()
        event = {**image_choice(), 'program_id':'editor', 'operation':'click'}
        with patch('replay_preflight.image_replay_preflight', return_value={'ready_for_input':True}), \
                patch.object(harness.editors, '_visual', return_value={'events':[event]}):
            recorded = harness.command('record', {})
        self.assertEqual(recorded['status'], 'ok')
        job = harness.editors.jobs[opened['editor_id']]
        receipt = job['last_recording']
        self.assertIn(receipt['id'], job['imported_receipt_ids'])
        captured = []
        def after_commit(run_dir, receipt_id, task_id):
            task = harness.manager.tasks.get(task_id)
            self.assertEqual(task['steps'][0]['image_target']['template_png'], event['template_png'])
            captured.append((run_dir, receipt_id))
        with patch('artifact_lifecycle.mark_recording_saved', side_effect=after_commit) as mark:
            with patch.object(harness.manager.tasks, 'save', side_effect=OperationError('full disk')):
                failed = harness.command('save', {'name':'owned recording', 'description':''})
            self.assertEqual(failed['status'], 'error')
            mark.assert_not_called()
            saved = harness.command('save', {'name':'owned recording', 'description':''})
            self.assertTrue(saved['saved_task']['runnable'])
            mark.assert_called_once()
        self.assertEqual(captured, [(harness.runtime.run_dir, receipt['id'])])
        self.assertTrue(receipt['path'].is_file(), 'saving never deletes the recovery original immediately')
        harness.status(opened, cancel=True)
        harness.terminal(opened)

    def test_saved_incomplete_draft_keeps_original_out_of_expiry_policy(self):
        harness, opened = self.editor()
        events = [{'operation':'manual_entry', 'program_id':'editor', 'reason':'image_capture_required'}]
        with patch('replay_preflight.image_replay_preflight', return_value={'ready_for_input':True}), \
                patch.object(harness.editors, '_visual', return_value={'events':events}):
            recorded = harness.command('record', {})
        self.assertEqual(recorded['status'], 'ok')
        with patch('artifact_lifecycle.mark_recording_saved') as mark:
            saved = harness.command('save', {'name':'recover me', 'description':''})
        self.assertFalse(saved['saved_task']['runnable'])
        mark.assert_not_called()
        receipt = harness.editors.jobs[opened['editor_id']]['last_recording']
        self.assertTrue(receipt['path'].is_file())
        harness.status(opened, cancel=True)
        harness.terminal(opened)

    def test_saved_image_task_survives_expiry_of_its_recording_original(self):
        from artifact_lifecycle import initialize_run_artifacts, mark_run_closed, cleanup_artifacts
        from test_process_editor import EditorIPCTests, image_choice
        harness = EditorIPCTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        root = Path(harness.config['state_dir'])
        run = root / 'runs' / uuid.uuid4().hex
        run.mkdir(parents=True)
        harness.runtime.run_dir = run
        initialize_run_artifacts(run, {'name':'저장 후 원본 정리 시험'}, run.name)
        harness.context()
        opened = harness.start()
        event = {**image_choice(), 'program_id':'editor', 'operation':'click'}
        with patch('replay_preflight.image_replay_preflight', return_value={'ready_for_input':True}), \
                patch.object(harness.editors, '_visual', return_value={'events':[event]}):
            self.assertEqual(harness.command('record', {})['status'], 'ok')
        saved = harness.command('save', {'name':'repeat later', 'description':''})
        self.assertTrue(saved['saved_task']['runnable'])
        task_id = saved['saved_task']['id']
        receipt = harness.editors.jobs[opened['editor_id']]['last_recording']
        task_bytes = (root / 'tasks.json').read_bytes()
        harness.status(opened, cancel=True)
        harness.terminal(opened)
        self.assertIsNone(harness.editors.pending())
        (run / 'session.json').write_text(json.dumps({'session_id':run.name, 'run_dir':str(run), 'state':'stopped'}))
        mark_run_closed(run)
        marker = json.loads((run / '.artifacts.json').read_text(encoding='utf-8'))
        saved_at = marker['saved_recordings'][receipt['id']]['saved_at']
        result = cleanup_artifacts(root, now=saved_at+7*24*60*60+60, time_budget_seconds=2)
        self.assertEqual(result['error_count'], 0, result)
        self.assertFalse(receipt['path'].exists(), result)
        self.assertEqual((root / 'tasks.json').read_bytes(), task_bytes)
        task = harness.manager.tasks.get(task_id)
        self.assertEqual(task['steps'][0]['image_target']['template_png'], event['template_png'])


if __name__ == '__main__':
    unittest.main()
