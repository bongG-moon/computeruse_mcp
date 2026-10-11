"""Prepare phase 1 recording fixture; reuse the existing public-MCP command server.

No command here supplies mouse/keyboard input, manufactures recording events, or
claims saved-process replay. Actual demonstration and editor actions belong to
the Computer Use operator. `verify` checks independent effects only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from acceptance_record_replay015 import write
from live_validation import Client, DRIVER, decoded
from native_fixture import compiler_path, fixture_folder
from settings import VERSION, save_config

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fixtures" / "Phase1RecordingFixture.cs"


def preserve_images(response: dict, folder: Path, command_stem: str) -> list[dict]:
    """Save exact synthetic MCP image blocks, retaining their response order."""
    from PIL import Image
    images = []
    for block_index, block in enumerate(response.get("content", [])):
        if block.get("type") != "image":
            continue
        mime = block.get("mimeType")
        if mime not in {"image/png", "image/jpeg"}:
            raise ValueError("Unsupported image format in acceptance response")
        encoded = block.get("data", "")
        if not isinstance(encoded, str) or len(encoded) > 64 * 1024 * 1024:
            raise ValueError("Invalid or oversized image block")
        pixels = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(pixels)) as probe:
            width, height = probe.size
            expected = "PNG" if mime == "image/png" else "JPEG"
            if probe.format != expected:
                raise ValueError("Image bytes do not match declared MIME type")
            probe.verify()
        suffix = ".png" if mime == "image/png" else ".jpg"
        output = folder / (command_stem + "-image-" + str(len(images) + 1) + suffix)
        output.write_bytes(pixels)
        images.append({"path": str(output.resolve()), "content_index": block_index,
                       "mime_type": mime, "width": width, "height": height,
                       "sha256": hashlib.sha256(pixels).hexdigest()})
    return images


def serve(folder: Path, bundle: Path | None) -> None:
    """Existing command-server pattern, with real frame artifacts for reviews."""
    folder = fixture_folder(folder)
    config = json.loads((folder / "config.json").read_text(encoding="utf-8-sig"))
    programs = config.get("programs", [])
    if (len(programs) != 1 or programs[0].get("id") != "phase1"
            or Path(programs[0].get("exe", "")).resolve() != folder / "Phase1RecordingFixture.exe"):
        raise ValueError("This image-preserving server only accepts the isolated phase 1 fixture")
    root = bundle.resolve() if bundle else HERE
    python = root / "runtime/python.exe" if bundle else Path(sys.executable)
    from build_portable import runtime_environment
    client = Client(folder / "config.json", root / "server.py", python,
                    runtime_environment(python.parent) if bundle else None)
    write(folder / "server-ready.json", {"pid": client.child.pid, "version": VERSION,
                                        "root": str(root), "exact_response_images_preserved": True})
    try:
        seen = {path.name for path in (folder / "responses").glob("*.json")}
        while not (folder / "stop-server.flag").exists():
            for path in sorted((folder / "commands").glob("*.json")):
                if path.name in seen:
                    continue
                command = json.loads(path.read_text(encoding="utf-8-sig"))
                seen.add(path.name)
                began = time.monotonic()
                try:
                    response = client.request("tools/call", {"name": command["tool"],
                        "arguments": command.get("arguments", {})}, timeout=90)
                    frames = preserve_images(response, folder / "responses", path.stem)
                    result = {"command": command, "result": decoded(response),
                              "isError": bool(response.get("isError")), "image_count": len(frames),
                              "images": frames,
                              "text_blocks": [{"content_index": i, "text": block.get("text", "")}
                                  for i, block in enumerate(response.get("content", [])) if block.get("type") == "text"]}
                except Exception as error:
                    result = {"command": command, "exception": str(error), "traceback": traceback.format_exc()}
                result["elapsed_ms"] = round((time.monotonic() - began) * 1000, 2)
                write(folder / "responses" / path.name, result)
            time.sleep(.1)
    finally:
        client.close()


def prepare(folder: Path) -> dict:
    folder = fixture_folder(folder)
    if folder.exists():
        raise ValueError("Use a fresh isolated fixture folder")
    folder.mkdir(parents=True)
    for name in ("commands", "responses"):
        (folder / name).mkdir()
    source, exe = folder / SOURCE.name, folder / "Phase1RecordingFixture.exe"
    source.write_bytes(SOURCE.read_bytes())
    subprocess.run([
        str(compiler_path()), "/nologo", "/target:winexe", "/optimize+", "/codepage:65001",
        "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
        "/reference:System.Web.Extensions.dll", "/out:" + str(exe), str(source),
    ], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    save_config(folder / "config.json", {
        "version": 1, "driver": str(DRIVER.resolve()), "state_dir": str(folder / "state"),
        "programs": [{"id": "phase1", "name": "Phase 1 recording fixture", "exe": str(exe), "enabled": True}],
        "mode": "uia", "tool_profile": "simple", "approval": "client", "log_detail": "metadata",
        "max_minutes": 30, "max_actions": 300, "observation_timeout_seconds": 12,
    })
    manifest = {
        "version": VERSION, "synthetic_fixture": True, "gui_launched": False,
        "exe": str(exe), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "program_id": "phase1",
        "receipt": str(folder / "receipt.json"), "recording_events_fabricated": False,
        "private_application_used": False, "real_llm_used": False,
        "controls": {"native_combo": "Native dropdown", "query": "Query", "reset": "Reset fixture",
                     "small_uia_button": "Small UIA play", "image_rows": ["ALPHA 001", "BETA 002", "GAMMA 003", "DELTA 004"]},
    }
    write(folder / "provenance.json", manifest)
    return manifest


def verify(folder: Path, expected_path: Path) -> dict:
    """Exact state assertions; expected file is chosen by the operator per run."""
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8-sig"))
    expected = json.loads(expected_path.read_text(encoding="utf-8-sig"))
    allowed = {"generation", "selection", "dropdown_opened", "play_rows", "selections", "queries", "small_button_clicks", "result"}
    if not isinstance(expected, dict) or not expected or set(expected) - allowed:
        raise ValueError("Expected must be a nonempty object containing independent receipt state fields")
    if receipt.get("synthetic_fixture") is not True:
        raise ValueError("Not a synthetic fixture receipt")
    failures = {key: {"expected": value, "actual": receipt.get(key)}
                for key, value in expected.items() if receipt.get(key) != value}
    result = {"passed": not failures, "independent_effects_only": True, "failures": failures,
              "expected": expected, "receipt": receipt}
    write(folder / (expected_path.stem + "-verification.json"), result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "serve", "verify"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--expected", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        print(json.dumps(prepare(args.folder), ensure_ascii=False, indent=2))
    elif args.mode == "serve":
        serve(fixture_folder(args.folder), args.bundle)
    else:
        if args.expected is None:
            parser.error("verify requires --expected")
        result = verify(fixture_folder(args.folder), args.expected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
