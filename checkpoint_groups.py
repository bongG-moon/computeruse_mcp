"""Explicit, revisioned visual review batches; never an automatic approval."""
from __future__ import annotations

import copy


def use_popup_image_conditions(steps, checkpoint_ids):
    """Explicitly replace selected open-popup reviews with the next saved target.

    This changes a review policy, not an input or its image. Runtime popup
    discovery and unique image matching still decide whether it can proceed.
    """
    from task_inputs import STEP_ID
    from image_targets import validate_image_target
    if (not isinstance(checkpoint_ids, list) or not 1 <= len(checkpoint_ids) <= 20
            or any(not isinstance(value, str) or not STEP_ID.fullmatch(value) for value in checkpoint_ids)
            or len(set(checkpoint_ids)) != len(checkpoint_ids)):
        raise ValueError("자동 이미지 조건으로 바꿀 팝업 확인 단계 ID 1~20개를 지정하세요.")
    proposed = copy.deepcopy(steps)
    found = [(index, item) for index, item in enumerate(proposed)
             if item.get("step_id") in checkpoint_ids]
    if len(found) != len(checkpoint_ids):
        raise ValueError("같은 단계 목록에서 지정한 팝업 확인 ID를 고유하게 찾지 못했습니다.")
    for index, item in found:
        if (item.get("operation") != "checkpoint" or item.get("review_mode") != "visual"
                or item.get("review_origin") not in {None, "recording_generated", "user_grouped"}
                or not item.get("opened_from") or item.get("return_from") or index+1 >= len(proposed)):
            raise ValueError("열린 팝업의 모델 확인만 자동 이미지 조건으로 바꿀 수 있습니다. 사람·필수 확인과 업무 결과 확인은 유지하세요.")
        following = proposed[index+1]
        key = lambda row: (row.get("program_id"), row.get("window_ref", "main"))
        if (following.get("operation") not in {"image_click", "image_right_click"}
                or key(item) != key(following) or not following.get("image_target")):
            raise ValueError("같은 팝업의 바로 다음 이미지 클릭 대상이 저장되어 있어야 합니다.")
        validate_image_target(following["image_target"])
        if item.get("review_origin") is None:
            item["review_origin"] = "user_grouped"
        item.update(review_mode="popup_image", image_target=copy.deepcopy(following["image_target"]),
            message="관련 팝업 안에서 다음 녹화 대상 이미지가 유일하게 보이는지 확인합니다. 업무 결과 성공 판정은 아닙니다.")
    return proposed


def batch_links(steps):
    """Return deferred checkpoint -> final checkpoint indices, per iteration."""
    links = {}
    for index, step in enumerate(steps):
        if step.get("review_mode") != "deferred_visual":
            continue
        matches = [i for i, other in enumerate(steps) if other.get("step_id") == step.get("review_after")
                   and other.get("iteration_id") == step.get("iteration_id")]
        if len(matches) != 1 or matches[0] <= index:
            raise ValueError("묶음 확인의 마지막 단계가 없거나 순서가 올바르지 않습니다.")
        end = matches[0]
        final = steps[end]
        key = lambda item: (item.get("program_id"), item.get("window_ref", "main"))
        if (step.get("review_origin") not in {"recording_generated", "user_grouped"} or step.get("opened_from")
                or final.get("operation") != "checkpoint" or final.get("review_mode") != "visual"
                or final.get("opened_from") or key(step) != key(final)):
            raise ValueError("같은 결과 창의 녹화 확인만 마지막 모델 확인으로 묶을 수 있습니다.")
        for between in steps[index+1:end]:
            if between.get("program_id") != step.get("program_id"):
                raise ValueError("프로그램이 바뀌는 구간을 묶을 수 없습니다.")
            if between.get("operation") == "checkpoint" and between.get("review_mode") not in {"popup_image", "deferred_visual"}:
                raise ValueError("중간의 사람 확인이나 별도 모델 확인을 건너뛸 수 없습니다.")
        links[index] = end
    for start, end in links.items():
        if any(start < other < end and final != end for other, final in links.items()):
            raise ValueError("서로 겹치는 확인 묶음은 사용할 수 없습니다.")
    for end in set(links.values()):
        conditions = [steps[i]["message"] for i, final in links.items() if final == end]
        if len("\n".join([*conditions, steps[end]["message"]])) > 1900:
            raise ValueError("묶음 완료 조건이 너무 깁니다. 더 작은 구간으로 나누세요.")
    return links


def group_reviews(steps, checkpoint_ids, condition):
    """An explicit edit only: retain actions, IDs, images and old conditions."""
    if (not isinstance(checkpoint_ids, list) or not 2 <= len(checkpoint_ids) <= 20
            or any(not isinstance(v, str) for v in checkpoint_ids)
            or len(set(checkpoint_ids)) != len(checkpoint_ids)
            or not isinstance(condition, str) or not condition.strip() or len(condition) > 1000):
        raise ValueError("묶을 확인 단계 ID 2~20개와 마지막에 함께 확인할 조건을 지정하세요.")
    proposed = copy.deepcopy(steps)
    found = [(i, item) for i, item in enumerate(proposed) if item.get("step_id") in checkpoint_ids]
    if len(found) != len(checkpoint_ids):
        raise ValueError("같은 단계 목록에서 묶을 확인 ID를 찾지 못했습니다.")
    for _, item in found:
        if (item.get("operation") != "checkpoint" or item.get("review_mode") != "visual"
                or item.get("review_origin") not in {None, "recording_generated", "user_grouped"} or item.get("opened_from")):
            raise ValueError("선택한 모델 확인만 묶을 수 있습니다. 사람 확인과 명시적 필수 확인은 유지하세요.")
        if item.get("review_origin") is None:
            # Legacy recordings lack provenance. Only this explicit edit can
            # group them; never claim that they were recording-generated.
            item["review_origin"] = "user_grouped"
    final = found[-1][1]
    # Keep deferred conditions on their own steps. The runner assembles the
    # current conditions at assessment time, including later revision edits.
    message = "함께 확인: " + condition.strip() + "\n마지막 완료 조건:\n" + final["message"]
    if len(message) > 2000:
        raise ValueError("묶음 완료 조건이 너무 깁니다. 더 작은 구간으로 나누세요.")
    for _, item in found[:-1]:
        item.update(review_mode="deferred_visual", review_after=final["step_id"])
    final["message"] = message
    batch_links(proposed)
    return proposed
