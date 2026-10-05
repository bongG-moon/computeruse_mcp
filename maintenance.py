"""Human-invoked cleanup of completed, generated records; never an MCP tool."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from consent import _local_path, _plain_chain, _read_small_object

APP_DIR = Path(__file__).resolve().parent
LOG_HEADER = "Computer Use MCP startup log v1"
RUN_FILES = {"session.json", ".computer-use-active.json", "capabilities.json", "policy.json",
             "actions.jsonl", "driver-stderr.log", "stop.flag"}
SCHEMA_FILES = {"schema-session.json", "driver-stderr.log", "stop.flag"}
CONSENT_FILE = re.compile(r"[a-f0-9]{64}\.(?:request|response|audit)\.json$")


def _root(value: dict | str | Path) -> Path:
    root = _local_path(value["state_dir"] if isinstance(value, dict) else value)
    if not _plain_chain(root, allow_missing=True):
        raise ValueError("기록 폴더에 바로가기 경로가 있어 정리하지 않습니다.")
    return root


def _identity(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1:
        raise ValueError("일반 파일이 아니거나 다른 경로와 연결된 파일이 있습니다.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _inspect_run(run: Path, root: Path) -> dict:
    schema_run = bool(re.fullmatch(r"schema-[a-f0-9]{32}", run.name))
    if run.parent != root / "runs" or not (schema_run or re.fullmatch(r"[a-f0-9]{32}", run.name)):
        raise ValueError("이 도구가 만든 작업 폴더가 아닙니다.")
    if not _plain_chain(run) or not run.is_dir():
        raise ValueError("일반 작업 폴더가 아닙니다.")
    state = _read_small_object(run / ("schema-session.json" if schema_run else "session.json"))
    if state.get("state") != "stopped":
        raise ValueError("종료 완료가 확인되지 않은 작업입니다.")
    if state.get("session_id") != run.name or _local_path(state.get("run_dir", "")) != run:
        raise ValueError("작업 상태의 경로와 식별자가 다릅니다.")
    if schema_run:
        if state.get("format") != "computer-use-schema/v1":
            raise ValueError("이 도구의 연결 확인 기록을 확인하지 못했습니다.")
    else:
        owner = _read_small_object(run / ".computer-use-active.json")
        if (owner.get("version") != 1 or _local_path(owner.get("run_dir", "")) != run
                or type(owner.get("pid")) is not int or owner["pid"] <= 0
                or not isinstance(owner.get("birth"), str) or not owner["birth"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(owner.get("token", "")))):
            raise ValueError("이 도구의 작업 소유 기록을 확인하지 못했습니다.")
    files = []
    for item in sorted(run.iterdir()):
        if item.name == "consent" and not schema_run:
            if not _plain_chain(item) or not item.is_dir():
                raise ValueError("승인 기록 폴더가 일반 폴더가 아닙니다.")
            for detail in sorted(item.iterdir()):
                if not CONSENT_FILE.fullmatch(detail.name):
                    raise ValueError("사용자 파일 또는 알 수 없는 승인 기록을 보존합니다.")
                files.append((str(detail.relative_to(run)), _identity(detail)))
        elif item.name in (SCHEMA_FILES if schema_run else RUN_FILES):
            files.append((item.name, _identity(item)))
        else:
            raise ValueError("사용자 파일 또는 알 수 없는 파일이 있어 폴더를 보존합니다.")
    fingerprint = hashlib.sha256(json.dumps(files, ensure_ascii=False).encode()).hexdigest()
    return {"kind": "run", "path": str(run), "bytes": sum(identity[2] for _, identity in files),
            "file_count": len(files), "fingerprint": fingerprint, "files": files}


def _log_paths() -> tuple[Path, ...]:
    fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ComputerUseMCP"
    return tuple(dict.fromkeys((_local_path(APP_DIR / "startup-log.txt"), _local_path(fallback / "startup-log.txt"))))


def _inspect_log(path: Path) -> dict:
    if path not in _log_paths() or not _plain_chain(path):
        raise ValueError("이 도구의 시작 기록 경로가 아닙니다.")
    identity = _identity(path)
    with path.open("r", encoding="utf-8-sig") as stream:
        if stream.readline(128).strip() != LOG_HEADER:
            raise ValueError("시작 기록의 소유 표시가 없어 보존합니다.")
    return {"kind": "log", "path": str(path), "bytes": identity[2], "file_count": 1,
            "fingerprint": hashlib.sha256(repr(identity).encode()).hexdigest()}


def preview_cleanup(config_or_state_dir: dict | str | Path) -> dict:
    """Return exact candidates for the UI's confirmation. No files are changed."""
    root = _root(config_or_state_dir)
    result = {"root": str(root), "candidates": [], "preserved": [], "errors": []}
    runs = root / "runs"
    if runs.exists():
        if not _plain_chain(runs) or not runs.is_dir():
            raise ValueError("작업 기록 폴더가 일반 폴더가 아니어서 정리하지 않습니다.")
        for run in sorted(runs.iterdir()):
            try:
                result["candidates"].append(_inspect_run(run, root))
            except (OSError, ValueError, TypeError) as error:
                result["preserved"].append({"path": str(run), "reason": str(error)})
    for path in _log_paths():
        if path.exists():
            try:
                result["candidates"].append(_inspect_log(path))
            except (OSError, ValueError, UnicodeError) as error:
                result["preserved"].append({"path": str(path), "reason": str(error)})
    result.update(candidate_count=len(result["candidates"]), candidate_bytes=sum(c["bytes"] for c in result["candidates"]),
                  preserved_count=len(result["preserved"]))
    return result


