"""Real MCP/Driver validation on a new, offline synthetic Chrome window only."""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import subprocess
import time
import uuid

from live_validation import Client, decoded, DRIVER, HERE, safe
from settings import default_config, save_config
from vendor.windows import OwnedProcess


HTML = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:18px sans-serif;padding:32px;background:#f5f7fb;color:#17253f}main{background:white;padding:30px;max-width:850px;border-radius:16px}label{display:block;margin:18px 0 6px}select,input,button{font:18px sans-serif;padding:9px}table{margin-top:25px;border-collapse:collapse}td,th{border:1px solid #ddd;padding:10px}</style>
<main><h1>Computer Use MCP 검증용 화면</h1><p>개인 자료와 외부 전송이 없는 합성 시험입니다.</p>
<label for="batch">교육 회차</label><select id="batch" aria-label="교육 회차"><option>A 회차</option><option>B 회차</option></select>
<label for="status">신청 상태</label><select id="status" aria-label="신청 상태"><option>전체 상태</option><option>신청</option><option>취소</option><option>대기</option></select>
<label for="search">신청자 검색</label><input id="search" aria-label="신청자 검색">
<label for="count">조회 인원</label><input id="count" aria-label="조회 인원" readonly value="12">
<label for="changes">선택 변경 횟수</label><input id="changes" aria-label="선택 변경 횟수" readonly value="0">
<button id="apply">검색 적용</button><label for="applied">확인 문구</label><input id="applied" aria-label="확인 문구" readonly value="확인 전">
<table><thead><tr><th>신청자</th><th>부서</th><th>상태</th></tr></thead><tbody id="rows"></tbody></table></main>
<script>const statuses=['신청','신청','신청','신청','신청','신청','신청','신청','취소','취소','대기','대기'];
function render(){const selected=document.querySelector('#status').value;const rows=statuses.map((s,i)=>({s,i})).filter(r=>selected==='전체 상태'||r.s===selected);document.querySelector('#count').value=String(rows.length);document.querySelector('#rows').innerHTML=rows.slice(0,4).map(r=>`<tr><td>시험자 ${r.i+1}</td><td>시험 부서</td><td>${r.s}</td></tr>`).join('');}
document.querySelector('#status').addEventListener('change',()=>{document.querySelector('#changes').value=String(Number(document.querySelector('#changes').value)+1);render()});
document.querySelector('#apply').onclick=()=>document.querySelector('#applied').value='확인: '+document.querySelector('#search').value;render();</script></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["probe", "popup", "benchmark", "select", "recipe"])
    parser.add_argument("--delivery", choices=["background", "foreground"], default="background")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--known-order", action="store_true", help="Use the explicit option order defined in this synthetic fixture.")
    parser.add_argument("--cold-foreground", action="store_true", help="Let the operation handle explicit-foreground readiness without fixture pre-fronting.")
    opts = parser.parse_args()
    folder = HERE / ".data" / "validation040" / (opts.mode + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file() or not DRIVER.is_file():
        raise RuntimeError("Manually supplied Chrome/Driver is not available.")
    config_path = folder / "config.json"
    config = default_config(config_path)
    config.update(driver=str(DRIVER), programs=[{"id":"chrome", "name":"시험용 Chrome", "exe":str(chrome),
        "control_exes":[], "hints":"", "enabled":True}], approval="client", max_minutes=5, max_actions=40)
    save_config(config_path, config)
    title = "MCP040 " + folder.name
    page = folder / "fixture.html"
    page.write_text(HTML.replace("__TITLE__", title), encoding="utf-8")
    proc = subprocess.Popen([str(chrome), '--user-data-dir='+str(folder/'profile'), '--no-first-run',
        '--no-default-browser-check','--disable-background-networking','--disable-sync','--disable-extensions',
        '--disable-component-update','--new-window',page.as_uri()], creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    tree = OwnedProcess(proc)
    if opts.bundle:
        from build_portable import runtime_environment
        client = Client(config_path, opts.bundle / "server.py", opts.bundle / "runtime/python.exe", runtime_environment(opts.bundle / "runtime"))
    else:
        client = Client(config_path)
    records = []
    def call(name, args=None):
        started = time.monotonic()
        value = client.request("tools/call", {"name":name,"arguments":args or {}}, timeout=100)
        records.append({"tool":name,"seconds":round(time.monotonic()-started,4),"result":safe(value)})
        (folder/'steps.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        return value
    report = {"folder":str(folder),"mode":opts.mode,"delivery":opts.delivery,"llm_used":False,"passed":False,
              "chrome_fixture_accessibility_forced":False,"known_option_order":opts.known_order}
    try:
        client.request('tools/list')
        begun = call('computer_begin',{'program_ids':['chrome'],'task_description':'새로 만든 합성 시험 창의 입력·선택·결과 확인만 검증합니다.'})
        assert not begun.get('isError'), begun
        deadline=time.monotonic()+20
        while True:
            windows=decoded(call('list_windows'))['windows']
            found=[w for w in windows if title in w.get('title','')]
            if len(found)==1: break
            if time.monotonic()>deadline: raise RuntimeError('Unique fixture window not found')
            time.sleep(.2)
        target={k:found[0][k] for k in ('pid','window_id')}
        state=decoded(call('get_window_state',target))
        if opts.delivery=='foreground' and not opts.cold_foreground:
            assert not call('bring_to_front',target).get('isError')
            state=decoded(call('get_window_state',target))
            report['fixture_brought_to_front']=True
        for attempt in range(3):
            if opts.cold_foreground:
                break
            if any(e.get('label')=='신청 상태' and e.get('role')=='ComboBox' for e in state.get('elements',[])):
                break
            state=decoded(call('get_window_state',dict(target,max_depth=32,max_elements=5000)))
        if not opts.cold_foreground:
            assert any(e.get('label')=='신청 상태' and e.get('role')=='ComboBox' for e in state.get('elements',[])), 'Fixture page accessibility not ready; no input dispatched'
        print(json.dumps({"folder":str(folder),"controls":[{k:e[k] for k in ('element_index','parent_index','role','label','value','actions') if k in e}
            for e in state.get('elements',[]) if e.get('role') in ('ComboBox','Edit','ListItem','MenuItem','Option')]},ensure_ascii=False),flush=True)
        if opts.mode=='popup':
            before_windows=decoded(call('list_windows',{'pid':target['pid'],'on_screen_only':True}))['windows']
            combo=[e for e in state['elements'] if e.get('label')=='신청 상태' and e.get('role')=='ComboBox']
            assert len(combo)==1
            call('click',dict(target,element_token=combo[0]['element_token'],delivery_mode=opts.delivery))
            windows=decoded(call('list_windows',{'pid':target['pid'],'on_screen_only':True}))['windows']
            popups=[w for w in windows if w['window_id'] not in {v['window_id'] for v in before_windows}]
            assert len(popups)==1,windows
            after=decoded(call('get_window_state',{**{k:popups[0][k] for k in ('pid','window_id')},'max_depth':32,'max_elements':5000}))
            print(json.dumps({'popup_elements':[{k:e[k] for k in ('element_index','parent_index','role','label','value','actions','selected','focused','rect') if k in e} for e in after.get('elements',[])], 'windows':windows},ensure_ascii=False),flush=True)
            assert after.get('elements'),after
        elif opts.mode=='benchmark':
            samples=[]
            for i in range(3):
                for label,caps in (("legacy",{"max_depth":25,"max_elements":5000}),("bounded",{"max_depth":12,"max_elements":600})) if i%2==0 else (("bounded",{"max_depth":12,"max_elements":600}),("legacy",{"max_depth":25,"max_elements":5000})):
                    answer=call('get_window_state',dict(target,**caps)); data=decoded(answer)
                    assert not answer.get('isError') and any(e.get('label')=='신청 상태' for e in data.get('elements',[])),answer
                    samples.append({"profile":label,"seconds":records[-1]['seconds'],"metrics":data.get('computer_use_metrics')})
            report['samples']=samples
        elif opts.mode=='select':
            answer=call('computer_perform',dict(target,delivery_mode=opts.delivery,step={"operation":"select_option",
                "selector":{"name":"신청 상태","role":"ComboBox"},"value":"신청",
                **({"option_order":["전체 상태","신청","취소","대기"]} if opts.known_order else {}),
                "expect":[{"selector":{"name":"조회 인원","role":"Edit"},"property":"value","equals":"8"}]}))
            report['operation']=decoded(answer)
            assert report['operation'].get('task_verified') is True,report['operation']
        elif opts.mode=='recipe':
            task={"id":"synthetic","name":"합성 반복 입력","instructions":"검색 문구를 입력하고 확인 버튼을 누릅니다.","expected":"확인 문구가 일치합니다.",
                "program_ids":["chrome"],"variables":{"word":{"description":"검색할 합성 문구"}},"steps":[
                    {"program_id":"chrome","operation":"set_value","selector":{"name":"신청자 검색","role":"Edit"},"value":"${word}"},
                    {"program_id":"chrome","operation":"click","selector":{"name":"검색 적용","role":"Button"},
                     "expect":[{"selector":{"name":"확인 문구","role":"Edit"},"property":"value","equals":"확인: ${word}"}]}]}
            assert not call('computer_save_task',task).get('isError')
            report['runs']=[]
            for word in ('시험 A','시험 B'):
                args={'task_id':'synthetic','inputs':{'word':word},'targets':[dict(target,program_id='chrome')],'delivery_mode':opts.delivery}
                answer=decoded(call('computer_run_task',args)); report['runs'].append(answer)
                assert answer.get('task_verified') is True,answer
                resumed=decoded(call('computer_run_task',dict(args,resume_run_id=answer['run_id'])))
                assert resumed.get('task_verified') is True and resumed['last_result']['input_dispatched'] is False,resumed
                report['resume_verified_without_input']=True
        report['passed']=True
    except Exception as exc:
        report['error']=str(exc)[:4000]
        if 'target' in locals():
            try:
                call('computer_end')
                call('computer_begin',{'program_ids':['chrome'],'mode':'visual','task_description':'이 시험 창의 오류 상태만 캡처합니다. 입력은 하지 않습니다.'})
                visual=call('get_window_state',target)
                picture=next(c for c in visual.get('content',[]) if c.get('type')=='image')
                (folder/'failure.png').write_bytes(base64.b64decode(picture['data']))
                report['failure_screenshot']=str(folder/'failure.png')
            except Exception:
                pass
    finally:
        try: call('computer_end')
        finally:
            client.close()
            tree.close()
        (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':
    raise SystemExit(main())
