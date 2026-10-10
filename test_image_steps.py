"""Image recipe boundaries and interrupted-input recovery, without a desktop."""
import base64
import copy
import json
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from image_targets import png_dimensions, validate_image_target, validate_match
from image_steps import validate_image_step, execute_image_step
from operations import OperationError
from vendor.guard import Guard, ProtectedImageInput, native_edit_password, action_guidance, result_summary, metadata_event
from workflows import WorkflowRunner, validate_recipe, WorkflowError


def png(width=16, height=16):
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)
    rows = b''.join(b'\0'+bytes((x*17+y*31)%256 for x in range(width*3)) for y in range(height))
    return base64.b64encode(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
        chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')).decode()


def image_target():
    return {'format':'computer-image-target/v1','template_png':png(),'width':16,'height':16,
            'anchor':{'x':.5,'y':.5},'capture_window':{'width':100,'height':100},'min_score':.94,'ambiguity_margin':.03}


TARGET={'pid':100,'window_id':200}


class ImageTargetTests(unittest.TestCase):
    def test_password_style_is_limited_to_known_native_edit_classes(self):
        self.assertTrue(native_edit_password('Edit',0x500100A0))
        self.assertTrue(native_edit_password('WindowsForms10.EDIT.app.0.2bf8098_r6_ad1',0x500100A0))
        self.assertFalse(native_edit_password('Edit',0x50010080))
        for name in ('Chrome_RenderWidgetHostHWND','Button','CustomPassword','WindowsForms10.BUTTON.app.0'):
            self.assertFalse(native_edit_password(name,0x0020))

    def test_replace_all_is_explicit_boolean_and_empty_only_means_explicit_clear(self):
        base={'operation':'image_type_text','image_target':image_target(),'value':'new'}
        self.assertNotIn('replace_all',validate_image_step(base))
        self.assertEqual(validate_image_step({**base,'value':'','replace_all':True})['value'],'')
        for patch in ({'replace_all':'true'},{'replace_all':1},{'value':''}):
            with self.subTest(patch=patch),self.assertRaises(OperationError):
                validate_image_step({**base,**patch})
        with self.assertRaises(OperationError):
            validate_image_step({'operation':'image_click','image_target':image_target(),'replace_all':True})

    def test_png_and_target_roundtrip_has_no_runtime_identity(self):
        target=image_target()
        self.assertEqual(png_dimensions(target['template_png']), (16,16))
        self.assertEqual(validate_image_target(target),target)
        for key in ('pid','window_id','x','path'):
            with self.subTest(key=key), self.assertRaises(OperationError):
                validate_image_target({**target,key:1})

    def test_malformed_oversized_mismatched_or_weak_target_rejected(self):
        for patch in ({'template_png':'AAAA'},{'template_png':png(513,16),'width':513},
                      {'width':17},{'min_score':.5},{'ambiguity_margin':0},{'anchor':{'x':float('nan'),'y':.5}},
                      {'capture_window':{'width':True,'height':100}}, {'source_size':{'width':40,'height':16}}):
            with self.subTest(patch=list(patch)), self.assertRaises(OperationError):
                validate_image_target({**image_target(),**patch})
        original=image_target()['template_png']
        with self.assertRaises(OperationError):
            png_dimensions(original[:-6]+'AAAAAA')

    def test_moved_and_scaled_matches_use_new_pixel_coordinates(self):
        shot=png(100,100)
        for rect, expected in [({'x':10,'y':20,'width':16,'height':16},(18,28)),
                               ({'x':50,'y':50,'width':24,'height':24},(62,62))]:
            value=validate_match({'status':'matched','score':.99,'rect':rect,'screenshot':{'width':100,'height':100}},image_target(),shot)
            self.assertEqual((value['x'],value['y']),expected)

    def test_missing_and_ambiguous_return_no_coordinates(self):
        for status in ('not_found','ambiguous'):
            value=validate_match({'status':status},image_target(),png(100,100))
            self.assertEqual(value['status'],status)
            self.assertNotIn('x',value)

    def test_match_bounds_score_and_actual_screenshot_size_are_required(self):
        good={'status':'matched','score':.99,'rect':{'x':10,'y':10,'width':16,'height':16},'screenshot':{'width':100,'height':100}}
        for patch in ({'score':.93},{'score':float('nan')},{'rect':{'x':90,'y':10,'width':16,'height':16}},
                      {'screenshot':{'width':99,'height':100}}):
            with self.subTest(patch=patch), self.assertRaises(OperationError):
                validate_match({**good,**patch},image_target(),png(100,100))


class Transport:
    def __init__(self):
        self.calls=[];self.after=None;self.error=False
    def driver_request(self, method, request, timeout=None):
        self.calls.append(copy.deepcopy(request))
        if request['name']=='get_window_state':
            result={'structuredContent':{**TARGET,'screenshot_width':100,'screenshot_height':100},
                    'content':[{'type':'image','mimeType':'image/png','data':png(100,100)}]}
        else:
            result={'isError':True} if self.error else {'structuredContent':{'effect':'unverifiable'},'content':[]}
        if self.after:self.after(request,result)
        return result


class ImageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.transport=Transport();self.owner=100;self.ready=True;self.geometry=(0,0,100,100,2);self.focus=300
        self.policy={'driver':str(Path(self.tmp.name)/'driver.exe'),'run_dir':self.tmp.name,
                     'allowed_apps':[str(Path(self.tmp.name)/'Editor.exe')],'mode':'uia','approval_mode':'run','max_actions':10}
        self.guard=Guard(self.policy,transport=self.transport,process_resolver=lambda p:self.policy['allowed_apps'][0],
            window_resolver=lambda h:self.owner,checkpoint_ready_resolver=lambda h:self.ready,
            image_geometry_resolver=lambda h:self.geometry,image_focus_resolver=lambda target:self.focus)
        self.match={'status':'matched','x':30,'y':40,'screenshot':{'width':100,'height':100}}
        self.matcher=mock.Mock(side_effect=lambda t,p:dict(self.match))
        self.step={'operation':'image_click','image_target':image_target()}
    def run_step(self, step=None):
        return self.guard.image_action(step or self.step,TARGET,self.matcher,lambda:None)
    def mutations(self):return [c for c in self.transport.calls if c['name']!='get_window_state']

    def test_fresh_capture_one_input_no_direct_uia_grant_or_success_claim(self):
        answer=self.run_step()
        self.assertTrue(answer['input_dispatched']);self.assertTrue(answer['verification_deferred'])
        self.assertFalse(answer['task_verified']);self.assertEqual(self.guard.policy['mode'],'uia')
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(self.mutations()[0]['arguments'],{**TARGET,'x':30,'y':40,'delivery_mode':'foreground'})
        self.assertEqual(self.guard.observed_targets,set())
        self.assertTrue(self.guard.call('click',{**TARGET,'x':30,'y':40})['isError'])

    def test_unknown_ambiguous_and_wrong_target_never_dispatch(self):
        for status in ('not_found','ambiguous'):
            self.match['status']=status
            self.assertFalse(self.run_step()['input_dispatched'])
        self.match['status']='matched';self.owner=101
        self.assertFalse(self.run_step()['input_dispatched'])
        self.assertEqual(self.mutations(),[])

    def test_foreground_geometry_loss_or_cancellation_during_match_never_dispatch(self):
        for change in (lambda:setattr(self,'ready',False),lambda:setattr(self,'geometry',(1,0,101,100,2))):
            self.ready=True;self.geometry=(0,0,100,100,2)
            self.matcher.side_effect=lambda t,p:(change() or self.match)
            self.assertFalse(self.run_step()['input_dispatched'])
        self.assertEqual(self.mutations(),[])

    def test_cancel_after_capture_suppresses_input(self):
        def cancel(t,p):raise RuntimeError('cancelled')
        self.matcher.side_effect=cancel
        with self.assertRaises(RuntimeError):self.run_step()
        self.assertEqual(self.mutations(),[])
        self.assertEqual(self.guard.observed_targets,set())

    def test_dpi_unaware_and_mismatched_png_metadata_refuse_input(self):
        self.geometry=(0,0,100,100,0)
        self.assertEqual(self.run_step()['diagnostic']['code'],'image_dpi_unsupported')
        self.geometry=(0,0,100,100,2)
        self.transport.after=lambda req,result:result.get('structuredContent',{}).update(screenshot_width=99)
        self.assertEqual(self.run_step()['diagnostic']['code'],'image_coordinate_mismatch')
        self.assertEqual(self.mutations(),[])

    def test_text_focus_click_is_separate_and_refreshes_before_text(self):
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'hello'})
        self.assertTrue(answer['verification_deferred'])
        self.assertEqual([r['name'] for r in self.transport.calls],['get_window_state','click','get_window_state','type_text'])
        self.assertEqual(self.guard.action_count,2)
        self.assertNotIn('x',self.mutations()[-1]['arguments'])
        self.assertEqual(self.matcher.call_count,1)

    def test_focus_highlight_does_not_require_matching_old_template_again(self):
        self.matcher.side_effect=[self.match,AssertionError('focus highlight must not be matched again')]
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'replacement','replace_all':True})
        self.assertTrue(answer['verification_deferred'])
        self.assertEqual(self.matcher.call_count,1)
        self.assertEqual([r['name'] for r in self.transport.calls],
                         ['get_window_state','click','get_window_state','hotkey','get_window_state','type_text'])

    def test_replace_all_preserves_selection_and_sends_no_clipboard_or_duplicate_click(self):
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'replacement','replace_all':True})
        self.assertTrue(answer['verification_deferred']);self.assertFalse(answer['task_verified'])
        self.assertEqual([r['name'] for r in self.transport.calls],
                         ['get_window_state','click','get_window_state','hotkey','get_window_state','type_text'])
        self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey','type_text'])
        self.assertEqual(self.mutations()[1]['arguments'],{**TARGET,'delivery_mode':'foreground','keys':['CTRL','A']})
        self.assertEqual(self.mutations()[2]['arguments'],{**TARGET,'delivery_mode':'foreground','text':'replacement'})
        self.assertEqual(self.guard.action_count,3)

    def test_explicit_clear_selects_all_then_delete_without_reclicking(self):
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'','replace_all':True})
        self.assertTrue(answer['verification_deferred'])
        self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey','press_key'])
        self.assertEqual(self.mutations()[-1]['arguments'],{**TARGET,'delivery_mode':'foreground','key':'DELETE'})

    def test_replace_all_stops_after_failed_select_all_without_typing_or_retry(self):
        def fail(request,result):
            if request['name']=='hotkey':result['isError']=True
        self.transport.after=fail
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'never sent','replace_all':True})
        self.assertFalse(answer['verification_deferred']);self.assertTrue(answer['input_dispatched'])
        self.assertEqual(answer['diagnostic']['code'],'image_input_unconfirmed')
        self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey'])

    def test_focus_loss_after_select_all_stops_before_typing(self):
        self.transport.after=lambda request,result:setattr(self,'focus',301) if request['name']=='hotkey' else None
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'never sent','replace_all':True})
        self.assertFalse(answer['verification_deferred']);self.assertTrue(answer['input_dispatched'])
        self.assertEqual(answer['diagnostic']['code'],'image_focus_changed')
        self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey'])

    def test_known_password_focus_blocks_every_keyboard_action_after_first_click(self):
        def protected(target):raise ProtectedImageInput('Known password field')
        self.guard.image_focus_resolver=protected
        for step in ({'operation':'image_type_text','value':'not sent','replace_all':True},
                     {'operation':'image_type_text','value':'not sent'},
                     {'operation':'image_press_key','key':'ENTER'},
                     {'operation':'image_hotkey','keys':['CTRL','A']}):
            with self.subTest(operation=step['operation']):
                self.transport.calls.clear();self.guard.action_count=0
                answer=self.run_step({**step,'image_target':image_target()})
                self.assertTrue(answer['input_dispatched']);self.assertFalse(answer['verification_deferred'])
                self.assertEqual(answer['diagnostic']['code'],'image_protected_input')
                self.assertEqual([m['name'] for m in self.mutations()],['click'])
                self.assertEqual(self.guard.action_count,1)

    def test_field_becoming_password_after_select_all_blocks_final_text(self):
        def read_focus(target):
            if any(m['name']=='hotkey' for m in self.mutations()):
                raise ProtectedImageInput('Field became protected')
            return self.focus
        self.guard.image_focus_resolver=read_focus
        answer=self.run_step({'operation':'image_type_text','value':'not sent','replace_all':True,
                              'image_target':image_target()})
        self.assertFalse(answer['verification_deferred'])
        self.assertEqual(answer['diagnostic']['code'],'image_protected_input')
        self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey'])

    def test_failed_input_never_replayed_and_counts_attempt(self):
        self.transport.error=True
        answer=self.run_step()
        self.assertFalse(answer['verification_deferred']);self.assertTrue(answer['input_dispatched'])
        self.assertEqual(self.guard.action_count,1);self.assertEqual(len(self.mutations()),1)

    def test_explicit_semantic_failure_stops_before_later_keys_or_text(self):
        failures = [{'effect':'refused'}, {'effect':'partial'}, {'effect':'suspected_noop'},
                    {'effect':'unverifiable','escalation':{'reason':'delivery_failed','target':'foreground'}},
                    {'code':'background_unavailable'}, {'refusal':{'reason':'permission_required'}},
                    {'delivery':{'mode':'foreground','delivered_count':0}}, {'input_sent':False}]
        for failure in failures:
            with self.subTest(failure=failure):
                self.transport.calls.clear();self.guard.action_count=0
                def refuse(request,result):
                    if request['name']=='hotkey':result['structuredContent']=failure
                self.transport.after=refuse
                answer=self.run_step({'operation':'image_type_text','image_target':image_target(),
                                      'value':'never sent','replace_all':True})
                self.assertFalse(answer['verification_deferred']);self.assertTrue(answer['input_dispatched'])
                self.assertEqual(answer['diagnostic']['code'],'image_input_unconfirmed')
                self.assertEqual([m['name'] for m in self.mutations()],['click','hotkey'])
                self.assertEqual(self.guard.action_count,2)
                last=json.loads((Path(self.tmp.name)/'actions.jsonl').read_text(encoding='utf-8').splitlines()[-1])
                self.assertFalse(last['success'])

    def test_json_text_semantic_failure_blocks_final_review_transition(self):
        def refuse(request,result):
            if request['name']=='click':
                result['structuredContent']={}
                result['content']=[{'type':'text','text':json.dumps({'effect':'refused'})}]
        self.transport.after=refuse
        answer=self.run_step()
        self.assertFalse(answer['verification_deferred']);self.assertEqual(self.guard.action_count,1)

    def test_successful_foreground_sendinput_does_not_inherit_help_text_failure_code(self):
        raw={'structuredContent':{'delivery':{'mode':'foreground'},'effect':'unverifiable','route':'global_input'},
             'content':[{'type':'text','text':'Sent click via SendInput (delivery_mode:foreground).'}]}
        result=action_guidance(raw,'click')
        self.assertFalse(result.get('isError',False))
        self.assertIn('background_unavailable',result['structuredContent']['computer_use_guidance']['next_step'])
        summary=result_summary(result)
        self.assertNotIn('background_unavailable',summary['text'])
        log=metadata_event('result',{'tool':'click','success':True,'summary':summary})
        self.assertEqual(log['summary']['diagnostic_code'],'effect_unconfirmed')

    def test_actual_driver_refusal_diagnostic_is_preserved(self):
        result=action_guidance({'structuredContent':{'effect':'refused','error_code':'background_unavailable'},
            'content':[{'type':'text','text':'background_unavailable: input was not sent'}]},'hotkey')
        self.assertTrue(result['isError'])
        log=metadata_event('result',{'tool':'hotkey','success':False,'summary':result_summary(result)})
        self.assertEqual(log['summary']['diagnostic_code'],'background_unavailable')

    def test_final_success_can_change_foreground_or_close_window_without_claiming_success(self):
        for change in (lambda:setattr(self,'ready',False),lambda:setattr(self,'owner',0),
                       lambda:setattr(self,'geometry',(20,0,120,100,2))):
            self.ready=True;self.owner=100;self.geometry=(0,0,100,100,2)
            self.transport.after=lambda request,result:change() if request['name']=='click' else None
            answer=self.run_step()
            self.assertTrue(answer['input_dispatched'])
            self.assertTrue(answer['verification_deferred'])
            self.assertFalse(answer['task_verified'])
            self.assertEqual(answer['diagnostic']['code'],'image_input_awaiting_review')
        self.assertEqual(len(self.mutations()),3)

    def test_intermediate_focus_click_still_requires_same_window_before_text(self):
        self.transport.after=lambda request,result:setattr(self,'ready',False) if request['name']=='click' else None
        answer=self.run_step({'operation':'image_type_text','image_target':image_target(),'value':'never sent'})
        self.assertTrue(answer['input_dispatched']);self.assertFalse(answer['verification_deferred'])
        self.assertEqual(answer['diagnostic']['code'],'image_requires_foreground')
        self.assertEqual([r['name'] for r in self.transport.calls],['get_window_state','click'])

    def test_input_error_remains_uncertain_even_if_it_also_changes_foreground(self):
        self.transport.error=True
        self.transport.after=lambda request,result:setattr(self,'ready',False) if request['name']=='click' else None
        answer=self.run_step()
        self.assertTrue(answer['input_dispatched']);self.assertFalse(answer['verification_deferred'])
        self.assertEqual(answer['diagnostic']['code'],'image_input_unconfirmed')
        self.assertEqual(len(self.mutations()),1)

    def test_wait_is_readonly_and_approval_geometry_is_rechecked(self):
        answer=self.run_step({'operation':'wait_for_image','image_target':image_target(),'timeout_ms':0})
        self.assertTrue(answer['task_verified']);self.assertEqual(self.mutations(),[])
        self.guard.approve=lambda *args:setattr(self,'ready',False)
        answer=self.run_step()
        self.assertFalse(answer['input_dispatched']);self.assertEqual(self.mutations(),[])


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        from test_workflows import FakeRuntime
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=FakeRuntime(self.tmp.name);self.runtime.stop_event=threading.Event()
        self.runtime.image_action=mock.Mock(return_value={'task_verified':False,'input_dispatched':True,'verification_deferred':True})
        self.runtime.capture_checkpoint=lambda t:{'structuredContent':t,'content':[{'type':'image','data':png(),'mimeType':'image/png'}]}
        self.runner=WorkflowRunner(self.tmp.name)
        self.task={'id':'images','revision':1,'program_ids':['editor'],'variables':{},'steps':[
            {'program_id':'editor','operation':'image_click','image_target':image_target()},
            {'program_id':'editor','operation':'checkpoint','message':'Verify result'}]}
        self.targets=[{'program_id':'editor',**TARGET}]
    def test_mutation_must_be_followed_by_same_target_review(self):
        for steps in (self.task['steps'][:1],[self.task['steps'][0],{'program_id':'editor','window_ref':'other','operation':'checkpoint','message':'x'}]):
            with self.assertRaises(WorkflowError):validate_recipe(steps,{},['editor'])
    def test_delivered_image_pauses_and_resumes_after_explicit_review_without_replay(self):
        answer=self.runner.run(self.runtime,self.task,{},self.targets)
        self.assertEqual(answer['status'],'needs_review');self.assertEqual(answer['completed_steps'],1)
        self.assertFalse(answer['task_verified'])
        again=self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'])
        self.assertEqual(self.runtime.image_action.call_count,1)
        done=self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'],acknowledge_checkpoint=again['checkpoint']['id'])
        self.assertTrue(done['task_verified']);self.assertFalse(done['checkpoint_images_verified'])
        self.assertEqual(self.runtime.image_action.call_count,1)
    def test_interrupted_input_cannot_be_replayed_or_marked_verified(self):
        self.runtime.image_action.side_effect=RuntimeError('connection lost after possible click')
        answer=self.runner.run(self.runtime,self.task,{},self.targets)
        self.assertEqual(answer['status'],'interrupted');self.assertEqual(answer['pending_step'],0)
        again=self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'])
        self.assertEqual(again['status'],'needs_review');self.assertFalse(again['task_verified'])
        self.assertEqual(self.runtime.image_action.call_count,1)
        self.assertEqual(again['last_result']['diagnostic']['code'],'image_action_uncertain')
        with self.assertRaises(WorkflowError):
            self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'],acknowledge_checkpoint='a'*32)

    def test_changed_foreground_checkpoint_recovers_without_repeating_delivered_input(self):
        capture=self.runtime.capture_checkpoint
        self.runtime.capture_checkpoint=lambda t:{'isError':True,'structuredContent':{'error_code':'checkpoint_requires_foreground'},'content':[]}
        answer=self.runner.run(self.runtime,self.task,{},self.targets)
        self.assertEqual(answer['status'],'needs_review');self.assertEqual(answer['completed_steps'],1)
        self.assertEqual(answer['pending_step'],1);self.assertFalse(answer['checkpoint']['capture_available'])
        self.assertEqual(answer['last_result']['diagnostic']['code'],'checkpoint_requires_foreground')
        self.assertEqual(self.runtime.image_action.call_count,1)
        self.runtime.capture_checkpoint=capture
        fresh=self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'])
        self.assertTrue(fresh['checkpoint']['capture_available']);self.assertEqual(self.runtime.image_action.call_count,1)
        done=self.runner.run(self.runtime,self.task,{},self.targets,resume_run_id=answer['run_id'],acknowledge_checkpoint=fresh['checkpoint']['id'])
        self.assertTrue(done['task_verified']);self.assertEqual(self.runtime.image_action.call_count,1)
    def test_wait_retries_only_absence_not_ambiguity(self):
        step={'operation':'wait_for_image','image_target':image_target(),'timeout_ms':1000,'poll_interval_ms':100}
        self.runtime.image_action.side_effect=[{'task_verified':False,'diagnostic':{'code':'image_not_found'}},{'task_verified':True}]
        self.assertTrue(execute_image_step(self.runtime,step,TARGET)['task_verified'])
        self.assertEqual(self.runtime.image_action.call_count,2)
        self.runtime.image_action.reset_mock();self.runtime.image_action.side_effect=None
        self.runtime.image_action.return_value={'task_verified':False,'diagnostic':{'code':'image_ambiguous'}}
        self.assertFalse(execute_image_step(self.runtime,step,TARGET)['task_verified'])
        self.assertEqual(self.runtime.image_action.call_count,1)


if __name__=='__main__':unittest.main()
