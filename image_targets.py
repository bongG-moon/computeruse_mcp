"""Bounded offline PNG targets and a fixed, cancellable native image matcher.

Saved targets contain pixels chosen by the user, never a path, desktop coordinate,
process ID, window handle, or executable code. Matching only reads supplied images.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import time
import uuid
import zlib

from operations import OperationError

HELPER_NAME = "Computer Use MCP 이미지 도구.exe"
MAX_TEMPLATE_BYTES = 512 * 1024
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024


def png_dimensions(encoded, *, template=False):
    maximum = MAX_TEMPLATE_BYTES if template else MAX_SCREENSHOT_BYTES
    if not isinstance(encoded, str) or not encoded or len(encoded) > ((maximum + 2)//3)*4:
        raise OperationError("이미지의 크기 또는 형식이 올바르지 않습니다.", "invalid_image")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > maximum or raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("PNG")
        position, width, height, data_seen, ended = 8, None, None, False, False
        while position < len(raw):
            if position + 12 > len(raw):
                raise ValueError("chunk")
            length = struct.unpack(">I", raw[position:position+4])[0]
            kind = raw[position+4:position+8]
            stop = position+8+length
            if stop+4 > len(raw):
                raise ValueError("length")
            data = raw[position+8:stop]
            if zlib.crc32(kind+data) & 0xffffffff != struct.unpack(">I", raw[stop:stop+4])[0]:
                raise ValueError("CRC")
            if position == 8:
                if kind != b"IHDR" or length != 13:
                    raise ValueError("header")
                width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                limit = 512 if template else 4096
                minimum = 8 if template else 1
                if not minimum <= width <= limit or not minimum <= height <= limit or depth != 8 or color not in {0, 2, 4, 6} or compression or filtering or interlace:
                    raise ValueError("dimensions")
            elif kind == b"IHDR":
                raise ValueError("duplicate header")
            if kind == b"IDAT":
                data_seen = True
            if kind == b"IEND":
                if length or stop+4 != len(raw):
                    raise ValueError("end")
                ended = True
            position = stop+4
        if not data_seen or not ended:
            raise ValueError("incomplete")
        return width, height
    except (ValueError, TypeError, struct.error, binascii.Error):
        raise OperationError("손상됐거나 지원하지 않는 PNG 이미지입니다.", "invalid_image") from None


def validate_image_target(value):
    fields = {"format", "template_png", "width", "height", "anchor", "capture_window", "min_score", "ambiguity_margin"}
    if not isinstance(value, dict) or not fields <= set(value) or set(value)-fields-{"source_size"} or value.get("format") != "computer-image-target/v1":
        raise OperationError("저장한 이미지 대상의 형식이 올바르지 않습니다.", "invalid_image_target")
    width, height = png_dimensions(value["template_png"], template=True)
    if type(value["width"]) is not int or type(value["height"]) is not int or (value["width"], value["height"]) != (width, height):
        raise OperationError("이미지 대상의 크기 정보가 실제 PNG와 다릅니다.", "invalid_image_target")
    anchor, capture = value["anchor"], value["capture_window"]
    if (not isinstance(anchor, dict) or set(anchor) != {"x", "y"}
            or any(type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1 for v in anchor.values())
            or not isinstance(capture, dict) or set(capture) != {"width", "height"}
            or any(type(v) is not int or not 8 <= v <= 16384 for v in capture.values())
            or width > capture["width"] or height > capture["height"]):
        raise OperationError("이미지 대상의 중심점 또는 캡처 크기가 올바르지 않습니다.", "invalid_image_target")
    if (type(value["min_score"]) not in {int, float} or not .94 <= value["min_score"] <= 1
            or type(value["ambiguity_margin"]) not in {int, float} or not .03 <= value["ambiguity_margin"] <= .2):
        raise OperationError("이미지의 최소 유사도와 중복 판정 기준을 확인하세요.", "invalid_image_target")
    original = value.get("source_size", {"width": width, "height": height})
    if (not isinstance(original, dict) or set(original) != {"width", "height"}
            or any(type(v) is not int or not 8 <= v <= 4096 for v in original.values())
            or original["width"] > capture["width"] or original["height"] > capture["height"]
            or abs(original["width"]*height-original["height"]*width) > max(original.values())*2):
        raise OperationError("이미지 원본 영역의 크기와 비율을 확인하세요.", "invalid_image_target")
    return copy.deepcopy(value)


def image_target_summary(value):
    target = validate_image_target(value)
    return {"recognition": "image", "width": target["width"], "height": target["height"], "min_score": target["min_score"]}


def match_failure_message(details):
    """Explain evidence, not an unproven diagnosis that the screen is wrong."""
    message = "저장 이미지와 현재 대상의 일치를 확인하지 못했습니다."
    if all(type(details.get(k)) in (int,float) for k in ("score","threshold")):
        message += f" 유사도 {details['score']:.3f} / 기준 {details['threshold']:.3f}."
    capture, current = details.get("capture_window",{}), details.get("screenshot",{})
    if all(k in capture and k in current for k in ("width","height")):
        message += f" 캡처 크기 {capture['width']}×{capture['height']} → {current['width']}×{current['height']}."
    message += " 화면이 다르거나 DPI가 바뀌었다고 확정한 것은 아닙니다."
    if details.get("review_candidates"):
        message += " 주변 윤곽이 비슷한 확인용 후보가 있습니다. 후보는 자동 클릭하지 않습니다."
    return message + " 현재 대상 확인 또는 편집창에서 이 단계만 다시 지정하세요. 전체 재녹화는 필요하지 않습니다."


def validate_match(answer, image_target, screenshot_png):
    target = validate_image_target(image_target)
    width, height = png_dimensions(screenshot_png)
    if not isinstance(answer, dict) or answer.get("status") not in {"matched", "not_found", "ambiguous"}:
        raise OperationError("이미지 비교 결과를 확인하지 못했습니다.", "image_match_failed")
    details = {"screenshot": {"width": width, "height": height}, "capture_window": copy.deepcopy(target["capture_window"]),
               "threshold": target["min_score"], "ambiguity_margin": target["ambiguity_margin"]}
    dx, dy = width-target["capture_window"]["width"], height-target["capture_window"]["height"]
    details["frame_comparison"] = {"width_delta":dx, "height_delta":dy,
        "size_relation":"same_size" if dx == dy == 0 else "small_border_difference" if max(abs(dx),abs(dy))<=4 else "size_changed",
        "dpi_change_proven":False, "screen_change_proven":False}
    details["frame_sha256"] = hashlib.sha256(screenshot_png.encode("ascii")).hexdigest()
    for field in ("score", "second_score"):
        value = answer.get(field)
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1:
            details[field] = round(value, 6)
    if type(answer.get("candidate_count")) is int and 0 <= answer["candidate_count"] <= 100000:
        details["candidate_count"] = answer["candidate_count"]
    if type(answer.get("scale")) in {int, float} and math.isfinite(answer["scale"]) and .2 <= answer["scale"] <= 16:
        details["scale"] = round(answer["scale"], 6)
    if answer.get("code") == "template_low_detail":
        details["low_detail"] = True
    if answer.get("code") == "candidate_budget_exceeded":
        evaluations, budget = answer.get("candidate_evaluations"), answer.get("candidate_budget")
        if (answer["status"] != "ambiguous" or answer.get("search_complete") is not False
                or type(evaluations) is not int or type(budget) is not int
                or not 1 <= evaluations == budget <= 100000 or "candidate_count" in answer):
            raise OperationError("이미지 탐색 예산 진단의 형식을 확인하지 못했습니다.", "image_match_invalid")
        details.update(search_diagnostic="candidate_budget_exceeded", search_complete=False,
                       candidate_evaluations=evaluations, candidate_budget=budget)
    if answer["status"] != "matched":
        if "score" in details:
            details["score_gap"] = round(max(0,target["min_score"]-details["score"]),6)
        suggestions = answer.get("review_candidates", [])
        if answer["status"] == "not_found" and isinstance(suggestions,list):
            safe=[]
            for item in suggestions[:3]:
                if not isinstance(item,dict) or item.get("review_only") is not True: continue
                rect=item.get("rect")
                if (not isinstance(rect,dict) or set(rect)!={"x","y","width","height"}
                        or any(type(v) is not int for v in rect.values())
                        or min(rect["x"],rect["y"])<0 or min(rect["width"],rect["height"])<8
                        or rect["x"]+rect["width"]>width or rect["y"]+rect["height"]>height): continue
                if any(type(item.get(key)) not in (int,float) or not math.isfinite(item[key]) or not 0<=item[key]<=1 for key in ("score","structure_score")): continue
                safe.append({"rect":dict(rect),"score":round(item["score"],6),"structure_score":round(item["structure_score"],6),"review_only":True})
            details["review_candidates"]=safe
        return {"status": answer["status"], "code": "image_ambiguous" if answer["status"] == "ambiguous" else
                "image_template_low_detail" if details.get("low_detail") else "image_not_found", **details}
    rect, screenshot, score = answer.get("rect"), answer.get("screenshot"), answer.get("score")
    if (screenshot != {"width": width, "height": height}
            or not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}
            or any(type(v) is not int for v in rect.values())
            or rect["width"] < 1 or rect["height"] < 1 or rect["x"] < 0 or rect["y"] < 0
            or rect["x"]+rect["width"] > width or rect["y"]+rect["height"] > height
            or type(score) not in {int, float} or not math.isfinite(score) or not target["min_score"] <= score <= 1):
        raise OperationError("이미지 비교의 크기·위치·유사도를 검증하지 못했습니다.", "image_match_invalid")
    second = answer.get('second_score')
    if ('second_score' in answer and (type(second) not in {int, float} or not math.isfinite(second)
            or not 0 <= second <= 1 or score-second < target['ambiguity_margin'])
            or type(answer.get('candidate_count')) is int and answer['candidate_count'] > 1 and 'second_score' not in answer):
        raise OperationError('서로 다른 이미지 후보를 충분히 구분하지 못했습니다.', 'image_match_invalid')
    # This location is ephemeral and belongs solely to the supplied screenshot.
    x = min(rect["x"]+rect["width"]-1, rect["x"]+int(target["anchor"]["x"]*rect["width"]))
    y = min(rect["y"]+rect["height"]-1, rect["y"]+int(target["anchor"]["y"]*rect["height"]))
    if 'template_clip' in answer:
        clip = answer['template_clip']
        fields = {'left', 'top', 'right', 'bottom', 'original_width', 'original_height'}
        original = target.get('source_size', {'width': target['width'], 'height': target['height']})
        if (not isinstance(clip, dict) or set(clip) != fields or any(type(v) is not int for v in clip.values())
                or rect['width'] < 8 or rect['height'] < 8
                or any(not 0 <= clip[key] <= 4 for key in ('left', 'top', 'right', 'bottom'))
                or clip['original_width'] != original['width'] or clip['original_height'] != original['height']
                or clip['original_width'] != rect['width']+clip['left']+clip['right']
                or clip['original_height'] != rect['height']+clip['top']+clip['bottom']
                or not any(clip[key] for key in ('left', 'top', 'right', 'bottom'))
                or any(abs(target['capture_window'][key]-value) > 4 for key, value in (('width', width), ('height', height)))):
            raise OperationError('이미지 테두리 보정의 크기와 범위를 확인하지 못했습니다.', 'image_match_invalid')
        for dimension, origin, start, end, extent in (('width', 'x', 'left', 'right', width), ('height', 'y', 'top', 'bottom', height)):
            removed = clip[start]+clip[end]
            partial_edge = (original[dimension] < extent and 0 <= rect[origin] <= 1
                            and clip[end] == 0 and 0 < target['capture_window'][dimension]-extent <= 4
                            and clip[start]-rect[origin] == (target['capture_window'][dimension]-extent)//2)
            far_gap = extent-rect[origin]-rect[dimension]
            partial_edge = partial_edge or (original[dimension] < extent and 0 <= far_gap <= 1
                            and clip[start] == 0 and 0 < target['capture_window'][dimension]-extent <= 4
                            and clip[end]-far_gap == (target['capture_window'][dimension]-extent)//2)
            if removed and (removed > 4 or original[dimension] != target['capture_window'][dimension]
                    or rect[dimension] != extent or rect[origin] != 0
                    or removed != target['capture_window'][dimension]-extent
                    or clip[start] != removed//2 or clip[end] != removed-removed//2) and not partial_edge:
                raise OperationError('이미지 테두리 밖의 영역을 잘라낼 수 없습니다.', 'image_match_invalid')
        # Preserve the recorded point in original source pixels. Rescaling the
        # retained crop would shift off-center clicks; clipping must never clamp
        # a point that was actually removed onto a different control pixel.
        offset_x = min(original['width']-1, int(target['anchor']['x']*original['width']))-clip['left']
        offset_y = min(original['height']-1, int(target['anchor']['y']*original['height']))-clip['top']
        if not 0 <= offset_x < rect['width'] or not 0 <= offset_y < rect['height']:
            raise OperationError('기록한 클릭 지점이 보정 후 이미지 밖에 있습니다. 다시 선택하세요.', 'image_match_invalid')
        x, y = rect['x']+offset_x, rect['y']+offset_y
        details['template_clip'] = dict(clip)
    return {**details, "status": "matched", "x": x, "y": y, "score": score, "rect": dict(rect), "screenshot": screenshot}


def _native_match(runtime, target, screenshot_png, helper, deadline):
    """Cancellable fixed helper; this function never dispatches input."""
    from learning import ElementLibrary
    if not helper.is_file():
        raise OperationError("이미지 도구 실행파일이 없습니다. 전체 배포 ZIP으로 업데이트하세요.", "image_helper_missing")
    root = Path(runtime.run_dir) / "im"
    ElementLibrary._reject_link(root)
    root.mkdir(exist_ok=True)
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    request, response = root / (nonce[:16]+".in"), root / (nonce[:16]+".out")
    child = None
    try:
        with request.open("x", encoding="utf-8") as stream:
            json.dump({"nonce": nonce, "template_png": target["template_png"], "screenshot_png": screenshot_png,
                       "capture_window": target["capture_window"], "min_score": target["min_score"],
                       **({"source_size": target["source_size"]} if "source_size" in target else {}),
                       "ambiguity_margin": target["ambiguity_margin"]}, stream)
        child = subprocess.Popen([str(helper), "--match", str(request), str(response)], cwd=str(helper.parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        while child.poll() is None:
            runtime.check_active()
            if time.monotonic() >= deadline:
                raise OperationError("이미지 비교 시간이 초과됐습니다. 입력하지 않았습니다.", "image_match_timeout")
            runtime.stop_event.wait(.05)
        runtime.check_active()
        ElementLibrary._reject_link(response, file=True)
        if child.returncode != 0 or not response.is_file() or response.stat().st_size > 64000:
            raise OperationError("이미지 비교 도구가 결과를 반환하지 못했습니다.", "image_match_failed")
        answer = json.loads(response.read_text(encoding="utf-8-sig"))
        if answer.get("nonce") != nonce:
            raise OperationError("다른 이미지 비교 요청의 응답입니다.", "image_match_invalid")
        return answer
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, OperationError):
            raise
        raise OperationError("로컬 이미지 비교 도구의 응답을 읽지 못했습니다.", "image_match_failed") from error
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        for path in (request, response):
            try:
                ElementLibrary._reject_link(path, file=True)
                path.unlink(missing_ok=True)
            except (OSError, OperationError):
                pass


class _BackendCheckAborted(BaseException):
    """Preserve any session cancellation exception across backend fallbacks."""
    def __init__(self, cause):
        self.cause = cause


def _backend_fingerprint(helper):
    """Cache results only for byte-identical adapters and declared runtimes."""
    from learning import ElementLibrary
    root = Path(__file__).resolve().parent
    parts = []
    for path in (helper, root/"airtest_backend.py", root/"image_backend_runtime/BACKEND-MANIFEST.json"):
        ElementLibrary._reject_link(path, file=True)
        if not path.is_file():
            parts.append(path.name+":missing")
        elif path == helper:
            stat = path.stat()
            parts.append(path.name+":"+str(stat.st_mtime_ns)+":"+str(stat.st_size))
        else:
            parts.append(path.name+":"+hashlib.sha256(path.read_bytes()).hexdigest())
    return "|".join(parts)


def match_image(runtime, image_target, screenshot_png, *, allow_selection_normalization=False):
    """Select a bundled local matcher; preserve ambiguity and input safeguards.

    Production measurements include isolated-worker startup, so the native
    matcher remains first for every frame. Airtest is a bounded fallback for a
    genuinely absent target. Both only read supplied PNGs.
    """
    if type(allow_selection_normalization) is not bool:
        raise OperationError("Invalid private selection appearance capability", "image_match_invalid")
    target = validate_image_target(image_target)
    width, height = png_dimensions(screenshot_png)
    runtime.check_active()
    helper = Path(__file__).resolve().with_name(HELPER_NAME)
    fingerprint = _backend_fingerprint(helper)
    key = hashlib.sha256((json.dumps(target, sort_keys=True, separators=(",", ":"))+"\0"+screenshot_png
                          +"\0"+fingerprint+"\0selection="+str(allow_selection_normalization)).encode("utf-8")).hexdigest()
    cache = getattr(runtime, "_image_match_cache", {})
    if isinstance(cache, dict) and key in cache:
        runtime.check_active()
        return {**copy.deepcopy(cache[key]), "search_reused": True}
    started = time.monotonic()
    deadline = started+22
    import airtest_backend
    present = airtest_backend.available()
    order = ("native", "airtest")
    fallback = None
    validated = None

    def active():
        try:
            runtime.check_active()
        except Exception as error:
            raise _BackendCheckAborted(error) from error

    for backend in order:
        runtime.check_active()
        if time.monotonic() >= deadline:
            raise OperationError("이미지 비교 시간이 초과됐습니다. 입력하지 않았습니다.", "image_match_timeout")
        if backend == "airtest":
            if not present:
                continue
            try:
                answer = airtest_backend.match_isolated(runtime, target, screenshot_png, deadline=deadline,
                    check_active=active, allow_selection_normalization=allow_selection_normalization)
                candidate = validate_match(answer, target, screenshot_png)
            except _BackendCheckAborted as error:
                raise error.cause
            except (airtest_backend.BackendUnavailable, OSError, ImportError, ValueError, TimeoutError) as error:
                runtime.check_active()
                if isinstance(error, OperationError) and error.code not in ("image_match_failed", "image_match_invalid"):
                    raise
                fallback = "image_backend_unavailable" if isinstance(error, airtest_backend.BackendUnavailable) else "image_backend_failed"
                if validated is not None:
                    break
                continue
        else:
            answer = _native_match(runtime, target, screenshot_png, helper, deadline)
            candidate = validate_match(answer, target, screenshot_png)
        runtime.check_active()
        validated = {**candidate, "backend": answer.get("backend", "airtest-template-rgb") if backend == "airtest" else "native-rgb"}
        if answer.get("appearance_verified") is True:
            validated["appearance_verified"] = True
            validated["original_score"] = answer.get("original_score")
        if answer.get("appearance_rejected") in {"foreground_ink_changed"}:
            validated["appearance_rejected"] = answer["appearance_rejected"]
            appearance_score = answer.get("appearance_score")
            if type(appearance_score) in (int, float) and 0 <= appearance_score <= 1:
                validated["appearance_score"] = appearance_score
        # Uniqueness and detail rejection are final, never a reason to try a
        # more permissive engine. Only a genuinely absent target may fall back.
        if candidate["status"] != "not_found" or candidate.get("low_detail"):
            break
    if validated is None:
        raise OperationError("로컬 이미지 비교 도구를 시작하지 못했습니다.", "image_match_failed")
    validated["search_ms"] = round((time.monotonic()-started)*1000, 2)
    if fallback:
        validated["backend_fallback"] = fallback
    cache = dict(cache) if isinstance(cache, dict) else {}
    while len(cache) >= 8:
        cache.pop(next(iter(cache)))
    cache[key] = copy.deepcopy(validated)
    runtime._image_match_cache = cache
    return {**validated, "search_reused": False}
