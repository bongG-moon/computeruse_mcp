"""Read-only native fixture matrix; no external app input or corporate data.

Fixture model changes are internal deterministic setup, not recorded CUA input.
100/125/150% are content/font scales, NOT changes to Windows display DPI.
Each A-F case is repeated ten times by default, cycling those three scales.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_portable import COMPILER, compile_accessibility_diagnostics

SOURCES = ('uia_raw', 'uia_control', 'uia_content', 'direct_msaa')
CASES = {
    'A': 'Mixed WinForms/WPF host with deliberately empty WPF child peer',
    'B': 'Native ComboBox list open with N selected',
    'C': 'Separate Searchable modal with same-process owner',
    'D': 'PropertyGrid row plus dynamically created grid editor dropdown',
    'E': 'Keyed result grid scrolled to DEV-017 and property model refreshed',
    'F': 'Recreated context menu and Export to excel > Active sheet submenu',
}


def compile_fixture(path):
    refs = ['System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Drawing.Design.dll',
            'System.Windows.Forms.dll', 'System.Web.Extensions.dll', 'System.Design.dll', 'System.Xaml.dll',
            str(COMPILER.parent / 'Accessibility.dll')]
    refs += [str(COMPILER.parent / 'WPF' / name) for name in (
        'WindowsBase.dll', 'PresentationCore.dll', 'PresentationFramework.dll', 'WindowsFormsIntegration.dll',
        'UIAutomationClient.dll', 'UIAutomationTypes.dll')]
    command = [str(COMPILER), '/nologo', '/target:exe', '/platform:anycpu', '/optimize+', '/codepage:65001',
               *['/reference:' + ref for ref in refs], '/out:' + str(path),
               str(ROOT / 'fixtures/AccessibilityMatrixFixture.cs')]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)


def percentile(values, quantile):
    return round(sorted(values)[max(0, math.ceil(len(values) * quantile)-1)], 3) if values else None


def names_of(data):
    names = []
    for element in data.get('elements', []):
        prop = element.get('properties', {}).get('name', {})
        value = prop.get('value') if isinstance(prop, dict) else None
        if isinstance(value, str):
            names.append(value)
    return names


def pattern_value(element, pattern):
    value = element.get('patterns', {}).get(pattern, {}).get('properties', {})
    return value.get('value', {}) if isinstance(value, dict) else {}


def evaluate_case(case, state, observations):
    raw = observations['uia_raw']
    names = names_of(raw)
    nodes = raw.get('elements', [])
    checks = {'four_independent_sources_returned': len(observations) == 4,
              'no_source_timeout_or_error': all(item.get('status') in ('ok', 'partial', 'empty')
                                                for item in observations.values())}
    if case == 'A':
        peers = [node for node in nodes if node.get('properties', {}).get('name', {}).get('value') == 'CustomWindowAutomationPeer']
        checks.update(empty_peer_observed=len(peers) == 1,
                      hidden_peer_child_not_invented='Hidden peer child' not in names,
                      native_peer_reports_zero_children=bool(peers and peers[0].get('children_status') == 'provider_returned_zero'))
    elif case == 'B':
        selected = [node for node in nodes if node.get('properties', {}).get('name', {}).get('value') == 'N'
                    and pattern_value(node, 'SelectionItem').get('selected') is True]
        checks.update(N_uniquely_selected_in_native_provider=len(selected) == 1,
                      N_and_P_labels_distinct=names.count('N') == 1 and names.count('P') == 1)
    elif case == 'C':
        checks.update(actual_modal=state['modal'] is True,
                      modal_owned_by_fixture=state['owner_window_id'] == state['main_window_id'],
                      modal_choices_observed='Searchable modal choices' in names)
    elif case == 'D':
        values = [pattern_value(node, 'Value').get('value') for node in nodes]
        checks.update(dynamic_editor_owned_by_fixture=state['owner_window_id'] == state['main_window_id'],
                      grid_editor_observed='Property editor choices' in names,
                      code_and_display_distinct='H' in values and 'Hybrid' in values)
    elif case == 'E':
        values = [pattern_value(node, 'Value').get('value') for node in nodes]
        checks.update(scrolled_row_and_property_value_both_observed=values.count('DEV-017') >= 2,
                      fixture_selection_after_scroll=state['selected_device'] == 'DEV-017')
    elif case == 'F':
        accessible = names_of(observations['direct_msaa'])
        checks.update(actual_submenu_stays_open=state['menu_visible'] and state['submenu_visible'],
                      active_sheet_unique_in_msaa=accessible.count('Active sheet') == 1,
                      all_sheet_distinct_in_msaa=accessible.count('All sheet') == 1)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.data/accessibility-matrix-20261028')
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--soft-budget-ms', type=int, default=None,
                        help='Optional native partial-result deadline, below the hard subprocess timeout.')
    parser.add_argument('--cases', nargs='+', choices=tuple(CASES), default=list(CASES))
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10 or not 1 <= args.timeout <= 10:
        parser.error('Use 1..10 repetitions and 1..10 second bounded reads.')
    if args.soft_budget_ms is not None and not 250 <= args.soft_budget_ms <= args.timeout * 1000 - 300:
        parser.error('Native soft budget must be at least 250 ms and 300 ms below the hard timeout.')
    if len(set(args.cases)) != len(args.cases):
        parser.error('Each fixture case can occur only once in --cases.')
    folder = args.output.resolve(); folder.mkdir(parents=True, exist_ok=True)
    helper, fixture = folder / 'accessibility-reader.exe', folder / 'matrix-fixture.exe'
    compile_accessibility_diagnostics(helper); compile_fixture(fixture)
    raw = folder / 'observations'; raw.mkdir(exist_ok=True)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    process = subprocess.Popen([str(fixture)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=flags)
    lines = queue.Queue()
    def reader():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=reader, daemon=True).start()
    def receive():
        line = lines.get(timeout=10)
        if line is None:
            raise RuntimeError('Fixture exited before its state receipt: ' + process.stderr.read()[:2000])
        value = json.loads(line)
        if value.get('error'):
            raise RuntimeError('Fixture state failed: ' + json.dumps(value))
        return value
    samples, states, assertions = [], [], []
    started = time.perf_counter()
    try:
        ready = receive()
        assert ready['ready'] and ready['pid'] == process.pid
        for repetition in range(args.repetitions):
            scale = (1, 1.25, 1.5)[repetition % 3]
            for case in args.cases:
                process.stdin.write(json.dumps({'state': case, 'scale': scale}) + '\n'); process.stdin.flush()
                state = receive()
                assert state['state'] == case and state['pid'] == process.pid
                assert state['input_path'] == 'fixture_internal_state_transition_no_CUA_input'
                states.append(state)
                observations = {}
                for source in SOURCES:
                    request = {key: state[key] for key in ('pid', 'window_id')}
                    request.update(source=source, max_depth=12, max_elements=600)
                    if args.soft_budget_ms is not None:
                        request['time_budget_ms'] = args.soft_budget_ms
                    tick = time.perf_counter()
                    foreground_before = int(user.GetForegroundWindow() or 0)
                    try:
                        result = subprocess.run([str(helper)], input=json.dumps(request) + '\n', text=True,
                            encoding='utf-8', capture_output=True, timeout=args.timeout, creationflags=flags)
                        data = json.loads(result.stdout)
                    except subprocess.TimeoutExpired:
                        data = {'source': source, 'status': 'timeout', 'complete': False, 'observed_count': None,
                                'read_only': True, 'authorizes_input': False, 'elements': []}
                    elapsed = round((time.perf_counter()-tick)*1000, 3)
                    foreground_after = int(user.GetForegroundWindow() or 0)
                    observations[source] = data
                    path = raw / ('%02d-%s-%s.json' % (repetition + 1, case, source))
                    path.write_text(json.dumps({'fixture': state, 'observation': data}, ensure_ascii=False, indent=2), encoding='utf-8')
                    names = names_of(data)
                    samples.append({'case': case, 'repetition': repetition + 1, 'scale': scale,
                        'source': source, 'status': data.get('status'), 'complete': data.get('complete'),
                        'observed_count': data.get('observed_count'), 'elapsed_ms': elapsed,
                        'actual_window_dpi': state['actual_window_dpi'], 'name_count': len(names),
                        'foreground_unchanged': foreground_before == foreground_after,
                        'target_still_exists': bool(user.IsWindow(state['window_id'])),
                        'evidence': path.relative_to(folder).as_posix()})
                checks = evaluate_case(case, state, observations)
                assertions.append({'case': case, 'repetition': repetition + 1, 'scale': scale,
                                   'passed': all(checks.values()), 'checks': checks})
                print(json.dumps({'case': case, 'repetition': repetition + 1, 'scale': scale,
                    'statuses': [row['status'] for row in samples[-4:]], 'assertions_passed': all(checks.values())}), flush=True)
    finally:
        if process.poll() is None:
            try:
                process.stdin.write('{"state":"quit"}\n'); process.stdin.flush(); process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill(); process.wait(timeout=3)
        process.stdin.close(); process.stdout.close(); process.stderr.close()
    matrix = []
    for case in args.cases:
        for source in SOURCES:
            selected = [row for row in samples if row['case'] == case and row['source'] == source]
            values = [row['elapsed_ms'] for row in selected]
            matrix.append({'case': case, 'description': CASES[case], 'source': source, 'reads': len(selected),
                'p50_ms': round(statistics.median(values), 3), 'p95_ms': percentile(values, .95),
                'statuses': {status: sum(row['status'] == status for row in selected)
                             for status in sorted({str(row['status']) for row in selected})}})
    evidence = {}
    for case in args.cases:
        first = json.loads((raw / ('01-%s-uia_raw.json' % case)).read_text(encoding='utf-8'))
        names = names_of(first['observation'])
        evidence[case] = {'named_elements_sample': names[:30], 'fixture_state': first['fixture'],
                         'native_status': first['observation'].get('status')}
    report = {'format': 'computer-accessibility-fixture-matrix/v1', 'read_only': True,
        'external_ui_input': False, 'recording_or_replay_test': False, 'production_app_test': False,
        'source_hashes': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('AccessibilityDiagnostics.cs', 'fixtures/AccessibilityMatrixFixture.cs')},
        'repetitions_per_case': args.repetitions, 'scales': [100, 125, 150],
        'scale_scope': 'Synthetic content/font scale only. Windows display DPI and monitors unchanged.',
        'read_order': list(SOURCES), 'independent_observations_not_atomic': True,
        'helper_process_startup_included': True, 'driver_full_tree_reads': 0,
        'hard_timeout_seconds_per_source': args.timeout, 'soft_budget_ms_per_source': args.soft_budget_ms,
        'total_seconds': round(time.perf_counter()-started, 3), 'matrix': matrix, 'evidence': evidence,
        'samples': samples, 'case_assertions': assertions, 'all_case_assertions_passed': all(row['passed'] for row in assertions),
        'input_failure_metrics': 'not_applicable_no_CUA_input_was_sent', 'limits': [
            'No Infragistics/FarPoint vendor binaries; native standard controls approximate the structural cases.',
            'Property editor service creates its own synthetic dropdown; this does not prove third-party PropertyGrid activation.',
            'Internal fixture selection is setup evidence, not successful CUA input or recording/replay.',
            'Timeout/partial/error inventory is not an empty Provider result.',
            'No real operating-system DPI, multi-monitor, corporate app, export or Excel data validation.']}
    destination = folder / 'RESULT.json'; destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(destination), 'reads': len(samples), 'total_seconds': report['total_seconds']}))


if __name__ == '__main__':
    main()
