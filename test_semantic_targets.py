import copy
import unittest

from learning import stable_selector, LearningError
from operations import _unique, validate_selector, OperationError


def snapshot(group='Conditions', label='Family', identity='123'):
    return {'accessibility_normalization': {'status': 'verified'}, 'elements': [
        {'element_index': 0, 'role': 'Group', 'name': group},
        {'element_index': 1, 'parent_index': 0, 'role': 'ComboBox', 'name': 'Editor',
         'automation_id': identity, 'actions': ['set_value'], 'value': 'W'}],
        'read_only_labels': [{'element_index': -1, 'parent_index': 0, 'role': 'Text', 'label': label,
            'source': 'driver.tree_markdown', 'read_only': True, 'following_element_index': 1}]}


class SemanticTargetsTests(unittest.TestCase):
    def test_restart_with_new_ids_and_indices(self):
        old = snapshot()
        selector = stable_selector(old, old['elements'][1])
        self.assertNotIn('automation_id', selector)
        fresh = snapshot(identity='999')
        fresh['elements'][1]['element_index'] = 80
        fresh['read_only_labels'][0]['following_element_index'] = 80
        self.assertIs(_unique(fresh, validate_selector(selector)), fresh['elements'][1])

    def test_duplicate_label_does_not_choose_first(self):
        old = snapshot()
        selector = stable_selector(old, old['elements'][1])
        old['elements'].append({**old['elements'][1], 'element_index': 2})
        old['read_only_labels'].append({**old['read_only_labels'][0], 'element_index': -2, 'following_element_index': 2})
        with self.assertRaises(OperationError) as error: _unique(old, selector)
        self.assertEqual(error.exception.code, 'ambiguous_selector')

    def test_changed_label_and_rejected_projection_never_match(self):
        old = snapshot(); selector = stable_selector(old, old['elements'][1])
        for fresh in (snapshot(label='Other'), snapshot(group='Other')):
            with self.assertRaises(OperationError): _unique(fresh, selector)
        old['accessibility_normalization']['status'] = 'rejected'
        with self.assertRaises(OperationError): _unique(old, selector)

    def test_ambiguous_text_does_not_become_saved_numeric_fallback(self):
        old = snapshot()
        old['elements'].append({**old['elements'][1], 'element_index': 2, 'automation_id': '456'})
        old['read_only_labels'] = []
        with self.assertRaises(LearningError) as error: stable_selector(old, old['elements'][1])
        self.assertEqual(error.exception.code, 'unstable_control_identity')

    def test_provider_label_and_schema(self):
        old = snapshot(); old.pop('accessibility_normalization'); old['read_only_labels'] = []
        old['elements'].append({'element_index': 2, 'role': 'Text', 'name': 'Family'})
        old['elements'][1]['labeled_by'] = 2
        selector = stable_selector(old, old['elements'][1])
        self.assertEqual(selector['label']['relation'], 'uia_labeled_by')
        for bad in ({'label': selector['label']}, {'role': 'Edit','label': {'text': 'x','relation':'nearest'}},
                    {'role': 'Edit','label': {'text': 'x','relation':'rendered_preceding'}}):
            with self.assertRaises(OperationError): validate_selector(bad)

    def test_generic_names_and_changed_layout(self):
        for name in ('Account', 'Part number', 'Search phrase', '고객명'):
            old = snapshot(label=name)
            selector = stable_selector(old, old['elements'][1])
            fresh = snapshot(label=name, identity='888')
            fresh['elements'][1]['frame'] = {'x': -600, 'y': 200, 'w': 140, 'h': 25}
            self.assertIs(_unique(fresh, selector), fresh['elements'][1])

if __name__ == '__main__': unittest.main()
