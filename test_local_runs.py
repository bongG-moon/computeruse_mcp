"""Local form identity, frozen runs and real-receipt continuation without UI."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from local_delivery import receipt
from local_runs import LocalRuns
from operations import OperationError


TASK = "11" * 16
RUN = "22" * 16
CHECK = "33" * 16
SECRET = "SENTINEL-private-input"


def task():
    return {"id": TASK, "revision": 1, "program_ids": ["app"],
            "variables": {"search": {"type": "text", "default": SECRET}},
            "steps": [{"operation": "set_value", "program_id": "app", "selector": {"name": "Search"},
                       "value": {"input_ref": "search"}}]}


def progress(status="needs_review", **extra):
    return {"status": status, "task_verified": status == "verified", "task_id": TASK,
            "revision": 1, "run_id": RUN, "completed_steps": 1, "total_steps": 4,
            "pending_step": 1, **extra}


class LocalRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = SimpleNamespace(config={"state_dir": self.tmp.name}, tasks=mock.Mock(),
                                       call=mock.Mock(), workflows=mock.Mock(), checkpoint_reviews=mock.Mock(),
                                       session=SimpleNamespace(id="session-one"))
        self.api = LocalRuns(self.manager)
        self.display_patch = mock.patch.object(self.api, '_show_result')
        self.display = self.display_patch.start()
        self.addCleanup(self.display_patch.stop)

    def job(self):
        return {"id": "44" * 16, "task": task(), "inputs": {"search": SECRET},
                "cancel": threading.Event(), "done": threading.Event(),
                "result": {"status": "awaiting_input", "task_id": TASK}}

    def responses(self, records):
        self.manager.call.side_effect = [{"structuredContent": receipt(record)} for record in records]
        self.manager.workflows.progress.side_effect = [record for record in records if record["status"] != "verified"]

    def checkpoint(self, **extra):
        return progress(checkpoint={"id": CHECK, "review_mode": "human"}, **extra)

    def confirmed(self, **extra):
        return {"status": "confirmed", "human_reviewed": True, "run_id": RUN, "checkpoint_id": CHECK, **extra}

    def run_form(self, job=None, *, result_status="confirmed", inputs=None, ready_change=None,
                 response_nonce=None, helper_exit=0, visible=False):
        job = job or self.job()
        child = mock.Mock(pid=4567)
        child.poll.return_value = helper_exit
        def launch(argv, **kwargs):
            request, response = Path(argv[2]), Path(argv[3])
            data = json.loads(request.read_text(encoding="utf-8"))
            ready = {"nonce": data["nonce"], "status": "ready", "helper_pid": child.pid, "helper_window_id": 9876}
            ready.update(ready_change or {})
            Path(str(response) + ".ready.json").write_text(json.dumps(ready), encoding="utf-8")
            response.write_text(json.dumps({"nonce": data["nonce"] if response_nonce is None else response_nonce,
                                           "status": result_status, "inputs": {"search": SECRET} if inputs is None else inputs}), encoding="utf-8")
            return child
        with mock.patch("local_runs.subprocess.Popen", side_effect=launch) as launched, \
             mock.patch("learning_picker._helper_visible", return_value=visible), \
             mock.patch("learning_picker._end_helper") as ended, mock.patch.object(self.api, "_execute") as execute:
            self.api._work(job)
        return job, launched, execute, ended

    def test_confirmed_owned_child_nonce_passes_values_locally_and_cleans_exchange(self):
        job, launched, execute, ended = self.run_form()
        self.assertEqual(job["inputs"], {"search": SECRET})
        execute.assert_called_once_with(job)
        self.assertTrue(job["done"].is_set())
        self.assertEqual(list((Path(self.tmp.name) / "local-inputs").iterdir()), [])
        self.assertEqual(launched.call_count, 1)
        self.assertTrue(ended.called)

    def test_cancelled_form_or_missing_required_value_never_dispatches(self):
        for status, inputs in (("cancelled", {"search": SECRET}), ("confirmed", {"search": None})):
            with self.subTest(status=status):
                job, _, execute, _ = self.run_form(result_status=status, inputs=inputs)
                execute.assert_not_called()
                self.assertFalse(job["result"]["task_verified"])
                self.assertIn(job["result"]["status"], {"cancelled", "blocked"})

    def test_forged_nonce_foreign_child_or_invalid_ready_receipt_blocks_input(self):
        cases = [{"response_nonce": "wrong"}, {"ready_change": {"nonce": "wrong"}},
                 {"ready_change": {"helper_pid": 111}}, {"ready_change": {"helper_pid": True}},
                 {"ready_change": {"status": "other"}}, {"ready_change": {"helper_window_id": 0}},
                 {"helper_exit": 1}, {"helper_exit": None, "visible": False}]
        for changes in cases:
            with self.subTest(changes=changes):
                job, _, execute, _ = self.run_form(**changes)
                execute.assert_not_called()
                self.assertIn(job["result"]["status"], {"blocked", "cancelled"})

    def test_cancel_before_form_creation_does_not_launch_any_helper(self):
        job = self.job()
        job["cancel"].set()
        _, launched, execute, _ = self.run_form(job)
        launched.assert_not_called()
        execute.assert_not_called()

    def test_start_freezes_revision_and_defaults_before_form(self):
        original = task()
        with mock.patch("local_runs.TaskRevisions") as revisions, mock.patch.object(self.api, "_start_worker"):
            revisions.return_value.get.return_value = original
            started = self.api.start(TASK, 1)
        job = self.api.jobs[started["local_run_id"]]
        original["revision"] = 2
        original["variables"]["search"]["default"] = "changed"
        self.assertEqual(job["task"]["revision"], 1)
        self.assertEqual(job["task"]["variables"]["search"]["default"], SECRET)
        self.assertNotIn(SECRET, json.dumps(started))
        job["done"].set()

    def test_preflight_rejects_external_review_before_form_or_input(self):
        value = task()
        value["steps"] = [{"operation": "checkpoint", "review_mode": "visual"}]
        with mock.patch("local_runs.TaskRevisions") as revisions, mock.patch.object(self.api, "_start_worker") as start:
            revisions.return_value.get.return_value = value
            with self.assertRaises(OperationError) as failure:
                self.api.start(TASK, 1)
        self.assertEqual(failure.exception.code, "local_visual_dependency")
        start.assert_not_called()
        self.manager.call.assert_not_called()

    def test_chunk_and_budget_continuations_keep_same_run_revision_and_inputs(self):
        self.responses([progress("interrupted", continuation={"reason": "session_budget_exhausted", "session_id": "one"}),
                        progress("interrupted", completed_steps=2, pending_step=2,
                                 continuation={"reason": "workflow_chunk_complete", "session_id": "two"}), progress("verified")])
        job = self.job()
        self.api._execute(job)
        self.assertEqual(self.manager.call.call_count, 3)
        first, second, third = [call.args[1] for call in self.manager.call.call_args_list]
        self.assertNotIn("resume_run_id", first)
        self.assertEqual(second["resume_run_id"], RUN)
        self.assertEqual(third["resume_run_id"], RUN)
        self.assertTrue(all(args["revision"] == 1 and args["inputs"] == {"search": SECRET} for args in (first, second, third)))

    def test_actual_matching_human_receipt_continues_then_drops_consumed_ack(self):
        self.responses([self.checkpoint(),
                        progress("interrupted", completed_steps=2, pending_step=2,
                                 continuation={"reason": "workflow_chunk_complete", "session_id": "one"}), progress("verified")])
        self.manager.checkpoint_reviews.acknowledgement.return_value = self.confirmed()
        self.api._execute(self.job())
        calls = [call.args[1] for call in self.manager.call.call_args_list]
        self.assertEqual(calls[1]["acknowledge_checkpoint"], CHECK)
        self.assertNotIn("acknowledge_checkpoint", calls[2])
        self.assertEqual(calls[1]["resume_run_id"], RUN)

    def test_false_foreign_or_rejected_human_confirmation_never_continues(self):
        decisions = [self.confirmed(human_reviewed=False), self.confirmed(run_id="wrong"),
                     self.confirmed(checkpoint_id="wrong"), self.confirmed(status="rejected"),
                     {"status": "unavailable", "human_reviewed": False}]
        for decision in decisions:
            with self.subTest(decision=decision):
                self.manager.call.reset_mock()
                self.responses([self.checkpoint()])
                self.manager.checkpoint_reviews.acknowledgement.return_value = decision
                self.api._execute(self.job())
                self.assertEqual(self.manager.call.call_count, 1)

    def test_repeated_same_continuation_is_bounded_not_an_infinite_retry(self):
        state = progress("interrupted", continuation={"reason": "workflow_chunk_complete", "session_id": "one"})
        self.responses([state, state])
        with self.assertRaises(OperationError) as failure:
            self.api._execute(self.job())
        self.assertEqual(failure.exception.code, "local_run_no_progress")
        self.assertEqual(self.manager.call.call_count, 2)

    def test_repeated_same_human_checkpoint_after_ack_does_not_loop(self):
        self.responses([self.checkpoint(), self.checkpoint()])
        self.manager.checkpoint_reviews.acknowledgement.return_value = self.confirmed()
        with self.assertRaises(OperationError) as failure:
            self.api._execute(self.job())
        self.assertEqual(failure.exception.code, "local_run_no_progress")
        self.assertEqual(self.manager.call.call_count, 2)

    def test_pending_human_wait_can_be_cancelled_without_dispatch(self):
        job = self.job()
        self.responses([self.checkpoint()])
        def waiting(*args):
            job["cancel"].set()
            return {"status": "pending", "human_reviewed": False}
        self.manager.checkpoint_reviews.acknowledgement.side_effect = waiting
        with self.assertRaises(OperationError) as failure:
            self.api._execute(job)
        self.assertEqual(failure.exception.code, "local_input_cancelled")
        self.assertEqual(self.manager.call.call_count, 1)

    def test_progress_identity_mismatch_stops_before_another_call(self):
        self.responses([progress(task_id="another")])
        with self.assertRaises(OperationError) as failure:
            self.api._execute(self.job())
        self.assertEqual(failure.exception.code, "local_run_changed")
        self.assertEqual(self.manager.call.call_count, 1)

    def test_resume_retains_original_run_and_does_not_reopen_form(self):
        job = self.job()
        job.update(run_id=RUN, result=progress())
        job["done"].set()
        self.api.jobs[job["id"]] = job
        self.responses([progress("verified")])
        with mock.patch.object(self.api, "_work", side_effect=AssertionError("form must not reopen")):
            self.api.resume(job["id"])
            self.assertTrue(job["done"].wait(2))
        args = self.manager.call.call_args.args[1]
        self.assertEqual(args, {"task_id": TASK, "revision": 1, "inputs": {"search": SECRET}, "resume_run_id": RUN})
        self.assertEqual(self.api.status(job["id"])["status"], "verified")

    def test_resume_without_run_id_or_after_user_stop_never_starts_again(self):
        job = self.job()
        job["done"].set()
        self.api.jobs[job["id"]] = job
        with mock.patch.object(self.api, "_start_worker") as start:
            with self.assertRaises(OperationError): self.api.resume(job["id"])
            job['run_id'] = RUN
            job['cancel'].set()
            with self.assertRaises(OperationError): self.api.resume(job["id"])
        start.assert_not_called()

    def test_close_cancels_and_joins_waiting_worker_without_any_input(self):
        job = self.job()
        entered = threading.Event()
        def waiting(item):
            entered.set()
            item["cancel"].wait(2)
            item["done"].set()
        self.api.jobs[job["id"]] = job
        self.api._start_worker(job, waiting)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.api.close(1))
        self.assertTrue(job["cancel"].is_set())
        self.assertTrue(job["done"].is_set())
        self.manager.call.assert_not_called()

    def test_helper_cleanup_failure_blocks_new_work_until_close_confirms_exit(self):
        job = self.job()
        job['helper'] = mock.Mock()
        job['done'].set()
        self.api.jobs[job['id']] = job
        with mock.patch('learning_picker._end_helper', side_effect=OperationError('close failed', 'picker_close_failed')):
            self.assertFalse(self.api._cleanup_form(job))
        self.assertTrue(self.api.status(job['id'])['pending'])
        with self.assertRaises(OperationError):
            self.api.resume(job['id'])
        with mock.patch('learning_picker._end_helper'):
            self.assertTrue(self.api.close(1))
        self.assertFalse(self.api.status(job['id'])['pending'])
        self.manager.call.assert_not_called()

    def test_non_object_execution_response_fails_without_another_dispatch(self):
        job = self.job()
        self.manager.call.return_value = {'structuredContent': SECRET}
        self.api._resume_work(job)
        self.assertEqual(job['result']['status'], 'blocked')
        self.assertTrue(job['done'].is_set())
        self.assertNotIn(SECRET, json.dumps(receipt(job['result'])))
        self.assertEqual(self.manager.call.call_count, 1)

    def test_raw_local_result_is_kept_for_result_window_but_never_status(self):
        self.manager.local_results = threading.local()
        raw = progress('needs_review', diagnostic={'code': 'driver_problem', 'message': SECRET})
        def execute(*args):
            self.manager.local_results.value = {'structuredContent': raw}
            return {'structuredContent': receipt(raw)}
        self.manager.call.side_effect = execute
        self.manager.workflows.progress.return_value = raw
        job = self.job()
        self.api.jobs[job['id']] = job
        self.api._execute(job)
        self.assertEqual(job['local_result']['diagnostic']['message'], SECRET)
        self.assertNotIn(SECRET, json.dumps(self.api.status(job['id'])))

    def test_one_local_result_form_has_private_details_and_no_execution_pending(self):
        job = self.job()
        job['done'].set()
        job['result'] = receipt(progress('needs_review'))
        job['local_result'] = progress('needs_review', diagnostic={'code': 'private_failure', 'message': SECRET})
        self.api.jobs[job['id']] = job
        child = mock.Mock(pid=2468)
        child.poll.return_value = None
        with mock.patch('local_runs.subprocess.Popen', return_value=child) as launch:
            LocalRuns._show_result(self.api, job)
            LocalRuns._show_result(self.api, job)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0][1], '--result')
        request = Path(launch.call_args.args[0][2])
        self.assertIn(SECRET, request.read_text(encoding='utf-8'))
        self.assertIs(job['helper'], child)
        self.assertFalse(self.api.status(job['id'])['pending'])
        self.assertFalse(self.api.busy())
        self.assertNotIn(SECRET, json.dumps(self.api.status(job['id'])))
        with mock.patch('learning_picker._end_helper'):
            self.assertTrue(self.api.close(1))
        self.assertFalse(request.exists())

    def test_result_display_failure_does_not_downgrade_verified_business_result(self):
        job = self.job()
        job['result'] = progress('verified')
        self.display.side_effect = OSError(SECRET)
        with mock.patch.object(self.api, '_cleanup_form', return_value=True):
            self.api._finish(job)
        self.assertTrue(job['result']['task_verified'])
        self.assertEqual(job['result']['status'], 'verified')
        self.assertEqual(job['result_display_error'], SECRET)
        self.assertNotIn(SECRET, json.dumps(receipt(job['result'])))
        self.assertTrue(job['done'].is_set())

    def test_new_run_closes_prior_result_before_new_form_worker(self):
        old = self.job()
        old['done'].set()
        old['helper_role'] = 'result'
        self.api.jobs[old['id']] = old
        order = []
        with mock.patch('local_runs.TaskRevisions') as revisions, \
             mock.patch.object(self.api, '_cleanup_form', side_effect=lambda item: order.append('closed') or True), \
             mock.patch.object(self.api, '_start_worker', side_effect=lambda *args: order.append('started')):
            revisions.return_value.get.return_value = task()
            self.api.start(TASK, 1)
        self.assertEqual(order, ['closed', 'started'])
        for job in self.api.jobs.values(): job['done'].set()

    def test_busy_and_worker_identity_exclude_idle_result_form(self):
        job = self.job()
        job['worker'] = threading.current_thread()
        self.api.jobs[job['id']] = job
        self.assertTrue(self.api.busy())
        self.assertTrue(self.api.is_worker_thread())
        job['done'].set()
        job['helper_role'] = 'result'
        self.assertFalse(self.api.busy())
        self.assertFalse(self.api.is_worker_thread())
        job['cleanup_pending'] = True
        self.assertTrue(self.api.busy())

    def configure_programs(self):
        self.manager.config['programs'] = [
            {'id': 'app', 'name': SECRET, 'enabled': True},
            {'id': 'other', 'name': SECRET, 'enabled': True},
            {'id': 'disabled', 'name': 'Disabled', 'enabled': False}]
        self.manager.easy = SimpleNamespace(_call=mock.Mock(return_value={'structuredContent': {
            'status': 'editing', 'editor_id': '55' * 16, 'title': SECRET}}))

    def chooser_job(self):
        self.configure_programs()
        with mock.patch.object(self.api, '_start_worker'):
            started = self.api.open_editor(name='Local draft')
        return started, self.api.jobs[started['local_run_id']]

    def test_local_program_chooser_hides_names_and_supports_multiple_programs(self):
        started, job = self.chooser_job()
        self.assertNotIn(SECRET, json.dumps(started))
        self.assertEqual(list(job['program_choices'].values()), ['app', 'other'])
        choices = list(job['program_choices'])
        self.assertNotEqual(choices[0], choices[1])
        job['inputs'] = {'programs': choices}
        self.api._open_editor(job)
        self.assertEqual(self.manager.easy._call.call_args.args[:2],
                         ('computer_process_editor', {'program_ids': ['app', 'other'], 'name': 'Local draft'}))
        self.assertEqual(job['result']['editor_id'], '55' * 16)
        self.assertNotIn(SECRET, json.dumps(job['result']))
        self.manager.tasks.save.assert_not_called()
        job['done'].set()

    def test_chooser_confirmation_reuses_owned_local_input_form(self):
        _, job = self.chooser_job()
        choice = list(job['program_choices'])[1]
        job, _, execute, _ = self.run_form(job, inputs={'programs': [choice]})
        execute.assert_not_called()
        self.manager.easy._call.assert_called_once()
        self.assertEqual(self.manager.easy._call.call_args.args[1]['program_ids'], ['other'])
        self.assertEqual(job['result']['status'], 'editing')

    def test_cancelled_chooser_never_opens_an_editor_or_creates_a_task(self):
        _, job = self.chooser_job()
        choice = list(job['program_choices'])[0]
        self.run_form(job, inputs={'programs': [choice]}, result_status='cancelled')
        self.manager.easy._call.assert_not_called()
        self.manager.tasks.save.assert_not_called()

    def test_duplicate_or_newly_disabled_programs_block_editor_open(self):
        for duplicate in (True, False):
            with self.subTest(duplicate=duplicate):
                self.api.jobs.clear()
                _, job = self.chooser_job()
                choice = list(job['program_choices'])[0]
                job['inputs'] = {'programs': [choice, choice] if duplicate else [choice]}
                if not duplicate:
                    self.manager.config['programs'][0]['enabled'] = False
                with self.assertRaises(OperationError):
                    self.api._open_editor(job)
                self.manager.easy._call.assert_not_called()
                job['done'].set()

    def test_existing_visual_task_can_open_for_editing_without_running_or_choosing(self):
        self.configure_programs()
        stored = task()
        stored['steps'] = [{'operation': 'checkpoint', 'review_mode': 'visual'}]
        with mock.patch('local_runs.TaskRevisions') as revisions, mock.patch.object(self.api, '_start_worker'):
            revisions.return_value.get.return_value = stored
            started = self.api.open_editor(TASK)
        job = self.api.jobs[started['local_run_id']]
        self.assertNotIn('program_choices', job)
        self.api._open_editor(job)
        self.assertEqual(self.manager.easy._call.call_args.args[1], {'program_ids': ['app'], 'task_id': TASK})
        self.manager.call.assert_not_called()
        job['done'].set()

    def test_no_registered_program_stops_before_form_creation(self):
        with mock.patch.object(self.api, '_start_worker') as worker:
            with self.assertRaises(OperationError) as failure:
                self.api.open_editor()
        self.assertEqual(failure.exception.code, 'local_programs_missing')
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
