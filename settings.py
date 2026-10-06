"""User-owned configuration only. No model, network, or Claude configuration writes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import uuid

from vendor.guard import check_app, normalize_exe, GuardError
from vendor.windows import discover

VERSION = "0.9.0"
PROGRAM_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def default_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
    return base / "CompanyComputerUse" / "config.json"


def default_config(config_path: Path | None = None) -> dict:
    path = Path(config_path or default_config_path()).resolve()
    programs = []
    for item in discover()["apps"]:
        programs.append({key: item.get(key, default) for key, default in (
            ("id", ""), ("name", ""), ("exe", ""), ("control_exes", []), ("hints", ""))})
        programs[-1]["enabled"] = bool(item.get("available") and item.get("exe"))
        if item.get("id") == "notepad":
            programs[-1]["hints"] += " 저장은 파일 → 저장 메뉴를 먼저 사용하고 실제 파일을 확인합니다. 단축키 동작은 프로그램과 Driver 조합에 따라 다를 수 있습니다."
        if item.get("id") == "excel":
            programs[-1]["hints"] += " 입력 확정 후 셀 주소와 값을 다시 확인하고 저장·재열기 결과까지 확인합니다. UIA 즉시 값 또는 여러 셀 문자열 입력만으로 성공을 판단하지 않습니다."
    return {"version": 1, "driver": "", "programs": programs, "mode": "uia",
            "approval": "client", "log_detail": "metadata", "max_minutes": 10, "max_actions": 120,
            "approval_timeout_seconds": 300, "observation_timeout_seconds": 20, "state_dir": str(path.parent)}


def validate_config(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("지원하지 않는 설정 형식입니다.")
    config = copy.deepcopy(value)
    config.setdefault("log_detail", "metadata")
    if not isinstance(config["log_detail"], str) or config["log_detail"] not in {"metadata", "content"}:
        raise ValueError("기록 방식은 기본 정보(metadata) 또는 내용 포함(content)이어야 합니다.")
    if config.get("mode") not in {"uia", "visual"}:
        raise ValueError("화면 방식은 uia 또는 visual이어야 합니다.")
    if config.get("approval") not in {"session", "each", "client"}:
        raise ValueError("승인 방식은 session, each, client 중 하나여야 합니다.")
    for key, low, high in (("max_minutes", 1, 30), ("max_actions", 1, 1000), ("approval_timeout_seconds", 1, 600)):
        if key not in config and key == "approval_timeout_seconds":
            config[key] = 300
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} 값은 {low}~{high} 범위여야 합니다.")
    if type(config.get("observation_timeout_seconds", 20)) is not int or not 5 <= config.get("observation_timeout_seconds", 20) <= 90:
        raise ValueError("화면 읽기 제한 시간은 5~90초로 지정하세요.")
    driver = config.get("driver", "")
    if not isinstance(driver, str):
        raise ValueError("Driver 실행파일 위치를 확인해주세요.")
    if driver:
        try:
            normalize_exe(driver)
        except GuardError as error:
            raise ValueError(str(error)) from error
    state = config.get("state_dir")
    if not isinstance(state, str) or not Path(state).is_absolute() or state.startswith(("\\\\", "//")):
        raise ValueError("기록 폴더는 이 PC의 전체 경로로 지정해주세요.")
    programs = config.get("programs")
    if not isinstance(programs, list) or len(programs) > 100:
        raise ValueError("프로그램은 최대 100개까지 등록할 수 있습니다.")
    seen = set()
    for app in programs:
        if not isinstance(app, dict) or not isinstance(app.get("id"), str) or not PROGRAM_ID.fullmatch(app["id"]):
            raise ValueError("프로그램 식별자를 확인해주세요.")
        if app["id"] in seen:
            raise ValueError("같은 프로그램 식별자가 두 번 등록되어 있습니다.")
        seen.add(app["id"])
        if not isinstance(app.get("name"), str) or not 1 <= len(app["name"].strip()) <= 100:
            raise ValueError("프로그램 이름은 1~100자로 입력해주세요.")
        if type(app.get("enabled")) is not bool:
            raise ValueError("프로그램 사용 여부를 확인해주세요.")
        if not isinstance(app.get("exe"), str) or (app["enabled"] and not app["exe"]):
            raise ValueError("사용할 프로그램의 실행파일을 지정해주세요.")
        extra = app.get("control_exes", [])
        if not isinstance(extra, list) or len(extra) > 12:
            raise ValueError("추가 조작 실행파일은 최대 12개까지 지정할 수 있습니다.")
        for path in ([app["exe"]] if app["exe"] else []) + extra:
            try:
                check_app(path)
            except GuardError as error:
                raise ValueError(str(error)) from error
        hints = app.get("hints", "")
        if not isinstance(hints, str) or len(hints) > 10000:
            raise ValueError("프로그램 사용법은 10,000자 이하로 입력해주세요.")
        if set(app) - {"id", "name", "exe", "control_exes", "hints", "enabled"}:
            raise ValueError("프로그램 설정에 지원하지 않는 항목이 있습니다. 실행 인자는 등록하지 않습니다.")
        app["control_exes"] = extra
        app["hints"] = hints
    return config


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path | str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError("사용 설정이 없습니다. 배포본의 INSTALL.md를 Claude에게 읽고 연결해 달라고 요청하세요. 수동 설정 창도 사용할 수 있습니다.")
    if path.stat().st_size > 1_000_000:
        raise ValueError("설정 파일이 너무 큽니다.")
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise ValueError(f"설정을 읽지 못했습니다. 원본 파일은 유지됩니다: {path}\n{error}") from error


def save_config(path: Path | str, config: dict) -> None:
    atomic_json(Path(path), validate_config(config))


def stop_runs(config: dict) -> int:
    root = Path(config["state_dir"]).resolve() / "runs"
    if not root.is_dir():
        return 0
    count = 0
    for run in root.iterdir():
        # Only direct, real child directories created for this package.
        if run.is_dir() and not run.is_symlink() and run.resolve().parent == root:
            (run / "stop.flag").touch()
            count += 1
    return count
