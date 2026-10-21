"""Recorded popup bindings integrate with workflow execution and safe resume."""
import copy
import tempfile
import threading
import unittest
from unittest import mock

from test_workflows import FakeRuntime, ScriptedOperations
from test_recording_windows import Probe, row, step
from test_image_steps import image_target, png
from workflows import WorkflowRunner, WorkflowError, validate_recipe


class WorkflowWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = FakeRuntime(self.temp.name)
        self.runtime.stop_event = threading.Event()
        self.runtime.guard.window_resolver = lambda hwnd: 42 if hwnd in (10, 20, 21) else 0
        self.windows = [row(20)]
        self.runtime.create_transition_probe = lambda target: Probe(copy.deepcopy(self.windows))
        self.runner = WorkflowRunner(self.temp.name)
        self.task = {"id": "popup", "program_ids": ["editor"], "variables": {}, "steps": [step(),
            {"program_id": "editor", "window_ref": "popup_1", "operation": "set_value",
             "selector": {"name": "Field"}, "value": "done"},
            {"program_id": "editor", "operation": "set_value", "selector": {"name": "Parent field"}, "value": "done"}]}
        self.targets = [{"program_id": "editor", "pid": 42, "window_id": 10}]

    def run_task(self, engine, **kwargs):
        with mock.patch("operations.Operations", return_value=engine):
            return self.runner.run(self.runtime, self.task, {}, self.targets, **kwargs)

    def test_main_binding_is_enough_for_popup_and_return_to_main(self):
        engine = ScriptedOperations()
        answer = self.run_task(engine)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([call[1] for call in engine.calls], [{"pid": 42, "window_id": 20}, {"pid": 42, "window_id": 10}])

    def test_recreated_automatic_popup_binding_tracks_current_window_for_each_step(self):
        self.task["steps"].insert(2, copy.deepcopy(self.task["steps"][1]))
        original_execute = ScriptedOperations.execute
        fixture = self
        class RecreatingOperations(ScriptedOperations):
            def execute(self, spec, target, **kwargs):
                answer = original_execute(self, spec, target, **kwargs)
                if target["window_id"] == 20: fixture.windows = [row(21)]
                return answer
        updates=[]
        self.runtime.report_progress = lambda phase, **fields: updates.append((phase,fields))
        engine = RecreatingOperations()
        answer = self.run_task(engine)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([call[1]["window_id"] for call in engine.calls], [20,21,10])
        self.assertTrue(any(fields.get("target_window_id")==21 and fields.get("window_kind")=="related" for _,fields in updates))

    def test_named_main_binding_is_resolved_before_owned_window_wait(self):
        self.targets = [{"program_id": "editor", "pid": 42, "window_title": "Parent"}]
        self.runtime.call = lambda *args: {"structuredContent": {"windows": [{"pid": 42, "window_id": 10, "title": "Parent"}]}}
        self.assertTrue(self.run_task(ScriptedOperations())["task_verified"])

    def test_missing_or_ambiguous_popup_sends_no_input(self):
        for rows in ([], [row(20), row(21)]):
            self.windows = rows; engine = ScriptedOperations()
            answer = self.run_task(engine)
            self.assertFalse(answer["task_verified"]); self.assertEqual(engine.calls, [])

    def test_resume_rebinds_popup_and_verifies_failed_input_without_replay(self):
        first = self.run_task(ScriptedOperations([{"task_verified": False}]))
        self.assertEqual(first["pending_step"], 1)
        engine = ScriptedOperations()
        answer = self.run_task(engine, resume_run_id=first["run_id"])
        self.assertTrue(answer["task_verified"])
        self.assertEqual([call[0]["operation"] for call in engine.calls], ["assert", "set_value"])
        self.assertEqual(engine.calls[0][1]["window_id"], 20)

    def test_resume_rejects_supplied_popup_that_differs_from_owned_match(self):
        first = self.run_task(ScriptedOperations([{"task_verified": False}]))
        self.targets.append({"program_id": "editor", "window_ref": "popup_1", "pid": 42, "window_id": 21})
        engine = ScriptedOperations()
        answer = self.run_task(engine, resume_run_id=first["run_id"])
        self.assertEqual(answer["status"], "needs_binding")
        self.assertFalse(answer["task_verified"])
        self.assertEqual(answer["pending_step"], 1)
        self.assertEqual(engine.calls, [])

    def test_resume_rechecks_recorded_popup_relationship_with_explicit_binding(self):
        first = self.run_task(ScriptedOperations([{"task_verified": False}]))
        self.targets.append({"program_id": "editor", "window_ref": "popup_1", "pid": 42, "window_id": 20})
        for windows in ([row(20, owner=99)], [row(20, title="Other")],
                        [row(20, class_name="Other")], [row(20), row(21)]):
            with self.subTest(windows=windows):
                self.windows = windows
                engine = ScriptedOperations()
                answer = self.run_task(engine, resume_run_id=first["run_id"])
                self.assertEqual(answer["status"], "needs_binding")
                self.assertFalse(answer["task_verified"])
                self.assertEqual(engine.calls, [])

    def test_resume_accepts_explicit_binding_to_verified_owned_popup(self):
        first = self.run_task(ScriptedOperations([{"task_verified": False}]))
        self.targets.append({"program_id": "editor", "window_ref": "popup_1", "pid": 42, "window_id": 20})
        engine = ScriptedOperations()
        answer = self.run_task(engine, resume_run_id=first["run_id"])
        self.assertTrue(answer["task_verified"])
        self.assertEqual([call[0]["operation"] for call in engine.calls], ["assert", "set_value"])
        self.assertEqual(engine.calls[0][1]["window_id"], 20)

    def closing_popup_engine(self):
        self.task["steps"][1] = {"program_id": "editor", "window_ref": "popup_1", "operation": "click",
            "selector": {"name": "Apply popup"}, "expect": [{"selector": {"name": "Parent field"},
                "property": "value", "equals": "done"}], "window_transition": {"mode": "auto"}}
        engine = ScriptedOperations([{"task_verified": True,
            "transition": {"state": "verified", "target": {"pid": 42, "window_id": 10}}}])
        execute = engine.execute
        def close(*args, **kwargs):
            answer = execute(*args, **kwargs)
            if answer.get("transition"):
                self.windows = []
                self.runtime.guard.window_resolver = lambda hwnd: 42 if hwnd == 10 else 0
            return answer
        engine.execute = close
        return engine

    def test_verified_popup_close_allows_next_explicit_owner_step(self):
        engine = self.closing_popup_engine()
        answer = self.run_task(engine)
        self.assertTrue(answer["task_verified"])
        self.assertEqual([call[0]["operation"] for call in engine.calls], ["click", "set_value"])
        self.assertEqual([call[1]["window_id"] for call in engine.calls], [20, 10])

    def test_verified_popup_close_never_redefines_popup_ref_as_owner(self):
        engine = self.closing_popup_engine()
        self.task["steps"][2]["window_ref"] = "popup_1"
        answer = self.run_task(engine)
        self.assertFalse(answer["task_verified"])
        self.assertEqual(answer["status"], "needs_binding")
        self.assertEqual(answer["completed_steps"], 2)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0][1]["window_id"], 20)

    def test_closed_popup_checkpoint_returns_to_only_its_declared_owner(self):
        self.task["steps"] = [step(), {"program_id": "editor", "window_ref": "popup_1",
            "operation": "image_click", "image_target": image_target()},
            {"program_id": "editor", "operation": "checkpoint", "return_from": "popup_1", "message": "Confirm parent"}]
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True, "verification_deferred": True})
        captures = []
        def capture(target):
            captures.append(target)
            return {"structuredContent": target, "content": [{"type": "image", "mimeType": "image/png", "data": png()}]}
        self.runtime.capture_checkpoint = capture
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["pending_step"], 2)
        self.assertEqual(captures, [{"pid": 42, "window_id": 10}])
        done = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
        self.assertTrue(done["task_verified"]); self.assertEqual(self.runtime.image_action.call_count, 1)
        for changes in ({"window_ref": "other"}, {"return_from": "main"}, {"program_id": "other"}):
            invalid = copy.deepcopy(self.task["steps"]); invalid[2].update(changes)
            with self.assertRaises(WorkflowError): validate_recipe(invalid, {}, ["editor", "other"])

    def opened_popup_steps(self):
        return [{"program_id": "editor", "operation": "image_click", "image_target": image_target()}, step(),
            {"program_id": "editor", "window_ref": "popup_1", "operation": "checkpoint",
             "opened_from": "main", "message": "Confirm the opened popup"}]

    def test_automatic_popup_image_is_read_only_and_preserves_final_human_review(self):
        steps = self.opened_popup_steps()
        steps[2].update(review_mode='popup_image',review_origin='recording_generated',image_target=image_target())
        steps += [{'operation':'image_click','program_id':'editor','window_ref':'popup_1','image_target':image_target()},
                  {'operation':'checkpoint','program_id':'editor','window_ref':'popup_1','message':'Final business result'}]
        self.task['steps']=steps
        self.runtime.capture_checkpoint=mock.Mock(return_value={'content':[{'type':'image','mimeType':'image/png','data':png()}]})
        calls=[]
        def action(spec,target):
            calls.append(spec['operation'])
            if spec['operation']=='wait_for_image': return {'task_verified':True,'input_dispatched':False}
            return {'task_verified':False,'input_dispatched':True,'verification_deferred':True}
        self.runtime.image_action=action
        result=self.run_task(ScriptedOperations())
        self.assertEqual(result['completed_steps'],4)
        self.assertFalse(result['task_verified'])
        self.assertEqual(calls,['image_click','wait_for_image','image_click'])
        self.assertTrue(result['checkpoint']['capture_available'])
        self.assertEqual(result['step_receipts'][0]['status'],'verified')
        self.assertEqual(result['step_receipts'][3]['status'],'awaiting_verification')
        invalid=copy.deepcopy(steps); invalid[2]['review_origin']='explicit'
        with self.assertRaises(ValueError): validate_recipe(invalid,{},['editor'])

    def test_automatic_popup_missing_image_does_not_click_next_item(self):
        steps=self.opened_popup_steps()
        steps[2].update(review_mode='popup_image',review_origin='recording_generated',image_target=image_target())
        steps += [{'operation':'image_click','program_id':'editor','window_ref':'popup_1','image_target':image_target()},
                  {'operation':'checkpoint','program_id':'editor','window_ref':'popup_1','message':'Final result'}]
        self.task['steps']=steps
        calls=[]
        def action(spec,target):
            calls.append(spec['operation'])
            return ({'task_verified':False,'input_dispatched':False,'diagnostic':{'code':'image_ambiguous'}}
                if spec['operation']=='wait_for_image' else
                {'task_verified':False,'input_dispatched':True,'verification_deferred':True})
        self.runtime.image_action=action
        first=self.run_task(ScriptedOperations())
        self.assertEqual(first['completed_steps'],2)
        self.assertEqual(calls,['image_click','wait_for_image'])
        resumed=self.run_task(ScriptedOperations(),resume_run_id=first['run_id'])
        self.assertFalse(resumed['task_verified'])
        self.assertEqual(calls.count('image_click'),1)

    def test_sent_focus_failure_resume_keeps_automatic_popup_policy(self):
        steps=self.opened_popup_steps()
        steps[2].update(review_mode='popup_image',review_origin='recording_generated',image_target=image_target())
        steps += [{'operation':'image_click','program_id':'editor','window_ref':'popup_1','image_target':image_target()},
                  {'operation':'checkpoint','program_id':'editor','window_ref':'popup_1','message':'Final result'}]
        self.task['steps']=steps
        self.runtime.capture_checkpoint=mock.Mock()
        calls=[]
        def action(spec,target):
            calls.append(spec['operation'])
            return ({'task_verified':True,'input_dispatched':False} if spec['operation']=='wait_for_image' else
                    {'task_verified':False,'input_dispatched':True,'verification_deferred':False})
        self.runtime.image_action=action
        first=self.run_task(ScriptedOperations())
        self.assertEqual(first['completed_steps'],0)
        resumed=self.run_task(ScriptedOperations(),resume_run_id=first['run_id'])
        self.assertEqual(resumed['completed_steps'],3)
        self.assertFalse(resumed['task_verified'])
        self.assertEqual(calls,['image_click','wait_for_image'])
        self.runtime.capture_checkpoint.assert_not_called()
        self.assertNotIn('checkpoint',resumed)

    def test_dispatched_focus_failure_resumes_at_popup_review_without_reclick(self):
        self.task["steps"] = self.opened_popup_steps()
        self.runtime.image_action = mock.Mock(return_value={
            "task_verified":False,"input_dispatched":True,"verification_deferred":False,
            "diagnostic":{"code":"image_input_unconfirmed"}})
        self.runtime.capture_checkpoint = mock.Mock(return_value={
            "content":[{"type":"image","mimeType":"image/png","data":png()}]})
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["completed_steps"],0)
        resumed = self.run_task(ScriptedOperations(),resume_run_id=first["run_id"])
        self.assertEqual(resumed["completed_steps"],2)
        self.assertFalse(resumed["task_verified"])
        self.assertTrue(resumed["checkpoint"]["capture_available"])
        self.assertEqual(resumed["step_receipts"][0]["status"],"awaiting_verification")
        self.runtime.image_action.assert_called_once()
        done = self.run_task(ScriptedOperations(),resume_run_id=first["run_id"],
                             acknowledge_checkpoint=resumed["checkpoint"]["id"])
        self.assertTrue(done["task_verified"])
        self.runtime.image_action.assert_called_once()

    def test_opened_popup_checkpoint_captures_popup_instead_of_modal_owner(self):
        self.task["steps"] = self.opened_popup_steps()
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True, "verification_deferred": True})
        captures = []
        def capture(target):
            captures.append(target)
            if target["window_id"] != 20:
                return {"isError": True, "structuredContent": {"error_code": "checkpoint_requires_foreground"}}
            return {"structuredContent": target, "content": [{"type": "image", "mimeType": "image/png", "data": png()}]}
        self.runtime.capture_checkpoint = capture
        first = self.run_task(ScriptedOperations())
        self.assertFalse(first["task_verified"])
        self.assertEqual(first["pending_step"], 2)
        self.assertTrue(first["checkpoint"]["capture_available"])
        self.assertTrue(first["checkpoint"]["preserve_target_focus"])
        self.assertEqual(captures, [{"pid": 42, "window_id": 20}])
        done = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
        self.assertTrue(done["task_verified"])
        self.assertFalse(done["checkpoint_images_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_custom_winforms_popup_replay_selects_then_returns_without_reopening(self):
        self.task["steps"] = self.opened_popup_steps() + [
            {"program_id":"editor", "window_ref":"popup_1", "operation":"image_click", "image_target":image_target()},
            {"program_id":"editor", "operation":"checkpoint", "return_from":"popup_1", "message":"Confirm selected value"}]
        self.task["steps"][1].update(title="", class_name="WindowsForms10.Window.808.app.0.3d8c971_r10_ad1")
        self.windows = [row(20, title="", class_name="WindowsForms10.Window.808.app.0.ab123_r12_ad2")]
        actions = []
        def action(step, target):
            actions.append(target["window_id"])
            if target["window_id"] == 20:
                self.windows = []
                self.runtime.guard.window_resolver = lambda hwnd: 42 if hwnd == 10 else 0
            return {"task_verified":False, "input_dispatched":True, "verification_deferred":True}
        self.runtime.image_action = action
        self.runtime.capture_checkpoint = lambda target: {"structuredContent":target,
            "content":[{"type":"image", "mimeType":"image/png", "data":png()}]}
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["pending_step"], 2)
        self.assertTrue(first["checkpoint"]["preserve_target_focus"])
        second = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"], acknowledge_checkpoint=first["checkpoint"]["id"])
        self.assertEqual(second["pending_step"], 4)
        self.assertNotIn("preserve_target_focus", second["checkpoint"])
        final = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"], acknowledge_checkpoint=second["checkpoint"]["id"])
        self.assertTrue(final["task_verified"])
        self.assertEqual(actions, [10, 20])

    def test_old_popup_recording_msaa_scope_survives_resume_without_reopening(self):
        self.windows=[]
        self.task['steps']=self.opened_popup_steps()+[
            {'program_id':'editor','window_ref':'popup_1','operation':'image_click','image_target':image_target()},
            {'program_id':'editor','operation':'checkpoint','return_from':'popup_1','message':'Confirm value'}]
        original=copy.deepcopy(self.task)
        self.runtime.recover_msaa_dropdown=mock.Mock(return_value={'pid':42,'window_id':10})
        actions=[]
        def action(spec,target):
            actions.append((spec['operation'],copy.deepcopy(getattr(self.runtime,'msaa_replay_target',None))))
            return {'task_verified':False,'input_dispatched':True,'verification_deferred':True}
        self.runtime.image_action=action
        self.runtime.capture_checkpoint=lambda target: {'structuredContent':target,'content':[{'type':'image','mimeType':'image/png','data':png()}]}
        first=self.run_task(ScriptedOperations())
        self.assertEqual(first['pending_step'],2)
        second=self.run_task(ScriptedOperations(),resume_run_id=first['run_id'],acknowledge_checkpoint=first['checkpoint']['id'])
        self.assertEqual(second['pending_step'],4)
        final=self.run_task(ScriptedOperations(),resume_run_id=first['run_id'],acknowledge_checkpoint=second['checkpoint']['id'])
        self.assertTrue(final['task_verified'])
        self.assertEqual(actions,[('image_click',None),('image_click',{'pid':42,'window_id':10})])
        self.assertEqual(self.task,original)

    def test_opened_popup_checkpoint_cannot_bypass_unknown_image_input(self):
        self.task["steps"] = self.opened_popup_steps()
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": None, "verification_deferred": False})
        self.runtime.capture_checkpoint = mock.Mock()
        self.runtime.create_transition_probe = mock.Mock(side_effect=AssertionError("Popup must not be inspected after unknown input"))
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["pending_step"], 0)
        resumed = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"])
        self.assertFalse(resumed["task_verified"])
        self.assertEqual(resumed["pending_step"], 0)
        self.assertEqual(self.runtime.image_action.call_count, 1)
        self.runtime.capture_checkpoint.assert_not_called()
        self.runtime.create_transition_probe.assert_not_called()

    def test_opened_popup_missing_wait_can_resume_read_only_without_reopening(self):
        self.task["steps"] = self.opened_popup_steps()
        self.windows = []
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True, "verification_deferred": True})
        self.runtime.capture_checkpoint = mock.Mock(return_value={"content": [{"type": "image", "mimeType": "image/png", "data": png()}]})
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["pending_step"], 1)
        self.runtime.capture_checkpoint.assert_not_called()
        self.windows = [row(20)]
        resumed = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"])
        self.assertEqual(resumed["pending_step"], 2)
        self.assertTrue(resumed["checkpoint"]["capture_available"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_owner_mismatch_resume_uses_hosted_chain_without_repeating_click(self):
        self.task["steps"] = self.opened_popup_steps()
        self.windows = [row(20, owner=30)]
        self.runtime.image_action = mock.Mock(return_value={"task_verified": False, "input_dispatched": True, "verification_deferred": True})
        self.runtime.capture_checkpoint = mock.Mock(return_value={"content": [{"type":"image", "mimeType":"image/png", "data":png()}]})
        first = self.run_task(ScriptedOperations())
        self.assertEqual(first["pending_step"], 1)
        def resolved(target):
            probe = Probe(copy.deepcopy(self.windows))
            probe.ownership_path = lambda hwnd, ancestor: [{"window_id":20}, {"window_id":30}, {"window_id":10}]
            return probe
        self.runtime.create_transition_probe = resolved
        resumed = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"])
        self.assertEqual(resumed["pending_step"], 2)
        done = self.run_task(ScriptedOperations(), resume_run_id=first["run_id"], acknowledge_checkpoint=resumed["checkpoint"]["id"])
        self.assertTrue(done["task_verified"])
        self.assertEqual(self.runtime.image_action.call_count, 1)

    def test_opened_popup_checkpoint_requires_exact_adjacent_owner_binding(self):
        steps = self.opened_popup_steps()
        validate_recipe(steps, {}, ["editor"])
        invalid_recipes = [steps[:1] + steps[2:],
            [steps[0], {**steps[1], "owner_ref": "other"}, steps[2]],
            [steps[0], steps[1], {**steps[2], "opened_from": "other"}],
            [steps[0], steps[1], {**steps[2], "window_ref": "other"}],
            [steps[0], steps[1], {**steps[2], "program_id": "other"}],
            [steps[0], steps[1], {**steps[2], "return_from": "popup_1"}],
            [steps[0], steps[1], {"program_id": "editor", "operation": "delay", "duration_ms": 0}, steps[2]]]
        for invalid in invalid_recipes:
            with self.subTest(steps=invalid), self.assertRaises(ValueError):
                validate_recipe(invalid, {}, ["editor", "other"])


if __name__ == "__main__": unittest.main()
