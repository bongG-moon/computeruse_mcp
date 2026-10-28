import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import accessibility_diagnostics as diagnostic
from operations import OperationError


TARGET = {'pid': 42, 'window_id': 10}


def response(source='uia_raw', **changes):
    return {**TARGET, 'source': source, 'status': 'ok', 'complete': True,
        'read_only': True, 'authorizes_input': False, 'elements': [], 'observed_count': 0, **changes}


class Probe:
    def __init__(self):
        self.state = {'process_exited': False, 'target_present': True, 'creation_time': 123,
            'executable': 'fixture.exe', 'windows': [{**TARGET, 'class_name': 'Fixture',
            'thread_id': 8, 'owner_window_id': 0}]}
        self.closed = False

    def capture(self):
        return copy.deepcopy(self.state)

    snapshot = capture

    def close(self):
        self.closed = True


def runtime():
    probe = Probe()
    guard = SimpleNamespace(policy={}, process_resolver=None, window_resolver=None,
        check_stop=mock.Mock(), log=mock.Mock())
    return SimpleNamespace(guard=guard, check_active=mock.Mock(),
        create_transition_probe=mock.Mock(return_value=probe)), probe


class AccessibilityDiagnosticTests(unittest.TestCase):
    def test_independent_sources_and_no_input_authorization(self):
        r, probe = runtime()
        with mock.patch('vendor.guard.validate_arguments') as authorize, mock.patch.object(
                diagnostic, '_read_source', side_effect=lambda t,s,*a,**kw: response(s)) as read:
            result = diagnostic.diagnose(r, TARGET)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(set(result['sources']), set(diagnostic.SOURCES))
        self.assertTrue(result['read_only']); self.assertFalse(result['authorizes_input'])
        self.assertFalse(result['input_dispatched']); self.assertFalse(result['focus_dispatched'])
        self.assertEqual(result['screen_state'], 'independent_sequential_samples')
        self.assertTrue(probe.closed)
        self.assertGreaterEqual(authorize.call_count, 9)
        self.assertNotIn('Hybrid', str(r.guard.log.call_args_list))

    def test_provider_timeout_does_not_suppress_other_source(self):
        r, _ = runtime()
        def read(t,s,*a,**kw):
            return diagnostic._empty(s, 'timeout', 'accessibility_provider_timeout') if s == 'uia_raw' else response(s)
        with mock.patch('vendor.guard.validate_arguments'), mock.patch.object(diagnostic, '_read_source', side_effect=read):
            result = diagnostic.diagnose(r, TARGET)
        self.assertFalse(result['complete'])
        self.assertEqual(result['sources']['uia_raw']['status'], 'timeout')
        self.assertIsNone(result['sources']['uia_raw']['observed_count'])
        self.assertEqual(result['sources']['direct_msaa']['status'], 'ok')

    def test_guard_denial_precedes_all_reads(self):
        r, _ = runtime()
        with mock.patch('vendor.guard.validate_arguments', side_effect=OperationError('denied')), \
                mock.patch.object(diagnostic, '_read_source') as read:
            with self.assertRaises(OperationError): diagnostic.diagnose(r, TARGET)
            read.assert_not_called()
            r.create_transition_probe.assert_not_called()

    def test_reused_window_discards_entire_report(self):
        r, probe = runtime()
        def read(t,s,*a,**kw):
            probe.state['creation_time'] += 1
            return response(s)
        with mock.patch('vendor.guard.validate_arguments'), mock.patch.object(diagnostic, '_read_source', side_effect=read) as called:
            with self.assertRaises(OperationError) as caught: diagnostic.diagnose(r, TARGET)
        self.assertEqual(caught.exception.code, 'accessibility_target_changed')
        self.assertEqual(called.call_count, 1); self.assertTrue(probe.closed)

    def test_bounds_and_targets_are_validated_before_helper(self):
        r, _ = runtime()
        for kwargs in ({'timeout': float('nan')}, {'timeout': 0}, {'max_depth': 33},
                {'max_elements': 2001}, {'max_elements': True}):
            with self.assertRaises(OperationError): diagnostic.diagnose(r, TARGET, **kwargs)
        with self.assertRaises(OperationError): diagnostic.diagnose(r, {**TARGET, 'x': 100})
        r.create_transition_probe.assert_not_called()

    def test_helper_contract_rejects_wrong_target_and_action_authorization(self):
        for changes in ({'pid': 43}, {'source': 'other'}, {'read_only': False},
                {'authorizes_input': True}, {'elements': [None]}, {'complete': 'true'}):
            process=mock.Mock(returncode=0)
            process.communicate.return_value=(json.dumps(response(**changes)), '')
            process.poll.return_value=0
            with mock.patch.object(diagnostic.Path, 'is_file', return_value=True), \
                    mock.patch.object(diagnostic.subprocess, 'Popen', return_value=process):
                result=diagnostic._read_source(TARGET, 'uia_raw', 12, 600, 2)
            self.assertEqual(result['diagnostic']['code'], 'accessibility_invalid_response')

    def test_cancel_terminates_native_worker(self):
        process=mock.Mock(returncode=None)
        process.poll.return_value=None
        with mock.patch.object(diagnostic.Path, 'is_file', return_value=True), \
                mock.patch.object(diagnostic.subprocess, 'Popen', return_value=process):
            with self.assertRaises(OperationError):
                diagnostic._read_source(TARGET, 'uia_raw', 12, 600, 2,
                    check=mock.Mock(side_effect=OperationError('cancelled')))
        process.kill.assert_called_once()

    def test_nonfinite_helper_json_is_rejected(self):
        process=mock.Mock(returncode=0)
        process.communicate.return_value=(json.dumps(response(extra=float('nan'))), '')
        process.poll.return_value=0
        with mock.patch.object(diagnostic.Path, 'is_file', return_value=True), \
                mock.patch.object(diagnostic.subprocess, 'Popen', return_value=process):
            result=diagnostic._read_source(TARGET, 'uia_raw', 12, 600, 2)
        self.assertEqual(result['diagnostic']['code'], 'accessibility_invalid_response')

    def test_soft_budget_reserves_startup_and_serialization_without_extending_hard_deadline(self):
        for timeout, expected in ((.25, 150), (1, 600), (2, 1200), (7.5, 6500)):
            with self.subTest(timeout=timeout):
                process=mock.Mock(returncode=0)
                process.communicate.return_value=(json.dumps(response()), '')
                process.poll.return_value=0
                with mock.patch.object(diagnostic.Path, 'is_file', return_value=True), \
                        mock.patch.object(diagnostic.subprocess, 'Popen', return_value=process):
                    result=diagnostic._read_source(TARGET, 'uia_raw', 12, 600, timeout)
                request=json.loads(process.communicate.call_args_list[0].args[0])
                self.assertEqual(request['time_budget_ms'], expected)
                self.assertLess(request['time_budget_ms'], int(timeout * 1000))
                self.assertLessEqual(process.communicate.call_args_list[0].kwargs['timeout'], min(.1, timeout))
                self.assertTrue(result['complete'])


