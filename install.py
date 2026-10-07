"""Conversation-first setup. Inspect/prepare never start Driver or change Claude.

Apply executes a reviewed plan through the same validators as manual setup.
The plan digest detects changed input; it is NOT proof of human consent. The
calling assistant must obtain that consent and obey the host's permissions.
No download, model call, screen observation, input, or privilege change occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import register
from consent import _plain_chain
from diagnostics import run_diagnostics
from settings import VERSION, load_config, validate_config
from vendor.windows import _app_path


APP_DIR = Path(__file__).resolve().parent
SCOPES = {"local": "이 작업 폴더에서만", "user": "내 모든 프로젝트에서", "export": "연결 파일만 만들기"}
APPS = {"chrome": ("Chrome", "chrome.exe"), "edge": ("Edge", "msedge.exe"),
        "notepad": ("메모장", "notepad.exe"), "excel": ("Excel", "excel.exe")}
RESERVED = {".claude.json", "settings.json", ".mcp.json", "install-receipt.json"}


class SetupError(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")) or not _plain_chain(path, allow_missing=True):
        raise SetupError("이 PC의 연결되지 않은 절대 경로를 지정해 주세요. 네트워크·심볼릭 링크 경로는 사용하지 않습니다.")
    return path.resolve()


def _hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise SetupError("파일 위치에 폴더가 있습니다: " + str(path))
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _read(path: Path) -> dict:
    if path.stat().st_size > 1_000_000:
        raise SetupError("설치 기록 파일이 너무 큽니다.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SetupError("설치 기록의 형식이 올바르지 않습니다.")
    return value


def _code_fingerprint() -> str:
    files = ("install.py", "register.py", "settings.py", "diagnostics.py", "server.py",
             "consent.py", "vendor/guard.py", "vendor/windows.py", "learning_picker.py", "teaching_sessions.py",
             "teaching_support.py", "process_editor.py", "image_targets.py", "image_steps.py", "program_launch.py",
             "operations.py", "process_steps.py", "workflows.py", "session_runtime.py", "repeat_profiles.py", "scoped_controls.py", "result_files.py",
             "Computer Use MCP 빠른 확인.exe")
    from teaching_support import TEACHING_HELPER_FILES
    files += TEACHING_HELPER_FILES
    return _digest({name: _hash(_path(APP_DIR / name)) for name in files})


def _receipt_result(receipt: dict, **extra) -> dict:
    # Keep the model-facing result short; detailed evidence stays on disk.
    keys = ("ok", "status", "version", "config_path", "scope", "project", "connection_file", "screen_accessed", "message")
    return {**{key: receipt[key] for key in keys if key in receipt}, **extra}


def _receipt_matches(receipt: dict, payload: dict) -> bool:
    fields = ("version", "bundle", "code_sha256", "entry", "config_path", "scope", "project", "driver_sha256")
    expected_status = "exported" if payload["scope"] == "export" else "registered"
    return (receipt.get("ok") is True and receipt.get("status") == expected_status
            and all(receipt.get(key) == payload.get(key) for key in fields))


def _create(path: Path, value: dict) -> None:
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        if _read(path) != value:
            raise SetupError("다른 내용의 파일을 덮어쓰지 않았습니다: " + str(path))


def _candidates(values) -> list[str]:
    found = {}
    for value in values:
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() and path.is_file() and _plain_chain(path):
            resolved = str(path.resolve())
            found[os.path.normcase(resolved)] = resolved
    return list(found.values())


def driver_candidates(config: dict | None) -> list[str]:
    # No recursive PC search and no execution of candidates.
    return _candidates([(config or {}).get("driver"), shutil.which("cua-driver.exe"),
                        APP_DIR / "driver" / "cua-driver.exe"])


def app_candidates(name: str) -> list[str]:
    _, executable = APPS[name]
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    known = {
        "chrome": [pf / "Google/Chrome/Application/chrome.exe", pf86 / "Google/Chrome/Application/chrome.exe", local / "Google/Chrome/Application/chrome.exe"],
        "edge": [pf86 / "Microsoft/Edge/Application/msedge.exe", pf / "Microsoft/Edge/Application/msedge.exe"],
        "notepad": [windir / "System32/notepad.exe"],
        "excel": [pf / "Microsoft Office/root/Office16/EXCEL.EXE", pf86 / "Microsoft Office/root/Office16/EXCEL.EXE"],
    }
    return _candidates([_app_path(executable), *known[name]])


def inspect_setup(config_path, *, driver=None, apps=(), app_exes=(), scope=None, project=None,
                  app_launch_uri=None, app_arguments=(), app_working_directory=None, app_control_exes=()) -> dict:
    config_path = _path(config_path)
    if config_path.name.lower() in RESERVED:
        raise SetupError("Claude 설정과 다른 전용 설정 파일 이름을 지정해 주세요.")
    existing = load_config(config_path) if config_path.exists() else None
    questions = []
    candidates = driver_candidates(existing) if not driver else _candidates([_path(driver)])
    if driver and (not candidates or Path(candidates[0]).name.lower() != "cua-driver.exe"):
        raise SetupError("압축을 푼 실제 cua-driver.exe 경로를 지정해 주세요. 파일은 실행하지 않았습니다.")
    if len(candidates) != 1:
        questions.append({"field": "driver", "question": "준비한 cua-driver.exe는 어디에 있나요?" if not candidates else "어느 Driver 파일을 사용할까요?", "candidates": candidates})
    if scope not in SCOPES:
        questions.append({"field": "scope", "question": "어디에서 사용할까요?", "choices": SCOPES})
    project_path = _path(project) if project else None
    if scope == "local" and (project_path is None or not project_path.is_dir()):
        questions.append({"field": "project", "question": "사용할 작업 폴더의 전체 경로를 알려주세요."})
    programs = []
    custom_exes = list(dict.fromkeys(app_exes))
    has_profile = bool(app_launch_uri is not None or app_arguments or app_working_directory is not None or app_control_exes)
    if has_profile and len(custom_exes) != 1:
        raise SetupError("실행 주소·인자·시작 폴더·추가 조작 파일을 지정할 때는 --app-exe를 정확히 한 개 지정하세요.")
    if app_launch_uri is not None and (app_arguments or app_working_directory is not None):
        raise SetupError("주소 실행과 EXE 인자·시작 폴더는 함께 지정하지 않습니다.")
    for name in dict.fromkeys(apps):
        if name not in APPS:
            raise SetupError("지원하는 이름은 chrome, edge, notepad, excel입니다. 다른 프로그램은 --app-exe로 정확한 실행파일을 지정하세요.")
        matches = app_candidates(name)
        if len(matches) != 1:
            questions.append({"field": "app", "app": name, "question": APPS[name][0] + "의 실행파일을 선택해 주세요.", "candidates": matches})
        else:
            programs.append({"id": name, "name": APPS[name][0], "exe": matches[0], "enabled": True, "control_exes": [], "hints": "요청한 시험용 창만 사용합니다."})
    for index, value in enumerate(custom_exes):
        path = _path(value)
        if not path.is_file() or path.suffix.lower() != ".exe":
            raise SetupError("사용할 프로그램의 실제 .exe 파일을 지정해 주세요.")
        if any(os.path.normcase(p["exe"]) == os.path.normcase(str(path)) for p in programs):
            if has_profile:
                raise SetupError("실행 방식을 지정할 프로그램은 --app 대신 --app-exe로만 등록하세요.")
            continue
        programs.append({"id": "app-" + str(index + 1), "name": path.stem, "exe": str(path), "enabled": True, "control_exes": [], "hints": "요청한 시험용 창만 사용합니다."})
        if app_launch_uri is not None:
            programs[-1]["launch"] = {"kind": "uri", "target": app_launch_uri}
        elif app_arguments or app_working_directory is not None:
            programs[-1]["launch"] = {"kind": "exe", "arguments": list(app_arguments)}
            if app_working_directory is not None:
                programs[-1]["launch"]["cwd"] = str(_path(app_working_directory))
        for control_exe in app_control_exes:
            control_path = _path(control_exe)
            if not control_path.is_file() or control_path.suffix.lower() != ".exe":
                raise SetupError("추가 조작 실행파일은 실제 .exe 파일을 지정하세요.")
            programs[-1]["control_exes"].append(str(control_path))
    if not apps and not app_exes:
        questions.append({"field": "app", "question": "어떤 프로그램에서 사용할까요?", "choices": {key: value[0] for key, value in APPS.items()}})
    result = {"ok": not questions, "status": "input_required" if questions else "ready_to_prepare", "questions": questions,
              "config_path": str(config_path), "driver_candidates": candidates, "programs": programs,
              "scope": scope, "project": str(project_path) if project_path else None,
              "screen_accessed": False, "driver_started": False, "claude_changed": False}
    if questions:
        return result
    config = validate_config({"version": 1, "driver": candidates[0], "programs": programs,
                              "mode": "uia", "approval": "client", "log_detail": "metadata",
                              "max_minutes": 10, "max_actions": 120, "approval_timeout_seconds": 300,
                              "state_dir": str(config_path.parent)})
    if existing is not None and existing != config:
        raise SetupError("기존 설정의 내용이 달라 덮어쓰지 않았습니다. 새 전용 설정 경로를 지정하거나 수동 설정에서 변경 내용을 확인해 주세요.")
    result["config"] = config
    result["summary"] = {"use_in": SCOPES[scope], "programs": [p["name"] for p in programs],
                         "driver": candidates[0], "settings": str(config_path), "limits": "허용한 프로그램만 · 10분 · 최대 120회 · 기본 정보 기록",
                         "approval": "이 MCP의 시작·조작 승인 창은 생략합니다. 연결한 Claude/MCP 프로그램의 승인 설정은 변경하지 않습니다.",
                         "screen_mode": "글자·버튼 정보 사용(UIA). 화면 읽기와 클릭·입력 기능이 있으며, 연결 완료만으로 작업을 시작하지 않습니다.",
                         "changes": "이 연결만 추가합니다. 기존 모델·로그인·다른 연결은 변경하지 않습니다."}
    return result


def prepare(config_path, **options) -> dict:
    result = inspect_setup(config_path, **options)
    if not result["ok"]:
        return result
    path = Path(result["config_path"])
    if result["scope"] != "export":
        status = register.registration_status(path, scope=result["scope"], project_dir=result["project"])
        if not status["ok"]:
            raise SetupError(status["message"])
    payload = {"format": 1, "version": VERSION, "bundle": str(APP_DIR), "config_path": str(path),
               "config": result["config"], "scope": result["scope"], "project": result["project"],
               "driver_sha256": _hash(Path(result["config"]["driver"])),
               "config_before_sha256": _hash(path), "entry": register.make_server_entry(path), "code_sha256": _code_fingerprint()}
    receipt_path = _path(path.parent / "install-receipt.json")
    if receipt_path.is_file():
        receipt = _read(receipt_path)
        if (path.is_file() and load_config(path) == result["config"]
                and _receipt_matches(receipt, payload) and _connection_present(payload)):
            return _receipt_result(receipt, changed=False, message="같은 설정으로 이미 연결되어 있습니다. 다시 설치하거나 점검하지 않았습니다.")
        raise SetupError("다른 설치 완료 기록이 있습니다. 기존 연결을 유지하고 새 설정 폴더를 지정해 주세요.")
    digest = _digest(payload)
    plan_path = path.parent / ("setup-plan-" + digest[:16] + ".json")
    _create(plan_path, {"digest": digest, "plan": payload})
    return {"ok": True, "status": "confirmation_required", "plan_path": str(plan_path), "digest": digest,
            "summary": result["summary"], "screen_accessed": False, "driver_started": False,
            "message": "이 계획을 사용자에게 설명하고 확인받은 뒤 apply를 실행하세요. 해시값은 사용자 승인 증거가 아닙니다."}


def _register(payload: dict) -> dict:
    if payload["scope"] == "export":
        path = register.export_config(payload["config_path"])
        return {"ok": True, "status": "exported", "connection_file": str(path), "scope": "export"}
    return register.register_claude(payload["config_path"], scope=payload["scope"], project_dir=payload["project"])


def _connection_present(payload: dict) -> bool:
    if payload["scope"] == "export":
        path = Path(payload["config_path"]).with_name("computer-use.mcp.json")
        return path.is_file() and _read(_path(path)) == {"mcpServers": {register.SERVER_NAME: payload["entry"]}}
    status = register.registration_status(payload["config_path"], scope=payload["scope"], project_dir=payload["project"])
    return bool(status.get("ok")) and status.get("status") == "registered"


def _check_plan_paths(payload: dict, config: dict) -> None:
    path = _path(payload["config_path"])
    if path.name.lower() in RESERVED or str(path) != payload["config_path"]:
        raise SetupError("전용 설정 파일의 정확한 위치를 확인해 주세요.")
    if payload["scope"] == "local":
        project = _path(payload["project"])
        if not project.is_dir() or str(project) != payload["project"]:
            raise SetupError("작업 폴더가 달라졌습니다. 다시 준비하세요.")
    driver = _path(config["driver"])
    if str(driver) != config["driver"] or _hash(driver) != payload["driver_sha256"]:
        raise SetupError("Driver 위치 또는 파일이 달라졌습니다. 다시 준비하세요.")
    for program in config["programs"]:
        for executable in [program["exe"], *program.get("control_exes", [])]:
            app = _path(executable)
            if not app.is_file() or str(app) != executable:
                raise SetupError("선택한 프로그램 위치가 달라졌습니다. 다시 준비하세요.")
        cwd = program.get("launch", {}).get("cwd")
        if cwd and not _path(cwd).is_dir():
            raise SetupError("등록한 시작 폴더가 없어졌습니다. 다시 준비하세요.")
    if payload.get("code_sha256") != _code_fingerprint():
        raise SetupError("배포 파일이 달라졌습니다. 현재 배포본으로 다시 준비하세요.")


def apply(plan_path, approval: str) -> dict:
    document = _read(_path(plan_path))
    payload = document.get("plan", {})
    digest = _digest(payload)
    if not approval or document.get("digest") != digest or approval != digest:
        raise SetupError("확인한 설치 계획과 일치하지 않습니다. 준비 결과를 다시 확인해 주세요.")
    if payload.get("format") != 1 or payload.get("bundle") != str(APP_DIR) or payload.get("version") != VERSION:
        raise SetupError("다른 배포본의 설치 계획입니다. 현재 배포본으로 다시 준비해 주세요.")
    path = _path(payload["config_path"])
    config = validate_config(payload["config"])
    if payload.get("scope") not in SCOPES or config["state_dir"] != str(path.parent):
        raise SetupError("설치 계획의 저장 위치 또는 사용 범위를 확인해 주세요.")
    _check_plan_paths(payload, config)
    if config["mode"] != "uia" or config["approval"] != "client" or config["log_detail"] != "metadata" or config["max_minutes"] != 10 or config["max_actions"] != 120:
        raise SetupError("대화형 설치의 기본 권한·제한은 변경할 수 없습니다. 수동 설정에서 별도로 검토하세요.")
    if not config["programs"] or not all(p["enabled"] for p in config["programs"]):
        raise SetupError("명시한 실행파일만 허용할 수 있습니다.")
    driver = _path(config["driver"])
    if driver.name.lower() != "cua-driver.exe" or not payload["driver_sha256"] or _hash(driver) != payload["driver_sha256"]:
        raise SetupError("준비 이후 Driver 파일이 바뀌었습니다. 새 파일을 확인하고 다시 준비하세요.")
    for program in config["programs"]:
        if not _path(program["exe"]).is_file():
            raise SetupError("선택한 프로그램 파일이 없어 설치하지 않았습니다.")
    if register.make_server_entry(path) != payload["entry"]:
        raise SetupError("MCP 실행 위치가 바뀌었습니다. 다시 준비해 주세요.")
    receipt_path = path.parent / "install-receipt.json"
    receipt = _read(_path(receipt_path)) if receipt_path.is_file() else None
    if receipt and receipt.get("plan_digest") != digest:
        raise SetupError("다른 설치 완료 기록이 있습니다. 새 설정 폴더를 사용하세요.")
    if receipt and not _receipt_matches(receipt, payload):
        raise SetupError("이전 완료 기록이 현재 배포본과 다릅니다. 다시 준비하세요.")
    if receipt and path.is_file() and load_config(path) == config and _connection_present(payload):
        return _receipt_result(receipt, changed=False, diagnostics_reused=True)
    if path.exists() and load_config(path) != config:
        raise SetupError("기존 설정이 바뀌어 덮어쓰지 않았습니다.")
    if not path.exists() and payload["config_before_sha256"] is not None:
        raise SetupError("확인했던 기존 설정 파일이 없어졌습니다. 다시 준비하세요.")
    if payload["scope"] != "export":
        status = register.registration_status(path, scope=payload["scope"], project_dir=payload["project"])
        if not status["ok"]:
            raise SetupError(status["message"])
    # The only execution phase: metadata/version and MCP handshake, never begin.
    report = run_diagnostics(config)
    if not report.get("ok"):
        return {"ok": False, "status": "diagnostics_failed", "checks": report.get("checks", []),
                "screen_accessed": False, "claude_changed": False,
                "message": "연결 점검을 통과하지 못해 설정·등록을 진행하지 않았습니다. 실패 항목만 확인해 주세요."}
    _check_plan_paths(payload, config)
    _create(path, config)
    connection = _register(payload)
    expected_status = "exported" if payload["scope"] == "export" else "registered"
    if not connection.get("ok") or connection.get("status") != expected_status or not _connection_present(payload):
        raise SetupError("등록 후 연결 상태를 확인하지 못했습니다. 설정을 유지했으며 자동으로 재시도하지 않습니다.")
    receipt = {"ok": True, "status": connection["status"], "version": VERSION, "plan_digest": digest,
               "bundle": str(APP_DIR), "code_sha256": payload["code_sha256"], "entry": payload["entry"],
               "config_path": str(path), "scope": payload["scope"], "project": payload["project"], "driver_sha256": payload["driver_sha256"],
               "connection_file": connection.get("connection_file"), "screen_accessed": False,
               "checks": report.get("checks", []), "message": "연결 준비를 마쳤습니다. 필요하면 Claude 대화를 다시 열어 연결을 확인하세요. 실제 화면 작업은 아직 시작하지 않았습니다."}
    _create(receipt_path, receipt)
    return _receipt_result(receipt, changed=True, receipt_path=str(receipt_path))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claude 대화에서 화면 작업 기능 연결하기")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("inspect", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--driver", type=Path)
        command.add_argument("--app", action="append", choices=tuple(APPS), default=[])
        command.add_argument("--app-exe", action="append", type=Path, default=[])
        command.add_argument("--app-launch-uri", help="한 개의 --app-exe에 연결할 전용 protocol:// 또는 http(s):// 주소")
        command.add_argument("--app-argument", action="append", default=[], help="한 개의 --app-exe에 쓸 인자. --app-argument=--flag 형태로 반복")
        command.add_argument("--app-working-directory", type=Path)
        command.add_argument("--app-control-exe", action="append", type=Path, default=[])
        command.add_argument("--scope", choices=tuple(SCOPES))
        command.add_argument("--project", type=Path)
    command = sub.add_parser("apply")
    command.add_argument("--plan", required=True, type=Path)
    command.add_argument("--approve", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "apply":
            result = apply(args.plan, args.approve)
        else:
            method = prepare if args.action == "prepare" else inspect_setup
            result = method(args.config, driver=args.driver, apps=args.app, app_exes=args.app_exe, scope=args.scope, project=args.project,
                            app_launch_uri=args.app_launch_uri, app_arguments=args.app_argument,
                            app_working_directory=args.app_working_directory, app_control_exes=args.app_control_exe)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "status": "setup_failed", "message": str(error), "retry_automatically": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") or result.get("status") == "input_required" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
