"""Host-specific policy consumes live evidence, never static business selectors."""
import copy
import unittest
from types import SimpleNamespace

from inspection import inspect_window
from interaction import InteractionEngine
from local_delivery import filter_response
from operations import OperationError
from smartfx_profiles import ObservedStrategy, host_profile, compare_accessibility_sources
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
        self.assertEqual(data['application_strategy']['observed_regions'], {'grid_combo': 1, 'data_grid': 1, 'property_editor': 1})
        self.assertTrue(all(c['suggested_operations'] == ['assert'] for c in data['controls']))
        self.assertIn('search_modal_is_separate', data['controls'][0]['application_context']['policy'])
        self.assertIn('reobserve_after_editor_activation', data['controls'][2]['application_context']['policy'])
        self.assertNotIn('FAMILY_CD', str(data))

    def test_four_dropdown_structures_are_distinct_readonly_observation_plans(self):
        nodes = [
            {'element_index': 1, 'role': 'Custom', 'class_name': 'FXGridCombo'},
            {'element_index': 2, 'role': 'Window', 'class_name': 'FrmSearchList'},
            {'element_index': 3, 'role': 'Custom', 'class_name': 'FXCheckedComboBox'},
            {'element_index': 4, 'role': 'Pane', 'class_name': 'MesPropertyGrid'},
            {'element_index': 5, 'parent_index': 4, 'role': 'Custom', 'class_name': 'UltraGrid'},
        ]
        strategy = ObservedStrategy(host_profile(PNT), {'elements': nodes})
        contexts = [strategy.context(node) for node in (nodes[0], nodes[1], nodes[2], nodes[4])]
        self.assertEqual([row['control_family'] for row in contexts], [
            'tabular_query_combo', 'combo_search_modal', 'dynamic_cell_editor', 'dynamic_property_editor'])
        self.assertIn('require_origin_combo_relation', contexts[1]['policy'])
        self.assertIn('never_toggle_blindly', contexts[2]['policy'])
        self.assertIn('verify_value_on_original_property', contexts[3]['policy'])
        self.assertTrue(all(row['authorizes_input'] is False for row in contexts))
        unresolved = ObservedStrategy(host_profile(PNT), {'elements': [nodes[4]]}).context(nodes[4])
        self.assertEqual(unresolved['control_family'], 'unresolved_grid_editor_or_list')

    def test_grid_class_does_not_mean_result_grid_or_require_row_key_for_sheet_work(self):
        nodes = [{'element_index': i, 'role': 'Custom', 'class_name': 'FXGrid', 'automation_id': 'grid' + str(i)} for i in (1, 2)]
        strategy = ObservedStrategy(host_profile(MENU), {'elements': nodes})
        for node in nodes:
            context = strategy.context(node)
            self.assertEqual(context['grid_purpose'], 'requires_business_scope')
            self.assertIn('sheet_export_requires_active_sheet_and_selection_scope', context['policy'])
            self.assertIn('row_operation_requires_row_key_and_column', context['policy'])
            self.assertNotIn('require_unique_row_key_and_column', context['policy'])

    def test_host_and_nested_business_toolbars_are_not_interchangeable(self):
        nodes = elements() + [
            {'element_index': 5, 'parent_index': 2, 'role': 'ToolBar', 'automation_id': 'innerCommands'},
            {'element_index': 6, 'parent_index': 5, 'role': 'Button', 'name': '조회'},
            {'element_index': 7, 'role': 'ToolBar'},
        ]
        strategy = ObservedStrategy(host_profile(MENU), {'elements': nodes})
        self.assertEqual(strategy.context(nodes[3])['command_scope'], 'host_toolbar')
        self.assertEqual(strategy.context(nodes[6])['command_scope'], 'business_content')
        self.assertEqual(strategy.context(nodes[7])['command_scope'], 'unresolved_toolbar')

    def test_popup_named_business_class_and_host_boundary_do_not_prove_owner(self):
        nodes = [{'element_index': 1, 'role': 'Window', 'class_name': 'FxViewHostPopupForm'},
                 {'element_index': 2, 'parent_index': 1, 'role': 'Pane', 'class_name': 'ElementHost'}]
        context = ObservedStrategy(host_profile(MENU), {'elements': nodes}).context(nodes[1])
        self.assertIn('window_class_does_not_prove_modal_or_owner_relation', context['policy'])
        self.assertIn('reobserve_after_tab_dock_or_window_change', context['policy'])
        self.assertNotIn('owner_verified', context)


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



