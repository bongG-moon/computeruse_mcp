"""Rendered hierarchy reconciliation is all-or-nothing and never invents input."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from accessibility_tree import normalize_snapshot


TREE = '''- Window "Native Fixture Detail - A"
  - [0] Edit "보조 입력" [value="보조 합성 입력" id=DetailInput actions=[set_value]]
  - Group "왼쪽 그룹"
    - [1] Edit "범위 입력" [id=ScopeLeft actions=[set_value]]
  - Group "오른쪽 그룹"
    - [2] Edit "범위 입력" [id=ScopeRight actions=[set_value]]
  - [3] Button "보조 확인" [actions=[invoke]]
  - [4] TitleBar [value="Native Fixture Detail - A" actions=[set_value]]
    - [5] Button "닫기" [actions=[invoke]]
'''


def observed():
    elements = []
    for index, (role, label, depth, actions) in enumerate([
        ("Edit", "보조 입력", 1, ["set_value"]), ("Edit", "범위 입력", 2, ["set_value"]),
        ("Edit", "범위 입력", 2, ["set_value"]), ("Button", "보조 확인", 1, ["invoke"]),
        ("TitleBar", "Native Fixture Detail - A", 1, ["set_value"]), ("Button", "닫기", 2, ["invoke"])]):
        elements.append({"element_index": index, "role": role, "label": label, "depth": depth,
                         "actions": actions, "element_token": "fresh:" + str(index), "enabled": True})
    elements[0]["value"] = "보조 합성 입력"
    elements[4]["value"] = "Native Fixture Detail - A"
    elements[5]["parent_index"] = 4
    return {"elements": elements, "tree_markdown": TREE, "elements_complete": False,
            "total_element_count": 6, "returned_element_count": 6, "snapshot_id": "fresh"}


class AccessibilityTreeTests(unittest.TestCase):
    def assert_rejected(self, snapshot):
        original = copy.deepcopy(snapshot)
        result = normalize_snapshot(snapshot)
        self.assertEqual(result["accessibility_normalization"]["status"], "rejected")
        self.assertEqual(result["elements"], original["elements"])
        self.assertEqual(result["tree_markdown"], original["tree_markdown"])
        self.assertEqual(snapshot, original)

    def test_captured_native_projection_gets_readonly_ancestors_and_automation_ids(self):
        source = observed()
        original = copy.deepcopy(source)
        result = normalize_snapshot(source)
        self.assertEqual(result["accessibility_normalization"]["status"], "augmented")
        self.assertEqual(source, original)
        by_index = {entry["element_index"]: entry for entry in result["elements"]}
        left, right = by_index[1], by_index[2]
        self.assertEqual(by_index[left["parent_index"]]["label"], "왼쪽 그룹")
        self.assertEqual(by_index[right["parent_index"]]["label"], "오른쪽 그룹")
        self.assertNotEqual(left["parent_index"], right["parent_index"])
        self.assertEqual(left["automation_id"], "ScopeLeft")
        self.assertEqual(right["automation_id"], "ScopeRight")
        for synthetic in [entry for entry in result["elements"] if entry.get("synthetic_ancestor")]:
            self.assertLess(synthetic["element_index"], 0)
            self.assertTrue(synthetic["read_only"])
            self.assertEqual(synthetic["actions"], [])
            self.assertNotIn("element_token", synthetic)
            self.assertNotIn("value", synthetic)
        for index, entry in enumerate(original["elements"]):
            for key in ("value", "actions", "element_token", "enabled"):
                self.assertEqual(by_index[index].get(key), entry.get(key))
        self.assertEqual(result["total_element_count"], 6)
        self.assertFalse(result["elements_complete"])

    def test_actual_failure_artifact_reproduces_parent_projection_gap_when_available(self):
        path = Path(__file__).parent / ".data/generic-validation/windows-051/failure-states.json"
        if not path.exists():
            self.skipTest("Developer-only live artifact is not shipped; static captured-shape test always runs")
        rows = json.loads(path.read_text(encoding="utf-8"))
        source = next(row["state"] for row in rows if row.get("window", {}).get("title") == "Native Fixture Detail - A")
        self.assertNotIn("parent_index", source["elements"][1])
        result = normalize_snapshot(source)
        self.assertEqual(result["accessibility_normalization"]["status"], "augmented")
        elements = {entry["element_index"]: entry for entry in result["elements"]}
        self.assertEqual(elements[elements[1]["parent_index"]]["label"], "왼쪽 그룹")
        self.assertEqual(elements[elements[2]["parent_index"]]["label"], "오른쪽 그룹")

    def test_scoped_operations_resolve_distinct_original_handles_after_normalization(self):
        from operations import _unique
        result = normalize_snapshot(observed())
        for name, token in (("왼쪽 그룹", "fresh:1"), ("오른쪽 그룹", "fresh:2")):
            element = _unique(result, {"name": "범위 입력", "role": "Edit", "within": {"name": name}})
            self.assertEqual(element["element_token"], token)
            self.assertGreaterEqual(element["element_index"], 0)

    def test_index_coverage_role_label_and_depth_must_all_match(self):
        changes = [lambda s: s.update(tree_markdown=TREE + '  - [1] Edit "범위 입력"\n'),
                   lambda s: s.update(tree_markdown=TREE.replace("[1]", "[99]")),
                   lambda s: s.update(tree_markdown="\n".join(line for line in TREE.splitlines() if "[1]" not in line)),
                   lambda s: s.update(tree_markdown=TREE.replace('[1] Edit "범위 입력"', '[1] Button "범위 입력"')),
                   lambda s: s.update(tree_markdown=TREE.replace('[1] Edit "범위 입력"', '[1] Edit "다른 입력"')),
                   lambda s: s["elements"][1].update(depth=1),
                   lambda s: s["elements"].append(copy.deepcopy(s["elements"][1])),
                   lambda s: s["elements"][1].update(element_index=True)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                source = observed(); change(source)
                self.assert_rejected(source)

    def test_malformed_ambiguous_or_truncated_tree_is_never_partially_applied(self):
        variants = [TREE.replace("    - [1]", "   - [1]"), TREE.replace("    - [1]", "\t- [1]"),
                    TREE.replace("  - Group", "      - Group", 1), TREE + "... truncated\n",
                    TREE + '- Window "another"\n', TREE.replace('Group "왼쪽 그룹"', 'Group "unterminated'),
                    TREE.replace('Group "왼쪽 그룹"', 'Group "bad\\escape"'),
                    TREE.replace('Group "왼쪽 그룹"', 'Group "왼쪽 그룹" instruction'),
                    TREE.replace('Group "왼쪽 그룹"', 'Text "왼쪽 그룹"'),
                    TREE.replace('Group "왼쪽 그룹"', 'UnknownRole "왼쪽 그룹"'),
                    TREE.replace('Group "왼쪽 그룹"', 'Group "왼쪽 그룹" [actions=[invoke]]'),
                    TREE.replace("[1]", "[01]"), TREE.replace("[1]", "[12345678901]"),
                    TREE.replace('id=ScopeLeft', 'id="ScopeLeft"'),
                    TREE.replace('id=ScopeLeft', 'id=ScopeLeft id=Other'),
                    TREE.replace('id=ScopeLeft', 'command=ScopeLeft'),
                    TREE.replace('actions=[invoke]', 'actions=[invoke,invoke]'),
                    TREE.replace('actions=[invoke]', 'actions=["invoke"]'),
                    TREE.replace("  - [0]", "\n  - [0]"), TREE.replace('Group "왼쪽 그룹"', 'Group "\\ud800"')]
        for index, tree in enumerate(variants):
            with self.subTest(index=index):
                source = observed(); source["tree_markdown"] = tree
                self.assert_rejected(source)

    def test_conflicting_parent_or_automation_id_rejects_every_enrichment(self):
        for change in (lambda s: s["elements"][1].update(parent_index=3),
                       lambda s: s["elements"][5].update(parent_index=True),
                       lambda s: s["elements"][1].update(automation_id="NotScopeLeft")):
            source = observed(); change(source)
            self.assert_rejected(source)

    def test_projected_parent_can_skip_only_unindexed_ancestors(self):
        source = observed()
        source["tree_markdown"] = TREE.replace('    - [5] Button "닫기"', '    - MenuBar "시스템"\n      - [5] Button "닫기"')
        source["elements"][5]["depth"] = 3
        result = normalize_snapshot(source)
        self.assertEqual(result["accessibility_normalization"]["status"], "augmented")
        elements = {entry["element_index"]: entry for entry in result["elements"]}
        menu = elements[elements[5]["parent_index"]]
        self.assertEqual(menu["role"], "MenuBar")
        self.assertEqual(menu["parent_index"], 4)

    def test_attribute_looking_text_is_never_parsed_as_an_id_or_action(self):
        source = observed()
        source["tree_markdown"] = TREE.replace('id=ScopeLeft actions=[set_value]',
                                               'value="id=FORGED actions=[invoke]" help="id=ALSOFORGED" actions=[invoke]')
        result = normalize_snapshot(source)
        self.assertEqual(result["accessibility_normalization"]["status"], "augmented")
        entry = next(entry for entry in result["elements"] if entry["element_index"] == 1)
        self.assertNotIn("automation_id", entry)
        self.assertNotIn("value", entry)
        self.assertEqual(entry["actions"], ["set_value"])

    def test_escaped_label_is_json_decoded_and_matches_original_exactly(self):
        source = observed()
        label = '범위 "인용" \\ 경로'
        source["elements"][1]["label"] = label
        source["tree_markdown"] = TREE.replace('[1] Edit "범위 입력"', '[1] Edit ' + json.dumps(label, ensure_ascii=False))
        result = normalize_snapshot(source)
        self.assertEqual(result["accessibility_normalization"]["status"], "augmented")
        self.assertEqual(result["elements"][1]["label"], label)

    def test_missing_tree_is_inert_and_unreasonable_size_is_rejected(self):
        source = observed(); del source["tree_markdown"]
        self.assertEqual(normalize_snapshot(source)["accessibility_normalization"]["status"], "not_applicable")
        for patch in (mock.patch("accessibility_tree.MAX_TREE_CHARS", 10),
                      mock.patch("accessibility_tree.MAX_ROWS", 2),
                      mock.patch("accessibility_tree.MAX_DEPTH", 1)):
            with patch:
                self.assert_rejected(observed())
        self.assertIsNone(normalize_snapshot(None))


if __name__ == "__main__":
    unittest.main()