def cleanup_completed_runs(config_or_state_dir: dict | str | Path, preview: dict) -> dict:
    """Delete only confirmed, unchanged candidates; recheck every path before unlink."""
    root = _root(config_or_state_dir)
    if not isinstance(preview, dict) or preview.get("root") != str(root) or not isinstance(preview.get("candidates"), list):
        raise ValueError("같은 기록 폴더를 다시 미리 확인한 뒤 정리해 주세요.")
    result = {"root": str(root), "deleted_count": 0, "deleted_bytes": 0,
              "preserved_count": preview.get("preserved_count", 0), "errors": []}
    seen = set()
    for candidate in preview["candidates"]:
        try:
            path = _local_path(candidate["path"])
            if path in seen:
                continue
            seen.add(path)
            check = _inspect_run(path, root) if candidate.get("kind") == "run" else _inspect_log(path)
            if check["fingerprint"] != candidate.get("fingerprint"):
                raise ValueError("확인 후 기록이 바뀌어 보존했습니다. 다시 미리 확인해 주세요.")
            if check["kind"] == "log":
                path.unlink()
                result["deleted_bytes"] += check["bytes"]
            else:
                # Keep ownership and terminal state until all payload files are gone.
                files = sorted(check["files"], key=lambda item: (item[0] in {"session.json", ".computer-use-active.json", "schema-session.json"}, item[0]))
                for relative, identity in files:
                    item = path / relative
                    if not _plain_chain(item) or _identity(item) != tuple(identity):
                        raise ValueError("정리 도중 파일이 바뀌어 나머지 기록을 보존했습니다.")
                    item.unlink()
                    result["deleted_bytes"] += identity[2]
                consent = path / "consent"
                if consent.exists():
                    if not _plain_chain(consent):
                        raise ValueError("승인 기록 경로가 바뀌어 폴더를 보존했습니다.")
                    consent.rmdir()
                if not _plain_chain(path) or path.parent != root / "runs":
                    raise ValueError("작업 폴더 경로가 바뀌어 보존했습니다.")
                path.rmdir()
            result["deleted_count"] += 1
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["preserved_count"] += 1
            result["errors"].append({"path": str(candidate.get("path", "")) if isinstance(candidate, dict) else "", "reason": str(error)})
    return result
