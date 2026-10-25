"""Local authoring's public boundary never returns business payloads."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from local_delivery import enabled, filter_response, receipt, require_local_task
from operations import OperationError
from server import ComputerManager


SECRET = "SENTINEL-business-value-private-company.internal"
OPAQUE = "ab" * 16


class LocalDeliveryTests(unittest.TestCase):
    def assert_private(self, value):
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("screenshot-bytes", encoded)
        self.assertNotIn("business-password", encoded)

    def test_nested_business_fields_images_skill_body_and_raw_errors_are_not_returned(self):
        raw = {"status": "needs_review", "task_id": OPAQUE, "task_verified": False,
               "title": SECRET, "name": SECRET, "value": SECRET, "path": SECRET,
               "skill_markdown": SECRET, "input_schema": {"business-password": {"default": SECRET}},
               "programs": [{"name": SECRET}], "diagnostic": {"code": SECRET, "message": SECRET},
               "saved_task": {"id": OPAQUE, "name": SECRET, "steps": [{"value": SECRET}]},
               "last_test": {"status": "failed", "message": SECRET, "error": SECRET},
               "tasks": [{"id": OPAQUE, "name": SECRET, "variables": {"business-password": SECRET}}]}
        response = filter_response({"content": [{"type": "text", "text": SECRET},
                                                {"type": "image", "data": "screenshot-bytes", "mimeType": "image/png"}],
                                    "structuredContent": raw, "isError": True, "_meta": {"secret": SECRET}})
        self.assert_private(response)
        self.assertEqual([v["type"] for v in response["content"]], ["text"])
        self.assertEqual(json.loads(response["content"][0]["text"]), response["structuredContent"])
        self.assertEqual(response["structuredContent"]["saved_task"]["step_count"], 1)
        self.assertEqual(response["structuredContent"]["diagnostic"]["code"], "local_operation_failed")

    def test_opaque_identifiers_counts_and_boolean_receipts_survive(self):
        result = receipt({"status": "verified", "task_id": OPAQUE, "run_id": "run-" + OPAQUE,
                          "revision": 2, "completed_steps": 6, "total_steps": 6,
                          "task_verified": True, "input_dispatched": False, "cancelled": False})
        self.assertEqual(result["task_id"], OPAQUE)
        self.assertEqual(result["run_id"], "run-" + OPAQUE)
        self.assertEqual(result["completed_steps"], 6)
        self.assertTrue(result["task_verified"])
        self.assertIs(result["input_dispatched"], False)

    def test_typed_fields_reject_payload_strings_boolean_counts_and_named_identifiers(self):
        result = receipt({"status": "saved", "task_id": SECRET, "run_id": SECRET,
                          "revision": True, "count": -1, "total_steps": 10**20,
                          "task_verified": SECRET, "pending": {"secret": SECRET}, "steps": []})
        self.assert_private(result)
        for key in ("task_id", "run_id", "revision", "count", "total_steps", "task_verified", "pending"):
            self.assertNotIn(key, result)
        self.assertEqual(result["step_count"], 0)

    def test_unknown_diagnostic_code_does_not_imply_success(self):
        result = receipt({"status": "new_failure_status", "diagnostic": {"code": SECRET}})
        self.assert_private(result)
        self.assertIn(result["status"], {"blocked", "failed"})
        self.assertEqual(result["diagnostic"]["code"], "local_operation_failed")

    def test_known_local_policy_error_remains_actionable_without_details(self):
        result = receipt({"status": "blocked", "diagnostic": {"code": "local_visual_dependency", "message": SECRET}})
        self.assertEqual(result["diagnostic"], {"code": "local_visual_dependency"})
        self.assertEqual(result["status"], "blocked")
        self.assert_private(result)

    def test_missing_or_unclassifiable_structured_payload_fails_closed(self):
        for raw in (None, [], SECRET, {"content": [{"type": "text", "text": SECRET}]},
                    {"structuredContent": {"status": [SECRET]}}):
            with self.subTest(raw=type(raw).__name__):
                result = filter_response(raw)
                self.assert_private(result)
                self.assertEqual(result["structuredContent"]["status"], "blocked")

    def test_list_payload_is_bounded_and_drops_non_records(self):
        result = receipt({"tasks": [{"id": OPAQUE, "name": SECRET}] * 200 + [SECRET]})
        self.assertEqual(len(result["tasks"]), 100)
        self.assert_private(result)

    def test_only_explicit_connected_policy_can_release_content(self):
        self.assertTrue(enabled({"data_policy": "local"}))
        self.assertFalse(enabled({"data_policy": "connected"}))
        self.assertTrue(enabled({}))
        self.assertTrue(enabled({"data_policy": SECRET}))

    def test_alias_callback_preserves_legacy_tasks_without_exposing_business_ids(self):
        public = 'task-' + OPAQUE
        alias = mock.Mock(side_effect=lambda value: public if value == 'legacy_business' else value)
        response = filter_response({'structuredContent': {'tasks': [
            {'id': 'legacy_business', 'name': SECRET}, {'id': OPAQUE, 'name': SECRET}]}}, alias)
        self.assertEqual([item['task_id'] for item in response['structuredContent']['tasks']], [public, OPAQUE])
        self.assertNotIn('legacy_business', json.dumps(response))
        self.assert_private(response)

    def test_identifier_prefix_is_not_an_arbitrary_business_text_channel(self):
        value = receipt({'run_id': 'confidentialproject-' + OPAQUE, 'task_id': 'project-' + OPAQUE})
        self.assertNotIn('run_id', value)
        self.assertNotIn('task_id', value)

    def test_product_version_can_be_confirmed_without_arbitrary_metadata(self):
        self.assertEqual(receipt({'version': '0.19.0'})['version'], '0.19.0')
        self.assertNotIn('version', receipt({'version': SECRET}))

    def test_editor_trial_receipt_never_aliases_unsaved_draft_identity(self):
        def alias(value):
            if value.startswith('draft-'): raise ValueError('not in TaskStore')
            return value
        response = filter_response({'structuredContent': {'status': 'editing', 'editor_id': OPAQUE,
            'saved_task': {'id': OPAQUE, 'revision': 1},
            'last_test': {'task_id': 'draft-' + OPAQUE, 'run_id': 'run-' + OPAQUE,
                          'status': 'verified', 'task_verified': True, 'completed_steps': 3}}}, alias)
        self.assertEqual(response['structuredContent']['status'], 'editing')
        trial = response['structuredContent']['last_test']
        self.assertTrue(trial['task_verified'])
        self.assertEqual(trial['run_id'], 'run-' + OPAQUE)
        self.assertNotIn('task_id', trial)
        self.assertNotIn('draft-', json.dumps(response))

    def test_visual_or_deferred_dependency_is_rejected_before_any_execution(self):
        for mode in ("visual", "deferred_visual"):
            task = {"steps": [{"operation": "set_value", "value": SECRET},
                              {"operation": "foreach", "steps": [{"operation": "checkpoint", "review_mode": mode}]}]}
            before = copy.deepcopy(task)
            with self.assertRaises(OperationError) as error:
                require_local_task(task)
            self.assertEqual(error.exception.code, "local_visual_dependency")
            self.assertEqual(task, before)

    def test_human_and_existing_local_conditions_are_preserved(self):
        task = {"steps": [{"operation": "checkpoint", "review_mode": "human", "message": SECRET},
                          {"operation": "checkpoint", "review_mode": "popup_image"},
                          {"operation": "checkpoint"}, {"operation": "set_value", "value": SECRET}]}
        before = copy.deepcopy(task)
        self.assertIsNone(require_local_task(task))
        self.assertEqual(task, before)

    def make_manager(self):
        manager = ComputerManager.__new__(ComputerManager)
        manager.config = {"data_policy": "local", "tool_profile": "full"}
        import threading
        manager.local_results = threading.local()
        manager.local_runs = SimpleNamespace(busy=lambda: False, is_worker_thread=lambda: False)
        manager.task_ids = SimpleNamespace(alias=lambda value: value, resolve=lambda value: value)
        manager.image_delivery = SimpleNamespace(configure=mock.Mock(), filter_response=lambda response, **kwargs: response)
        manager._call = mock.Mock(return_value={"structuredContent": {"status": "saved", "task_id": OPAQUE, "name": SECRET},
                                               "content": [{"type": "text", "text": SECRET}]})
        manager.tasks = mock.Mock()
        manager.workflows = mock.Mock()
        return manager

    def test_manager_filters_full_profile_response_and_blocks_explicit_vision(self):
        manager = self.make_manager()
        result = manager.call("computer_status", {})
        self.assert_private(result)
        manager._call.reset_mock()
        blocked = manager.call("computer_open", {"image_delivery": "vision"})
        self.assertEqual(blocked["structuredContent"]["diagnostic"]["code"], "local_vision_disabled")
        manager._call.assert_not_called()

    def test_manager_preflights_visual_dependency_before_dispatch_or_any_input(self):
        manager = self.make_manager()
        with mock.patch("task_revision.TaskRevisions") as api:
            api.return_value.get.return_value = {"id": OPAQUE, "steps": [{"operation": "checkpoint", "review_mode": "visual"}]}
            result = manager.call("computer_run_task", {"task_id": OPAQUE})
        self.assertEqual(result["structuredContent"]["diagnostic"]["code"], "local_visual_dependency")
        manager._call.assert_not_called()
        manager.workflows.run.assert_not_called()

    def test_manager_resolves_public_alias_only_on_external_entry(self):
        manager = self.make_manager()
        manager.task_ids.resolve = mock.Mock(return_value='legacy_business')
        manager.call('computer_tasks', {'action': 'get', 'task_id': 'task-' + OPAQUE})
        manager.task_ids.resolve.assert_called_once_with('task-' + OPAQUE)
        self.assertEqual(manager._call.call_args.args[1]['task_id'], 'legacy_business')
        manager.local_runs.is_worker_thread = lambda: True
        manager.task_ids.resolve.reset_mock()
        manager.call('computer_tasks', {'action': 'get', 'task_id': 'legacy_business'})
        manager.task_ids.resolve.assert_not_called()

    def test_alias_lookup_failure_never_dispatches_or_returns_raw_identifier(self):
        manager = self.make_manager()
        manager.task_ids.resolve = mock.Mock(side_effect=ValueError(SECRET))
        response = manager.call('computer_tasks', {'action': 'get', 'task_id': SECRET})
        manager._call.assert_not_called()
        self.assertEqual(response['structuredContent']['status'], 'blocked')
        self.assert_private(response)

    def test_resumed_task_checks_frozen_snapshot_not_changed_latest_recipe(self):
        manager = self.make_manager()
        manager.workflows.resume_snapshot.return_value = ({"id": OPAQUE, "steps": [{"operation": "checkpoint", "review_mode": "visual"}]}, {})
        with mock.patch("task_revision.TaskRevisions") as api:
            api.return_value.get.return_value = {"id": OPAQUE, "steps": [{"operation": "checkpoint", "review_mode": "human"}]}
            result = manager.call("computer_run_task", {"task_id": OPAQUE, "resume_run_id": OPAQUE})
        self.assertEqual(result["structuredContent"]["diagnostic"]["code"], "local_visual_dependency")
        manager._call.assert_not_called()

    def test_tools_list_cannot_leak_driver_schema_error_under_local_policy(self):
        manager = self.make_manager()
        manager.schema_error = SECRET
        manager.lowlevel_schemas = mock.Mock(return_value=[])
        self.assert_private(manager.tools_list())


if __name__ == "__main__":
    unittest.main()
