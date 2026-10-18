"""Compare saved PNG matching only; never capture a screen or inject input."""
from __future__ import annotations
import argparse
import base64
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import airtest_backend


def encoded(array, cv2):
    return base64.b64encode(cv2.imencode(".png", array)[1]).decode()


def target_for(template, capture, cv2, source=None):
    h, w = template.shape[:2]
    return {"format": "computer-image-target/v1", "template_png": encoded(template, cv2),
        "width": w, "height": h, "anchor": {"x": .75, "y": .5},
        "capture_window": {"width": capture[0], "height": capture[1]},
        "min_score": .94, "ambiguity_margin": .03,
        **({"source_size": {"width": source[0], "height": source[1]}} if source else {})}


def cases(mumu=None):
    cv2, np, _ = airtest_backend._load()
    template = np.full((40, 92, 3), (40, 80, 145), np.uint8)
    cv2.rectangle(template, (0, 0), (91, 39), (230, 230, 230), 2)
    cv2.putText(template, "RUN 1", (7, 27), cv2.FONT_HERSHEY_SIMPLEX, .65, (255, 255, 255), 1, cv2.LINE_AA)
    target = target_for(template, (640, 400), cv2)
    for name, factor, duplicate in (("fixture_exact", 1, False), ("fixture_scale125", 1.25, False),
                                    ("fixture_scale150", 1.5, False), ("fixture_duplicate", 1, True)):
        screen = np.full((400, 640, 3), 25, np.uint8)
        scaled = cv2.resize(template, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        h, w = scaled.shape[:2]
        screen[83:83+h, 143:143+w] = scaled
        if duplicate:
            screen[183:183+h, 343:343+w] = scaled
        yield name, target, encoded(screen, cv2), "ambiguous" if duplicate else "matched", (143, 83, w, h)
    for name, transform in (("fixture_missing", "missing"), ("fixture_wrong_color", "color"), ("fixture_occluded", "occluded")):
        screen = np.full((400, 640, 3), 25, np.uint8)
        if transform != "missing":
            changed = template.copy()
            if transform == "color":
                changed = 255-changed
            else:
                changed[:, :50] = 25
            screen[83:123, 143:235] = changed
        yield name, target, encoded(screen, cv2), "not_found", None
    border = cv2.imread(str(ROOT/"test_data/recorded-border-screen.png"))
    border_template = cv2.imread(str(ROOT/"test_data/recorded-border-template.png"))
    if border is not None and border_template is not None:
        yield "recorded_fixture_border", target_for(border_template, (764, border.shape[0]), cv2, (764, 96)), encoded(border, cv2), "matched", None
    if mumu and Path(mumu).is_file():
        screen = cv2.imread(str(mumu))
        # These coordinates are only fixture annotations for the October 10
        # archived 1280x862 MuMu capture; never used to control a live desktop.
        if screen.shape[:2] == (862, 1280):
            for name, box, status in (("mumu_toolbar", (1025, 84, 1251, 153), "matched"),
                                      ("mumu_repeated_play", (1098, 284, 1149, 334), "ambiguous")):
                x, y, right, bottom = box
                template = screen[y:bottom, x:right].copy()
                item = target_for(template, (1280, 862), cv2)
                yield name, item, encoded(screen, cv2), status, (x, y, right-x, bottom-y)
                if status == "matched":
                    scaled = cv2.resize(screen, None, fx=.75, fy=.75, interpolation=cv2.INTER_CUBIC)
                    yield name+"_75", item, encoded(scaled, cv2), status, (round(x*.75), round(y*.75), round((right-x)*.75), round((bottom-y)*.75))


def native_match(target, screenshot, directory, helper):
    req, out = directory/"native.in.json", directory/"native.out.json"
    req.write_text(json.dumps({**target, "screenshot_png": screenshot, "nonce": "a"*64}), encoding="utf-8")
    out.unlink(missing_ok=True)
    child = subprocess.run([str(helper), "--match", str(req), str(out)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if child.returncode or not out.exists():
        return {"status": "error", "exit_code": child.returncode}
    return json.loads(out.read_text(encoding="utf-8-sig"))


def upstream_match(target, screenshot, multiscale=None):
    """Unmodified upstream best-only API, shown separately from our adapter."""
    cv2, np, TemplateMatching = airtest_backend._load()
    template = cv2.imdecode(np.frombuffer(base64.b64decode(target["template_png"]), np.uint8), cv2.IMREAD_COLOR)
    screen = cv2.imdecode(np.frombuffer(base64.b64decode(screenshot), np.uint8), cv2.IMREAD_COLOR)
    matcher = multiscale or TemplateMatching
    best = matcher(template, screen, threshold=target["min_score"], rgb=True).find_best_result()
    if not best:
        return {"status": "not_found"}
    points = best["rectangle"]
    return {"status": "matched", "score": best["confidence"], "uniqueness_checked": False,
            "rect": {"x": points[0][0], "y": points[0][1], "width": points[2][0]-points[0][0], "height": points[2][1]-points[0][1]}}


def run(output, mumu=None, upstream_source=None):
    helper = ROOT/"Computer Use MCP 이미지 도구.exe"
    rows = []
    multiscale = None
    if upstream_source:
        airtest_backend._load()
        spec = importlib.util.spec_from_file_location("airtest.aircv.benchmark_multiscale", upstream_source/"airtest/aircv/multiscale_template_matching.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        multiscale = module.MultiScaleTemplateMatching
    with tempfile.TemporaryDirectory(prefix="match-benchmark-", dir=output.parent) as temporary:
        for name, target, screenshot, expected, rect in cases(mumu):
            for backend in ("native", "airtest_upstream_best", *(('airtest_upstream_multiscale',) if multiscale else ()), "airtest_guarded", "airtest_isolated"):
                start = time.perf_counter()
                try:
                    if backend == "native":
                        answer = native_match(target, screenshot, Path(temporary), helper)
                    elif backend == "airtest_upstream_best":
                        answer = upstream_match(target, screenshot)
                    elif backend == "airtest_upstream_multiscale":
                        answer = upstream_match(target, screenshot, multiscale)
                    elif backend == "airtest_isolated":
                        runtime = SimpleNamespace(run_dir=temporary, stop_event=threading.Event(), check_active=lambda: None)
                        answer = airtest_backend.match_isolated(runtime, target, screenshot)
                    else:
                        answer = airtest_backend.match_candidates(target, screenshot)
                except Exception as error:
                    answer = {"status": "error", "code": type(error).__name__}
                elapsed = round((time.perf_counter()-start)*1000, 2)
                correct = answer["status"] == expected
                if correct and expected == "matched" and rect:
                    actual = answer["rect"]
                    correct = all(abs(actual[key]-value) <= 2 for key, value in zip(("x", "y", "width", "height"), rect))
                row = {"case": name, "backend": backend, "expected": expected, "correct": correct,
                       "elapsed_ms": elapsed, "result": answer}
                rows.append(row)
                print(json.dumps({key: row[key] for key in ("case", "backend", "correct", "elapsed_ms")}), flush=True)
                output.write_text(json.dumps({"format": "image-backend-benchmark/v1", "live_input": False, "cases": rows}, indent=2), encoding="utf-8")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mumu", type=Path)
    parser.add_argument("--upstream-source", type=Path, help="Reviewed extracted Airtest source for its unmodified multiscale comparison")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run(args.output, args.mumu, args.upstream_source)
