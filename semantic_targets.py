"""Re-resolve a recorded label relationship in each fresh, scoped observation.

No coordinates, element indices or numeric provider handles are persisted.
Rendered relationships are explicit (not UIA LabeledBy) and are accepted only
from the fully reconciled Driver tree. Geometry-only hints remain read-only.
"""
from __future__ import annotations


def labels_for(snapshot, element):
    from operations import _name
    from inspection import _protected
    elements = snapshot.get('elements', [])
    if (_protected(element) or element.get('synthetic_ancestor')
            or element.get('role') not in {'Edit', 'TextBox', 'ComboBox', 'Document', 'CheckBox', 'RadioButton'}):
        return []
    by_index = {e.get('element_index'): e for e in elements}
    names = []
    for key in ('labeled_by', 'labelled_by'):
        index = element.get(key)
        if isinstance(index, dict): index = index.get('element_index')
        label = by_index.get(index) if type(index) is int else None
        if label and not _protected(label) and label.get('role') in {'Text', 'Label', 'StaticText'}:
            names.append((_name(label), 'uia_labeled_by'))
    if snapshot.get('accessibility_normalization', {}).get('status') in {'augmented', 'verified'}:
        for label in snapshot.get('read_only_labels', []):
            if (label.get('source') == 'driver.tree_markdown' and label.get('read_only') is True
                    and label.get('following_element_index') == element.get('element_index')
                    and label.get('parent_index') == element.get('parent_index')):
                names.append((_name(label), 'rendered_preceding'))
    names = list(dict.fromkeys(names))
    # Conflicting/multiple text is not silently reduced to the nearest label.
    if len({text for text, _ in names}) != 1: return []
    return [{'text': text, 'relation': relation} for text, relation in names
            if isinstance(text, str) and 0 < len(text.strip()) <= 200
            and text != element.get('value')]


def matches_label(snapshot, element, label):
    return label in labels_for(snapshot, element)


def recorded_label_selector(snapshot, element):
    from operations import _unique, OperationError, _name
    from inspection import _protected
    labels = labels_for(snapshot, element)
    if not labels: return None
    by_index = {e.get('element_index'): e for e in snapshot['elements']}
    parent, seen, scopes = element.get('parent_index'), set(), []
    while type(parent) is int and parent not in seen and parent in by_index:
        seen.add(parent)
        node = by_index[parent]
        if _protected(node): return None
        name = _name(node)
        # Provider-inherited Pane/Window titles are not field groups.
        if node.get('role') in {'Group', 'ToolBar', 'TabItem', 'Form', 'Custom'} and name:
            scopes.append({'name': name, 'role': node['role']})
        parent = node.get('parent_index')
    for label in labels:
        for scope in scopes + ([None] if label['relation'] == 'uia_labeled_by' else []):
            candidate = {'role': element['role'], 'label': label}
            if scope: candidate['within'] = scope
            try:
                if _unique(snapshot, candidate) is element: return candidate
            except OperationError:
                pass
    return None
