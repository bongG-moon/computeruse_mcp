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
             "FxContentPane": "business_tab", "TabGroupPane": "business_tabs",
             "FxViewHostPopupForm": "business_window", "FxAgentMainForm": "host_shell",
             "UltraCombo": "grid_combo", "FXGridCombo": "grid_combo", "FXGridComboBase": "grid_combo",
             "FrmSearchList": "combo_search_modal",
             "FXComboBox": "cell_combo_editor", "FXCheckedComboBox": "cell_check_editor",
             "ComboBoxContainer": "cell_dropdown_container",
             "CustomListBox": "cell_list", "CustomCheckedListBox": "cell_check_list",
             "PropertyGrid": "property_editor", "MesPropertyGrid": "property_editor",
             "UltraGrid": "grid_editor_or_list",
             "FpSpread": "data_grid", "MesGrid": "data_grid", "FXGrid": "data_grid"}
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
            kinds = {kind for kind, _ in areas}
            # An observed class explains the representation, not its origin,
            # value semantics or supported input patterns. These are local
            # observation plans; no selector/capability is synthesized.
            if any(kind == "grid_combo" for kind, _ in areas):
                policy += ["observe_after_expand", "require_observed_selectable_row",
                           "distinguish_display_value_code_and_check_state", "search_modal_is_separate"]
                result["control_family"] = "tabular_query_combo"
            if "combo_search_modal" in kinds:
                result["control_family"] = "combo_search_modal"
                policy += ["require_origin_combo_relation", "observe_search_results_then_confirmation",
                           "verify_value_on_original_combo", "do_not_treat_as_plain_dropdown"]
            if kinds & {"cell_combo_editor", "cell_check_editor", "cell_dropdown_container", "cell_list", "cell_check_list"}:
                result["control_family"] = "dynamic_cell_editor"
                policy += ["require_origin_grid_cell_relation", "reobserve_after_editor_activation",
                           "separate_display_text_and_code", "verify_value_on_original_cell",
                           "editor_closed_is_not_business_saved"]
                if kinds & {"cell_check_editor", "cell_check_list"}:
                    policy += ["read_current_and_target_check_sets", "apply_only_verified_set_difference",
                               "never_toggle_blindly"]
            if any(kind == "property_editor" for kind, _ in areas):
                result["control_family"] = "dynamic_property_editor"
                policy += ["require_category_property_and_business_row_context", "verify_read_only_state",
                           "reobserve_after_editor_activation", "verify_value_on_original_property",
                           "property_changed_is_not_business_saved"]
            if "grid_editor_or_list" in kinds and not kinds & {"grid_combo", "property_editor", "combo_search_modal"}:
                result["control_family"] = "unresolved_grid_editor_or_list"
                policy += ["establish_origin_before_classifying_editor"]
            if kinds & {"result_grid", "data_grid"}:
                policy += ["distinguish_row_operation_from_sheet_export", "row_operation_requires_row_key_and_column",
                           "sheet_export_requires_active_sheet_and_selection_scope", "reobserve_after_scroll_or_edit",
                           "data_cell_is_not_header_filter_or_sheet_tab"]
                result["grid_purpose"] = "observed_result_region" if "result_grid" in kinds else "requires_business_scope"
            if "command_toolbar" in kinds:
                toolbar = next(index for kind, index in areas if kind == "command_toolbar")
                # ToolBar alone does not mean the MenuAgent's outer toolbar.
                # A nested toolbar keeps the business content scope.
                result["command_scope"] = ("business_content" if kinds & {"business_content", "conditions", "condition_commands"}
                    else "host_toolbar" if self.nodes[toolbar].get("automation_id") == "tsViewCommand"
                    else "unresolved_toolbar")
                policy += ["do_not_substitute_same_label_in_another_command_scope"]
            if kinds & {"host_boundary", "business_tab", "business_tabs", "business_window"}:
                policy += ["require_current_business_container", "reobserve_after_tab_dock_or_window_change"]
            if "business_window" in kinds:
                policy += ["window_class_does_not_prove_modal_or_owner_relation"]
            if element.get("role") == "MenuItem":
                policy += ["reobserve_after_menu_open", "do_not_persist_runtime_id_or_index"]
            if policy:
                result["policy"] = list(dict.fromkeys(policy))
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