@unittest.skipUnless(os.name == 'nt', 'Windows accessibility providers required')
class NativeAccessibilityDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler=Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
        if not compiler.is_file(): raise unittest.SkipTest('.NET Framework compiler missing')
        cls.temp=tempfile.TemporaryDirectory(prefix='accessibility-native-')
        cls.folder=Path(cls.temp.name)
        cls.helper=cls.folder / diagnostic.HELPER
        cls.fixture=cls.folder / 'fixture.exe'
        cls.regression=cls.folder / 'classification.exe'
        base=compiler.parent
        references=['System.dll','System.Core.dll','System.Web.Extensions.dll',str(base/'Accessibility.dll'),
            *[str(base/'WPF'/name) for name in ('WindowsBase.dll','UIAutomationClient.dll','UIAutomationTypes.dll')]]
        for source,output,refs in [('AccessibilityDiagnostics.cs',cls.helper,references),
                ('fixtures/AccessibilityDiagnosticFixture.cs',cls.fixture,['System.dll','System.Drawing.dll','System.Windows.Forms.dll'])]:
            result=subprocess.run([str(compiler),'/nologo','/target:exe','/platform:anycpu','/optimize+',
                '/codepage:65001',*['/reference:'+r for r in refs],'/out:'+str(output),str(Path(__file__).parent/source)],
                capture_output=True,text=True,encoding='mbcs',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode: raise AssertionError(result.stdout+result.stderr)
        regression_source=cls.folder/'classification.cs'
        regression_source.write_text('''using System;
using System.Collections;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;
class FakeAccessible : AccessibleObject {
    public AccessibleObject Owner;
    public AccessibleRole Kind;
    public Rectangle Area;
    public override AccessibleObject Parent {get{return Owner;}}
    public override AccessibleRole Role {get{return Kind;}}
    public override Rectangle Bounds {get{return Area;}}
}
class DiagnosticClassification {
    static int Main() {
        Type t=typeof(AccessibilityDiagnostics);
        BindingFlags f=BindingFlags.Static|BindingFlags.NonPublic;
        MethodInfo method=t.GetMethod("AcceptMsaaWindow",f);
        FieldInfo errors=t.GetField("errors",f), failed=t.GetField("providerErrors",f), outside=t.GetField("outside",f);
        foreach(int hr in new int[]{unchecked((int)0x80004005),0}) {
            ((IList)errors.GetValue(null)).Clear();failed.SetValue(null,false);outside.SetValue(null,0);
            bool accepted=(bool)method.Invoke(null,new object[]{hr,IntPtr.Zero});
            if(accepted||!(bool)failed.GetValue(null)||((IList)errors.GetValue(null)).Count!=1||(int)outside.GetValue(null)!=0) return 1;
        }
        using(var form=new Form()) using(var sibling=new Form()) {
            IntPtr root=form.Handle,other=sibling.Handle;
            t.GetField("target",f).SetValue(null,root);
            t.GetField("pid",f).SetValue(null,Process.GetCurrentProcess().Id);
            var owner=new FakeAccessible {Kind=AccessibleRole.MenuItem};
            var menu=new FakeAccessible {Kind=AccessibleRole.MenuPopup,Owner=owner};
            var item=new FakeAccessible {Kind=AccessibleRole.MenuItem,Owner=owner,
                Area=new Rectangle(form.Bounds.Left+1,form.Bounds.Top+1,10,10)};
            t.GetField("msaaRoot",f).SetValue(null,menu);
            var scope=t.GetMethod("VirtualMenuScope",f);
            var identities=(System.Collections.Generic.HashSet<string>)t.GetField("directMenuChildren",f).GetValue(null);
            string childIdentity=(string)t.GetMethod("AccessibleIdentity",f).Invoke(null,new object[]{item,0});
            // The identity must come from this root's AccessibleChildren call;
            // same PID/thread and matching bounds alone never authorize a read.
            if((bool)scope.Invoke(null,new object[]{item,0,other}))return 2;
            identities.Add(childIdentity);
            if(!(bool)scope.Invoke(null,new object[]{item,0,other}))return 2;
            identities.Remove(childIdentity);
            if((bool)scope.Invoke(null,new object[]{item,0,other}))return 3;
            identities.Add(childIdentity);item.Area=new Rectangle(form.Bounds.Right+1,form.Bounds.Top,10,10);
            if((bool)scope.Invoke(null,new object[]{item,0,other}))return 4;
            item.Area=new Rectangle(form.Bounds.Left+1,form.Bounds.Top+1,10,10);item.Kind=AccessibleRole.Text;
            if((bool)scope.Invoke(null,new object[]{item,0,other}))return 5;
            item.Kind=AccessibleRole.MenuItem;
            if((bool)scope.Invoke(null,new object[]{item,0,IntPtr.Zero}))return 6;
        }
        return 0;
    }
}''',encoding='utf-8')
        result=subprocess.run([str(compiler),'/nologo','/target:exe','/main:DiagnosticClassification',
            '/codepage:65001',*['/reference:'+r for r in references+['System.Drawing.dll','System.Windows.Forms.dll']],'/out:'+str(cls.regression),
            str(Path(__file__).parent/'AccessibilityDiagnostics.cs'),str(regression_source)],
            capture_output=True,text=True,encoding='mbcs',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stdout+result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_hidden_native_provider_reads_and_redacts_password(self):
        process=subprocess.Popen([str(self.fixture)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            hwnd=int(process.stdout.readline().strip())
            results={}
            for source in ('uia_raw','uia_control','uia_content','direct_msaa'):
                request={'pid':process.pid,'window_id':hwnd,'source':source,'max_depth':12,'max_elements':200}
                output=subprocess.run([str(self.helper)],input=json.dumps(request)+'\n',capture_output=True,
                    text=True,encoding='utf-8',timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
                results[source]=json.loads(output.stdout)
                self.assertEqual(results[source]['pid'],process.pid)
                self.assertEqual(results[source]['window_id'],hwnd)
                self.assertTrue(results[source]['read_only'])
                self.assertFalse(results[source]['authorizes_input'])
                self.assertNotIn('DO-NOT-EXPOSE',output.stdout)
                if source=='uia_raw':
                    limited=subprocess.run([str(self.helper)],input=json.dumps({**request,'time_budget_ms':1})+'\n',
                        capture_output=True,text=True,encoding='utf-8',timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
                    limit_report=json.loads(limited.stdout)
                    self.assertFalse(limit_report['complete'])
                    self.assertEqual(limit_report['status'],'partial')
                    self.assertIn('time_budget',limit_report['limit_reasons'])
            self.assertIn(results['direct_msaa']['status'],('ok','partial'))
            self.assertIn('Hybrid',json.dumps(results['direct_msaa']))
            self.assertGreater(len(results['direct_msaa']['elements']),1)
        finally:
            try: process.communicate('\n',timeout=3)
            except subprocess.TimeoutExpired: process.kill();process.communicate()

    def test_failed_msaa_hwnd_read_and_virtual_menu_scope_counterexamples(self):
        result=subprocess.run([str(self.regression)],capture_output=True,timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0)


if __name__ == '__main__': unittest.main()
