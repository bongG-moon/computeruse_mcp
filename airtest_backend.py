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


def _appearance_frame(frame):
    """Normalize light menu rows; erase blue outlines only at the frame edge."""
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
    b,g,r = (frame[:,:,i].astype(np.int16) for i in range(3))
    blue = (b-r > 60) & (g-r > 25) & (cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)>90)
    # Only a thin connected rectangular outline qualifies. Blue text/icons
    # inside the menu are never erased, even when the template is cropped.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(blue.astype(np.uint8),8)
    for component in range(1,count):
        x,y,w,h,area = (int(v) for v in stats[component])
        if w < 40 or h < 10: continue
        region = labels[y:y+h,x:x+w] == component
        interior = region[2:-2,2:-2]
        if interior.any(): continue
        if min(float(region[:2,:].any(axis=0).mean()), float(region[-2:,:].any(axis=0).mean()),
               float(region[:,:2].any(axis=1).mean()),float(region[:,-2:].any(axis=1).mean())) < .9:
            continue
        gray[y:y+h,x:x+w][region] = 255
    result = np.rint(gray).astype(np.uint8)
    return cv2.cvtColor(result,cv2.COLOR_GRAY2BGR), backgrounds


def _appearance_png(encoded):
    cv2, np, _ = _load()
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), dtype=np.uint8), cv2.IMREAD_COLOR)
    normalized, _ = _appearance_frame(frame)
    ok, data = cv2.imencode(".png", normalized)
    if not ok: raise ValueError("PNG normalization failed")
    return base64.b64encode(data).decode("ascii")


def _ink_agrees(pattern, current, anchor_y):
    _, np, _ = _load()
    a, _ = _appearance_frame(pattern); b, _ = _appearance_frame(current)
    ia = (255-a.astype(np.float32).mean(axis=2))
    ib = (255-b.astype(np.float32).mean(axis=2))
    center = min(a.shape[0]-1, max(0, int(anchor_y*a.shape[0])))
    for left, right, minimum in ((ia, ib, .98),
            (ia[max(0,center-8):center+9], ib[max(0,center-8):center+9], .95)):
        mask = (left > 35) | (right > 35)
        if int(mask.sum()) < 12:
            return False
        dice = 2*float(np.minimum(left,right).sum())/max(1,float(left.sum()+right.sum()))
        if dice < minimum or float(np.abs(left-right)[mask].mean()) > 10:
            return False
    return True


def match_candidates(image_target, screenshot_png, *, deadline=None, check_active=None):
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
    normalized = dict(target, template_png=_appearance_png(target["template_png"]))
    result = _rgb_candidates(normalized, _appearance_png(screenshot_png), deadline=end, check_active=check_active)
    if result["status"] == "ambiguous":
        return {**result, "backend": "airtest-light-background", "original_score": original.get("score")}
    if result["status"] != "matched" or result.get("template_clip"):
        return original
    cv2, np, _ = _load()
    pattern = cv2.imdecode(np.frombuffer(base64.b64decode(target["template_png"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    screen = cv2.imdecode(np.frombuffer(base64.b64decode(screenshot_png), dtype=np.uint8), cv2.IMREAD_COLOR)
    rect = result["rect"]; x,y,w,h = (rect[k] for k in ("x","y","width","height"))
    if pattern.shape[:2] != (h,w):
        pattern = cv2.resize(pattern, (w,h), interpolation=cv2.INTER_CUBIC)
    if not _ink_agrees(pattern, screen[y:y+h,x:x+w], target["anchor"]["y"]):
        return {**original, "appearance_rejected": "foreground_ink_changed"}
    if check_active: check_active()
    return {**result, "backend": "airtest-light-background", "original_score": original.get("score"),
            "appearance_verified": True}


def match_isolated(runtime, image_target, screenshot_png, *, deadline=None, check_active=None):
    """Bound native-library startup and matching outside the MCP worker thread.

    Some Windows/NumPy combinations hang while importing an extension from the
    server's background thread. The child imports on its main thread; even a
    broken binary can now be cancelled/killed without trapping the MCP server.
    """
    from learning import ElementLibrary
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
            json.dump({"nonce": nonce, "target": target, "screenshot_png": screenshot_png}, stream)
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
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        return 2
    try:
        result = match_candidates(value["target"], value["screenshot_png"])
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__}
    with response.open("x", encoding="utf-8") as stream:
        json.dump({**result, "nonce": nonce}, stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
