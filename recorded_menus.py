"""Import proven menu transitions without persisting native instance handles.

An owner graph is not an instruction to open a menu. Only an observed parent
transition and its actual child evidence can create an executable expansion.
Old receipts are never rewritten or guessed from popup class names.
"""
from __future__ import annotations

import copy

from image_targets import validate_image_target
from learning import stable_selector
from learning_picker import match_picked_element
from operations import OperationError, _descendant, _elements, _find, _unique


def recorded_image(event):
    if "template_png" not in event:
        return None
    try:
        return validate_image_target({"format": "computer-image-target/v1",
            **{key: event[key] for key in ("template_png", "width", "height", "anchor", "capture_window")},
            "min_score": .94, "ambiguity_margin": .03,
            **({"source_size": event["source_size"]} if "source_size" in event else {})})
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationError("메뉴 펼침 전 이미지가 올바르지 않습니다.", "menu_expansion_unverified") from exc


def _reject(message):
    raise OperationError(message, "menu_expansion_unverified")


def recorded_menu_target(native):
    """Persist only actual native menu labels and their observed role hierarchy."""
    if not isinstance(native, dict):
        return None
    ancestors = native.get("ancestors", [])
    element = native.get("element")
    if not isinstance(ancestors, list) or len(ancestors) > 16 or not isinstance(element, dict):
        return None
    if element.get("role") != "MenuItem":
        return None
    path = []
    for row in [*reversed(ancestors), element]:
        if not isinstance(row, dict):
            return None
        if row.get("role") != "MenuItem":
            continue
        name = row.get("name")
        if (row.get("is_password") is True or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 1000):
            return None
        path.append({"name": name, "role": "MenuItem"})
    return {"path": path} if 1 <= len(path) <= 16 else None


def recorded_menu_command(event, target, menu_target):
    """A proven recorded child click becomes a fresh path lookup at execution."""
    native = event.get("native_target")
    observed = recorded_menu_target(native)
    if (event.get("operation") != "click" or event.get("input_method") == "keyboard_unverified"
            or observed is None or any(native.get(k) != target[k] for k in ("pid", "window_id"))
            or observed["path"][-1] != menu_target["path"][-1]):
        return None
    return {"operation": "menu_command", "program_id": target["program_id"],
            "window_ref": target.get("window_ref", "main"),
            "menu_target": copy.deepcopy(menu_target), "completion_mode": "human"}


def _menu_selector(menu_target):
    selector = None
    for part in menu_target["path"]:
        selector = {**part, **({"within": selector} if selector is not None else {})}
    return selector


