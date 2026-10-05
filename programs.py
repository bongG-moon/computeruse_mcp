"""Local, explicitly requested program registration; never a screen/MCP tool.

The calling assistant uses this CLI only after the user asks to add a program.
Preview is technical inspection, not an additional consent prompt. No app,
Driver, model, Claude settings, or desktop session is started or changed here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from consent import _plain_chain
from settings import validate_config
from vendor.guard import GuardError, check_app


class ProgramError(ValueError):
    pass


def _plain_file(value, *, executable=False) -> Path:
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or not _plain_chain(path) or not path.is_file()):
        raise ProgramError("이 PC에 있는 파일의 전체 경로를 지정하세요. 바로가기·연결·네트워크 경로는 사용하지 않습니다.")
    if executable:
        try:
            check_app(str(path))
        except GuardError as error:
            raise ProgramError("등록할 수 없는 실행파일입니다. 터미널·명령 실행기·시스템 관리 프로그램은 화면 작업 대상으로 등록하지 않습니다.") from error
    elif path.stat().st_nlink != 1:
        raise ProgramError("설정 파일은 다른 파일과 연결되지 않은 원래 경로여야 합니다.")
    return path.resolve()


def _read(config_path):
    path = _plain_file(config_path)
    if path.stat().st_size > 1_000_000:
        raise ProgramError("설정 파일이 너무 큽니다. 원본은 변경하지 않았습니다.")
    data = path.read_bytes()
    original = json.loads(data.decode("utf-8-sig"))
    # Validate a copy, but keep original fields unchanged when appending.
    checked = validate_config(original)
    return path, original, checked, hashlib.sha256(data).hexdigest()


def inspect_programs(config_path) -> dict:
    path, _, checked, digest = _read(config_path)
    return {"ok": True, "status": "configured_programs", "config_path": str(path),
            "config_sha256": digest, "programs": checked["programs"],
            "approval": checked["approval"], "changed": False,
            "screen_accessed": False, "driver_started": False}


def _entry(config, *, exe, name, program_id=None, hints="", control_exes=()):
    path = _plain_file(exe, executable=True)
    canonical = check_app(str(path))
    controls = [_plain_file(item, executable=True) for item in control_exes]
    canonical_controls = [check_app(str(item)) for item in controls]
    if canonical in canonical_controls or len(canonical_controls) != len(set(canonical_controls)):
        raise ProgramError("추가 조작 실행파일은 기본 실행파일과 다르고 서로 중복되지 않아야 합니다.")
    matches = [item for item in config["programs"] if item.get("exe") and check_app(item["exe"]) == canonical]
    if len(matches) > 1:
        raise ProgramError("같은 실행파일이 여러 항목에 등록되어 있습니다. 설정에서 중복을 먼저 확인하세요.")
    existing = matches[0] if matches else None
    selected_id = program_id if program_id is not None else (
        existing["id"] if existing else "app-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12])
    proposed = {"id": selected_id, "name": name.strip() if isinstance(name, str) else name,
                "exe": str(path), "enabled": True, "control_exes": [str(item) for item in controls],
                "hints": hints}
    # Reuse all normal name/id/app/hints limits without changing existing data.
    validate_config({**config, "programs": [proposed]})
    if existing:
        comparable = {**existing, "exe": canonical, "control_exes": [check_app(p) for p in existing["control_exes"]]}
        expected = {**proposed, "exe": canonical, "control_exes": canonical_controls}
        if comparable != expected:
            raise ProgramError("이 실행파일의 기존 등록 내용이 다릅니다. 이름·사용 여부·권한을 자동 변경하지 않았습니다. 설정에서 기존 항목을 확인하세요.")
        return copy.deepcopy(existing), True
    if any(item["id"] == selected_id for item in config["programs"]):
        raise ProgramError("같은 프로그램 ID가 이미 사용 중입니다. 다른 ID를 지정하세요.")
    return proposed, False


def preview_add(config_path, *, exe, name, program_id=None, hints="", control_exes=()) -> dict:
    path, original, config, digest = _read(config_path)
    entry, exists = _entry(config, exe=exe, name=name, program_id=program_id, hints=hints, control_exes=control_exes)
    if not exists:
        validate_config({**original, "programs": [*original["programs"], entry]})
    return {"ok": True, "status": "already_present" if exists else "ready_to_add",
            "config_path": str(path), "expected_config_sha256": digest, "program": entry,
            "changed": False, "screen_accessed": False, "driver_started": False,
            "message": "이미 같은 내용으로 등록되어 있습니다." if exists else
                       "이 항목만 추가할 수 있습니다. 기존 프로그램·승인 방식·저장한 작업은 유지합니다."}


@contextmanager
def _write_lock(path):
    # Separate stable lock serializes concurrent calls to this CLI. Other
    # settings writers are caught by the source hash immediately before replace.
    lock_path = path.with_name(path.name + ".programs.lock")
    if not _plain_chain(lock_path, allow_missing=True):
        raise ProgramError("설정 잠금 경로가 연결 파일입니다. 설정은 변경하지 않았습니다.")
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
        raise ProgramError("설정 잠금 파일의 원래 위치를 확인하세요.")
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProgramError("다른 프로그램 추가 작업이 저장 중입니다. 현재 등록 목록을 다시 확인하세요.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _replace_if_unchanged(path, original_hash, updated, entry):
    temporary = path.with_name(path.name + ".programs-tmp-" + uuid.uuid4().hex)
    data = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_file(entry["exe"], executable=True)
        for extra in entry["control_exes"]:
            _plain_file(extra, executable=True)
        _, _, _, current_hash = _read(path)
        if current_hash != original_hash:
            raise ProgramError("확인 이후 설정이 바뀌었습니다. 다른 변경을 덮어쓰지 않았습니다. 등록 목록을 다시 확인하세요.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def add_program(config_path, *, expected_config_sha256, exe, name, program_id=None, hints="", control_exes=()) -> dict:
    if not isinstance(expected_config_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_config_sha256):
        raise ProgramError("preview-add가 반환한 설정 SHA256을 그대로 지정하세요.")
    options = dict(exe=exe, name=name, program_id=program_id, hints=hints, control_exes=control_exes)
    # Reject malformed requests before creating even a lock file.
    preview = preview_add(config_path, **options)
    path = Path(preview["config_path"])
    with _write_lock(path):
        path, original, config, digest = _read(path)
        entry, exists = _entry(config, **options)
        if exists:
            return {"ok": True, "status": "already_present", "changed": False, "config_path": str(path),
                    "config_sha256": digest, "program": entry, "reconnect_required": True,
                    "screen_accessed": False, "driver_started": False,
                    "message": "이미 같은 내용으로 등록되어 있습니다. 실행 중인 MCP가 이전 목록을 보이면 다시 연결하세요."}
        if digest != expected_config_sha256:
            raise ProgramError("확인 이후 설정이 바뀌었습니다. 다른 변경을 덮어쓰지 않았습니다. 등록 목록을 다시 확인하세요.")
        updated = copy.deepcopy(original)
        updated["programs"].append(entry)
        validate_config(updated)
        digest = _replace_if_unchanged(path, digest, updated, entry)
    return {"ok": True, "status": "added", "changed": True, "config_path": str(path), "config_sha256": digest,
            "program": entry, "reconnect_required": True, "screen_accessed": False, "driver_started": False,
            "message": "프로그램을 추가했습니다. 화면 작업을 끝낸 뒤 MCP를 다시 연결하면 새 목록을 사용할 수 있습니다."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="요청한 프로그램을 기존 Computer Use MCP 설정에 추가하기")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("inspect", "list", "preview-add", "add"):
        command = sub.add_parser(action)
        command.add_argument("--config", required=True, type=Path)
        if action in {"preview-add", "add"}:
            command.add_argument("--exe", required=True, type=Path)
            command.add_argument("--name", required=True)
            command.add_argument("--id", dest="program_id")
            command.add_argument("--hints", default="")
            command.add_argument("--control-exe", action="append", type=Path, default=[])
        if action == "add":
            command.add_argument("--expected-config-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {"inspect", "list"}:
            result = inspect_programs(args.config)
        else:
            options = dict(exe=args.exe, name=args.name, program_id=args.program_id,
                           hints=args.hints, control_exes=args.control_exe)
            if args.action == "add":
                result = add_program(args.config, expected_config_sha256=args.expected_config_sha256, **options)
            else:
                result = preview_add(args.config, **options)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result = {"ok": False, "status": "program_change_failed", "message": str(error),
                  "retry_automatically": False, "screen_accessed": False, "driver_started": False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
