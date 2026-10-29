"""Native menu provider evidence/actions in a disposable offscreen fixture only."""
import copy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest import mock

import menu_accessibility as menu
from semantic_menus import resolve_menu

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')


@unittest.skipUnless(os.name == 'nt' and CSC.is_file(), 'Windows .NET compiler required')
class NativeMenuAccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / '.data' / 'menu-native-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='menu-accessibility-')
        folder = Path(cls.temp.name)
        cls.helper, cls.fixture = folder / 'reader.exe', folder / 'fixture.exe'
        refs = ['System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.IO.Compression.dll',
                'System.Web.Extensions.dll', str(CSC.parent / 'Accessibility.dll')]
        refs += [str(CSC.parent / 'WPF' / name) for name in
                 ('WindowsBase.dll', 'UIAutomationClient.dll', 'UIAutomationTypes.dll', 'UIAutomationProvider.dll')]
        for source, destination in ((ROOT / 'MenuAccessibility.cs', cls.helper),
                                    (ROOT / 'fixtures' / 'MenuAccessibilityFixture.cs', cls.fixture)):
            built = subprocess.run([str(CSC), '/nologo', '/target:exe', '/codepage:65001',
                                    *('/reference:' + name for name in refs), '/out:' + str(destination), str(source)],
                                   capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            if built.returncode:
                raise RuntimeError(built.stdout + built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.result_path = Path(self.temp.name) / (self._testMethodName + '.xlsx')
        self.process = subprocess.Popen([str(self.fixture), str(self.result_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.responses = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.responses.put(line)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        ready = json.loads(self.responses.get(timeout=5))
        self.target = {key: ready[key] for key in ('pid', 'window_id')}

    def tearDown(self):
        if self.process.poll() is None:
            self.process.stdin.write('quit\n')
            self.process.stdin.flush()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def command(self, command):
        self.process.stdin.write(command + '\n')
        self.process.stdin.flush()
        return json.loads(self.responses.get(timeout=3))

    def observe(self, **kwargs):
        with mock.patch.object(menu.Path, 'with_name', return_value=self.helper):
            return menu.observe(self.target, timeout=8, **kwargs)

    def act(self, row, operation, **kwargs):
        with mock.patch.object(menu.Path, 'with_name', return_value=self.helper):
            return menu.act(self.target, row['locator'], row['evidence'], operation, timeout=5, **kwargs)

    @staticmethod
    def named(observation, source, name):
        return next(row for row in observation['sources'][source]['elements'] if row['name'] == name)

    def test_independent_actual_uia_and_msaa_parent_leaf_and_once_only_invoke(self):
        initial = self.observe()
        self.assertTrue(initial['complete'], initial)
        for source in menu.SOURCES:
            parent = self.named(initial, source, 'Export to excel')
            self.assertEqual(parent['role'], 'MenuItem')
            self.assertTrue(parent['enabled'])
            self.assertTrue(parent['visible'])
            self.assertTrue(parent['has_popup'])
            self.assertEqual(parent['coordinate_space'], 'desktop_physical')
            self.assertLess(parent['bounds']['x'], -10000)
            self.assertFalse(parent['authorizes_input'])
            self.assertEqual(parent['parent_index'], 0)
            self.assertNotIn('invoke', parent['supported_actions'])
        parent = resolve_menu(initial, {'path': [{'name': 'Export to excel', 'role': 'MenuItem'}]}, purpose='parent')['item']
        self.assertIn('expand', parent['supported_actions'])
        result = self.act(parent, 'expand')
        self.assertEqual(result['delivery'], 'sent', result)
        self.assertFalse(result['business_result_verified'])
        self.assertTrue(self.command('status')['expanded'])
        after = self.observe()
        for source in menu.SOURCES:
            leaf = self.named(after, source, 'Active sheet')
            parent = self.named(after, source, 'Export to excel')
            self.assertEqual(leaf['parent_index'], parent['index'])
            self.assertFalse(leaf['has_children'])
        leaf = resolve_menu(after, {'path': [{'name': 'Export to excel', 'role': 'MenuItem'},
                                             {'name': 'Active sheet', 'role': 'MenuItem'}]}, purpose='command')['item']
        self.assertTrue(leaf['provider_has_children'])  # Raw text label is not a submenu.
        self.assertFalse(leaf['has_children'])
        self.assertIn('invoke', leaf['supported_actions'])
        result = self.act(leaf, 'invoke')
        self.assertEqual(result['delivery'], 'sent', result)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['task_verified'])
        self.assertEqual(self.command('status')['commands'], 1)
        # Invocation delivery is distinct from actual result-file verification.
        self.assertTrue(self.result_path.is_file())
        with zipfile.ZipFile(self.result_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('xl/workbook.xml', archive.namelist())
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            self.assertEqual(sheet.find('.//s:c[@r="A1"]/s:is/s:t', ns).text, 'SYNTHETIC-MENU-RESULT')

    def test_ten_native_dual_source_observations_record_bounded_latency(self):
        samples = []
        for _ in range(10):
            observation = self.observe()
            self.assertTrue(observation['complete'], observation)
            self.assertFalse(observation['input_dispatched'])
            samples.append(observation['elapsed_ms'])
        report = {'sample_count': len(samples), 'sources_per_sample': len(menu.SOURCES),
                  'p50_ms': round(statistics.median(samples), 2),
                  'p95_ms': round(statistics.quantiles(samples, n=100, method='inclusive')[94], 2),
                  'maximum_ms': max(samples), 'fixture_only': True, 'input_dispatched': False}
        print('native_menu_observe_timings=' + json.dumps(report))
        self.assertEqual(self.command('status')['commands'], 0)
        self.assertFalse(self.result_path.exists())

    def test_saved_recipe_rebinds_recreated_fixture_through_actual_semantic_execution(self):
        """Provider/recipe integration only; this harness does not certify Guard."""
        from closing import NativeClosureProbe
        from operations import OperationError
        from semantic_menus import execute_semantic_expansion, execute_menu_command
        from server import TaskStore
        from workflows import render_steps, operation_step

        # The durable recipe contains only names actually returned by the two
        # providers. Fixture setup opens its own menu without any global input.
        self.command('expand')
        recorded = self.observe()
        parent = self.named(recorded, 'uia_raw', 'Export to excel')
        leaf = self.named(recorded, 'uia_raw', 'Active sheet')
        parent_target = {'path': [{'name': parent['name'], 'role': parent['role']}]}
        leaf_target = {'path': [*parent_target['path'], {'name': leaf['name'], 'role': leaf['role']}]}
        config = {'programs': [{'id': 'fixture'}]}
        store_path = Path(self.temp.name) / 'recipe-state'
        store = TaskStore(store_path, config)
        saved = store.save({'id': 'native-menu-replay', 'name': 'Disposable menu replay',
            'instructions': 'Expand the observed menu then export Active sheet once.',
            'expected': 'Verify the newly created XLSX contents separately.', 'program_ids': ['fixture'],
            'steps': [
                {'operation': 'expand_menu', 'program_id': 'fixture', 'menu_target': parent_target,
                 'child_menu_target': leaf_target, 'expansion_method': 'click', 'step_timeout_ms': 10000,
                 'verification_timeout_ms': 2000},
                {'operation': 'menu_command', 'program_id': 'fixture', 'menu_target': leaf_target,
                 'completion_mode': 'human', 'step_timeout_ms': 10000},
                {'operation': 'checkpoint', 'program_id': 'fixture', 'message': 'Verify the new XLSX result.'}]})
        reopened = TaskStore(store_path, config).get(saved['id'])
        self.assertEqual(reopened, saved)
        serialized = json.dumps(reopened)
        for key in ('"window_id"', '"pid"', '"locator"', '"evidence"', '"bounds"', '"image_target"'):
            self.assertNotIn(key, serialized)
        old_target = dict(self.target)
        self.tearDown()
        self.setUp()
        self.assertNotEqual(old_target['pid'], self.target['pid'])
        self.assertFalse(self.command('status')['expanded'])

        case = self
        class ProviderRuntime:
            """Exact disposable fixture only. No production Guard is simulated."""
            def __init__(self):
                self.stop_event = threading.Event()
                self.actions = []
                self.probes = []
            def check_active(self):
                if self.stop_event.is_set() or case.process.poll() is not None:
                    raise OperationError('Fixture stopped', 'session_stopped')
            def create_transition_probe(self, target):
                case.assertEqual(target, case.target)
                probe = NativeClosureProbe(target['pid'], target['window_id'])
                self.probes.append(probe)
                return probe
            def observe_menu(self, target, *, timeout_ms=2000):
                case.assertEqual(target, case.target)
                with mock.patch.object(menu.Path, 'with_name', return_value=case.helper):
                    return menu.observe(target, timeout=timeout_ms / 1000, check=self.check_active)
            def menu_action(self, target, menu_target, operation, *, timeout_ms=10000):
                selected = resolve_menu(self.observe_menu(target), menu_target,
                                        purpose='command' if operation == 'command' else 'parent')
                native_operation = 'invoke' if operation == 'command' else operation
                # Keep this integration entirely offscreen and pattern-driven.
                case.assertIn(native_operation, ('expand', 'invoke'))
                case.assertIn(native_operation, selected['item']['supported_actions'])
                self.actions.append(native_operation)
                with mock.patch.object(menu.Path, 'with_name', return_value=case.helper):
                    return menu.act(target, selected['item']['locator'], selected['item']['evidence'],
                                    native_operation, timeout=min(5, timeout_ms / 1000), check=self.check_active)
            def image_action(self, *args, **kwargs):
                raise AssertionError('Semantic replay must not compare or click an image.')

        runtime = ProviderRuntime()
        expanded, _ = render_steps(reopened, {})
        transition = execute_semantic_expansion(runtime, operation_step(expanded[0]), self.target)
        self.assertTrue(transition['task_verified'], transition)
        self.assertEqual(transition['verification_scope'], 'menu_child_visibility_only')
        self.assertFalse(transition['business_result_verified'])
        self.assertFalse(self.result_path.exists())
        command = execute_menu_command(runtime, operation_step(expanded[1]), self.target)
        self.assertTrue(command['input_dispatched'], command)
        self.assertFalse(command['task_verified'])
        self.assertFalse(command['business_result_verified'])
        self.assertEqual(runtime.actions, ['expand', 'invoke'])
        self.assertTrue(all(probe.handle is None for probe in runtime.probes))
        self.assertEqual(self.command('status')['commands'], 1)
        with zipfile.ZipFile(self.result_path) as archive:
            self.assertIsNone(archive.testzip())
            xml = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            value = xml.find('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')
            self.assertEqual(value.text, 'SYNTHETIC-MENU-RESULT')

    def test_renamed_or_disabled_leaf_revalidation_never_invokes(self):
        self.command('expand')
        old = self.named(self.observe(), 'uia_raw', 'Active sheet')
        self.command('rename')
        result = self.act(old, 'invoke')
        self.assertEqual(result['delivery'], 'not_sent', result)
        self.assertEqual(result['diagnostic']['code'], 'menu_accessibility_evidence_changed')
        fresh = self.named(self.observe(), 'uia_raw', 'Changed sheet')
        self.command('disable')
        result = self.act(fresh, 'invoke')
        self.assertEqual(result['delivery'], 'not_sent', result)
        self.assertEqual(self.command('status')['commands'], 0)

    def test_msaa_without_stable_identity_cannot_default_action_and_physical_offscreen_fails(self):
        initial = self.observe()
        parent = self.named(initial, 'direct_msaa', 'Export to excel')
        self.assertEqual(parent['identity_kind'], 'path_and_current_properties_only')
        self.assertNotIn('expand', parent['supported_actions'])
        self.assertEqual(self.act(parent, 'expand')['delivery'], 'not_sent')
        position = wintypes.POINT()
        self.assertTrue(ctypes.windll.user32.GetCursorPos(ctypes.byref(position)))
        before = (position.x, position.y)
        for operation in ('hover', 'click_parent'):
            result = self.act(parent, operation)
            self.assertEqual(result['delivery'], 'not_sent', result)
            self.assertEqual(result['diagnostic']['code'], 'menu_accessibility_offscreen')
        ctypes.windll.user32.GetCursorPos(ctypes.byref(position))
        self.assertEqual((position.x, position.y), before)
        self.command('expand')
        leaf = self.named(self.observe(), 'direct_msaa', 'Active sheet')
        self.assertIn('click_leaf', leaf['supported_actions'])
        self.assertEqual(self.act(leaf, 'invoke')['delivery'], 'not_sent')
        self.assertEqual(self.act(leaf, 'click_leaf')['delivery'], 'not_sent')
        self.assertEqual(self.command('status')['commands'], 0)

    def test_node_and_depth_limits_remain_incomplete_not_absent(self):
        self.command('expand')
        for args, reason in (({'max_elements': 1}, 'max_elements'), ({'max_depth': 1}, 'max_depth')):
            data = self.observe(**args)
            for source in menu.SOURCES:
                observed = data['sources'][source]
                self.assertEqual(observed['status'], 'partial', observed)
                self.assertFalse(observed['complete'])
                self.assertTrue(observed['truncated'])
                self.assertIn(reason, observed['limit_reasons'])

    def test_disabled_parent_and_incorrect_native_identity_never_send(self):
        initial = self.observe()
        disabled = self.named(initial, 'uia_raw', 'Disabled command')
        self.assertEqual(disabled['supported_actions'], [])
        self.assertEqual(self.act(disabled, 'invoke')['delivery'], 'not_sent')
        parent = copy.deepcopy(self.named(initial, 'uia_raw', 'Export to excel'))
        parent['evidence']['target']['process_start_ticks'] = '0'
        result = self.act(parent, 'expand')
        self.assertEqual(result['delivery'], 'not_sent')
        self.assertFalse(self.command('status')['expanded'])

    def test_destroyed_hwnd_cannot_be_reused_for_input(self):
        parent = self.named(self.observe(), 'uia_raw', 'Export to excel')
        self.process.stdin.write('quit\n')
        self.process.stdin.flush()
        self.process.wait(timeout=3)
        result = self.act(parent, 'expand')
        self.assertEqual(result['delivery'], 'not_sent')
        self.assertEqual(result['diagnostic']['code'], 'menu_accessibility_target_changed')


if __name__ == '__main__':
    unittest.main()
