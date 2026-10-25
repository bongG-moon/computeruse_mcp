"""Controlled replay comparison. Uses a stateful fake UI; no desktop input.

Production WorkflowRunner, Operations, validation, snapshots and verified-run
profiles are exercised from each checkout. App/Driver/model latency, image
matching and interactive authoring are intentionally outside this benchmark.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parent.parent


def recipe():
    first = {"name": "첫 입력칸", "role": "Edit"}
    second = {"name": "둘째 입력칸", "role": "Edit"}
    return {"id": "local-authoring-benchmark", "revision": 1,
        "name": "Synthetic typed input replay", "instructions": "Synthetic input only",
        "expected": "Both field values equal their current inputs", "program_ids": ["editor"],
        "variables": {"family": {"type": "enum", "values": ["N", "P"]},
                      "devices": {"type": "list", "items": {"type": "text"}, "max_items": 6}},
        "steps": [{"operation": "foreach", "step_id": "device-list", "input": "devices", "item": "device",
            "steps": [
                {"operation": "set_value", "step_id": "family-device", "program_id": "editor",
                 "selector": first, "value": "${family}/${device}", "verification_timeout_ms": 0,
                 "expect": [{"selector": first, "property": "value", "equals": "${family}/${device}"}]},
                {"operation": "set_value", "step_id": "device-only", "program_id": "editor",
                 "selector": second, "value": {"input_ref": "device"}, "verification_timeout_ms": 0,
                 "expect": [{"selector": second, "property": "value", "equals": {"input_ref": "device"}}]}]}]}


def child(source, support, output, count, warm):
    """Import target production modules before explicitly loading test support."""
    sys.path.insert(0, str(source))
    imported_at = time.perf_counter()
    from workflows import WorkflowRunner
    import workflows
    if Path(workflows.__file__).resolve().parent != source:
        raise RuntimeError("Wrong production module was imported")
    spec = importlib.util.spec_from_file_location("_same_benchmark_test_support", support)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import_ms = (time.perf_counter() - imported_at) * 1000
    # The current file supplies the exact same fake runtime to both versions.
    runtime_type = module.GuardedUIRuntime
    output.mkdir(parents=True, exist_ok=True)
    runner = WorkflowRunner(output)
    task = recipe()
    inputs = {"family": "N", "devices": ["D" + str(i) for i in range(1, 7)]}
    target = [{"program_id": "editor", "pid": 100, "window_id": 200}]

    def run_one():
        runtime = runtime_type(output)
        before_modules = set(sys.modules)
        started = time.perf_counter()
        answer = runner.run(runtime, task, inputs, target, execution_mode="auto")
        elapsed = (time.perf_counter() - started) * 1000
        added_modules = sorted(set(sys.modules) - before_modules)
        calls = dict(sorted(Counter(name for name, _ in runtime.calls).items()))
        if (answer.get("task_verified") is not True or answer.get("completed_steps") != 12
                or runtime.values[(100, 200)] != {"첫 입력칸": "N/D6", "둘째 입력칸": "D6"}):
            raise RuntimeError("Synthetic workflow failed its actual postconditions: " + str(answer.get("diagnostic")))
        if set(calls) - {"get_window_state", "set_value"}:
            raise RuntimeError("Unexpected UI call in controlled fixture: " + str(calls))
        return {"wall_ms": round(elapsed, 4),
                "active_ms": answer.get("performance", {}).get("active_ms"),
                "between_calls_ms": answer.get("performance", {}).get("between_calls_ms", 0),
                "phases_ms": answer.get("performance", {}).get("phases_ms", {}),
                "mode": answer["execution"]["mode"], "completed_steps": answer["completed_steps"],
                "verified": answer["task_verified"], "calls": calls,
                "observations": calls.get("get_window_state", 0), "inputs": calls.get("set_value", 0),
                "image_captures": 0, "model_calls": 0, "fixed_delay_ms": answer["execution"]["fixed_delay_ms"],
                "new_modules_during_replay": added_modules}

    warmup = run_one() if warm else None
    samples = [run_one() for _ in range(count)]
    print(json.dumps({"source": str(source), "support": str(support), "import_ms": round(import_ms, 3),
                      "warmup": warmup, "samples": samples}, ensure_ascii=True))


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(samples):
    result = {"n": len(samples), "all_verified": all(row["verified"] for row in samples)}
    for metric in ("wall_ms", "active_ms"):
        values = [row[metric] for row in samples]
        if any(type(value) not in (float, int) for value in values):
            raise RuntimeError("Missing metric: " + metric)
        result[metric] = {"p50": round(statistics.median(values), 4), "p95": round(percentile(values, .95), 4),
                          "min": round(min(values), 4), "max": round(max(values), 4)}
    result["call_shapes"] = sorted(set(json.dumps(row["calls"], sort_keys=True) for row in samples))
    for field in ("mode", "inputs", "observations", "image_captures", "model_calls", "fixed_delay_ms"):
        result[field] = sorted(set(row[field] for row in samples))
    result["new_authoring_imports"] = sorted({name for row in samples for name in row["new_modules_during_replay"]
        if name in {"authoring_inputs", "local_skill", "local_inputs"}})
    return result


def main(args):
    baseline, current = Path(args.baseline).resolve(), Path(args.current).resolve()
    support = current / "test_workflows.py"
    run_root = Path(args.output).resolve().parent / ("benchmark-" + uuid.uuid4().hex[:12])
    run_root.mkdir(parents=True, exist_ok=False)
    samples = {name: {"cold": [], "warm": []} for name in ("baseline", "current")}
    process_samples = []
    sources = {"baseline": baseline, "current": current}
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
    for mode in ("cold", "warm"):
        block_size = 1 if mode == "cold" else min(10, args.repetitions)
        for offset in range(0, args.repetitions, block_size):
            # Alternate order to reduce monotonic machine-load/cache bias.
            names = ("baseline", "current") if (offset // block_size) % 2 == 0 else ("current", "baseline")
            for name in names:
                count = min(block_size, args.repetitions - offset)
                command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--child",
                    "--current", str(sources[name]), "--support", str(support),
                    "--output", str(run_root / (name + "-" + mode + "-" + str(offset))), "--repetitions", str(count)]
                if mode == "warm": command.append("--warm")
                started = time.perf_counter()
                finished = subprocess.run(command, cwd=sources[name], env=env, capture_output=True, text=True,
                                          encoding="utf-8", timeout=60, check=True)
                result = json.loads(finished.stdout)
                process_samples.append({"source": name, "mode": mode, "offset": offset,
                    "import_ms": result["import_ms"], "process_wall_ms": round((time.perf_counter()-started)*1000, 3)})
                samples[name][mode].extend(result["samples"])
    summaries = {name: {mode: summarize(rows) for mode, rows in modes.items()} for name, modes in samples.items()}
    comparisons = {}
    for mode in ("cold", "warm"):
        prior, after = summaries["baseline"][mode], summaries["current"][mode]
        checks = {"counts_identical": all(prior[key] == after[key] for key in
                  ("call_shapes", "mode", "inputs", "observations", "image_captures", "model_calls", "fixed_delay_ms")),
                  "no_new_authoring_imports": not after["new_authoring_imports"]}
        changes = {metric: {quantile: round((after[metric][quantile]/prior[metric][quantile]-1)*100, 2)
                            for quantile in ("p50", "p95")} for metric in ("wall_ms", "active_ms")}
        comparisons[mode] = {**checks, "change_percent": changes,
            "regression_alert": any(number >= 5 for value in changes.values() for number in value.values())}
    hashes = {}
    for file in ("workflows.py", "task_inputs.py", "operations.py"):
        values = {name: hashlib.sha256((source/file).read_bytes()).hexdigest() for name, source in sources.items()}
        hashes[file] = {**values, "identical": values["baseline"] == values["current"]}
    result = {"format": "computer-local-authoring-benchmark/v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "review_required" if any(c["regression_alert"] or not c["counts_identical"] for c in comparisons.values()) else "passed",
        "scope": "production_workflow_engine_with_stateful_synthetic_uia",
        "limitations": ["No desktop input, actual Driver, image matching, model latency or production SmartFX timings measured.",
                        "Cold means fresh Python process and fresh saved-run profile, not cold OS disk cache.",
                        "Warm means prior verified profile in the same process; each batch has one unmeasured warmup.",
                        "Process/import time is recorded separately and is not included in invocation wall_ms.",
                        "A 5% alert triggers investigation; it is not permission to introduce replay delay."],
        "recipe": {"expanded_steps": 12, "input_formats": ["typed_enum", "typed_list", "placeholder", "input_ref"],
                   "operations": ["set_value", "postcondition_read"], "repeat_count": 6},
        "sources": {name: str(source) for name, source in sources.items()},
        "same_support_sha256": hashlib.sha256(support.read_bytes()).hexdigest(),
        "source_hashes": hashes, "summaries": summaries, "comparison": comparisons,
        "samples": samples, "process_samples": process_samples}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "path": str(output), "comparison": comparisons,
                      "summaries": summaries, "source_hashes": hashes}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=str(ROOT / ".data/github-publish/repo"))
    parser.add_argument("--current", default=str(ROOT))
    parser.add_argument("--output", default=str(ROOT / ".data/local-authoring-20261025/performance.json"))
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--warm", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--support", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.child:
        child(Path(options.current).resolve(), Path(options.support).resolve(), Path(options.output).resolve(),
              options.repetitions, options.warm)
    else:
        if options.repetitions < 30: parser.error("At least 30 repetitions are required")
        main(options)
