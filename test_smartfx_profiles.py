"""Host-specific policy consumes live evidence, never static business selectors."""
import copy
import unittest
from types import SimpleNamespace

from inspection import inspect_window
from interaction import InteractionEngine
from local_delivery import filter_response
from operations import OperationError
from smartfx_profiles import ObservedStrategy, host_profile
from test_inspection import Runtime

PNT = r'C:\approved\SKhynix.SmartFX.PNTLauncher.exe'
MENU = r'C:\approved\SKhynix.SmartFX.ClientFX.MenuAgentHost.4.0.exe'
TARGET = {'pid': 11, 'window_id': 22}


def elements():
    return [
        {'element_index': 0, 'role': 'Window', 'name': '업무 화면'},
        {'element_index': 1, 'parent_index': 0, 'role': 'ToolBar', 'automation_id': 'tsViewCommand'},
        {'element_index': 2, 'parent_index': 0, 'role': 'Pane', 'automation_id': 'psView'},
        {'element_index': 3, 'parent_index': 1, 'role': 'Button', 'name': '조회', 'automation_id': '777', 'actions': ['invoke']},
        {'element_index': 4, 'parent_index': 2, 'role': 'Button', 'name': '조회', 'automation_id': '888', 'actions': ['invoke']},
    ]


class ProfileTests(unittest.TestCase):
    def inspect(self, exe, nodes=None):
        runtime = Runtime(elements() if nodes is None else nodes)
        result = inspect_window(runtime, TARGET, observation='uia', host_executable=exe)
        self.assertEqual([name for name, _ in runtime.calls], ['get_window_state'])
        return result['structuredContent']['inspection']

    def test_exact_executable_only_never_caption_or_dll_name(self):
        for exe in (r'C:\SmartFX\other.exe', 'PNTLauncher.exe', 'fake-SKhynix.SmartFX.PNTLauncher.exe', None):
            self.assertIsNone(host_profile(exe))
            self.assertNotIn('application_strategy', self.inspect(exe))
        self.assertEqual(host_profile(PNT.upper())['id'], 'smartfx_hosted')
        self.assertEqual(host_profile(MENU)['id'], 'fxmenuagent')

    def test_both_hosts_choose_proven_scopes_over_transient_ids(self):
        for exe in (PNT, MENU):
            data = self.inspect(exe)
            buttons = [c for c in data['controls'] if c['role'] == 'Button']
            self.assertEqual([c['selector'] for c in buttons], [
                {'name': '조회', 'role': 'Button', 'within': {'automation_id': 'tsViewCommand'}},
                {'name': '조회', 'role': 'Button', 'within': {'automation_id': 'psView'}}])
            self.assertEqual([c['application_context']['areas'][0] for c in buttons], ['command_toolbar', 'business_content'])
            self.assertFalse(data['application_strategy']['authorizes_input'])
            self.assertEqual(data['application_strategy']['extra_tree_reads'], 0)

    def test_observed_types_have_policies_not_invented_capabilities(self):
        nodes = [
            {'element_index': 1, 'role': 'Custom', 'automation_id': 'actual-combo', 'class_name': 'Vendor.UltraCombo'},
            {'element_index': 2, 'role': 'Custom', 'automation_id': 'actual-grid', 'class_name': 'FpSpread'},
            {'element_index': 3, 'role': 'Custom', 'automation_id': 'actual-property', 'class_name': 'MesPropertyGrid'},
        ]
        data = self.inspect(PNT, nodes)
        self.assertEqual(data['application_strategy']['observed_regions'], {'grid_combo': 1, 'result_grid': 1, 'property_editor': 1})
        self.assertTrue(all(c['suggested_operations'] == ['assert'] for c in data['controls']))
        self.assertIn('search_modal_is_separate', data['controls'][0]['application_context']['policy'])
        self.assertIn('reobserve_after_editor_activation', data['controls'][2]['application_context']['policy'])
        self.assertNotIn('FAMILY_CD', str(data))

    def test_static_fields_and_localization_keys_are_not_identities(self):
        data = self.inspect(PNT, [{'role': 'Pane', 'name': 'gcboFamily @FAMILYCD WindowsFormsHost'}])
        self.assertEqual(data['application_strategy']['structure_status'], 'not_exposed')
        self.assertNotIn('application_context', data['controls'][0])

    def test_ambiguous_scope_cannot_promote_selector(self):
        nodes = elements()
        nodes += [{'element_index': 5, 'role': 'Pane', 'automation_id': 'psView'},
                  {'element_index': 6, 'parent_index': 5, 'role': 'Button', 'name': '조회', 'actions': ['invoke']}]
        strategy = ObservedStrategy(host_profile(MENU), {'elements': nodes})
        self.assertIsNone(strategy.selector(nodes[4]))

    def test_indexed_scope_resolution_agrees_with_common_selector_contract(self):
        from operations import _unique
        for duplicate in (False, True):
            nodes = elements()
            for index in range(5, 85):
                nodes.append({'element_index': index, 'parent_index': 1 if index % 2 else 2,
                              'role': 'Button', 'name': 'action-' + str(index % 8 if duplicate else index),
                              'automation_id': str(10000 + index), 'actions': ['invoke']})
            snapshot = {'elements': nodes}
            strategy = ObservedStrategy(host_profile(MENU), snapshot)
            for node in nodes:
                selector = strategy.selector(node)
                if selector is not None:
                    self.assertIs(_unique(snapshot, selector), node)
            if duplicate:
                self.assertTrue(all(strategy.selector(node) is None for node in nodes[5:]))

    def test_synthetic_protected_and_truncated_evidence_never_promotes(self):
        for change in ({'synthetic_ancestor': True}, {'protected': True}):
            nodes = elements(); nodes[2].update(change)
            strategy = ObservedStrategy(host_profile(MENU), {'elements': nodes})
            self.assertIsNone(strategy.selector(nodes[4]))
        nodes = elements()
        strategy = ObservedStrategy(host_profile(MENU), {'elements': nodes, 'truncated': True})
        self.assertIsNone(strategy.selector(nodes[4]))

    def test_empty_peer_is_reported_without_claiming_provider_root_cause(self):
        data = self.inspect(PNT, [{'element_index': 1, 'role': 'Window', 'name': 'CustomWindowAutomationPeer'}])
        diagnostic = data['application_strategy']['navigation_diagnostic']
        self.assertFalse(diagnostic['provider_cause_confirmed'])
        self.assertEqual(diagnostic['code'], 'peer_has_no_observed_children')
        self.assertEqual(data['application_strategy']['observed_regions'], {})

    def test_generic_program_keeps_existing_selector_and_call_count(self):
        data = self.inspect('other.exe')
        buttons = [c for c in data['controls'] if c['role'] == 'Button']
        self.assertEqual([c['selector'] for c in buttons], [{'automation_id': '777'}, {'automation_id': '888'}])
        self.assertTrue(all('application_context' not in c for c in buttons))

    def test_local_delivery_never_exposes_profile_or_business_context(self):
        response = {'structuredContent': {'status': 'connected', 'application_profile': host_profile(PNT),
            'inspection': self.inspect(MENU)}, 'content': []}
        result = filter_response(response)
        self.assertEqual(result['structuredContent']['status'], 'connected')
        self.assertNotIn('SmartFX', str(result)); self.assertNotIn('조회', str(result))
        self.assertNotIn('application_profile', str(result))

    def test_real_facade_uses_bound_identity_and_rejects_replacement(self):
        runtime = Runtime(elements())
        state = {'process_exited': False, 'target_present': True, 'creation_time': 1, 'executable': MENU,
                 'windows': [{**TARGET, 'class_name': 'WindowsForms10.Window', 'thread_id': 2, 'owner_window_id': 0}]}
        probe = SimpleNamespace(capture=lambda: copy.deepcopy(state), snapshot=lambda: copy.deepcopy(state), close=lambda: None)
        runtime.create_transition_probe = lambda target: probe
        engine = InteractionEngine(runtime)
        binding = engine.bind(TARGET)
        self.assertEqual(binding['application_profile']['id'], 'fxmenuagent')
        answer = engine.observe(binding['window_ref'], observation='uia')['structuredContent']
        self.assertEqual(answer['inspection']['application_strategy']['id'], 'fxmenuagent')
        targets = [c for c in answer['inspection']['controls'] if c.get('name') == '조회']
        self.assertTrue(all(c['target_ref'] for c in targets))
        state['executable'] = PNT
        with self.assertRaises(OperationError):
            engine.observe(binding['window_ref'], observation='uia')
        self.assertEqual(len(runtime.calls), 1)
        engine.close()


if __name__ == '__main__':
    unittest.main()
