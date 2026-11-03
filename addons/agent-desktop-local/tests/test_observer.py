import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import observe
import local_core
from recognition import derive


def sample(children=None):
    return {'ok': True, 'data': {'tree': {'role': 'window', 'name': 'PRIVATE TITLE',
        'children': children or []}}}


class ObserverTests(unittest.TestCase):
    def test_only_exact_read_commands(self):
        observe.validate_command(['list-windows', '--app', 'App.exe', '--timeout-ms', '10000'])
        valid = ['snapshot', '--window-id', 'w-12', '--surface', 'window', '--max-depth',
                 '12', '--include-bounds', '--timeout-ms', '10000']
        observe.validate_command(valid)
        process_wide_menu = valid.copy()
        process_wide_menu[4] = 'menu'
        for command in (['click', '@x'], valid + ['--headed'], ['clipboard-get'],
                        ['screenshot'], ['batch', 'private.json'], ['snapshot', '--app', 'App.exe'], process_wide_menu):
            with self.subTest(command=command), self.assertRaises(local_core.EngineError):
                observe.validate_command(command)

    def test_executable_is_not_a_path_or_pattern(self):
        for value in ('*App.exe', '../App.exe', r'C:\App.exe', '--headed.exe', ' App.exe', 'App.exe\n'):
            with self.subTest(value=value), self.assertRaises(local_core.EngineError):
                observe.exe_name(value)

    def test_bad_arguments_never_echo_private_content(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = observe.main(['observe', '--exe-name', 'PRIVATE CLIENT.exe', '--secret', 'PRIVATE VALUE'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'blocked')
        self.assertNotIn('PRIVATE', stdout.getvalue() + stderr.getvalue())

    def test_exception_never_echoes_provider_or_path(self):
        stream = io.StringIO()
        with patch.object(observe, 'run_inventory', side_effect=RuntimeError('PRIVATE VALUE')), contextlib.redirect_stdout(stream):
            self.assertEqual(observe.main(['inventory', '--exe-name', 'App.exe', '--output', 'PRIVATE PATH']), 2)
        self.assertNotIn('PRIVATE', stream.getvalue())

    def test_user_interrupt_has_safe_response_without_traceback(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(observe, 'run_inventory', side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = observe.main(['inventory', '--exe-name', 'PRIVATE.exe', '--output', 'PRIVATE PATH'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'interrupted')
        self.assertEqual(stderr.getvalue(), '')
        self.assertNotIn('PRIVATE', stdout.getvalue())

    def test_binary_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'agent-desktop.exe'
            binary.write_bytes(b'tampered')
            with patch.object(observe, 'BINARY', binary), patch.object(observe, 'validate_local_directory', return_value=Path(folder)):
                with self.assertRaisesRegex(local_core.EngineError, 'binary_hash_mismatch'):
                    observe.verify_binary()

    def test_empty_and_error_responses_not_observed(self):
        for raw in ({}, {'ok': False, 'data': {'tree': {'role': 'window'}}}, {'ok': True, 'data': {'tree': {}}}):
            with self.subTest(raw=raw):
                self.assertNotEqual(derive(raw)['status'], 'observed')

    def test_values_selection_and_hierarchy_preserved_without_code_guess(self):
        result = derive(sample([{'role': 'combobox', 'name': 'Family', 'value': 'Hybrid',
            'children': [{'role': 'option', 'name': 'Hybrid', 'selected': True},
                         {'role': 'option', 'name': 'Normal', 'selected': False}]}]), 'combo')
        self.assertEqual(result['node_count'], 4)
        self.assertEqual(result['nodes'][2]['parent_path'], '0/0')
        self.assertEqual(result['selected_nodes'], 1)
        self.assertEqual(result['business_code_mapping'], 'unproven')
        self.assertFalse(result['pattern_metadata_exposed'])
        self.assertFalse(result['recognition_validated'])

    def test_duplicate_menu_names_keep_distinct_paths(self):
        result = derive(sample([{'role': 'menuitem', 'name': 'Export'}, {'role': 'menuitem', 'name': 'Export'}]), 'menu')
        self.assertEqual(result['duplicate_signature_groups'], 1)
        self.assertNotEqual(result['nodes'][1]['path'], result['nodes'][2]['path'])

    def test_contradictory_snapshot_target_is_rejected(self):
        for metadata in ({'window': {'id':'w-99'}}, {'pid':12}, {'app':'Other.exe'}, {'identity_unproven':True}):
            raw = sample()
            raw['data'].update(metadata)
            with self.subTest(metadata=metadata), self.assertRaises(local_core.EngineError):
                observe.validate_snapshot_identity(raw, 11, 22, 'App.exe')

    def test_truncated_tree_never_complete(self):
        raw = sample([{'role':'table', 'subtree_truncated':True}])
        raw['data']['complete'] = True
        self.assertFalse(derive(raw)['complete'])

    def test_depth_limited_children_are_not_empty_peer_candidates(self):
        raw = sample([{'role':'group', 'children_count':4, 'subtree_truncated':True}])
        result = derive(raw)
        self.assertNotIn('0/0', result['empty_container_candidates'])
        self.assertEqual(result['nodes'][1]['reported_children_count'], 4)

    def test_foreground_change_is_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / ('a' * 32)
            run.mkdir()
            args = SimpleNamespace(exe_name='App.exe', output=folder)
            with patch.multiple(observe, new_run=lambda _: run, inventory=lambda *a: [],
                                validate_local_directory=lambda p: Path(p)), \
                 patch.object(observe, 'foreground', side_effect=[10, 20]):
                result = observe.run_inventory(args)
            self.assertEqual(result['status'], 'interrupted')
            self.assertFalse(result['foreground_unchanged'])

    def test_unlisted_popup_does_not_invent_window_id_and_parent_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / ('a' * 32)
            run.mkdir()
            args = SimpleNamespace(exe_name='App.exe', output=folder, pid=11, hwnd=22,
                                   stage='combo', parent_hwnd=33, related_hwnd=[])
            identity = lambda pid, hwnd, image: (pid, hwnd, image, 1, 2)
            rows = [{'pid': 11, 'hwnd': 33, 'window_id': 'w-33', 'identity': identity(11, 33, 'App.exe')}]
            with patch.multiple(observe, process_path=lambda _: 'App.exe', _identity=identity,
                                new_run=lambda _: run, foreground=lambda: 77,
                                inventory=lambda *a: rows, validate_local_directory=lambda p: Path(p)), \
                 patch.object(observe, '_invoke', return_value=sample()) as invoke:
                receipt = observe.run_observe(args)
            self.assertEqual(receipt['status'], 'partial')
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(invoke.call_args.args[0][2], 'w-33')
            self.assertNotIn('PRIVATE', json.dumps(receipt))
            scopes = json.loads((run / 'observation.json').read_text())['scopes']
            self.assertEqual([s['scope'] for s in scopes], ['target', 'parent'])
            self.assertEqual(scopes[0]['status'], 'unavailable')

    def test_wrong_executable_blocks_before_creating_output(self):
        args = SimpleNamespace(exe_name='App.exe', output='unused', pid=11, hwnd=22,
                               stage='window', parent_hwnd=None, related_hwnd=[])
        with patch.object(observe, 'process_path', return_value='Other.exe'), patch.object(observe, 'new_run') as create:
            with self.assertRaises(local_core.EngineError):
                observe.run_observe(args)
            create.assert_not_called()

    def test_pid_lifetime_change_stops_snapshot(self):
        args = SimpleNamespace(exe_name='App.exe', output='unused', pid=11, hwnd=22,
                               stage='window', parent_hwnd=None, related_hwnd=[])
        with patch.multiple(observe, process_path=lambda _: 'App.exe', new_run=lambda _: Path('a'*32),
                            foreground=lambda: 9, inventory=lambda *a: [], _write=lambda *a: None), \
             patch.object(observe, '_identity', side_effect=[(11, 22, 'App.exe', 1), (11, 22, 'App.exe', 2)]), \
             patch.object(observe, '_invoke') as invoke:
            with self.assertRaisesRegex(local_core.EngineError, 'target_identity_changed'):
                observe.run_observe(args)
            invoke.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows path semantics')
    def test_unc_and_missing_output_fail(self):
        for path in (r'\\server\share\private', r'Z:\NoSuchLocalObservationDirectory'):
            with self.subTest(path=path), self.assertRaises(local_core.EngineError):
                local_core.validate_local_directory(path)

    @unittest.skipUnless(os.name == 'nt', 'Windows environment')
    def test_child_environment_drops_keys_proxies_and_debug(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'OPENAI_API_KEY': 'PRIVATE', 'HTTP_PROXY': 'https://private', 'AGENT_DESKTOP_DEBUG': '1'}):
            env = observe.child_environment(Path(folder))
        self.assertFalse(any(k in env for k in ('OPENAI_API_KEY', 'HTTP_PROXY', 'AGENT_DESKTOP_DEBUG')))
        self.assertEqual(env['RUST_LOG'], 'warn')

    @unittest.skipUnless(os.name == 'nt', 'Windows environment')
    def test_remote_programdata_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'PROGRAMDATA':r'\\server\private'}):
            with self.assertRaises(local_core.EngineError):
                observe.child_environment(Path(folder))


if __name__ == '__main__':
    unittest.main()
