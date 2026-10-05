"""Conservatively reconcile a Driver's UIA projection with its rendered tree.

Rendered text is data only. No values, actions or handles are manufactured from
it. A malformed or inconsistent tree leaves every original element unchanged.
"""
from __future__ import annotations

import copy
import json
import re


MAX_TREE_CHARS = 2_000_000
MAX_ROWS = 20_000
MAX_DEPTH = 100
ANCESTOR_ROLES = {"Window", "Group", "Pane", "List", "Tree", "Menu", "MenuBar", "ToolBar", "Tab", "Custom"}
KNOWN_ROLES = ANCESTOR_ROLES | {"Button", "Calendar", "CheckBox", "ComboBox", "DataGrid", "DataItem",
    "Document", "Edit", "Header", "HeaderItem", "Hyperlink", "Image", "ListItem", "MenuItem", "ProgressBar",
    "RadioButton", "ScrollBar", "Separator", "Slider", "Spinner", "SplitButton", "StatusBar", "TabItem",
    "Table", "Text", "TextBox", "Thumb", "TitleBar", "ToggleButton", "ToolTip", "TreeItem"}
ATOM = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
ROW = re.compile(r"( *)(?:- )(?:\[([0-9]+)\] )?([A-Za-z][A-Za-z0-9]{0,63})(.*)\Z")
ATTRIBUTE = re.compile(r"([a-z_]+)=")
ACTION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class _Reject(ValueError):
    pass


def _quoted(text):
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except (ValueError, RecursionError):
        raise _Reject("invalid_quoted_string") from None
    if not isinstance(value, str) or len(value) > 65536 or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise _Reject("invalid_quoted_string")
    return value, text[end:]


def _attributes(text):
    if not text.startswith("[") or not text.endswith("]"):
        raise _Reject("invalid_attributes")
    remaining, result = text[1:-1], {}
    if not remaining:
        raise _Reject("empty_attributes")
    while remaining:
        match = ATTRIBUTE.match(remaining)
        if not match:
            raise _Reject("invalid_attributes")
        key = match[1]
        if key not in {"value", "help", "id", "actions"} or key in result:
            raise _Reject("unsupported_or_duplicate_attribute")
        remaining = remaining[match.end():]
        if key in {"value", "help"}:
            if not remaining.startswith('"'):
                raise _Reject("invalid_quoted_string")
            value, remaining = _quoted(remaining)
        elif key == "actions":
            if not remaining.startswith("[") or "]" not in remaining:
                raise _Reject("invalid_actions")
            end = remaining.index("]")
            value = remaining[1:end].split(",") if end > 1 else []
            if any(not ACTION.fullmatch(action) for action in value) or len(value) != len(set(value)):
                raise _Reject("invalid_actions")
            remaining = remaining[end + 1:]
        else:
            value, separator, rest = remaining.partition(" ")
            if not ATOM.fullmatch(value):
                raise _Reject("invalid_id_atom")
            remaining = separator + rest
        result[key] = value
        if remaining:
            if not remaining.startswith(" ") or remaining.startswith("  "):
                raise _Reject("invalid_attribute_separator")
            remaining = remaining[1:]
            if not remaining:
                raise _Reject("trailing_attribute_separator")
    return result


def _parse(tree):
    if not isinstance(tree, str) or not tree or len(tree) > MAX_TREE_CHARS:
        raise _Reject("tree_size_or_type")
    lines = tree.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not 1 <= len(lines) <= MAX_ROWS:
        raise _Reject("row_limit")
    nodes, stack = [], []
    for line in lines:
        if line.endswith("\r"):
            line = line[:-1]
        match = ROW.fullmatch(line)
        if not match or len(match[1]) % 2:
            raise _Reject("invalid_row_or_indent")
        depth, index, role, tail = len(match[1]) // 2, match[2], match[3], match[4]
        if depth > MAX_DEPTH or depth > len(stack) or (nodes and depth == 0):
            raise _Reject("invalid_tree_structure")
        if not nodes and (depth != 0 or role not in ANCESTOR_ROLES):
            raise _Reject("invalid_root")
        if index is not None and (len(index) > 10 or (len(index) > 1 and index.startswith("0"))):
            raise _Reject("invalid_index")
        label, attrs = None, {}
        if tail:
            if not tail.startswith(" ") or tail.startswith("  "):
                raise _Reject("invalid_row_suffix")
            tail = tail[1:]
            if tail.startswith('"'):
                label, tail = _quoted(tail)
                if tail:
                    if not tail.startswith(" ") or tail.startswith("  "):
                        raise _Reject("invalid_row_suffix")
                    tail = tail[1:]
            if tail:
                attrs = _attributes(tail)
            elif label is None:
                raise _Reject("invalid_row_suffix")
        node = {"index": int(index) if index is not None else None, "role": role, "label": label,
                "depth": depth, "attrs": attrs, "parent": stack[depth - 1] if depth else None, "children": []}
        if index is None and role not in KNOWN_ROLES:
            raise _Reject("unknown_unindexed_role")
        if node["parent"] is not None:
            nodes[node["parent"]]["children"].append(len(nodes))
        stack[depth:] = [len(nodes)]
        nodes.append(node)
    return nodes