def compare_accessibility_sources(provider, driver):
    """Interpret existing local samples; do not read the UI or infer handles.

    Only the native reader's complete raw view can establish a sampled empty
    peer. Driver/MCP filtering is known for this Driver sample. Cross-reader
    differences are candidates, even when RuntimeId matches: reads are not
    atomic and runtime identity never becomes a saved selector.
    """
    sources = provider.get("sources", {}) if isinstance(provider, dict) else {}
    raw = sources.get("uia_raw", {}) if isinstance(sources, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    driver = driver if isinstance(driver, dict) else {}
    status = raw.get("status", provider.get("status", "not_observed") if isinstance(provider, dict) else "not_observed")
    complete = raw.get("complete") is True and status in {"ok", "empty"}
    nodes = raw.get("elements", []) if isinstance(raw.get("elements"), list) else []
    rows = driver.get("nodes", []) if isinstance(driver.get("nodes"), list) else []
    findings = []

    def property_value(node, key):
        properties = node.get("properties", {})
        value = properties.get(key) if isinstance(properties, dict) else None
        if isinstance(value, dict):
            return value.get("value") if value.get("status") in {"observed", "supported"} else None
        return value

    def identity(value):
        return tuple(value) if isinstance(value, list) and 1 <= len(value) <= 64 and all(type(v) is int for v in value) else None

    provider_by_id, driver_by_runtime, duplicate_runtime = {}, {}, set()
    provider_runtime_counts = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("protected") or row.get("source") != "driver.elements":
            continue
        meta = row.get("metadata", {})
        value = meta.get("runtime_id", {}) if isinstance(meta, dict) else {}
        runtime = identity(value.get("value")) if isinstance(value, dict) else None
        if runtime is not None:
            if runtime in driver_by_runtime:
                duplicate_runtime.add(runtime)
            driver_by_runtime[runtime] = row
    for node in nodes:
        if isinstance(node, dict) and type(node.get("node_id")) is int:
            provider_by_id[node["node_id"]] = node
            runtime = identity(property_value(node, "runtime_id"))
            if runtime is not None:
                provider_runtime_counts[runtime] = provider_runtime_counts.get(runtime, 0) + 1
    child_counts = {}
    for node in nodes:
        if isinstance(node, dict) and type(node.get("parent_id")) is int:
            child_counts[node["parent_id"]] = child_counts.get(node["parent_id"], 0) + 1
    for node in nodes:
        if not isinstance(node, dict):
            continue
        props = node.get("properties", {})
        if not isinstance(props, dict) or props.get("content_redacted") is True:
            continue
        peer = any(property_value(node, key) == "CustomWindowAutomationPeer" for key in ("name", "class_name"))
        if peer:
            state = node.get("children_status")
            empty = complete and state == "provider_returned_zero" and not child_counts.get(node.get("node_id"))
            observed_children = bool(child_counts.get(node.get("node_id")))
            findings.append({"code": "provider_empty" if empty else "provider_children_observed" if observed_children else "provider_children_unresolved",
                "source": "uia_raw", "node_id": node.get("node_id"), "children_status": state,
                "scope": "this_peer_at_native_sample", "provider_empty_observed": empty,
                "all_application_accessibility_absent": False,
                "next_action": "inspect_search_or_current_business_content" if empty else "compare_current_driver_projection" if observed_children else "resolve_partial_or_provider_error"})
        if not complete:
            continue
        runtime = identity(property_value(node, "runtime_id"))
        if runtime is None or runtime in duplicate_runtime or provider_runtime_counts.get(runtime) != 1:
            continue
        matched = driver_by_runtime.get(runtime)
        if matched and matched.get("projection") not in {None, "returned_control"}:
            findings.append({"code": "mcp_projection_excluded", "source": "driver_projection",
                "node_id": node.get("node_id"), "driver_ordinal": matched.get("observation_ordinal"),
                "reason": matched["projection"], "driver_filtering_confirmed": False})
        elif matched is None:
            parent = provider_by_id.get(node.get("parent_id"), {})
            parent_runtime = identity(property_value(parent, "runtime_id"))
            if (parent_runtime in driver_by_runtime and parent_runtime not in duplicate_runtime
                    and provider_runtime_counts.get(parent_runtime) == 1):
                findings.append({"code": "provider_child_absent_from_driver_sample", "source": "sequential_comparison",
                    "node_id": node.get("node_id"), "parent_node_id": node.get("parent_id"),
                    "driver_filtering_confirmed": False, "cause": "independent_samples_require_same_state_confirmation"})
    return {"format": "computer-accessibility-source-findings/v1", "read_only": True,
        "authorizes_input": False, "extra_tree_reads": 0, "atomic": False,
        "provider_raw_status": status, "provider_raw_complete": complete,
        "driver_projection_status": driver.get("status", "not_observed"),
        "cross_source_identity": "observed_runtime_id_only_current_samples",
        "findings": findings[:100], "findings_omitted": max(0, len(findings) - 100),
        "source_incomplete_is_not_empty": True}