def recorded_menu_step(event, index, events, target, discovered, snapshots):
    """Return one self-verifying expansion, or require repair of this event.

Native runtime identities corroborate the recording only. Playback uses fresh
guarded image/semantic targets and fresh child discovery, never those handles.
"""
    proof = event.get("menu_evidence")
    native = event.get("native_target")
    native_element = native.get("element") if isinstance(native, dict) else None
    method = event.get("expansion_method")
    if (not isinstance(proof, dict) or proof.get("source") != "uia_menu_transition"
            or proof.get("before_state") != "collapsed" or proof.get("after_state") != "expanded"
            or not isinstance(proof.get("parent_identity"), str) or not 0 < len(proof["parent_identity"]) <= 512
            or method not in {"hover", "click", "keyboard", "accessibility"}
            or method == "hover" and proof.get("input_version_unchanged") is not True
            or not isinstance(native_element, dict) or native_element.get("role") != "MenuItem"
            or native_element.get("is_password") is True
            or any(native.get(key) != target[key] for key in ("pid", "window_id"))):
        _reject("부모 메뉴의 실제 펼침 전후 증거가 부족합니다. 이 구간을 다시 녹화하세요.")
    children = proof.get("child_items")
    if (not isinstance(children, list) or not 1 <= len(children) <= 100
            or any(not isinstance(row, dict) or row.get("role") != "MenuItem"
                   or not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 2000 for row in children)):
        _reject("실제로 나타난 자식 메뉴의 관찰이 없습니다.")
    step = {"operation": "expand_menu", "program_id": target["program_id"],
            "window_ref": target.get("window_ref", "main"), "expansion_method": method,
            "verification_timeout_ms": 1500}
    parent_name = native_element.get("name")
    if isinstance(parent_name, str) and 1 <= len(parent_name.strip()) <= 1000:
        step["menu_path"] = [parent_name]
    semantic_parent = recorded_menu_target(native)
    if semantic_parent is not None:
        step["menu_target"] = semantic_parent
        # Even a separate child HWND has a semantic parent. Never use a
        # provider's internal automation identifier as the saved menu label.
        step["selector"] = _menu_selector(semantic_parent)
    # Diagnostic depth follows proven menu-opening episodes, not same-PID
    # windows or guessed HWND owner depth (sibling menus may share an owner).
    depth, ancestor = 1, step["window_ref"]
    for earlier in reversed(events[:index]):
        evidence = earlier.get("menu_evidence") if isinstance(earlier, dict) else None
        if (isinstance(evidence, dict) and earlier.get("operation") == "expand_menu"
                and earlier.get("program_id") == target["program_id"] and evidence.get("child_window_ref") == ancestor):
            depth += 1; ancestor = earlier.get("window_ref", "main")
    step["menu_depth"] = min(depth, 16)
    if method == "keyboard":
        step["expansion_key"] = event.get("expansion_key")
    parent_image = recorded_image(event)
    representation = proof.get("child_representation")
    if representation == "hwnd":
        if method not in {"hover", "click"}:
            _reject("이미지 부모 메뉴의 키 입력이나 접근성 펼침 경로가 검증되지 않았습니다.")
        child_ref = proof.get("child_window_ref")
        if not isinstance(child_ref, str):
            _reject("자식 메뉴 참조 형식이 올바르지 않습니다.")
        child_key = (target["program_id"], child_ref)
        if child_key not in discovered or child_ref == step["window_ref"] or parent_image is None and semantic_parent is None:
            _reject("부모 이미지 또는 실제 소유 관계가 확인된 자식 팝업이 없습니다.")
        child_target, wait = discovered[child_key]
        if child_target["pid"] != target["pid"] or child_target["window_id"] == target["window_id"]:
            _reject("같은 창의 논리 자식은 별도 팝업으로 저장할 수 없습니다.")
        # WindowsForms sibling menus can share the main owner. Require a
        # declared common owner path; same PID alone never establishes it.
        def path(ref):
            found = {ref}
            while (target["program_id"], ref) in discovered:
                ref = discovered[(target["program_id"], ref)][1]["owner_ref"]
                if ref in found: break
                found.add(ref)
            return found
        if not path(child_ref).intersection(path(step["window_ref"])):
            _reject("부모와 자식 메뉴의 소유 경로가 연결되지 않았습니다.")
        following = next((row for row in events[index+1:] if isinstance(row, dict)
            and (row.get("program_id"), row.get("window_ref", "main")) == child_key), None)
        if following is None:
            # Ending or cancelling the menu is a valid partial recording. The
            # importer keeps the other events and a repair step, not a guessed command.
            _reject("하위 메뉴를 연 뒤 선택한 항목이 기록되지 않았습니다. 이 메뉴 구간을 보완하세요.")
        child_image = recorded_image(following or {})
        child_native = following.get("native_target")
        child_element = child_native.get("element") if isinstance(child_native, dict) else None
        label = child_element.get("name") if isinstance(child_element, dict) else None
        if (isinstance(label, str) and 1 <= len(label) <= 1000
                and label in {row["name"] for row in children if row.get("enabled") is not False}
                and child_element.get("role") == "MenuItem" and child_element.get("is_password") is not True
                and all(child_native.get(k) == child_target[k] for k in ("pid", "window_id"))):
            step.setdefault("menu_path", []).append(label)
            step["child_menu_target"] = {"path": [{"name": label, "role": "MenuItem"}]}
        if child_image is None and "child_menu_target" not in step:
            _reject("자식 메뉴를 구별할 관찰된 이름 또는 사전 이미지가 없습니다. 자식 메뉴 구간을 보완하세요.")
        step["child_window"] = {k: wait[k] for k in ("title", "class_name")}
        if parent_image is not None: step["image_target"] = parent_image
        if child_image is not None: step["child_image_target"] = child_image
    elif representation == "logical":
        snapshot = snapshots.get((target["program_id"], step["window_ref"]))
        # Prefer the child actually used next, but only from the recorded
        # descendants. Never search a label globally and treat it as this path.
        following = next((row for row in events[index+1:] if isinstance(row, dict)
            and (row.get("program_id"), row.get("window_ref", "main")) == (target["program_id"], step["window_ref"])), {})
        selected_native = following.get("native_target")
        selected_element = selected_native.get("element") if isinstance(selected_native, dict) else None
        selected_name = selected_element.get("name") if isinstance(selected_element, dict) else None
        names = {row["name"] for row in children if row.get("enabled") is not False}
        if (not isinstance(selected_name, str) or selected_name not in names
                or selected_element.get("role") != "MenuItem" or selected_element.get("is_password") is True
                or any(selected_native.get(k) != target[k] for k in ("pid", "window_id"))):
            _reject("다음 동작을 실제 펼쳐진 자식 메뉴와 연결하지 못했습니다.")
        step.setdefault("menu_path", []).append(selected_name)
        if semantic_parent is not None:
            step["child_menu_target"] = {"path": [*copy.deepcopy(semantic_parent["path"]),
                                                    {"name": selected_name, "role": "MenuItem"}]}
        if semantic_parent is not None:
            # The recorded native transition proves the relationship. The
            # end-of-recording Driver tree may already have closed this menu;
            # never replace its real labels with a provider-internal ID.
            step["selector"] = _menu_selector(semantic_parent)
            step["child_selector"] = _menu_selector(step["child_menu_target"])
        elif isinstance(snapshot, dict):
            picked = match_picked_element(snapshot, native, {key: target[key] for key in ("pid", "window_id")})
            parent = _unique(snapshot, picked["expected_selector"])
            step["selector"] = stable_selector(snapshot, parent)
            matches = [row for row in _find(snapshot, {"role": "MenuItem", "name": selected_name})
                       if _descendant(row, parent, _elements(snapshot))]
            if len(matches) != 1 or matches[0].get("enabled") is False:
                _reject("같은 부모 아래의 자식 메뉴를 유일하게 구별하지 못했습니다.")
            step["child_selector"] = stable_selector(snapshot, matches[0])
        elif method == "hover" and parent_image is not None:
            # A leaf normally closes its popup, so the end-of-recording tree
            # cannot certify old handles. Keep only names from the recorded
            # native transition. They are NOT input handles: replay must prove
            # a unique fresh parent, its physical bounds, the same image point
            # and the actual child hierarchy before/after one guarded hover.
            parent_name = native["element"].get("name")
            if not isinstance(parent_name, str) or not 1 <= len(parent_name.strip()) <= 1000:
                _reject("다시 찾을 수 있는 부모 메뉴 이름이 없습니다.")
            step["selector"] = {"role": "MenuItem", "name": parent_name}
            step["child_selector"] = {"role": "MenuItem", "name": selected_name,
                                      "within": copy.deepcopy(step["selector"])}
        else:
            _reject("논리 자식 메뉴를 재관찰할 계층과 입력 근거가 없습니다.")
        if method == "hover":
            if parent_image is None and semantic_parent is None:
                _reject("논리 메뉴 호버 입력의 사전 이미지가 없습니다.")
            if parent_image is not None: step["image_target"] = parent_image
    else:
        _reject("자식 메뉴가 별도 창인지 논리 자식인지 확인하지 못했습니다.")
    from menu_expansion import validate_menu_expansion
    validate_menu_expansion({key: value for key, value in step.items() if key not in {"program_id", "window_ref"}})
    name = native_element.get("name")
    return copy.deepcopy(step), name if isinstance(name, str) and name.strip() else "부모 메뉴"
