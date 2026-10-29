"""Optional, offline Airtest candidate search with the MCP's stricter RGB gate.

Only supplied PNG bytes are read. There are no window, input, capture, network,
model, or Airtest device APIs. Callers retain fresh-capture/geometry validation.
The complete Airtest distribution is deliberately not imported: its device
dependencies do not support our Python 3.13 runtime.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from image_targets import png_dimensions, validate_image_target

RUNTIME_DIRECTORY = "image_backend_runtime"
_modules = None
_lock = threading.Lock()


class BackendUnavailable(RuntimeError):
    pass


class _ImportOutput(io.TextIOBase):
    """Discard dependency diagnostics; stdout belongs exclusively to MCP JSON."""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def available():
    return (Path(__file__).resolve().parent/RUNTIME_DIRECTORY/"BACKEND-MANIFEST.json").is_file()


def _load():
    global _modules
    if _modules is not None:
        return _modules
    with _lock:
        if _modules is not None:
            return _modules
        root = Path(__file__).resolve().parent/RUNTIME_DIRECTORY
        try:
            manifest = json.loads((root/"BACKEND-MANIFEST.json").read_text(encoding="utf-8"))
            if root.is_symlink() or manifest.get("format") != "computer-image-backend/v1" or manifest.get("airtest") != "1.4.3":
                raise ValueError("Unexpected runtime")
            # Never silently use a globally installed vision or input package.
            for name in ("cv2", "numpy", "PIL", "airtest"):
                existing = sys.modules.get(name)
                if existing and not Path(existing.__file__).resolve().is_relative_to(root.resolve()):
                    raise ValueError("Image dependency already loaded outside bundled runtime")
            sys.path.insert(0, str(root))
            try:
                # OpenCV's loader prints pip advice to stdout before raising
                # when NumPy is missing. A recoverable optional-backend failure
                # must not corrupt MCP's JSON stream. The server holds its own
                # output_stream reference; protocol responses do not use this
                # temporary dependency-only stdout binding.
                with redirect_stdout(_ImportOutput()):
                    cv2 = importlib.import_module("cv2")
                    np = importlib.import_module("numpy")
                    matcher = importlib.import_module("airtest.aircv.template_matching").TemplateMatching
            finally:
                sys.path.remove(str(root))
            cv2.setNumThreads(1)
            _modules = cv2, np, matcher
            return _modules
        except (OSError, ValueError, ImportError) as error:
            raise BackendUnavailable("Bundled offline image backend is unavailable") from error


def _same(a, b):
    ax, ay, aw, ah = (a["rect"][key] for key in ("x", "y", "width", "height"))
    bx, by, bw, bh = (b["rect"][key] for key in ("x", "y", "width", "height"))
    overlap = max(0, min(ax+aw, bx+bw)-max(ax, bx))*max(0, min(ay+ah, by+bh)-max(ay, by))
    tolerance = max(2, min(aw, ah, bw, bh)*.10)
    return (overlap/max(1, aw*ah+bw*bh-overlap) >= .72
            and abs(ax+aw/2-bx-bw/2) <= tolerance and abs(ay+ah/2-by-bh/2) <= tolerance)


def _scales(target, width, height):
    source = target.get("source_size", {"width": target["width"], "height": target["height"]})
    capture = target["capture_window"]
    rx, ry = width/capture["width"], height/capture["height"]
    ratio = (rx+ry)/2 if abs(rx-ry)/max(rx, ry) < .12 else 1
    source_scale = source["width"]/target["width"]
    near_border = abs(capture["width"]-width) <= 4 and abs(capture["height"]-height) <= 4
    # A missing frame border is not evidence that every client pixel shrank.
    # Retain source scale for tiny deltas instead of interpolating text by 1px.
    if near_border:
        ratio = 1
    seen = set()
    for factor in (1, ratio, .75, .85, 1.15, 1.25, 1.5, ratio*.9, ratio*1.1):
        w, h = round(source["width"]*factor), round(source["height"]*factor)
        clip = near_border and factor == 1 and all(
            n <= extent or n == source[key] == capture[key] and n-extent <= 4
            for n, extent, key in ((w, width, "width"), (h, height, "height")))
        if (w, h) in seen or min(w, h) < 8 or not .2 <= factor*source_scale <= 16 or (w > width or h > height) and not clip:
            continue
        seen.add((w, h))
        yield w, h, factor*source_scale, max(0, w-width)//2, max(0, h-height)//2
        # WGC includes the outer frame pixel that DWM screenshots omit.
        # An edge-anchored partial template can extend just outside the new
        # frame too, even though its overall size is smaller than the window.
        if near_border and factor == 1:
            cx = (capture["width"]-width)//2 if 0 < capture["width"]-width <= 4 and w < width else 0
            cy = (capture["height"]-height)//2 if 0 < capture["height"]-height <= 4 and h < height else 0
            for dx, dy in ((cx, 0), (0, cy), (cx, cy),
                           (cx+1 if cx else 0, cy), (cx, cy+1 if cy else 0),
                           (cx+1 if cx else 0, cy+1 if cy else 0)):
                if (dx or dy) and (w,h,dx,dy) not in seen:
                    seen.add((w,h,dx,dy))
                    yield w,h,factor*source_scale,dx,dy


def _rgb_candidates(image_target, screenshot_png, *, deadline=None, check_active=None):
    """Return a native-compatible result; ambiguity never returns coordinates.

    Airtest supplies NCC candidate maps, but NCC is not an interchangeable
    probability/score. Full RGB RMS must also pass the existing >=.94 threshold.
    Search is global across the supplied frame and every permitted scale.
    """
    target = validate_image_target(image_target)
    width, height = png_dimensions(screenshot_png)
    cv2, np, TemplateMatching = _load()
    start = time.monotonic()
    deadline = min(deadline, start+15) if deadline is not None else start+15
    def active():
        if check_active:
            check_active()
        if time.monotonic() >= deadline:
            raise TimeoutError("Offline image matching exceeded its deadline")
    active()
    pattern = cv2.imdecode(np.frombuffer(base64.b64decode(target["template_png"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    screen = cv2.imdecode(np.frombuffer(base64.b64decode(screenshot_png), dtype=np.uint8), cv2.IMREAD_COLOR)
    if pattern is None or screen is None or screen.shape[:2] != (height, width):
        raise ValueError("PNG decode dimensions differ from validated metadata")
    result = {"status": "not_found", "screenshot": {"width": width, "height": height},
              "backend": "airtest-template-rgb", "candidate_count": 0, "score": 0.0}
    step = max(1, min(pattern.shape[:2])//64)
    detail = pattern[::step, ::step].astype(np.float32).mean(axis=2)
    edge = max(float(np.abs(np.diff(detail, axis=0)).mean()), float(np.abs(np.diff(detail, axis=1)).mean()))
    if float(detail.var()) < 36 or edge < 1.2:
        return {**result, "code": "template_low_detail"}
    def raster(spec):
        full_w, full_h, _, clip_x, clip_y = spec
        resized = pattern if (full_w, full_h) == (target["width"], target["height"]) else cv2.resize(pattern, (full_w, full_h), interpolation=cv2.INTER_CUBIC)
        return resized[clip_y:clip_y+min(height, full_h), clip_x:clip_x+min(width, full_w)].copy()

    def rgb_matrix(resized):
        h, w = resized.shape[:2]
        squared = cv2.matchTemplate(screen, resized, cv2.TM_SQDIFF)
        return 1-np.sqrt(np.maximum(squared, 0)/(w*h*3))/255

    def exact_rgb(resized, point):
        x, y = point
        h, w = resized.shape[:2]
        delta = screen[y:y+h, x:x+w].astype(np.float32)-resized.astype(np.float32)
        return max(0., min(1., 1-float(np.sqrt(np.mean(delta*delta)))/255))

    def review_candidates():
        """Structural suggestions only; never a match or an input coordinate.

        Value/highlight changes can destroy RGB agreement while surrounding
        labels/borders remain. Show a bounded shortlist for explicit review,
        without treating edge correlation as probability or relaxing RGB gates.
        """
        suggestions = []
        screen_edges = cv2.Canny(screen, 60, 160)
        for spec in list(_scales(target, width, height))[:2]:
            if time.monotonic() > deadline-1: break
            active()
            resized = raster(spec)
            edges = cv2.Canny(resized, 60, 160)
            if np.count_nonzero(edges) < 20: continue
            matrix = cv2.matchTemplate(screen_edges, edges, cv2.TM_CCOEFF_NORMED)
            h, w = edges.shape
            for _ in range(3):
                _, correlation, _, point = cv2.minMaxLoc(matrix)
                if not math.isfinite(correlation) or correlation < .6: break
                x,y = point
                candidate = {"rect":{"x":x,"y":y,"width":w,"height":h},
                    "score":exact_rgb(resized, point), "structure_score":float(correlation),
                    "review_only":True}
                if not any(_same(candidate, old) for old in suggestions): suggestions.append(candidate)
                matrix[max(0,y-h//2):y+h//2+1,max(0,x-w//2):x+w//2+1] = -1
        return sorted(suggestions,key=lambda item:-item["structure_score"])[:3]

    # A mostly white field can have thousands of .91 runner-up locations but
    # no .94 match at any scale. Establish whether a winning score is possible
    # before spending the uniqueness budget on those background locations.
    # The full RGB maps cover every origin; no foreground/text is guessed away.
    ranked = []
    highest_evaluated = None
    for spec in _scales(target, width, height):
        active()
        resized = raster(spec)
        _, upper, _, point = cv2.minMaxLoc(rgb_matrix(resized))
        if not math.isfinite(upper):
            raise ValueError("Image score is not finite")
        score = exact_rgb(resized, point)
        highest_evaluated = score if highest_evaluated is None else max(highest_evaluated, score)
        ranked.append((upper, spec))
    if not ranked or (max(upper for upper, _ in ranked) < target["min_score"]-.00001
                      and (highest_evaluated is None or highest_evaluated < target["min_score"])):
        return {**result, "score": highest_evaluated or 0.0, "review_candidates":review_candidates()}
    # A genuine high-score scale is checked first. Once present, only candidates
    # inside its unchanged ambiguity margin can affect the result.
    ranked.sort(key=lambda item: item[0], reverse=True)
    best = []
    for _, spec in ranked:
        full_w, full_h, scale, clip_x, clip_y = spec
        active()
        resized = raster(spec)
        h, w = resized.shape[:2]
        # Call the actual reviewed Airtest implementation, not a reimplementation
        # labelled Airtest. Its public best-only matcher lacks duplicate checks.
        correlations = TemplateMatching(resized, screen, threshold=.75, rgb=False)._get_template_result_matrix()
        # NCC-only candidate enumeration can overlook a second region that is
        # close in RGB but lower in correlation. Search the complete RGB error
        # map as well, so an apparently unique NCC hit cannot bypass ambiguity.
        matrix = rgb_matrix(resized)
        for index in range(256):
            active()
            _, estimated_score, _, point = cv2.minMaxLoc(matrix)
            floor = max(target["min_score"], best[0]["score"] if best else 0)-target["ambiguity_margin"]
            if not math.isfinite(estimated_score) or estimated_score < floor-.00001:
                break
            x, y = point
            score = exact_rgb(resized, point)
            highest_evaluated = score if highest_evaluated is None else max(highest_evaluated, score)
            edge_valid = not (
                (clip_x and full_w < width and x != clip_x-(target["capture_window"]["width"]-width)//2)
                or (clip_y and full_h < height and y != clip_y-(target["capture_window"]["height"]-height)//2))
            if edge_valid and score >= target["min_score"]-target["ambiguity_margin"]:
                candidate = {"rect": {"x": x, "y": y, "width": w, "height": h}, "score": score, "scale": scale,
                             "correlation": float(correlations[y, x])}
                if full_w != w or full_h != h:
                    candidate["template_clip"] = {"left": clip_x, "right": full_w-w-clip_x,
                        "top": clip_y, "bottom": full_h-h-clip_y, "original_width": full_w, "original_height": full_h}
                duplicate = next((old for old in best if _same(candidate, old)), None)
                if duplicate is None or score > duplicate["score"]:
                    if duplicate is not None:
                        best.remove(duplicate)
                    best.append(candidate)
                    best.sort(key=lambda value: (-value["score"], value["rect"]["y"], value["rect"]["x"]))
                    del best[16:]
            # Every suppressed neighbor must represent the same target under
            # _same(). For tiny icons even a one-pixel diagonal can be distinct.
            radius = int(min(w, h)*.05)
            matrix[max(0, y-radius):y+radius+1, max(0, x-radius):x+radius+1] = -1
        else:
            # We did not establish uniqueness. Never choose one of many repeated
            # patterns just because the enumeration budget was exhausted.
            incomplete = {**result, "status": "ambiguous", "code": "candidate_budget_exceeded",
                "search_complete": False, "candidate_evaluations": 256, "candidate_budget": 256}
            incomplete.pop("candidate_count", None)
            if highest_evaluated is None:
                incomplete.pop("score", None)
            else:
                incomplete["score"] = highest_evaluated
            return incomplete
    active()
    result["candidate_count"] = len(best)
    if best:
        result["score"] = best[0]["score"]
    if len(best) > 1:
        result["second_score"] = best[1]["score"]
    if not best or best[0]["score"] < target["min_score"]:
        return {**result, "review_candidates":review_candidates()}
    if len(best) > 1 and best[0]["score"]-best[1]["score"] < target["ambiguity_margin"]:
        return {**result, "status": "ambiguous"}
    if not math.isfinite(best[0]["correlation"]) or best[0]["correlation"] < .75:
        return result
    return {**result, **best[0], "status": "matched"}


def _selection_bands(frame, *, dark):
    """Find short, rectangular blue selection bands, not arbitrary blue ink.

    These are appearance candidates only. _ink_agrees still proves the glyphs,
    their contrast, the clicked row and the unchanged surrounding content.
    """
    cv2, np, _ = _load()
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    levels = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    colored = ((b-r > (60 if dark else 35)) & (g-r > (25 if dark else 15))
               & (levels >= (45 if dark else 145)))
    if dark:
        colored &= levels <= 180
    rows = colored.mean(axis=1) >= .55
    bands = []
    start = 0
    while start < len(rows):
        if not rows[start]:
            start += 1
            continue
        stop = start+1
        while stop < len(rows) and rows[stop]:
            stop += 1
        if 10 <= stop-start <= 48:
            # Background must extend consistently across the band. A large
            # blue icon/triangle is not evidence of a selected menu row.
            columns = np.flatnonzero(colored[start:stop].mean(axis=0) >= .70)
            if len(columns) >= frame.shape[1]*.55:
                left, right = int(columns[0]), int(columns[-1])+1
                left = 0 if left <= 2 else left
                right = frame.shape[1] if right >= frame.shape[1]-2 else right
                if (right-left >= frame.shape[1]*.55
                        and colored[start:stop, left:right].mean() >= .70):
                    bands.append((start, stop, left, right))
        start = stop
    return bands


def _appearance_frame(frame, *, allow_selection_normalization=False):
    """Normalize bounded selection backgrounds while preserving menu ink."""
    cv2, np, _ = _load()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    backgrounds = np.zeros(frame.shape[:2], dtype=bool)
    for y, row in enumerate(frame):
        colors, counts = np.unique(row, axis=0, return_counts=True)
        index = int(counts.argmax()); bg = colors[index]
        if counts[index] >= len(row)*.55 and int(bg.min()) >= 145:
            level = float(cv2.cvtColor(bg.reshape(1,1,3), cv2.COLOR_BGR2GRAY)[0,0])
            gray[y] = np.minimum(255, gray[y]*255/level)
            backgrounds[y] = np.max(np.abs(row.astype(np.int16)-bg.astype(np.int16)),axis=1) <= 8
    for start, stop, left, right in (_selection_bands(frame, dark=True) if allow_selection_normalization else []):
        for y in range(start, stop):
            row = frame[y].astype(np.int16)
            colors, counts = np.unique(frame[y, left:right], axis=0, return_counts=True)
            bg = colors[int(counts.argmax())]
            level = float(cv2.cvtColor(bg.reshape(1, 1, 3), cv2.COLOR_BGR2GRAY)[0, 0])
            if not 45 <= level <= 180:
                continue
            normalized = 255-np.clip((gray[y]-level)*255/(255-level), 0, 255)
            neutral_grid = ((row.max(axis=1)-row.min(axis=1) <= 3)
                            & (row.min(axis=1) >= 145) & (row.max(axis=1) <= 225))
            selected = np.zeros(frame.shape[1], dtype=bool)
            selected[left:right] = True
            selected &= ~neutral_grid
            gray[y, selected] = normalized[selected]
        # Windows focus dots alternate between selection blue and neutral
        # pixels. Remove only that perimeter pattern, not blue interior ink.
        for edge_y in (start-1, stop):
            if not 0 <= edge_y < frame.shape[0]:
                continue
            row = frame[edge_y, left:right].astype(np.int16)
            selection = (row[:, 0]-row[:, 2] > 60) & (row[:, 1]-row[:, 2] > 25)
            neutral = (row.max(axis=1)-row.min(axis=1) <= 3) & (row.min(axis=1) >= 145)
            alternating = np.count_nonzero(selection[1:] != selection[:-1])
            if (selection.mean() >= .40 and neutral.mean() >= .40
                    and (selection | neutral).mean() >= .95 and alternating >= len(row)*.70):
                gray[edge_y, left:right][selection] = 255
    b,g,r = (frame[:,:,i].astype(np.int16) for i in range(3))
    blue = (b-r > 60) & (g-r > 25) & (cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)>90)
    # Only a thin connected rectangular outline qualifies. Blue text/icons
    # inside the menu are never erased, even when the template is cropped.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(blue.astype(np.uint8),8)
    for component in range(1,count):
        x,y,w,h,area = (int(v) for v in stats[component])
        if w < 40 or h < 10 or x > 2 or x+w < frame.shape[1]-2: continue
        region = labels[y:y+h,x:x+w] == component
        interior = region[2:-2,2:-2]
        filled_row = h <= 48 and area >= w*h*.70 and backgrounds[y:y+h,x:x+w].mean() >= .55
        if interior.any() and not filled_row: continue
        if min(float(region[:2,:].any(axis=0).mean()), float(region[-2:,:].any(axis=0).mean()),
               float(region[:,:2].any(axis=1).mean()),float(region[:,-2:].any(axis=1).mean())) < .9:
            continue
        outline = region.copy()
        outline[2:-2, 2:-2] = False
        gray[y:y+h,x:x+w][outline] = 255
    result = np.rint(gray).astype(np.uint8)
    return cv2.cvtColor(result,cv2.COLOR_GRAY2BGR), backgrounds


def _appearance_png(encoded, *, allow_selection_normalization=False):
    cv2, np, _ = _load()
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), dtype=np.uint8), cv2.IMREAD_COLOR)
    normalized, _ = _appearance_frame(frame, allow_selection_normalization=allow_selection_normalization)
    ok, data = cv2.imencode(".png", normalized)
    if not ok: raise ValueError("PNG normalization failed")
    return base64.b64encode(data).decode("ascii")


def _selection_text_tints(frame, *, allow_selection_normalization=False):
    """Only antialias mixtures of a selection background and white/black ink."""
    _, np, _ = _load()
    allowed = np.zeros(frame.shape[:2], dtype=bool)
    for dark in ((False, True) if allow_selection_normalization else (False,)):
        for start, stop, left, right in _selection_bands(frame, dark=dark):
            for y in range(start, stop):
                row = frame[y, left:right]
                colors, counts = np.unique(row, axis=0, return_counts=True)
                background = colors[int(counts.argmax())].astype(np.float32)
                direction = (255 if dark else 0)-background
                pixels = row.astype(np.float32)
                alpha = ((pixels-background)*direction).sum(axis=1)/max(1, float((direction*direction).sum()))
                expected = background+alpha[:, None]*direction
                allowed[y, left:right] = ((alpha >= 0) & (alpha <= 1)
                    & (np.max(np.abs(pixels-expected), axis=1) <= 8))
    return allowed


def _ink_diagnostics(pattern, current, anchor_y, *, allow_selection_normalization=False):
    """Return per-call measurements without changing any appearance gate.

    Only scalar evidence is retained; no pixels, labels, coordinates or shared
    last-error state escape this comparison. The first failing gate preserves
    the original evaluation order even when later metrics are also available.
    """
    cv2, np, _ = _load()
    if pattern.shape != current.shape:
        return {"accepted": False, "reason": "shape_mismatch", "scope": "whole_template"}
    a, _ = _appearance_frame(pattern, allow_selection_normalization=allow_selection_normalization)
    b, _ = _appearance_frame(current, allow_selection_normalization=allow_selection_normalization)
    ia = (255-a.astype(np.float32).mean(axis=2))
    ib = (255-b.astype(np.float32).mean(axis=2))
    # Grayscale normalization must not erase a meaningful colored icon/text.
    # Selection backgrounds and removed outlines have no remaining strong ink.
    colors_a, colors_b = pattern.astype(np.int16), current.astype(np.int16)
    colored_ink_a = (colors_a.max(axis=2)-colors_a.min(axis=2) > 25) & (ia >= 127)
    colored_ink_b = (colors_b.max(axis=2)-colors_b.min(axis=2) > 25) & (ib >= 127)
    neutral_a = colors_a.max(axis=2)-colors_a.min(axis=2) <= 3
    neutral_b = colors_b.max(axis=2)-colors_b.min(axis=2) <= 3
    tint_a = _selection_text_tints(pattern, allow_selection_normalization=allow_selection_normalization)
    tint_b = _selection_text_tints(current, allow_selection_normalization=allow_selection_normalization)
    text_antialias = ((neutral_a & tint_b) | (neutral_b & tint_a) | (tint_a & tint_b))
    changed_color = ((colored_ink_a | colored_ink_b)
                     & (np.max(np.abs(colors_a-colors_b), axis=2) > 25) & ~text_antialias)
    changed_color_pixels = int(changed_color.sum())
    # A small changed glyph (e.g. an added decimal point) can disappear in
    # the aggregate Dice score. Preserve strong ink topology while allowing
    # one-pixel antialias fringes around the same strokes.
    core_a, core_b = ia >= 127, ib >= 127
    kernel = np.ones((3,3), dtype=np.uint8)
    near_a = cv2.dilate(core_a.astype(np.uint8), kernel) != 0
    near_b = cv2.dilate(core_b.astype(np.uint8), kernel) != 0
    missing_ink = (core_a & ~near_b & (ib <= 35)) | (core_b & ~near_a & (ia <= 35))
    missing_ink_pixels = int(missing_ink.sum())
    diagnostic = {"accepted": True, "reason": "accepted", "scope": "whole_template",
                  "changed_color_pixels": changed_color_pixels, "missing_ink_pixels": missing_ink_pixels}
    def reject(reason, scope):
        if diagnostic["accepted"]:
            diagnostic.update(accepted=False, reason=reason, scope=scope)
    if changed_color_pixels >= 2:
        reject("colored_ink_changed", "whole_template")
    if missing_ink_pixels >= 2:
        reject("missing_ink", "whole_template")
    center = min(a.shape[0]-1, max(0, int(anchor_y*a.shape[0])))
    dark_bands = (_selection_bands(pattern, dark=True)+_selection_bands(current, dark=True)
                  if allow_selection_normalization else [])
    light_bands = _selection_bands(pattern, dark=False)+_selection_bands(current, dark=False)
    # Dark inversion requires surrounding menu context. A whole blue banner
    # or single cropped status badge cannot be proved to be a selection.
    if dark_bands and any(start < 8 and stop > a.shape[0]-8 for start, stop, _, _ in dark_bands):
        reject("selection_context_missing", "whole_template")
    inverted = any(start <= center < stop for start, stop, _, _ in dark_bands)
    light = any(start <= center < stop for start, stop, _, _ in light_bands)
    diagnostic.update(clicked_dark_selection=inverted, clicked_light_selection=light)
    for scope, left, right, minimum in (("whole_template", ia, ib, .98),
            ("clicked_band", ia[max(0,center-8):center+9], ib[max(0,center-8):center+9], .95)):
        mask = (left > 35) | (right > 35)
        metrics = {"ink_pixels": int(mask.sum()), "pattern_ink_pixels": int((left > 35).sum()),
                   "current_ink_pixels": int((right > 35).sum()), "minimum_ink_pixels": 12,
                   "minimum_dice": minimum, "maximum_mean_difference": 10,
                   "maximum_contrast_difference": 40}
        diagnostic[scope] = metrics
        if metrics["ink_pixels"] < 12 or not metrics["pattern_ink_pixels"] or not metrics["current_ink_pixels"]:
            metrics["accepted"] = False
            metrics["reason"] = "insufficient_ink"
            reject("insufficient_ink", scope)
            continue
        dice = 2*float(np.minimum(left,right).sum())/max(1,float(left.sum()+right.sum()))
        difference = float(np.abs(left-right)[mask].mean())
        correlation = float(cv2.matchTemplate(left.astype(np.float32), right.astype(np.float32),
                                              cv2.TM_CCOEFF_NORMED)[0, 0])
        contrast_difference = abs(float(np.percentile(left[left > 35], 90))
                                  - float(np.percentile(right[right > 35], 90)))
        metrics.update(dice=dice, mean_difference=difference,
                       contrast_difference=contrast_difference)
        if math.isfinite(correlation):
            metrics["correlation"] = correlation
        metrics["accepted"] = True
        metrics["reason"] = "accepted"
        metrics["selection_tolerance_used"] = False
        if dice < minimum or difference > 10:
            # Selection color evidence must be on the clicked band; a blue
            # row elsewhere cannot relax this row's text or disabled contrast.
            contrast = contrast_difference <= 40
            contrast_agrees = ((inverted and dice >= .85 and difference <= 35 and correlation >= .96)
                               or (light and dice >= .93 and difference <= 10 and correlation >= .99))
            if not (minimum == .95 and contrast and contrast_agrees and math.isfinite(correlation)):
                metrics.update(accepted=False, reason="ink_metrics_changed")
                reject("ink_metrics_changed", scope)
            else:
                metrics["selection_tolerance_used"] = True
    return diagnostic


def _ink_agrees(pattern, current, anchor_y, *, allow_selection_normalization=False):
    """Compatibility wrapper; all diagnostics belong to the individual call."""
    return _ink_diagnostics(pattern, current, anchor_y,
        allow_selection_normalization=allow_selection_normalization)["accepted"]


def match_candidates(image_target, screenshot_png, *, deadline=None, check_active=None,
                     allow_selection_normalization=False):
    if type(allow_selection_normalization) is not bool:
        raise ValueError("Invalid private selection appearance capability")
    end = min(deadline, time.monotonic()+15) if deadline is not None else time.monotonic()+15
    original = _rgb_candidates(image_target, screenshot_png, deadline=end, check_active=check_active)
    if original["status"] != "not_found" or original.get("code") == "template_low_detail":
        return original
    target = validate_image_target(image_target)
    width, height = png_dimensions(screenshot_png)
    if (0 < target["capture_window"]["width"]-width <= 4
            and 0 < target["capture_window"]["height"]-height <= 4):
        # Apply the same bounded edge rule to right/bottom frame templates.
        # Reflection preserves pixels; it does not blur text or lower scores.
        cv2, np, _ = _load()
        def flip_png(value, axis):
            pixels = cv2.imdecode(np.frombuffer(base64.b64decode(value),dtype=np.uint8),cv2.IMREAD_COLOR)
            return base64.b64encode(cv2.imencode(".png",cv2.flip(pixels,axis))[1]).decode()
        alternatives=[]
        for axis in (1,0,-1):
            flipped=dict(target,template_png=flip_png(target["template_png"],axis))
            candidate=_rgb_candidates(flipped,flip_png(screenshot_png,axis),deadline=end,check_active=check_active)
            if candidate["status"]=="ambiguous": return candidate
            if candidate["status"]!="matched" or not candidate.get("template_clip"): continue
            rect=candidate["rect"]; clip=candidate["template_clip"]
            if axis in (1,-1):
                rect["x"]=width-rect["x"]-rect["width"]
                clip["left"],clip["right"]=clip["right"],clip["left"]
            if axis in (0,-1):
                rect["y"]=height-rect["y"]-rect["height"]
                clip["top"],clip["bottom"]=clip["bottom"],clip["top"]
            if not any(_same(candidate,old) for old in alternatives): alternatives.append(candidate)
        if len(alternatives)>1:
            return {**original,"status":"ambiguous","candidate_count":len(alternatives)}
        if alternatives: return alternatives[0]
    normalized = dict(target, template_png=_appearance_png(target["template_png"],
        allow_selection_normalization=allow_selection_normalization))
    result = _rgb_candidates(normalized, _appearance_png(screenshot_png,
        allow_selection_normalization=allow_selection_normalization), deadline=end, check_active=check_active)
    if result["status"] == "ambiguous":
        return {**result, "backend": "airtest-light-background", "original_score": original.get("score"),
                "appearance_score": result.get("score")}
    if result["status"] != "matched" or result.get("template_clip"):
        return {**original, "original_score": original.get("score"), "appearance_score": result.get("score")}
    cv2, np, _ = _load()
    pattern = cv2.imdecode(np.frombuffer(base64.b64decode(target["template_png"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    screen = cv2.imdecode(np.frombuffer(base64.b64decode(screenshot_png), dtype=np.uint8), cv2.IMREAD_COLOR)
    rect = result["rect"]; x,y,w,h = (rect[k] for k in ("x","y","width","height"))
    if pattern.shape[:2] != (h,w):
        pattern = cv2.resize(pattern, (w,h), interpolation=cv2.INTER_CUBIC)
    appearance = _ink_diagnostics(pattern, screen[y:y+h,x:x+w], target["anchor"]["y"],
                                  allow_selection_normalization=allow_selection_normalization)
    if not appearance["accepted"]:
        return {**original, "appearance_rejected": "foreground_ink_changed", "appearance_score": result.get("score"),
                "original_score": original.get("score"), "appearance_diagnostic": appearance}
    if check_active: check_active()
    dark_selection = allow_selection_normalization and (
        _selection_bands(pattern, dark=True) or _selection_bands(screen[y:y+h,x:x+w], dark=True))
    return {**result, "backend": "airtest-list-selection" if dark_selection else "airtest-light-background",
            "original_score": original.get("score"),
            "appearance_score": result.get("score"), "appearance_diagnostic": appearance,
            "appearance_verified": True}


def match_isolated(runtime, image_target, screenshot_png, *, deadline=None, check_active=None,
                   allow_selection_normalization=False):
    """Bound native-library startup and matching outside the MCP worker thread.

    Some Windows/NumPy combinations hang while importing an extension from the
    server's background thread. The child imports on its main thread; even a
    broken binary can now be cancelled/killed without trapping the MCP server.
    """
    from learning import ElementLibrary
    if type(allow_selection_normalization) is not bool:
        raise ValueError("Invalid private selection appearance capability")
    target = validate_image_target(image_target)
    png_dimensions(screenshot_png)
    end = min(deadline, time.monotonic()+20) if deadline is not None else time.monotonic()+20
    check = check_active or runtime.check_active
    check()
    root = Path(runtime.run_dir)/"im"
    ElementLibrary._reject_link(root)
    root.mkdir(exist_ok=True)
    nonce = uuid.uuid4().hex+uuid.uuid4().hex
    request, response = root/(nonce[:16]+".air.in"), root/(nonce[:16]+".air.out")
    module = Path(__file__).resolve()
    ElementLibrary._reject_link(module, file=True)
    child = None
    try:
        with request.open("x", encoding="utf-8") as stream:
            json.dump({"nonce": nonce, "target": target, "screenshot_png": screenshot_png,
                       "allow_selection_normalization": allow_selection_normalization}, stream)
        # -I excludes user site/PYTHONPATH. Explicitly add only our own source
        # directory; selected vision dependencies are loaded from its bundle.
        entry = "import runpy,sys; root,script=sys.argv[1:3]; sys.argv=sys.argv[2:]; sys.path.insert(0,root); runpy.run_path(script,run_name='__main__')"
        child = subprocess.Popen([sys.executable, "-I", "-c", entry, str(module.parent), str(module),
                                  "--image-worker", str(request.resolve()), str(response.resolve())],
            cwd=str(module.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        while child.poll() is None:
            check()
            if time.monotonic() >= end:
                raise TimeoutError("Offline image worker startup or matching timed out")
            runtime.stop_event.wait(.03)
        check()
        ElementLibrary._reject_link(response, file=True)
        if child.returncode or not response.is_file() or response.stat().st_size > 64000:
            raise BackendUnavailable("Offline image worker did not return a result")
        answer = json.loads(response.read_text(encoding="utf-8"))
        if not isinstance(answer, dict) or answer.pop("nonce", None) != nonce:
            raise ValueError("Offline image worker response identity mismatch")
        if answer.get("status") == "failed":
            raise BackendUnavailable("Offline image worker could not load or compare images")
        return answer
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
            except (OSError, ValueError):
                pass


def _worker_main(arguments):
    if len(arguments) != 3 or arguments[0] != "--image-worker":
        return 2
    request, response = map(Path, arguments[1:])
    if not request.is_file() or request.stat().st_size > 32*1024*1024 or response.exists():
        return 2
    value = json.loads(request.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"nonce", "target", "screenshot_png", "allow_selection_normalization"}:
        return 2
    nonce = value.get("nonce")
    if type(value.get("allow_selection_normalization")) is not bool:
        return 2
    if not isinstance(nonce, str) or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        return 2
    try:
        result = match_candidates(value["target"], value["screenshot_png"],
                                  allow_selection_normalization=value["allow_selection_normalization"])
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__}
    with response.open("x", encoding="utf-8") as stream:
        json.dump({**result, "nonce": nonce}, stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
