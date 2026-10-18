"""Bounded cleanup of owned, closed-run scratch files; never delete user work.

The caller must stop every helper before mark_run_closed(). A stopped driver
alone is insufficient. Old runs without this module's marker are preserved.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from consent import _local_path, _plain_chain

FORMAT = "computer-use-run-artifacts/v1"
CLEANUP_FORMAT = "computer-use-artifact-cleanup/v1"
MARKER = ".artifacts.json"
SUMMARY = ".artifact-cleanup.json"
RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_METADATA_BYTES = 512 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_ENTRIES_PER_RUN = 128
MAX_FILES_PER_CLEANUP = 256
_HEX = r"[a-f0-9]{32}"
_SHORT = r"[a-f0-9]{16}"
_SCAN_LOCK = threading.RLock()
_SCANS = {}


def _timestamp(value=None):
    if value is None:
        value = time.time()
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("정리 기준 시간이 올바르지 않습니다.")
    return float(value)


def _path(value):
    raw = Path(value)
    if ".." in raw.parts:
        raise ValueError("상위 폴더를 가리키는 경로는 보존합니다.")
    result = _local_path(raw)
    if not _plain_chain(result) or not result.is_dir():
        raise ValueError("일반 로컬 폴더만 정리할 수 있습니다.")
    return result


def _run(value):
    path = _path(value)
    if path.parent.name != "runs" or not re.fullmatch(_HEX, path.name):
        raise ValueError("이 도구가 만든 실행 폴더가 아닙니다.")
    return path


def _identity(path):
    item = path.lstat()
    if not _plain_chain(path):
        raise ValueError("연결된 파일이나 바로가기는 보존합니다.")
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
            or getattr(item, "st_file_attributes", 0) & 0x400):
        raise ValueError("일반 단일 파일만 정리할 수 있습니다.")
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns


def _read(path, maximum=MAX_METADATA_BYTES):
    identity = _identity(path)
    if identity[2] > maximum:
        raise ValueError("소유 기록이 너무 커서 보존합니다.")
    with path.open("rb") as stream:
        if _stat_identity(os.fstat(stream.fileno())) != identity:
            raise ValueError("읽는 중 파일이 바뀌어 보존합니다.")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or _identity(path) != identity:
        raise ValueError("읽는 중 파일이 바뀌어 보존합니다.")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("소유 기록 형식을 확인하지 못했습니다.")
    return value, identity, raw


def _stat_identity(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("연결된 파일은 보존합니다.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _write(path, value, expected=None):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("보존 기록의 크기 한도를 넘어 변경하지 않았습니다.")
    _path(path.parent)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    identity = None
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
        identity = _identity(temporary)
        _path(path.parent)
        if expected is None:
            if path.exists() or path.is_symlink():
                raise ValueError("다른 소유 기록이 생겨 덮어쓰지 않았습니다.")
        elif _identity(path) != expected:
            raise ValueError("소유 기록이 바뀌어 보존합니다.")
        os.replace(temporary, path)
    finally:
        try:
            if identity is not None and _identity(temporary) == identity:
                temporary.unlink()
        except (OSError, ValueError):
            pass


@contextmanager
def _lock(folder, name, *, wait=.5):
    """OS locks release on process exit; never steal or delete a lock file."""
    _path(folder)
    path = folder / name
    if path.exists() or path.is_symlink():
        _identity(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    acquired = False
    try:
        _stat_identity(os.fstat(fd))
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        end = time.monotonic() + max(0, wait)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                if _identity(path)[:2] != _stat_identity(os.fstat(fd))[:2]:
                    raise ValueError("정리 잠금 경로가 바뀌어 보존합니다.")
                yield
                break
            except OSError:
                if acquired or time.monotonic() >= end:
                    raise
                time.sleep(min(.01, max(0, end - time.monotonic())))
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _metadata(run):
    value, identity, _ = _read(run / MARKER)
    required = {"format", "owner", "run_dir", "session_id", "task", "created_at", "policy", "saved_recordings", "cleanup_cursor"}
    if (not required <= value.keys() or value.get("format") != FORMAT
            or value.get("owner") != "computer-use-mcp" or value.get("session_id") != run.name
            or value.get("run_dir") != str(run)
            or value.get("policy") != {"temporary": "after_helpers_closed", "saved_recording_days": 7,
                                       "unsaved_recordings": "preserve", "task_templates": "preserve"}
            or not isinstance(value.get("task"), dict)
            or not isinstance(value.get("saved_recordings"), dict)
            or not isinstance(value.get("cleanup_cursor"), dict)):
        raise ValueError("실행 기록 소유 또는 보존 정책이 달라 보존합니다.")
    _timestamp(value["created_at"])
    if "closed_at" in value:
        _timestamp(value["closed_at"])
    return value, identity


def _session(run):
    value, identity, _ = _read(run / "session.json")
    if (value.get("state") != "stopped" or value.get("session_id") != run.name
            or _local_path(value.get("run_dir", "")) != run):
        raise ValueError("세션 종료가 확인되지 않아 보존합니다.")
    return identity


def initialize_run_artifacts(run_dir, task, session_id):
    run = _run(run_dir)
    if session_id != run.name:
        raise ValueError("실행 식별자가 다릅니다.")
    name = (task.get("name") or task.get("description") or task.get("instructions") or "화면 작업") if isinstance(task, dict) else task
    task_id = task.get("id", task.get("task_id", session_id)) if isinstance(task, dict) else session_id
    if not isinstance(name, str) or not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("업무 정보를 확인하지 못했습니다.")
    with _lock(run, ".artifacts.lock"):
        if (run / MARKER).exists():
            value, _ = _metadata(run)
            if value["task"] != {"id": task_id, "name": name[:300]}:
                raise ValueError("기존 업무 기록과 달라 변경하지 않았습니다.")
            return value
        value = {"format": FORMAT, "owner": "computer-use-mcp", "run_dir": str(run),
                 "session_id": session_id, "task": {"id": task_id, "name": name[:300]},
                 "created_at": _timestamp(), "policy": {"temporary": "after_helpers_closed", "saved_recording_days": 7,
                 "unsaved_recordings": "preserve", "task_templates": "preserve"},
                 "saved_recordings": {}, "cleanup_cursor": {}}
        _write(run / MARKER, value)
        return value


def mark_run_closed(run_dir):
    """Only call after all helper processes have ended and session is stopped."""
    run = _run(run_dir)
    with _lock(run, ".artifacts.lock"):
        value, identity = _metadata(run)
        session_identity = _session(run)
        if "closed_at" not in value:
            value["closed_at"] = _timestamp()
            value["closed_session_identity"] = list(session_identity)
            _write(run / MARKER, value, identity)
        elif value.get("closed_session_identity") != list(session_identity):
            raise ValueError("종료 이후 세션 기록이 바뀌어 보존합니다.")
        return value


def _receipt(run, receipt_id):
    if not isinstance(receipt_id, str) or not re.fullmatch(_HEX, receipt_id):
        raise ValueError("녹화 원본 식별자가 올바르지 않습니다.")
    path = run / "recording-receipts" / (receipt_id + ".json")
    value, identity, raw = _read(path, MAX_RECEIPT_BYTES)
    if (value.get("format") != "computer-recording-receipt/v1" or value.get("receipt_id") != receipt_id
            or not isinstance(value.get("recording"), dict)):
        raise ValueError("이 도구의 녹화 원본이 아니어서 보존합니다.")
    return path, identity, hashlib.sha256(raw).hexdigest()


def mark_recording_saved(run_dir, receipt_id, task_id):
    """Caller certifies a complete task has been persisted, not merely a draft."""
    run = _run(run_dir)
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("저장한 업무 식별자가 올바르지 않습니다.")
    with _lock(run, ".artifacts.lock"):
        value, marker_identity = _metadata(run)
        _, identity, digest = _receipt(run, receipt_id)
        old = value["saved_recordings"].get(receipt_id)
        if old is not None:
            if not isinstance(old, dict) or old.get("sha256") != digest or old.get("task_id") != task_id:
                raise ValueError("저장 표시 후 원본 또는 업무가 달라 보존합니다.")
            return value
        value["saved_recordings"][receipt_id] = {"saved_at": _timestamp(), "task_id": task_id,
                                                "sha256": digest, "bytes": identity[2]}
        _write(run / MARKER, value, marker_identity)
        return value


def _scan(path, offset, deadline, maximum):
    """Bound memory, retain OS iterators for cheap continuation between calls."""
    if type(offset) is not int or not 0 <= offset <= 100_000_000:
        raise ValueError("정리 진행 기록을 확인하지 못했습니다.")
    key = str(path)
    with _SCAN_LOCK:
        cached = _SCANS.get(key)
        if cached is None or cached[1] > offset:
            if cached:
                cached[0].close()
            cached = [os.scandir(path), 0]
            _SCANS[key] = cached
        # Keep at most 128 directory handles. The persisted offset survives eviction.
        while len(_SCANS) > 128:
            oldest = next(iter(_SCANS))
            if oldest == key:
                break
            _SCANS.pop(oldest)[0].close()
        values = []
        while time.monotonic() < deadline and len(values) < maximum:
            try:
                entry = next(cached[0])
            except StopIteration:
                cached[0].close()
                _SCANS.pop(key, None)
                return values, 0, True
            cached[1] += 1
            if cached[1] > offset:
                values.append(entry.name)
        return values, max(offset, cached[1]), False


def _helper_kind(name):
    if name in {"im", "visual", "learning", "process-editor", "image-crops", "recording-receipts"}:
        return name
    if re.fullmatch("checkpoint-review-" + _HEX, name):
        return "checkpoint"
    return None


def _allowed_file(kind, name):
    atomic = r"(?:\.tmp(?:-[a-f0-9]{32})?)?"
    patterns = {
        "im": _SHORT + r"\.(?:air\.)?(?:in|out)" + atomic,
        "visual": _SHORT + r"\.(?:req\.json|res\.json(?:\.(?:ready|progress)\.json)?)" + atomic,
        "learning": _SHORT + r"\.(?:request\.json|response\.json(?:\.ready\.json)?)" + atomic,
        "process-editor": _SHORT + r"\.(?:request\.json|response\.json(?:\.(?:ready|command|event|trial|trial\.ready)\.json)?)" + atomic,
        "checkpoint": r"(?:screen\.png|(?:request|ready|result)\.json|cancel\.flag)" + atomic,
        "crop": r"(?:request|response)\.json" + atomic,
    }
    return bool(kind in patterns and re.fullmatch(patterns[kind], name))


def _unchanged(run, marker_identity, session_identity):
    _run(run)
    if _identity(run / MARKER) != marker_identity or _identity(run / "session.json") != session_identity:
        raise ValueError("정리 도중 실행 정보가 바뀌어 나머지를 보존합니다.")


def _unlink(path, run, marker_identity, session_identity, expected, result):
    _unchanged(run, marker_identity, session_identity)
    if _identity(path) != expected:
        raise ValueError("정리 중 파일이 바뀌어 보존합니다.")
    path.unlink()
    result["deleted_files"] += 1
    result["deleted_bytes"] += expected[2]


def _error(result, path, error):
    result["error_count"] += 1
    if len(result["errors"]) < 12:
        result["errors"].append({"path": str(path), "reason": str(error)[:240]})


def _clean_directory(run, folder, kind, value, marker_identity, session_identity, now, deadline, result):
    _path(folder)
    key = str(folder.relative_to(run))
    cursors = value["cleanup_cursor"]
    offsets = cursors.setdefault("folders", {})
    if not isinstance(offsets, dict) or len(offsets) > 512:
        raise ValueError("정리 진행 기록이 너무 커서 보존합니다.")
    start_offset = offsets.get(key, 0)
    names, offset, complete = _scan(folder, start_offset, deadline,
                                   min(MAX_ENTRIES_PER_RUN, MAX_FILES_PER_CLEANUP - result["inspected_files"]))
    offsets[key] = offset
    for index, name in enumerate(names):
        if time.monotonic() >= deadline or result["inspected_files"] >= MAX_FILES_PER_CLEANUP:
            offsets[key] = start_offset + index
            complete = False
            break
        path = folder / name
        result["inspected_files"] += 1
        try:
            if kind == "image-crops":
                if re.fullmatch(r"crop-[a-z0-9_]{8}", name):
                    _clean_directory(run, path, "crop", value, marker_identity, session_identity, now, deadline, result)
                else:
                    result["preserved_files"] += 1
                continue
            if kind == "recording-receipts":
                if not re.fullmatch(_HEX + r"\.json", name):
                    result["preserved_files"] += 1
                    continue
                saved = value["saved_recordings"].get(name[:-5])
                if (not isinstance(saved, dict) or now - _timestamp(saved.get("saved_at")) < RECEIPT_RETENTION_SECONDS
                        or not isinstance(saved.get("task_id"), str) or not saved["task_id"]):
                    result["preserved_files"] += 1
                    continue
                _, identity, digest = _receipt(run, name[:-5])
                if digest != saved.get("sha256") or identity[2] != saved.get("bytes"):
                    raise ValueError("저장 이후 녹화 원본이 바뀌어 보존합니다.")
                if time.monotonic() >= deadline:
                    offsets[key] = start_offset + index
                    complete = False
                    break
                _unlink(path, run, marker_identity, session_identity, identity, result)
                value["saved_recordings"].pop(name[:-5], None)
            elif _allowed_file(kind, name):
                identity = _identity(path)
                _unlink(path, run, marker_identity, session_identity, identity, result)
            else:
                result["preserved_files"] += 1
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            _error(result, path, exc)
    if complete:
        offsets.pop(key, None)
        try:
            _unchanged(run, marker_identity, session_identity)
            _path(folder)
            folder.rmdir()  # Empty directories only; never recursive.
        except OSError:
            pass  # Unknown/retained files remain, or a file is locked.


def _clean_run(run, now, deadline, result):
    run = _run(run)
    # No marker means a historical/user folder: do not even create a lock there.
    if not (run / MARKER).exists():
        result["preserved_runs"] += 1
        return
    with _lock(run, ".artifacts.lock", wait=0):
        value, marker_identity = _metadata(run)
        if "closed_at" not in value:
            result["preserved_runs"] += 1
            return
        session_identity = _session(run)
        if (value.get("closed_session_identity") != list(session_identity)
                or value["closed_at"] > now):
            result["preserved_runs"] += 1
            return
        start_offset = value["cleanup_cursor"].get("run", 0)
        names, offset, _ = _scan(run, start_offset, deadline, MAX_ENTRIES_PER_RUN)
        value["cleanup_cursor"]["run"] = offset
        for index, name in enumerate(names):
            if time.monotonic() >= deadline or result["inspected_files"] >= MAX_FILES_PER_CLEANUP:
                value["cleanup_cursor"]["run"] = start_offset + index
                break
            kind = _helper_kind(name)
            if kind:
                try:
                    _clean_directory(run, run / name, kind, value, marker_identity, session_identity, now, deadline, result)
                except (OSError, ValueError, TypeError, UnicodeError) as exc:
                    _error(result, run / name, exc)
        _unchanged(run, marker_identity, session_identity)
        _write(run / MARKER, value, marker_identity)


def read_last_cleanup(state_dir):
    try:
        root = _path(state_dir)
        value, _, _ = _read(root / SUMMARY)
        if value.get("format") != CLEANUP_FORMAT or value.get("root") != str(root):
            return None
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def cleanup_artifacts(state_dir, now=None, max_runs=100, time_budget_seconds=.5):
    started = time.monotonic()
    now = _timestamp(now)
    if type(max_runs) is not int or not 1 <= max_runs <= 1000:
        raise ValueError("한 번에 확인할 실행 수는 1~1000이어야 합니다.")
    if type(time_budget_seconds) not in (int, float) or not math.isfinite(time_budget_seconds) or not 0 <= time_budget_seconds <= 30:
        raise ValueError("정리 시간은 0~30초여야 합니다.")
    deadline = started + time_budget_seconds
    result = {"format": CLEANUP_FORMAT, "checked_at": now, "status": "completed", "scanned_runs": 0,
              "preserved_runs": 0, "inspected_files": 0, "preserved_files": 0, "deleted_files": 0,
              "deleted_bytes": 0, "error_count": 0, "errors": [], "cursor": 0}
    try:
        raw = Path(state_dir)
        if ".." in raw.parts:
            raise ValueError("상위 폴더 경로는 보존합니다.")
        root = _local_path(raw)
        if not _plain_chain(root, allow_missing=True):
            raise ValueError("연결된 기록 폴더는 보존합니다.")
        result["root"] = str(root)
        if not root.exists():
            return result  # Fresh install: no files, and no need to create folders.
        root = _path(root)
        if time.monotonic() >= deadline:
            result["status"] = "deferred"
            return result
        with _lock(root, ".artifact-cleanup.lock", wait=0):
            previous = read_last_cleanup(root)
            cursor = previous.get("cursor", 0) if previous else 0
            result["cursor"] = cursor
            runs = root / "runs"
            if runs.exists():
                _path(runs)
                start_cursor = cursor
                names, cursor, complete = _scan(runs, cursor, deadline, max_runs)
                result["cursor"] = cursor
                for index, name in enumerate(names):
                    if time.monotonic() >= deadline or result["inspected_files"] >= MAX_FILES_PER_CLEANUP:
                        result["cursor"] = start_cursor + index
                        result["status"] = "deferred"
                        break
                    if not re.fullmatch(_HEX, name):
                        result["preserved_runs"] += 1
                        continue
                    result["scanned_runs"] += 1
                    try:
                        _clean_run(runs / name, now, deadline, result)
                    except (OSError, ValueError, TypeError, UnicodeError) as exc:
                        result["preserved_runs"] += 1
                        _error(result, runs / name, exc)
                if not complete:
                    result["status"] = "deferred"
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            summary_path = root / SUMMARY
            expected = _identity(summary_path) if summary_path.exists() else None
            _write(summary_path, result, expected)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        result["status"] = "deferred"
        _error(result, state_dir, exc)
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def storage_status(run_dir):
    try:
        run = _run(run_dir)
        value, _ = _metadata(run)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return {"status": "unmanaged", "records_preserved": True,
                "message": "소유 기록을 확인하지 못해 파일을 보존합니다.", "reason": str(exc)[:240]}
    return {"status": "managed", "run_dir": str(run), "task": value["task"], "session_id": run.name,
            "temporary_files": "cleanup_after_helpers_closed", "closed": "closed_at" in value,
            "saved_recording_retention_days": 7, "unsaved_recordings": "preserved",
            "task_templates": "preserved", "saved_recording_count": len(value["saved_recordings"]),
            "last_cleanup": read_last_cleanup(run.parent.parent)}
