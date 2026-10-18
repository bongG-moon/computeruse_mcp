"""Presentation-only context for duplicate recorded fields; no desktop access."""
import copy
import unittest

from accessibility_tree import normalize_snapshot
from inspection import control_display_label
from process_editor import ProcessDraft
import test_accessibility_tree as tree_fixtures
import test_process_editor as recording_fixtures


class DisplayLabelTests(unittest.TestCase):
    def projected(self):
        return normalize_snapshot(tree_fixtures.AccessibilityTreeTests.projected_business())

    def test_unique_rendered_context_labels_duplicate_inputs_without_mutating_snapshot(self):
        snapshot = self.projected()
        before = copy.deepcopy(snapshot)
        fields = [e for e in snapshot['elements'] if e['role'] == 'Edit']
        self.assertEqual(control_display_label(snapshot, fields[0], '[Editor] Edit Area'),
                         'Device (주변 글자 기준) · 조회 조건')
        self.assertEqual(control_display_label(snapshot, fields[1], '[Editor] Edit Area'),
                         'Device (주변 글자 기준) · 보관 조건')
        self.assertEqual(snapshot, before)

    def test_multiple_preceding_labels_and_unvalidated_context_do_not_rename(self):
        source = tree_fixtures.AccessibilityTreeTests.projected_business()
        source['tree_markdown'] = source['tree_markdown'].replace('      - Text "Device"',
            '      - Text "Device"\n      - Text "Family"', 1)
        snapshot = normalize_snapshot(source)
        self.assertEqual(control_display_label(snapshot, snapshot['elements'][0], 'unchanged'), 'unchanged')
        snapshot = self.projected()
        snapshot['accessibility_normalization']['status'] = 'rejected'
        self.assertEqual(control_display_label(snapshot, snapshot['elements'][0], 'unchanged'), 'unchanged')

    def test_noisy_inherited_window_pane_or_long_group_never_precedes_field_label(self):
        noise = '합성 시험 화면입니다. 특정 사내 앱 또는 상용 컨트롤의 정밀 재현을 뜻하지 않습니다.'
        for role in ('Window', 'Pane', 'Group'):
            snapshot = self.projected()
            field = snapshot['elements'][0]
            old_parent = field['parent_index']
            snapshot['elements'].append({'element_index': 500, 'parent_index': old_parent,
                'role': role, 'name': noise, 'actions': []})
            field['parent_index'] = 500
            for label in snapshot['read_only_labels']:
                if label.get('following_element_index') == field['element_index']:
                    label['parent_index'] = 500
            with self.subTest(role=role):
                display = control_display_label(snapshot, field, 'unchanged')
                self.assertEqual(display, 'Device (주변 글자 기준) · 조회 조건')
                self.assertNotIn(noise, display)
                self.assertLess(len(display), 45)

    def test_protected_document_entered_value_and_uniquely_named_fields_do_not_rename(self):
        for patch in ({'protected': True}, {'role': 'Document'}, {'value': 'Device'}, {'label': 'Unique Device'}):
            snapshot = self.projected()
            field = snapshot['elements'][0]
            field.update(patch)
            with self.subTest(patch=patch):
                self.assertEqual(control_display_label(snapshot, field, 'unchanged'), 'unchanged')
        snapshot = self.projected()
        field = snapshot['elements'][0]
        parent = next(e for e in snapshot['elements'] if e['element_index'] == field['parent_index'])
        parent['is_protected'] = True
        self.assertEqual(control_display_label(snapshot, field, 'unchanged'), 'unchanged')

    def test_multiple_nearby_geometry_labels_do_not_rename(self):
        fields = [
            {'element_index': 0, 'role': 'Group', 'name': 'Conditions'},
            {'element_index': 1, 'parent_index': 0, 'role': 'Edit', 'name': 'Editor',
             'frame': {'x': 100, 'y': 50, 'w': 100, 'h': 20}},
            {'element_index': 2, 'parent_index': 0, 'role': 'Edit', 'name': 'Editor'},
            {'element_index': 3, 'parent_index': 0, 'role': 'Text', 'name': 'Device',
             'frame': {'x': 50, 'y': 50, 'w': 45, 'h': 20}},
            {'element_index': 4, 'parent_index': 0, 'role': 'Text', 'name': 'Family',
             'frame': {'x': 100, 'y': 20, 'w': 100, 'h': 20}}]
        snapshot = {'elements': fields}
        self.assertEqual(control_display_label(snapshot, fields[1], 'unchanged'), 'unchanged')
        fields.pop()
        self.assertEqual(control_display_label(snapshot, fields[1], 'unchanged'),
                         'Device (주변 글자 기준) · Conditions')

    def test_verified_label_relationship_replaces_transient_numeric_id(self):
        source = recording_fixtures.RecordingSemanticTests(); source.setUp()
        event, snapshots = source.event(value='REC-A')
        snapshot = snapshots[('editor', 'main')]
        field = snapshot['elements'][0]
        field.update(name='[Editor] Edit Area', automation_id='95999070', parent_index=0)
        event['native_target']['element'].update(name='[Editor] Edit Area', automation_id='95999070')
        snapshot['elements'].extend([
            {'element_index': 0, 'role': 'Group', 'name': '조회 조건', 'actions': []},
            {**field, 'element_index': 2, 'automation_id': '371646172', 'value': '5'}])
        without_context = copy.deepcopy(snapshot)
        snapshot.update(accessibility_normalization={'status': 'augmented'}, read_only_labels=[
            {'element_index': -1, 'parent_index': 0, 'role': 'Text', 'label': 'Device', 'read_only': True,
             'source': 'driver.tree_markdown', 'following_element_index': 1}])
        baseline = ProcessDraft([source.program])
        baseline.recorded([event], snapshots={('editor', 'main'): without_context})
        source.draft.recorded([event], snapshots=snapshots)
        actual, previous = copy.deepcopy(source.draft.steps[0]), copy.deepcopy(baseline.steps[0])
        actual.pop('step_id', None); previous.pop('step_id', None)
        self.assertEqual(previous['operation'], 'manual_entry')
        self.assertEqual(actual['operation'], 'set_value')
        self.assertEqual(source.draft.labels, ['Device (주변 글자 기준) · 조회 조건'])
        self.assertNotIn('automation_id', actual['selector'])
        self.assertEqual(actual['selector']['label'], {'text':'Device','relation':'rendered_preceding'})
        self.assertEqual(actual['selector'], actual['expect'][0]['selector'])
        from operations import _unique
        fresh = copy.deepcopy(snapshot)
        for item in fresh['elements']:
            if 'automation_id' in item: item['automation_id'] = 'new-' + item['automation_id']
        self.assertEqual(_unique(fresh, actual['selector'])['element_index'], 1)
        summary = source.draft.summaries()[0]
        self.assertNotIn('95999070', summary['summary'])
        self.assertIn('확인: Device (주변 글자 기준)', summary['detail'])
        source.draft.steps[0]['expect'][0]['selector'] = {'automation_id': 'different-field'}
        summary = source.draft.summaries()[0]
        self.assertIn('확인: different-field', summary['detail'])


if __name__ == '__main__':
    unittest.main()