class SourceFindingTests(unittest.TestCase):
    def source(self, *, complete=True, status='ok', children=False):
        nodes = [{'node_id': 0, 'parent_id': None, 'children_status': 'enumerated' if children else 'provider_returned_zero',
                  'properties': {'name': {'status': 'observed', 'value': 'CustomWindowAutomationPeer'}, 'runtime_id': [1, 2]}}]
        if children:
            nodes.append({'node_id': 1, 'parent_id': 0, 'children_status': 'provider_returned_zero',
                          'properties': {'name': {'status': 'observed', 'value': 'private business label'}, 'runtime_id': [1, 3]}})
        return {'sources': {'uia_raw': {'status': status, 'complete': complete, 'elements': nodes}}}

    def driver(self, projection='returned_control'):
        return {'status': 'observed', 'nodes': [{'source': 'driver.elements', 'observation_ordinal': 0,
                 'projection': projection, 'metadata': {'runtime_id': {'value': [1, 2]}}}]}

    def test_complete_raw_zero_is_local_peer_empty_not_global_absence(self):
        result = compare_accessibility_sources(self.source(), self.driver())
        finding = result['findings'][0]
        self.assertEqual(finding['code'], 'provider_empty')
        self.assertTrue(finding['provider_empty_observed'])
        self.assertFalse(finding['all_application_accessibility_absent'])
        self.assertEqual(finding['scope'], 'this_peer_at_native_sample')
        self.assertEqual(result['extra_tree_reads'], 0)
        self.assertFalse(result['authorizes_input'])

    def test_partial_timeout_error_or_unknown_is_not_provider_empty(self):
        for status in ('partial', 'timeout', 'error', 'unavailable', 'not_observed'):
            result = compare_accessibility_sources(self.source(complete=False, status=status), self.driver())
            self.assertFalse(result['provider_raw_complete'])
            self.assertFalse(any(row['code'] == 'provider_empty' for row in result['findings']))
        result = compare_accessibility_sources({'status': 'timeout'}, self.driver())
        self.assertEqual(result['provider_raw_status'], 'timeout')
        self.assertEqual(result['findings'], [])

    def test_driver_to_mcp_filter_and_raw_to_driver_sample_gap_are_separate(self):
        result = compare_accessibility_sources(self.source(), self.driver('no_identity_or_actions'))
        excluded = next(row for row in result['findings'] if row['code'] == 'mcp_projection_excluded')
        self.assertEqual(excluded['reason'], 'no_identity_or_actions')
        gap = compare_accessibility_sources(self.source(children=True), self.driver())
        missing = next(row for row in gap['findings'] if row['code'] == 'provider_child_absent_from_driver_sample')
        self.assertFalse(missing['driver_filtering_confirmed'])
        self.assertFalse(gap['atomic'])
        self.assertNotIn('private business label', str(gap))

    def test_caption_handle_or_duplicate_runtime_cannot_match_readers(self):
        for mutation in ('missing', 'duplicate', 'synthetic', 'protected'):
            driver = self.driver()
            if mutation == 'missing':
                driver['nodes'][0]['metadata'] = {'native_window_handle': {'value': 123}}
                driver['nodes'][0]['name'] = 'CustomWindowAutomationPeer'
            elif mutation == 'duplicate': driver['nodes'].append(copy.deepcopy(driver['nodes'][0]))
            elif mutation == 'synthetic': driver['nodes'][0]['source'] = 'driver.tree_markdown_synthetic_ancestor'
            else: driver['nodes'][0]['protected'] = True
            result = compare_accessibility_sources(self.source(children=True), driver)
            self.assertFalse(any(row['code'] == 'provider_child_absent_from_driver_sample' for row in result['findings']))


if __name__ == '__main__':
    unittest.main()
