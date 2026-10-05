"""Developer-only, real-driver acceptance workflows on isolated synthetic files.

Uses this product's stdio protocol for all tested screen reads and input. File
access creates fixtures and independently verifies saved output; it never edits
an app document to substitute for the UI steps under test. Not in runtime ZIP.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
import zipfile
import xml.etree.ElementTree as ET

from live_validation import Client, decoded, safe, DRIVER
from settings import default_config, save_config

HERE = Path(__file__).resolve().parent
ROOT = HERE / ".data" / "acceptance-20261004"


class Trial:
    def __init__(self, scenario, attempt, bundle=None):
        self.id = f"{scenario}-{attempt}-{uuid.uuid4().hex[:6]}"
        self.folder = ROOT / self.id
        self.folder.mkdir(parents=True)
        self.config = default_config(self.folder / "config.json")
        self.config.update(driver=str(DRIVER), approval="client", max_minutes=10,
                           max_actions=180, log_detail="metadata")
        if scenario in {'approval_cancel','emergency'}:
            self.config['approval']='session'
        save_config(self.folder / "config.json", self.config)
        self.apps = {p['id']: p for p in self.config['programs']}
        if bundle:
            from build_portable import runtime_environment
            runtime = Path(bundle) / "runtime"
            self.client = Client(self.folder / "config.json", Path(bundle) / "server.py",
                                 runtime / "python.exe", runtime_environment(runtime))
        else:
            self.client = Client(self.folder / "config.json")
        self.steps = []
        self.started = time.monotonic()
        self.summary = {"id": self.id, "scenario": scenario, "attempt": attempt, "llm_used": False,
                        "fixture_folder": str(self.folder), "human_interventions": 0}
        self.schemas = {t['name']: t['inputSchema'] for t in self.client.request('tools/list')['tools']}
        (self.folder/'schemas.json').write_text(json.dumps(self.schemas,ensure_ascii=False,indent=2),encoding='utf-8')

    def tool(self, name, args=None, allow_error=False):
        started = time.monotonic()
        value = self.client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        self.steps.append({"tool": name, "seconds": round(time.monotonic()-started,4),
                           "isError": bool(value.get('isError')), "result": safe(value)})
        (self.folder/'steps.json').write_text(json.dumps(self.steps,ensure_ascii=False,indent=2),encoding='utf-8')
        if value.get('isError') and not allow_error:
            raise RuntimeError(f"{name}: {decoded(value)}")
        return value

    def begin(self, programs, mode='uia'):
        return decoded(self.tool('computer_begin', {'program_ids': programs, 'mode': mode,
            'task_description': f'개발용 합성 자료 {self.id}만 사용해 입력, 저장, 재열기와 복구를 확인합니다. 개인 자료는 조작하지 않습니다.'}))

    def launch_fixture(self, app_id, path, *options):
        return subprocess.Popen([self.apps[app_id]['exe'], *options, str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def windows(self):
        return decoded(self.tool('list_windows', {'on_screen_only': True}))['windows']

    def window(self, title, timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            found=[w for w in self.windows() if title in w.get('title','')]
            if len(found)==1:
                return {'pid':found[0]['pid'],'window_id':found[0]['window_id']}
            if len(found)>1:
                raise RuntimeError('Ambiguous synthetic window: '+title)
            time.sleep(.3)
        raise RuntimeError('Synthetic window not found: '+title)

    def observe(self,target):
        return decoded(self.tool('get_window_state',target))

    def key(self,target,key,modifiers=()):
        self.observe(target)
        name='hotkey' if modifiers else 'press_key'
        args=dict(target,**({'keys':list(modifiers)+[key]} if modifiers else {'key':key}))
        result=self.tool(name,args,True)
        message=' '.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        if result.get('isError') and ('PostMessage' in message and 'ignored' in message or 'background_unavailable' in message):
            self.observe(target)
            result=self.tool(name,dict(args,delivery_mode='foreground'))
            self.summary['foreground_fallbacks']=self.summary.get('foreground_fallbacks',0)+1
        elif result.get('isError'):
            raise RuntimeError(name+': '+message)
        return result

    def element(self,state,*,role=None,label=None,action=None):
        entries=[e for e in state.get('elements',[]) if (role is None or e.get('role')==role)
                 and (label is None or label == e.get('label',''))
                 and (action is None or action in e.get('actions',[]))]
        if len(entries)!=1:
            raise RuntimeError(f'Expected unique element {role}/{label}/{action}: {entries}')
        return entries[0]

    def click(self,target,*,role=None,label=None,action=None):
        state=self.observe(target)
        element=self.element(state,role=role,label=label,action=action)
        result=self.tool('click',dict(target,element_token=element['element_token']),True)
        text=' '.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        if result.get('isError') and 'background_unavailable' in text:
            state=self.observe(target)
            element=self.element(state,role=role,label=label,action=action)
            self.tool('click',dict(target,element_token=element['element_token'],delivery_mode='foreground'))
            self.summary['foreground_fallbacks']=self.summary.get('foreground_fallbacks',0)+1
        elif result.get('isError'):
            raise RuntimeError(text)
        return self.observe(target)

    def write(self,target,text,*,role='Document',label=None,replace=False):
        state=self.observe(target)
        item=self.element(state,role=role,label=label,action='set_value')
        self.tool('set_value' if replace else 'type_text', dict(target,element_token=item['element_token'],
                       **({'value':text} if replace else {'text':text})))
        after=self.observe(target)
        return self.element(after,role=role,label=label,action='set_value')['value']

    def menu(self,target,top,item):
        self.click(target,role='MenuItem',label=top)
        return self.click(target,role='MenuItem',label=item)

    def finish(self, passed, error=None):
        try:
            status=decoded(self.tool('computer_status')).get('session')
            self.summary['session']=status
            self.tool('computer_end')
        finally:
            self.client.close()
        self.summary.update(passed=passed,error=error,seconds=round(time.monotonic()-self.started,4),
                            driver='0.28.2', steps=len(self.steps))
        (self.folder/'result.json').write_text(json.dumps(self.summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(self.summary,ensure_ascii=False),flush=True)


def normalized(value):
    return value.replace('\r\n','\n').replace('\r','\n').rstrip('\n')


def notepad(trial):
    path=trial.folder/(trial.id+' 한글 문서.txt')
    seed='MCP 합성 자료 시작\n'
    path.write_text(seed,encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'])
    target=trial.window(path.name)
    content=('한글 English 0123 !@#$% & <> "따옴표" [검증]\n'+
             '긴 문장을 화면에서 입력하고 저장 후 다시 확인합니다. ABC abc 123.\n')*12
    state=trial.observe(target)
    assert normalized(trial.element(state,role='Document',action='set_value')['value'])==normalized(seed)
    observed=trial.write(target,content)
    expected=seed+content
    assert normalized(observed)==normalized(expected),(observed,expected)
    trial.menu(target,'파일','저장')
    trial.observe(target)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and normalized(path.read_text(encoding='utf-8-sig'))!=normalized(expected):
        time.sleep(.1)
    actual=path.read_text(encoding='utf-8-sig')
    assert normalized(actual)==normalized(expected),'Saved file differs'
    trial.click(target,role='Button',label='탭 닫기')
    time.sleep(.3)
    assert not [w for w in trial.windows() if path.name in w['title']], 'Tab did not close'
    trial.launch_fixture('notepad',path)
    reopened=trial.window(path.name)
    shown=trial.element(trial.observe(reopened),role='Document',action='set_value')['value']
    assert normalized(shown)==normalized(expected),'Reopened screen differs'
    trial.summary.update(saved_file=str(path),characters=len(expected),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),saved_and_reopened=True)


def semantics(trial):
    path=trial.folder/(trial.id+' 입력 의미.txt')
    path.write_text('합성 기준 ABC 123',encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'])
    target=trial.window(path.name)
    before=trial.element(trial.observe(target),role='Document',action='set_value')['value']
    appended=trial.write(target,' 덧붙이기')
    assert appended==before+' 덧붙이기'
    trial.menu(target,'편집','모두 선택')
    changed=trial.write(target,'선택 부분 교체')
    assert changed=='선택 부분 교체',changed
    replaced=trial.write(target,'전체 값 지정 456',replace=True)
    assert replaced=='전체 값 지정 456',replaced
    trial.observe(target)
    trial.tool('bring_to_front',target)
    assert trial.observe(target)['window_id']==target['window_id']
    trial.menu(target,'파일','저장')
    assert path.read_text(encoding='utf-8-sig')=='전체 값 지정 456'
    trial.summary.update(insertion=True,selection_replacement=True,whole_value_replacement=True,window_switch=True)


def replacement(trial):
    path=trial.folder/(trial.id+' 전체 교체.txt')
    path.write_text('합성 기준 ABC 123',encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'])
    target=trial.window(path.name)
    assert trial.write(target,' 덧붙이기')=='합성 기준 ABC 123 덧붙이기'
    assert trial.write(target,'전체 값 지정 456',replace=True)=='전체 값 지정 456'
    trial.observe(target)
    trial.tool('bring_to_front',target)
    assert trial.observe(target)['window_id']==target['window_id']
    trial.menu(target,'파일','저장')
    assert path.read_text(encoding='utf-8-sig')=='전체 값 지정 456'
    trial.summary.update(insertion=True,whole_value_replacement=True,window_switch=True)


def recovery(trial):
    path=trial.folder/(trial.id+' 복구.txt')
    path.write_text('복구용 합성 자료',encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'])
    target=trial.window(path.name)
    before=trial.observe(target)
    stale=trial.element(before,role='Document',action='set_value')['element_token']
    trial.observe(target)
    refused=trial.tool('type_text',dict(target,element_token=stale,text='쓰이면 안 되는 값'),True)
    assert refused.get('isError'),refused
    observed=trial.element(trial.observe(target),role='Document',action='set_value')['value']
    assert observed=='복구용 합성 자료',observed
    replacement='재관찰 후 복구 성공'
    assert trial.write(target,replacement,replace=True)==replacement
    trial.menu(target,'파일','저장')
    assert path.read_text(encoding='utf-8-sig')==replacement
    stopped=decoded(trial.tool('computer_stop'))
    assert stopped.get('stopped'),stopped
    refused=trial.tool('type_text',dict(target,text='중지 후 금지'),True)
    assert refused.get('isError')
    trial.begin(['notepad'])
    assert trial.element(trial.observe(target),role='Document',action='set_value')['value']==replacement
    trial.summary.update(stale_element_refused=True,stop_refused=True,restarted_preserved=True)


def snapshot(trial):
    trial.begin(['notepad','excel','browser'])
    for w in trial.windows():
        if 'MCPQA' in w.get('title','') or 'MCP-' in w.get('title',''):
            state=trial.observe({'pid':w['pid'],'window_id':w['window_id']})
            print(json.dumps(safe(state),ensure_ascii=False),flush=True)


def file_picker(trial):
    path=trial.folder/(trial.id+' 기존 문서.txt')
    path.write_text('파일 선택 전 유지할 합성 내용',encoding='utf-8')
    selected=trial.folder/'한글 공백 폴더'
    selected.mkdir()
    chosen=selected/'선택할 합성 파일.txt'
    chosen.write_text('파일 선택 성공 한글 English 123',encoding='utf-8')
    trial.begin(['notepad'])
    current=trial.windows()
    owned_pids={w['pid'] for w in current if w['title'].startswith('file_picker-') and '기존 문서.txt' in w['title']}
    for w in current:
        if w['pid'] in owned_pids and w['title']=='열기':
            trial.key({k:w[k] for k in ('pid','window_id')},'escape')
    trial.launch_fixture('notepad',path)
    target=trial.window(path.name)
    trial.menu(target,'파일','열기')
    windows=trial.windows()
    dialogs=[w for w in windows if w['pid']==target['pid'] and w['title']=='열기']
    if len(dialogs)!=1:
        state=trial.observe(target)
        print('DIALOG_ELEMENTS',json.dumps([{k:e[k] for k in ('role','label','actions') if k in e} for e in state['elements'] if e['role'] in ('Edit','Button')],ensure_ascii=False),flush=True)
        raise RuntimeError('Open dialog not separately exposed: '+json.dumps(windows,ensure_ascii=False))
    dialog={'pid':dialogs[0]['pid'],'window_id':dialogs[0]['window_id']}
    state=trial.observe(dialog)
    trial.observe(dialog)
    trial.tool('type_text',dict(dialog,text=str(path)))
    trial.key(dialog,'escape')
    target=trial.window(path.name)
    assert trial.element(trial.observe(target),role='Document',action='set_value')['value']=='파일 선택 전 유지할 합성 내용'
    trial.menu(target,'파일','열기')
    dialog_windows=[w for w in trial.windows() if w['pid']==target['pid'] and w['title']=='열기']
    assert len(dialog_windows)==1
    dialog={k:dialog_windows[0][k] for k in ('pid','window_id')}
    trial.observe(dialog)
    trial.tool('type_text',dict(dialog,text=str(chosen)))
    trial.key(dialog,'enter')
    target=trial.window(chosen.name)
    assert trial.element(trial.observe(target),role='Document',action='set_value')['value']=='파일 선택 성공 한글 English 123'
    trial.summary.update(cancelled_wrong_selection=True,korean_space_path_opened=True)


def approval_cancel(trial):
    result=trial.tool('computer_begin',{'program_ids':['notepad'],
        'task_description':'MCP 합성 승인 취소 시험: 허용하지 않고 취소합니다.'},True)
    assert result.get('isError'),'Expected cancellation'
    trial.summary['native_consent_cancelled']=True
    assert not list(trial.folder.glob('runs/*/consent/*.request.json'))
    assert not list(trial.folder.glob('runs/*/consent/*.response.json'))


def emergency(trial):
    result=trial.tool('computer_begin',{'program_ids':['notepad'],
        'task_description':'MCP 합성 긴급 중지 시험: 승인을 허용하지 않고 중지 단축키를 시험합니다.'},True)
    trial.summary['stop_observed_at']=datetime.now(timezone.utc).isoformat()
    assert result.get('isError'),'Expected emergency refusal'
    trial.summary['pending_approval_stopped']=True


def modified_key(trial):
    path=trial.folder/(trial.id+' 수정키.txt')
    path.write_text('수정키 시험용 원문',encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'])
    target=trial.window(path.name)
    trial.observe(target)
    response=trial.tool('press_key',dict(target,key='s',modifiers=['ctrl']),True)
    state=trial.observe(target)
    assert trial.element(state,role='Document',action='set_value')['value']=='수정키 시험용 원문'
    trial.summary.update(modifier_no_literal_character=True,shortcut_refused=bool(response.get('isError')))


def visual(trial):
    path=trial.folder/(trial.id+' 이미지.txt')
    path.write_text('MCP synthetic screenshot test',encoding='utf-8')
    trial.launch_fixture('notepad',path)
    trial.begin(['notepad'],mode='visual')
    target=trial.window(path.name)
    response=trial.tool('get_window_state',target)
    count=sum(item.get('type')=='image' for item in response.get('content',[]))
    assert count>0
    assert 'elements' not in response.get('structuredContent',{})
    trial.summary.update(image_count=count,visual_observation=True)


def modified_excel(trial):
    path=trial.folder/('MCPQA-'+trial.id+'.xlsx')
    shutil.copy2(HERE/'fixtures/blank.xlsx',path)
    trial.launch_fixture('excel',path,'/x')
    trial.begin(['excel'])
    target=trial.window(path.stem,30)
    trial.click(target,role='DataItem',label='A1')
    trial.observe(target)
    trial.tool('type_text',dict(target,text='42'))
    trial.key(target,'enter')
    trial.click(target,role='Button',label='저장')
    assert xlsx_values(path)=={'A1':'42'}
    trial.observe(target)
    refused=trial.tool('hotkey',dict(target,keys=['ctrl','w']),True)
    detail=refused.get('structuredContent',{})
    assert refused.get('isError') and detail.get('input_sent') is False,refused
    state=trial.observe(target)
    assert trial.element(state,role='DataItem',label='A1').get('value')=='42'
    assert not trial.element(state,role='DataItem',label='A2').get('value')
    trial.tool('hotkey',dict(target,keys=['ctrl','w'],delivery_mode='foreground'))
    deadline=time.monotonic()+8
    while time.monotonic()<deadline and any(path.stem in w['title'] for w in trial.windows()):time.sleep(.3)
    assert not any(path.stem in w['title'] for w in trial.windows())
    assert xlsx_values(path)=={'A1':'42'}
    trial.summary.update(background_refused_no_input=True,foreground_closed=True,saved_value_preserved=True)


def browser(trial):
    path=trial.folder/'로컬 입력 시험.html'
    title='MCPQA '+trial.id
    path.write_text('''<!doctype html><html lang="ko"><meta charset="utf-8"><title>'''+title+'''</title>
<style>body{font:22px sans-serif;margin:30px}section{padding:24px;border:1px solid #aaa;margin:20px 0}input,button{font:22px sans-serif;padding:12px}#space{height:1800px;background:linear-gradient(#fff,#ddd)}</style>
<h1>로컬 화면 시험</h1><p>합성 자료만 사용하는 외부 제출 없는 시험입니다.</p><a href="#details">입력 위치로 이동</a>
<div id="space">스크롤 구간</div><section id="details"><h2>입력 시험</h2><label for="entry">시험 문구</label>
<input id="entry" aria-label="시험 문구"><button id="check">입력 확인</button><p id="answer" aria-live="polite">확인 전</p>
<a href="#">처음으로 이동</a></section><script>document.getElementById('check').onclick=()=>{document.getElementById('answer').textContent='확인 완료: '+document.getElementById('entry').value};</script></html>''',encoding='utf-8')
    trial.launch_fixture('browser',path.as_uri(),'--user-data-dir='+str(trial.folder/'browser-profile'),
        '--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-sync','--disable-extensions','--new-window')
    trial.begin(['browser'])
    target=trial.window(title,30)
    initial=trial.observe(target)
    assert title in initial['window_title']
    trial.click(target,role='Hyperlink',label='입력 위치로 이동')
    value='한글 English 0123 & 특수문자 !?'
    actual=trial.write(target,value,role='Edit',label='시험 문구',replace=True)
    assert actual==value,actual
    after=trial.click(target,role='Button',label='입력 확인')
    assert '확인 완료: '+value in json.dumps(after,ensure_ascii=False),after
    trial.summary.update(local_only=True,link_scroll=True,input_and_result=True)


def excel_probe(trial):
    path=trial.folder/('MCPQA-'+trial.id+'.xlsx')
    shutil.copy2(HERE/'fixtures/blank.xlsx',path)
    trial.launch_fixture('excel',path,'/x')
    trial.begin(['excel'])
    target=trial.window(path.stem,30)
    state=trial.observe(target)
    cell=trial.element(state,role='DataItem',label='A1',action='set_value')
    trial.tool('set_value',dict(target,element_token=cell['element_token'],value='합성 제목'))
    state=trial.observe(target)
    print('A1_AFTER',json.dumps(trial.element(state,role='DataItem',label='A1'),ensure_ascii=False),flush=True)
    name=trial.element(state,role='Edit',label='이름 상자',action='set_value')
    trial.tool('set_value',dict(target,element_token=name['element_token'],value='B21'))
    trial.key(target,'enter')
    state=trial.observe(target)
    print('CELLS_AFTER',json.dumps([e for e in state['elements'] if e['role']=='DataItem'],ensure_ascii=False),flush=True)
    trial.click(target,role='Button',label='저장')
    trial.summary['workbook']=str(path)


def excel(trial):
    path=trial.folder/('MCPQA-'+trial.id+'.xlsx')
    shutil.copy2(HERE/'fixtures/blank.xlsx',path)
    trial.launch_fixture('excel',path,'/x')
    trial.begin(['excel'])
    target=trial.window(path.stem,30)
    trial.click(target,role='DataItem',label='A1')
    for i in range(1,21):
        state=trial.observe(target)
        selected=trial.element(state,role='DataItem',label=f'A{i}')
        assert selected.get('selected'),f'Wrong cell before row {i}'
        trial.tool('type_text',dict(target,text=str(i*10 if i!=10 else 999),delay_ms=30))
        trial.key(target,'enter')
        print('ROW',i,flush=True)
    state=trial.observe(target)
    assert trial.element(state,role='DataItem',label='A21').get('selected')
    trial.tool('type_text',dict(target,text='=SUM(A1:A20)'))
    trial.key(target,'enter')
    for _ in range(12):
        trial.key(target,'up')
    state=trial.observe(target)
    assert trial.element(state,role='DataItem',label='A10').get('selected')
    trial.tool('type_text',dict(target,text='100'))
    trial.key(target,'enter')
    trial.click(target,role='Button',label='저장')
    expected={f'A{i}':str(i*10) for i in range(1,21)}
    expected['A21']='2100'
    actual=xlsx_values(path)
    assert actual==expected,(actual,expected)
    excel_close(trial,target,path.stem)
    trial.launch_fixture('excel',path,'/x')
    target=trial.window(path.stem,30)
    state=trial.observe(target)
    assert trial.element(state,role='DataItem',label='A10').get('value')=='100'
    assert xlsx_values(path)==expected
    trial.summary.update(workbook=str(path),rows=20,corrected_cell='A10',formula='=SUM(A1:A20)',sum=2100,saved_and_reopened=True)


def excel_close(trial,target,title):
    state=trial.observe(target)
    buttons=[e for e in state['elements'] if e['role']=='Button' and e['label']=='닫기' and 'invoke' in e['actions']]
    assert buttons
    shallow=min(e['depth'] for e in buttons)
    buttons=[e for e in buttons if e['depth']==shallow]
    assert len(buttons)==1
    trial.tool('click',dict(target,element_token=buttons[0]['element_token']))
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        if not [w for w in trial.windows() if title in w['title']]:return
        time.sleep(.3)
    raise RuntimeError('Synthetic Excel did not close')


def excel_reopen(trial):
    previous=ROOT/'excel-1-0d3bb8'
    path=previous/('MCPQA-'+previous.name+'.xlsx')
    expected={f'A{i}':str(i*10) for i in range(1,21)}
    expected['A21']='2100'
    assert xlsx_values(path)==expected
    trial.begin(['excel'])
    target=trial.window(path.stem)
    dialogs=[target]+[w for w in trial.windows() if w['pid']==target['pid'] and w['window_id']!=target['window_id'] and w['title']]
    dismissed=False
    for w in dialogs:
        dialog={k:w[k] for k in ('pid','window_id')}
        state=trial.observe(dialog)
        matches=[e for e in state['elements'] if e['role']=='Button' and '저장안함' in e['label'].replace(' ','')]
        if len(matches)==1:
            trial.tool('click',dict(dialog,element_token=matches[0]['element_token']))
            dismissed=True
            break
    if not dismissed:
        excel_close(trial,target,path.stem)
    deadline=time.monotonic()+8
    while time.monotonic()<deadline and any(path.stem in w['title'] for w in trial.windows()):time.sleep(.3)
    assert not any(path.stem in w['title'] for w in trial.windows())
    trial.launch_fixture('excel',path,'/x')
    target=trial.window(path.stem,30)
    state=trial.observe(target)
    assert trial.element(state,role='DataItem',label='A10').get('value')=='100'
    assert xlsx_values(path)==expected
    trial.summary.update(previous_trial=previous.name,rows=20,corrected_cell='A10',sum=2100,saved_and_reopened=True,discarded_unsaved_shortcut_error=dismissed)


def xlsx_values(path):
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as z:
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        sheet=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        values={}
        for cell in sheet.findall('.//m:c',ns):
            value=cell.find('m:v',ns)
            if value is not None:
                values[cell.attrib['r']]=shared[int(value.text)] if cell.get('t')=='s' else value.text
        return values


def main():
    global DRIVER
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    sys.stderr.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('scenario',choices=['notepad','semantics','replacement','recovery','snapshot','browser','excel_probe','excel','excel_reopen','file_picker','approval_cancel','emergency','modified_key','modified_excel','visual'])
    parser.add_argument('--attempts',type=int,default=1)
    parser.add_argument('--bundle')
    parser.add_argument('--driver',type=Path,help='Manually prepared cua-driver.exe path')
    args=parser.parse_args()
    if args.driver:
        DRIVER=args.driver.resolve(strict=True)
    ROOT.mkdir(parents=True,exist_ok=True)
    failures=0
    for i in range(1,args.attempts+1):
        trial=Trial(args.scenario,i,args.bundle)
        try:
            globals()[args.scenario](trial)
        except Exception as error:
            failures+=1
            traceback.print_exc()
            trial.finish(False,str(error))
            break
        else:
            trial.finish(True)
    return bool(failures)


if __name__=='__main__':
    raise SystemExit(main())
