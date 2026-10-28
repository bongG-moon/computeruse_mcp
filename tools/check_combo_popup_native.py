"""One narrow native owner/parent and visibility-episode proof; no CUA input."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_portable import COMPILER
from closing import NativeClosureProbe
from combo_popup import ComboPopupDiscovery
from operations import OperationError
from benchmark_menuagent import Fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'.data/combo-popup-audit/native')
    folder = parser.parse_args().output.resolve(); folder.mkdir(parents=True, exist_ok=True)
    executable = folder/'combo-popup-proof.exe'
    source = ROOT/'fixtures/ComboPopupProofFixture.cs'
    refs = ['System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Web.Extensions.dll']
    compiled = subprocess.run([str(COMPILER), '/nologo', '/target:exe', '/platform:anycpu', '/optimize+',
        '/codepage:65001', *['/reference:'+name for name in refs], '/out:'+str(executable), str(source)],
        text=True, capture_output=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    if compiled.returncode: raise RuntimeError(compiled.stdout+compiled.stderr)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.argtypes = []; user.GetForegroundWindow.restype = wintypes.HWND
    fixture = Fixture(executable)
    runtime = SimpleNamespace(check_active=lambda: None,
        create_transition_probe=lambda target: NativeClosureProbe(target['pid'], target['window_id']))
    checks, evidence, foreground_changes = {}, [], []

    def prepare():
        state = fixture.command('reset')
        tracker = ComboPopupDiscovery(runtime, {key:state[key] for key in ('pid', 'window_id')})
        tracker.prepare()
        evidence.append({'kind': 'before', 'windows': tracker.initial})
        return tracker, state

    def observe(tracker, fn):
        before = int(user.GetForegroundWindow() or 0)
        try: return fn()
        finally: foreground_changes.append(before != int(user.GetForegroundWindow() or 0))

    try:
        tracker, state = prepare()
        try:
            popup_before = next(row for row in tracker.initial if row['window_id'] == state['popup_window_id'])
            checks['hidden_popup_before_expansion'] = popup_before['visible'] is False
            shown = fixture.command('show')
            target = observe(tracker, tracker.discover)
            checks['same_hidden_hwnd_connected_after_show'] = target == {'pid':state['pid'],'window_id':state['popup_window_id']}
            checks['embedded_hwnd_is_actual_owner'] = shown['popup_owner_window_id'] == state['embedded_window_id']
            checks['native_owner_and_parent_path'] = any(row['window_id'] == state['embedded_window_id']
                                                       for row in tracker.selected_path)
            children = observe(tracker, tracker.probe.transition_snapshot)['windows']
            child_ids = {shown['panel_window_id'], shown['list_window_id']}
            checks['native_panel_and_listbox_are_verified_children'] = child_ids <= {
                row['window_id'] for row in children if row.get('is_child') is True and row.get('visible') is True}
            checks['owned_popup_remains_top_level'] = next(row for row in children
                if row['window_id'] == state['popup_window_id']).get('is_child') is not True
            evidence.append({'kind':'native_child_proof','windows':children,'expected_child_hwnds':sorted(child_ids)})
            evidence.append({'kind':'connected','target':target,'path':tracker.selected_path,'window':tracker.selected})
            observe(tracker, tracker.confirm)
            checks['fresh_confirmation_passes'] = True
            fixture.command('hide')
            try: observe(tracker, tracker.confirm)
            except OperationError as error: checks['hidden_popup_rejected'] = error.code == 'combo_popup_changed'
            else: checks['hidden_popup_rejected'] = False
        finally: tracker.close()
        for command, expected in [('independent', None), ('modal', 'combo_popup_kind_unsupported'),
                                  ('disabled', 'combo_modal_owner_disabled')]:
            tracker, state = prepare()
            try:
                fixture.command(command)
                try:
                    target = observe(tracker, tracker.discover)
                    checks[command+'_rejected'] = expected is None and target is None
                except OperationError as error:
                    checks[command+'_rejected'] = error.code == expected
                    evidence.append({'kind':command,'diagnostic_code':error.code})
            finally: tracker.close()
        tracker, state = prepare()
        try:
            fixture.command('show'); fixture.command('second')
            try: observe(tracker, tracker.discover)
            except OperationError as error: checks['two_related_lists_rejected'] = error.code == 'ambiguous_popup'
            else: checks['two_related_lists_rejected'] = False
        finally: tracker.close()
    finally: fixture.close()
    report = {'format':'computer-combo-popup-native-validation/v1','checks':checks,'all_passed':all(checks.values()),
        'evidence':evidence,'foreground_changes_during_observation':sum(foreground_changes),
        'actual_cua_input':False,'recording_replay_verified':False,'production_app_test':False,
        'limits':['Self-owned offscreen fixture HWNDs only; no Infragistics/SmartFX DLLs.',
                  'Fixture IPC configured visibility; no actual dropdown selection or business task was executed.',
                  'Untitled custom modals without native modal evidence still require runtime list/selection evidence.'],
        'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                         ('combo_popup.py','operations.py','closing.py','fixtures/ComboPopupProofFixture.cs','tools/check_combo_popup_native.py')}}
    path = folder/'RESULT.json'; path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'all_passed':report['all_passed'],'checks':checks,'result':str(path)}))
    return 0 if report['all_passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
