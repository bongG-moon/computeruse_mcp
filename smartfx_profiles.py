"""Observed SmartFX host adapters, not app-specific selectors or capabilities.

An exact, authorized executable chooses guidance. Only actual provider metadata
and ancestry choose scopes. No extra capture, UIA walk, model, or input is used.
"""
from __future__ import annotations

import copy
import ntpath


_HOSTS = {
    "skhynix.smartfx.pntlauncher.exe": ("smartfx_hosted", "wpf_winforms_host"),
    "skhynix.smartfx.clientfx.menuagenthost.4.0.exe": ("fxmenuagent", "independent_business_windows"),
}
# These are structural hints, usable only when the Provider really reports the
# identifier. A static C# field name never becomes an AutomationId here.
_AREAS = {
    "tsViewCommand": "command_toolbar", "psView": "business_content",
    "pnlConditionContainer": "conditions", "pnlConditionArea": "conditions",
    "pnlConditionButtonArea": "condition_commands", "pnlMainContainer": "business_content",
    "pnlGridArea": "result_grid", "pnlPropertyContainer": "property_editor",
    "pnlPropertyArea": "property_editor",
}
_GUIDANCE = {
    "smartfx_hosted": "WPF 호스트 안의 실제 업무 Form 범위를 먼저 확인하세요. 같은 이름의 다른 탭과 섞지 않습니다.",
    "fxmenuagent": "독립 업무 창과 소유 팝업을 구분하세요. 시작 창을 고정하지 않고, 다른 업무 창은 별도로 연결합니다.",
}


def host_profile(executable):
    """Caller supplies the already authorized, live process identity."""
    match = _HOSTS.get(ntpath.basename(executable).casefold()) if isinstance(executable, str) else None
    if not match:
        return None
    return {"id": match[0], "host_model": match[1], "source": "authorized_executable",
            "runtime_structure_verified": False, "authorizes_input": False,
            "guidance": _GUIDANCE[match[0]]}


def _safe(node):
    return (isinstance(node, dict) and not node.get("synthetic_ancestor")
            and not (type(node.get("element_index")) is int and node["element_index"] < 0)
            and node.get("role") != "Password" and not any(node.get(k) is True for k in
                ("protected", "is_protected", "is_password", "password")))


def _kind(node):
    if not _safe(node):
        return None
    identifier = node.get("automation_id")
    if isinstance(identifier, str) and identifier in _AREAS:
        return _AREAS[identifier]
    # Framework-specific classes are evidence only when returned by the
    # provider, never from DLL availability, captions, or REA field names.
    cls = node.get("class_name", node.get("ClassName", ""))
    leaf = cls.rsplit(".", 1)[-1] if isinstance(cls, str) else ""
    types = {"WindowsFormsHost": "host_boundary", "ElementHost": "host_boundary",
             "UltraCombo": "grid_combo", "FXGridCombo": "grid_combo", "FXGridComboBase": "grid_combo",
             "PropertyGrid": "property_editor", "MesPropertyGrid": "property_editor",
             "FpSpread": "result_grid", "MesGrid": "result_grid", "FXGrid": "result_grid"}
    if leaf in types:
        return types[leaf]
    if node.get("role") == "ToolBar":
        return "command_toolbar"
    return None


