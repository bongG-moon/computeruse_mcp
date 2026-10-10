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


def validate_match(answer, image_target, screenshot_png):
    target = validate_image_target(image_target)
    width, height = png_dimensions(screenshot_png)
    if not isinstance(answer, dict) or answer.get("status") not in {"matched", "not_found", "ambiguous"}:
        raise OperationError("이미지 비교 결과를 확인하지 못했습니다.", "image_match_failed")
    details = {"screenshot": {"width": width, "height": height}, "capture_window": copy.deepcopy(target["capture_window"]),
               "threshold": target["min_score"], "ambiguity_margin": target["ambiguity_margin"]}
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
    if answer["status"] != "matched":
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
    # This location is ephemeral and belongs solely to the supplied screenshot.
    x = min(rect["x"]+rect["width"]-1, rect["x"]+int(target["anchor"]["x"]*rect["width"]))
    y = min(rect["y"]+rect["height"]-1, rect["y"]+int(target["anchor"]["y"]*rect["height"]))
    return {**details, "status": "matched", "x": x, "y": y, "score": score, "rect": dict(rect), "screenshot": screenshot}


def match_image(runtime, image_target, screenshot_png):
    """Launch our fixed matcher only; no business process or global input hooks."""
    from learning import ElementLibrary
    target = validate_image_target(image_target)
    png_dimensions(screenshot_png)
    runtime.check_active()
    helper = Path(__file__).resolve().with_name(HELPER_NAME)
    ElementLibrary._reject_link(helper, file=True)
    if not helper.is_file():
        raise OperationError("이미지 도구 실행파일이 없습니다. 전체 배포 ZIP으로 업데이트하세요.", "image_helper_missing")
    # A fresh capture is mandatory at every caller. Reuse the expensive global
    # search only when *all* screenshot bytes, template settings and helper
    # version are identical. Region-only reuse could miss a new duplicate.
    stat = helper.stat()
    key = hashlib.sha256((json.dumps(target, sort_keys=True, separators=(",", ":")) + "\0" + screenshot_png
        + "\0" + str(stat.st_mtime_ns) + ":" + str(stat.st_size)).encode("utf-8")).hexdigest()
    cache = getattr(runtime, "_image_match_cache", {})
    if isinstance(cache, dict) and key in cache:
        runtime.check_active()
        return {**copy.deepcopy(cache[key]), "search_reused": True}
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
        deadline = time.monotonic()+22
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
        validated = validate_match(answer, target, screenshot_png)
        cache = dict(cache) if isinstance(cache, dict) else {}
        while len(cache) >= 8:
            cache.pop(next(iter(cache)))
        cache[key] = copy.deepcopy(validated)
        runtime._image_match_cache = cache
        return {**validated, "search_reused": False}
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