def _result(snapshot, status, reason=None, **counts):
    answer = dict(snapshot)
    answer["accessibility_normalization"] = {"status": status, **({"reason": reason} if reason else {}), **counts}
    return answer


def normalize_snapshot(snapshot):
    """Enrich only after the entire rendered tree agrees with structured rows.

    Synthetic ancestors carry negative indices, read_only and no action/handle.
    Existing parent links may skip omitted nodes but must identify the nearest
    *indexed* ancestor in the same rendered path. This refines, never contradicts,
    the Driver's projected parent relationship.
    """
    if not isinstance(snapshot, dict) or "elements" not in snapshot:
        return snapshot
    if "tree_markdown" not in snapshot:
        return _result(snapshot, "not_applicable", "missing_tree")
    elements = snapshot["elements"]
    try:
        if not isinstance(elements, list) or not 1 <= len(elements) <= MAX_ROWS:
            raise _Reject("element_size_or_type")
        indexed = {}
        for element in elements:
            if not isinstance(element, dict):
                raise _Reject("invalid_structured_element")
            index = element.get("element_index")
            if type(index) is not int or index < 0 or index in indexed:
                raise _Reject("invalid_structured_index")
            if type(element.get("depth")) is not int or not 0 <= element["depth"] <= MAX_DEPTH:
                raise _Reject("invalid_structured_depth")
            indexed[index] = element
        nodes = _parse(snapshot["tree_markdown"])
        covered, id_additions = set(), {}
        for node in nodes:
            index = node["index"]
            if index is None:
                if node["children"] and node["role"] not in ANCESTOR_ROLES:
                    raise _Reject("unsupported_unindexed_ancestor")
                if node["children"] and node["attrs"].get("actions"):
                    raise _Reject("actionable_unindexed_ancestor")
                continue
            if index not in indexed or index in covered:
                raise _Reject("unknown_or_duplicate_index")
            element = indexed[index]
            if element.get("role") != node["role"] or element["depth"] != node["depth"]:
                raise _Reject("structured_row_mismatch")
            if node["label"] is not None:
                labels = [element[key] for key in ("label", "name") if key in element]
                if not labels or any(label != node["label"] for label in labels):
                    raise _Reject("structured_label_mismatch")
            covered.add(index)
            parent = node["parent"]
            while parent is not None and nodes[parent]["index"] is None:
                parent = nodes[parent]["parent"]
            nearest_index = nodes[parent]["index"] if parent is not None else None
            existing_parent = element.get("parent_index")
            if existing_parent is not None and (type(existing_parent) is not int or existing_parent != nearest_index):
                raise _Reject("parent_conflict")
            automation_id = node["attrs"].get("id")
            if automation_id is not None:
                if element.get("automation_id") is not None and element["automation_id"] != automation_id:
                    raise _Reject("automation_id_conflict")
                if element.get("automation_id") is None:
                    id_additions[index] = automation_id
        if covered != set(indexed):
            raise _Reject("incomplete_index_coverage")
        if len(nodes) > MAX_ROWS:
            raise _Reject("row_limit")
        answer = copy.deepcopy(snapshot)
        output = {element["element_index"]: element for element in answer["elements"]}
        synthetic, identities = [], {}
        for offset, node in enumerate(nodes):
            if node["index"] is not None:
                identities[offset] = node["index"]
            elif node["children"]:
                index = -1 - len(synthetic)
                identities[offset] = index
                ancestor = {"element_index": index, "role": node["role"], "depth": node["depth"],
                            "actions": [], "read_only": True, "synthetic_ancestor": True}
                if node["label"] is not None:
                    ancestor["label"] = node["label"]
                if "id" in node["attrs"]:
                    ancestor["automation_id"] = node["attrs"]["id"]
                synthetic.append(ancestor)
                output[index] = ancestor
        links = 0
        for offset, node in enumerate(nodes):
            if offset not in identities:
                continue
            element = output[identities[offset]]
            if node["parent"] is not None:
                parent = identities[node["parent"]]
                if element.get("parent_index") != parent:
                    links += 1
                element["parent_index"] = parent
            if node["index"] in id_additions:
                element["automation_id"] = id_additions[node["index"]]
        answer["elements"].extend(synthetic)
        return _result(answer, "augmented" if synthetic or links or id_additions else "verified",
                       original_element_count=len(elements), synthetic_ancestor_count=len(synthetic),
                       parent_links_added=links, automation_ids_added=len(id_additions))
    except _Reject as error:
        return _result(snapshot, "rejected", str(error))