class ObservedStrategy:
    def __init__(self, profile, snapshot):
        from operations import _elements, _name
        self.profile = copy.deepcopy(profile)
        self.snapshot = snapshot
        self.elements = _elements(snapshot)
        self.nodes = {e["element_index"]: e for e in self.elements if type(e.get("element_index")) is int}
        self.kinds = {index: kind for index, node in self.nodes.items() if (kind := _kind(node))}
        self.scopes = {}
        self.contexts = {}
        self.complete = not any(snapshot.get(key) for key in ("truncated", "max_depth_reached", "max_elements_reached"))
        observed_indices = [e["element_index"] for e in self.elements if type(e.get("element_index")) is int]
        self.complete = self.complete and len(set(observed_indices)) == len(observed_indices)
        # Index the already returned tree once. Repeated whole-tree selector
        # scans for hundreds of fields would undo the latency benefit.
        self.index = {}
        for position, node in enumerate(self.elements):
            for key, value in (("name", _name(node)), ("automation_id", node.get("automation_id")), ("role", node.get("role"))):
                if isinstance(value, str):
                    self.index.setdefault((key, value), set()).add(position)

    def _matches(self, selector):
        pools = [self.index.get((key, value), set()) for key, value in selector.items()]
        return set.intersection(*pools) if pools else set()

    def _unique(self, selector, element, ancestor=None):
        from operations import _descendant
        found = self._matches(selector)
        if ancestor is not None:
            found = {i for i in found if _descendant(self.elements[i], ancestor, self.elements, self.nodes)}
        return len(found) == 1 and self.elements[next(iter(found))] is element

    def context(self, element):
        index = element.get("element_index")
        if type(index) is not int or not _safe(element):
            return None
        if index in self.contexts:
            return self.contexts[index]
        current, seen, areas = index, set(), []
        for _ in range(33):
            if current in seen or current not in self.nodes or not _safe(self.nodes[current]):
                break
            seen.add(current)
            if current in self.kinds:
                areas.append((self.kinds[current], current))
            current = self.nodes[current].get("parent_index")
            if type(current) is not int:
                break
        result = {"areas": [kind for kind, _ in areas], "source": "observed_metadata_and_ancestry",
                  "read_only": True, "authorizes_input": False} if areas else None
        if result:
            policy = []
            if any(kind == "grid_combo" for kind, _ in areas):
                policy += ["observe_after_expand", "require_observed_selectable_row",
                           "distinguish_display_value_code_and_check_state", "search_modal_is_separate"]
            if any(kind == "property_editor" for kind, _ in areas):
                policy += ["reobserve_after_editor_activation", "verify_value_on_original_property"]
            if any(kind == "result_grid" for kind, _ in areas):
                policy += ["require_unique_row_key_and_column", "reobserve_after_scroll_or_edit"]
            if element.get("role") == "MenuItem":
                policy += ["reobserve_after_menu_open", "do_not_persist_runtime_id_or_index"]
            if policy:
                result["policy"] = policy
        self.contexts[index] = result
        return result

    def selector(self, element):
        """Prefer stable names in a proven structural region; never guess labels."""
        from inspection import _selector_candidates
        if not self.complete or not _safe(element):
            return None
        current, seen = element.get("parent_index"), set()
        candidates = [s for s in _selector_candidates(element) if not str(s.get("automation_id", "")).isdigit()]
        for _ in range(32):
            if type(current) is not int or current in seen or current not in self.nodes:
                break
            seen.add(current)
            ancestor = self.nodes[current]
            if not _safe(ancestor):
                return None
            if current in self.kinds:
                if current not in self.scopes:
                    self.scopes[current] = next((s for s in _selector_candidates(ancestor)
                        if not str(s.get("automation_id", "")).isdigit()
                        and self._unique(s, ancestor)), None)
                scope = self.scopes[current]
                if scope:
                    for candidate in candidates:
                        scoped = {**candidate, "within": copy.deepcopy(scope)}
                        if self._unique(candidate, element, ancestor):
                            return scoped
            current = ancestor.get("parent_index")
        return None

    def summary(self):
        counts = {}
        for kind in self.kinds.values():
            counts[kind] = counts.get(kind, 0) + 1
        parents = {node.get("parent_index") for node in self.elements if type(node.get("parent_index")) is int}
        empty_peer = any(_safe(node) and (node.get("class_name") == "CustomWindowAutomationPeer"
                         or node.get("name", node.get("label")) == "CustomWindowAutomationPeer")
                         and index not in parents
                         for index, node in self.nodes.items())
        result = {**self.profile, "observed_regions": counts,
                  "structure_status": "observed_hints" if counts else "not_exposed",
                  "capabilities": "observed_patterns_only", "extra_tree_reads": 0}
        if empty_peer:
            result["navigation_diagnostic"] = {"code": "peer_has_no_observed_children",
                "provider_cause_confirmed": False,
                "next_step": "현재 반환 트리에는 자식이 없습니다. 원본 Provider 진단과 비교하거나 해당 영역의 이미지로 확인하세요. 깊이 확대만으로 복구된다고 가정하지 않습니다."}
        return result
