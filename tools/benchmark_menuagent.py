"""Read-only native MenuAgent host contract test; no corporate app or CUA input.

Fixture setup uses its own stdin protocol. Content/font scale is synthetic and
does not change Windows display DPI. Existing shared control tests are not rerun.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_portable import COMPILER, compile_accessibility_diagnostics, compile_scoped_controls
from closing import NativeClosureProbe
from scoped_controls import ScopedControls
from operations import OperationError
from vendor.guard import validate_policy, windows_process_exe, windows_window_pid


def compile_fixture(path):
    refs = ['System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll',
            'System.Web.Extensions.dll','System.Xaml.dll']
    refs += [str(COMPILER.parent/'WPF'/name) for name in ('WindowsBase.dll','PresentationCore.dll',
        'PresentationFramework.dll','WindowsFormsIntegration.dll')]
    args = [str(COMPILER),'/nologo','/target:exe','/platform:anycpu','/optimize+','/codepage:65001',
            *['/reference:'+r for r in refs],'/out:'+str(path),str(ROOT/'fixtures/MenuAgentFixture.cs')]
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode: raise RuntimeError(result.stdout+result.stderr)


class Fixture:
    def __init__(self, executable):
        self.process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines = queue.Queue()
        def reader():
            for line in self.process.stdout: self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        assert self.receive()['ready']
    def receive(self):
        line = self.lines.get(timeout=10)
        if line is None: raise RuntimeError('Fixture exited: '+self.process.stderr.read()[:1500])
        value = json.loads(line)
        if value.get('error'): raise RuntimeError(str(value))
        return value
    def command(self, state, **fields):
        self.process.stdin.write(json.dumps({'state':state,**fields})+'\n'); self.process.stdin.flush()
        return self.receive()
    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write('{"state":"quit"}\n'); self.process.stdin.flush()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr): stream.close()


def percentile(values, fraction):
    return round(sorted(values)[max(0,math.ceil(len(values)*fraction)-1)],3)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'.data/menuagent-host-20261028')
    parser.add_argument('--repetitions',type=int,default=10)
    args=parser.parse_args()
    if not 1<=args.repetitions<=10: parser.error('Use 1..10 repetitions.')
    folder=args.output.resolve(); folder.mkdir(parents=True,exist_ok=True)
    observations=folder/'observations'; observations.mkdir(exist_ok=True)
    executable=folder/'menuagent-fixture.exe'; helper=folder/'accessibility-reader.exe'; scoped=folder/'scoped-reader.exe'
    compile_fixture(executable); compile_accessibility_diagnostics(helper); compile_scoped_controls(scoped)
    from business_windows import select_business_window, discover_business_windows, BusinessWindowError
    from window_readiness import wait_ready
    from window_transitions import WindowTransition
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.argtypes=[]; user.GetForegroundWindow.restype=wintypes.HWND
    samples=[]; checks=[]; snapshots=[]
    fixture=Fixture(executable)
    policy=validate_policy({'driver':str(folder/'unused-driver.exe'),'allowed_apps':[str(executable)],
        'run_dir':str(folder),'mode':'uia','approval_mode':'run','max_actions':10})
    runtime=SimpleNamespace(guard=SimpleNamespace(policy=policy,process_resolver=windows_process_exe,
        window_resolver=windows_window_pid,observed_targets=set()),check_active=lambda:None,
        create_transition_probe=lambda target:NativeClosureProbe(target['pid'],target['window_id']))
    reader=ScopedControls(runtime,factory=lambda arguments,**kwargs:subprocess.Popen([str(scoped),*arguments[1:]],**kwargs))
    runtime.observe_completion_controls=reader.observe
    started=time.perf_counter()
    def record(kind,repetition,call):
        before=int(user.GetForegroundWindow() or 0); tick=time.perf_counter()
        value=call()
        samples.append({'kind':kind,'repetition':repetition,'elapsed_ms':round((time.perf_counter()-tick)*1000,3),
                        'foreground_unchanged':before==int(user.GetForegroundWindow() or 0)})
        return value
    def native(state, hwnd, source, label, repetition):
        request={'pid':state['pid'],'window_id':hwnd,'source':source,'max_depth':12,'max_elements':300,'time_budget_ms':2000}
        result=record(label,repetition,lambda:subprocess.run([str(helper)],input=json.dumps(request)+'\n',text=True,
            encoding='utf-8',capture_output=True,timeout=3,creationflags=subprocess.CREATE_NO_WINDOW))
        report=json.loads(result.stdout)
        (observations/f'{repetition:02d}-{label}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        assert report['status'] in ('ok','partial') and report['read_only'] and not report['authorizes_input'],report
        return report
    def names(report):
        return [row.get('properties',{}).get('name',{}).get('value') for row in report.get('elements',[])]
    selectors=[{'name':'조회','within':{'name':'View Commands'}},
               {'name':'조회','within':{'name':'Business Content'}}]
    try:
        for repetition in range(1,args.repetitions+1):
            state=fixture.command('reset',scale=(1,1.25,1.5)[(repetition-1)%3]); snapshots.append(state)
            target={'pid':state['pid'],'window_id':state['first_window_id']}
            probe=NativeClosureProbe(**target)
            try:
                initial=record('native_windows',repetition,probe.capture)
                (observations/f'{repetition:02d}-windows.json').write_text(json.dumps(initial,indent=2),encoding='utf-8')
                selected=select_business_window(initial['windows'],{'title_prefix':'Device Setup Standard (CJTEST)'},pid=state['pid'])
                resident=next(row for row in initial['windows'] if row['window_id']==state['resident_window_id'])
                result={'repetition':repetition,'hidden_resident_excluded':not resident['visible'] and selected['window_id']==target['window_id'],
                    'independent_business_owner':selected['owner_window_id']==0,'stable_prefix_no_user_value':True}
                body=next(row for row in state['windows'] if row['window_id']==target['window_id'])
                toolbar=native(state,body['toolbar_window_id'],'direct_msaa','toolbar_msaa',repetition)
                content=native(state,body['body_window_id'],'uia_raw','body_raw',repetition)
                result['logical_toolbar_and_embedded_body_observed']=names(toolbar).count('조회')==1 and names(content).count('조회')==1
                scoped_result=record('scoped_duplicate_commands',repetition,lambda:reader.observe(target,selectors,timeout_ms=3000))
                rows=scoped_result['structuredContent']['elements']
                result['same_label_scoped_distinct']=sum(row.get('name')=='조회' for row in rows)==2
                (observations/f'{repetition:02d}-scoped.json').write_text(json.dumps(scoped_result,ensure_ascii=False,indent=2),encoding='utf-8')
                try: reader.observe(target,[{'name':'조회'}],timeout_ms=3000)
                except OperationError as error: result['unscoped_duplicate_rejected']=error.code=='ambiguous_selector'
                else: result['unscoped_duplicate_rejected']=False
                # Existing work window remains a separate explicit target.
                second=select_business_window(initial['windows'],{'title_prefix':'Equipment Setup Standard'},pid=state['pid'])
                result['existing_window_selected_while_original_present']=second['window_id']==state['second_window_id']
                transition=WindowTransition(runtime,target,{'mode':'business_window',
                    'window_selector':{'title_prefix':'Equipment Setup Standard'}})
                try:
                    transition.prepare()
                    existing=record('existing_window_transition',repetition,transition.inspect)
                    transition.confirm({'pid':existing['pid'],'window_id':existing['window_id']},existing)
                    result['explicit_existing_transition']=existing['window_id']==second['window_id']
                finally: transition.close()
                transition=WindowTransition(runtime,target,{'mode':'business_window',
                    'window_selector':{'title_prefix':'Inventory Setup'}})
                try:
                    transition.prepare(); fixture.command('new')
                    appeared=record('new_window_transition',repetition,transition.inspect)
                    transition.confirm({'pid':appeared['pid'],'window_id':appeared['window_id']},appeared)
                    result['explicit_new_transition']=appeared['title'].startswith('Inventory Setup')
                finally: transition.close()
                current=probe.snapshot()
                new=select_business_window(current['windows'],{'title_prefix':'Inventory Setup'},pid=state['pid'])
                result['new_modeless_selected_while_original_present']=current['target_present'] and new['owner_window_id']==0
                fixture.command('duplicate')
                try: select_business_window(probe.snapshot()['windows'],{'title_prefix':'Device Setup Standard'},pid=state['pid'])
                except BusinessWindowError as error: result['duplicate_work_window_rejected']=error.code=='business_window_ambiguous'
                else: result['duplicate_work_window_rejected']=False
                # Exercise the public native binding wrapper with real policy,
                # process and window resolvers; an explicit HWND is the only
                # allowed disambiguator for two identically titled work windows.
                duplicate_snapshot=probe.snapshot()
                discovery_rows=[{'pid':row['pid'],'window_id':row['window_id']}
                    for row in duplicate_snapshot['windows'] if row.get('visible')]
                selector={'title_prefix':'Device Setup Standard'}
                try: discover_business_windows(runtime,discovery_rows,selector)
                except BusinessWindowError as error:
                    result['native_discovery_duplicate_rejected']=error.code=='business_window_ambiguous'
                else: result['native_discovery_duplicate_rejected']=False
                explicit=record('explicit_native_business_binding',repetition,
                    lambda:discover_business_windows(runtime,discovery_rows,selector,explicit_target=target))
                result['explicit_native_binding_exact_hwnd']=bool(explicit and
                    (explicit['pid'],explicit['window_id'])==(target['pid'],target['window_id']))
                result['explicit_native_binding_lifetime_proven']=bool(explicit and
                    explicit.get('creation_time')==duplicate_snapshot['creation_time'] and
                    str(Path(explicit.get('executable','')).resolve()).casefold()==str(executable.resolve()).casefold())
                (observations/f'{repetition:02d}-explicit-business-binding.json').write_text(
                    json.dumps({'selected':explicit,'proof_snapshot':{
                        key:duplicate_snapshot[key] for key in ('creation_time','executable','target_present')},
                        'approved_inventory':discovery_rows},ensure_ascii=False,indent=2),encoding='utf-8')
            finally: probe.close()
            # Fresh reset removes intentionally ambiguous candidates.
            state=fixture.command('reset',scale=(1,1.25,1.5)[(repetition-1)%3]); target={'pid':state['pid'],'window_id':state['first_window_id']}
            delayed=fixture.command('delayed',delay_ms=350)
            result['async_starts_before_controls_attached']=next(row for row in delayed['windows'] if row['window_id']==target['window_id'])['phase']=='empty'
            readiness=record('async_readiness',repetition,lambda:wait_ready(runtime,target,
                {'controls':[{'selector':selectors[0],'enabled':True},{'selector':selectors[1],'enabled':True}], 'timeout_ms':2500}))
            (observations/f'{repetition:02d}-readiness.json').write_text(json.dumps(readiness,ensure_ascii=False,indent=2),encoding='utf-8')
            result['async_controls_ready']=readiness.get('status')=='ready'
            fixture.command('body_only')
            no_toolbar=record('body_only_readiness',repetition,lambda:wait_ready(runtime,target,
                {'controls':[{'selector':selectors[1],'enabled':True}], 'timeout_ms':1000}))
            result['toolbar_not_mandatory']=no_toolbar.get('status')=='ready'
            before=NativeClosureProbe(**target)
            try:
                before.capture(); reopened=fixture.command('reopen'); after=before.snapshot()
                new=select_business_window(after['windows'],{'title_prefix':'Device Setup Standard'},pid=state['pid'])
                result['reopen_discards_old_window']=not after['target_present'] and new['window_id']==reopened['first_window_id']
            finally: before.close()
            current=fixture.command('inspect')
            result['no_fixture_clicks']=all(row['body_clicks']==0 and row['toolbar_clicks']==0 for row in current['windows'])
            result['no_input_observation_lease']=not runtime.guard.observed_targets
            result['passed']=all(v for k,v in result.items() if k!='repetition')
            checks.append(result); print(json.dumps(result),flush=True)
        # One targeted alternate hosting read: WinForms -> ElementHost -> WPF.
        state=fixture.command('wpf'); secondary=next(row for row in state['windows'] if row['window_id']==state['second_window_id'])
        wpf=native(state,secondary['body_window_id'],'uia_raw','element_host_wpf',args.repetitions)
        wpf_pass='WPF business content' in names(wpf) and '조회' in names(wpf)
    finally:
        reader.close(); fixture.close()
    timings=[]
    for kind in sorted({row['kind'] for row in samples}):
        rows=[row['elapsed_ms'] for row in samples if row['kind']==kind]
        timings.append({'kind':kind,'count':len(rows),'p50_ms':round(statistics.median(rows),3),'p95_ms':percentile(rows,.95)})
    report={'format':'computer-menuagent-host-validation/v1','repetitions':args.repetitions,
        'input_path':'fixture internal setup, readonly native/scoped observation','actual_cua_input':False,
        'recording_replay_verified':False,'production_app_test':False,'actual_OS_DPI_changed':False,
        'content_scales':sorted({(100,125,150)[i % 3] for i in range(args.repetitions)}),
        'supported_content_scales':[100,125,150],
        'actual_window_dpi':sorted({row['actual_window_dpi'] for row in snapshots}),
        'full_driver_tree_reads':0,'scope_property_reads':'native scoped helper; identity metadata may traverse window',
        'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('fixtures/MenuAgentFixture.cs','tools/benchmark_menuagent.py','business_windows.py','window_readiness.py',
             'window_transitions.py','ScopedControls.cs','AccessibilityDiagnostics.cs')},
        'elapsed_seconds':round(time.perf_counter()-started,3),'checks':checks,'samples':samples,'timings':timings,
        'element_host_wpf_observed':wpf_pass,'all_passed':all(row['passed'] for row in checks) and wpf_pass,
        'foreground_changes_during_observation':sum(not row['foreground_unchanged'] for row in samples),
        'limits':['Standard native controls, not Infragistics/FarPoint/SmartMMDM DLLs.',
            'No CUA action, actual recording/replay, real DPI switching or Excel export.',
            'Existing-window selection is checked; foreground reactivation is not requested.']}
    (folder/'RESULT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'all_passed':report['all_passed'],'timings':timings,'result':str(folder/'RESULT.json')}),flush=True)
    return 0 if report['all_passed'] else 1

if __name__=='__main__': raise SystemExit(main())
