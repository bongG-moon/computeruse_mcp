"""Explicit user/local Claude MCP registration. Importing changes nothing.

User configuration is read for conflict checks; only the official CLI writes it.
No health-check command is run, because it could start unrelated MCP servers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


APP_DIR = Path(__file__).resolve().parent
# Claude reserves "computer-use" for a built-in integration.
SERVER_NAME = "local-computer-use"
# SHA256 of SHA256SUMS.txt from the actual completed release ZIPs. Every file
# listed in each manifest was verified against that ZIP before adding it here.
# Version labels alone are never trusted; a foreign same-name MCP is preserved.
# This proves these exact distributions, not signed-code security.
PREVIOUS_MANIFESTS = {
    "fa9db7c4f1371c014dcb558f2b2fb89732dc31e89957bd9b16bd93370919b8af": "0.7.1",
    "6d0c32b2338ddc185a4cc15cc0d52ea447e5bad1415df6b1a38601828c7bd47d": "0.7.0",
    "1778b37b2ed5fc839dcf0de1a7a11c145f96693269981eb70935f2968dff58a8": "0.6.0",
    "17e2c4cb787c6b54f001b0bbed58c88adb949d1c14f4b4e2b22cd43b82bb8ba9": "0.5.0",
    "fd408ef370fe7cd779e1dd985b35db955549c2888c6fa493d8038a2c9758b4ef": "0.1.0",
    "8e294e82ad446935a05d664b728fcbc994c2430e5c6836b5e6620bce5b44ee44": "0.2.0",
    "af4b7283c2ecd4a36e21bb58191c090152f1c58ec12dae2223bab4d58a29ce0e": "0.3.0",
    "1f475449932427a56428adca1a3a65aa878b2963bea921aae8c3a8daf3088033": "0.3.1",
    "fb4a839ac93f93a08f8f6e1d6d326aca36c58645be837ac3a7295b05acaf41b9": "0.3.2",
    "2e3f60cab88b9bc5a8afa55f610cf6db3be34508b81de4d0247e27cc5ee91298": "0.3.3",
    "59164c690d3419eebd767f620bdb8ff0638717de890c15363db07fa7067db326": "0.4.0",
}


class RegistrationError(ValueError):
    """A registration operation could not be verified safely."""


def _native_claude() -> str:
    from vendor.windows import native_claude
    value = native_claude()
    if not value or Path(value).suffix.lower() != ".exe" or not Path(value).is_file():
        raise RegistrationError("현재 사용자의 Claude Code 실행파일을 찾지 못했습니다. JSON 내보내기로 연결할 수 있습니다.")
    return str(Path(value).resolve())


def user_config_file() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    directory = Path(configured).expanduser().resolve() if configured else Path.home()
    return directory / ".claude.json"


def _read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise RegistrationError(f"기존 설정을 읽지 못해 변경하지 않았습니다: {path}") from error
    if not isinstance(value, dict):
        raise RegistrationError(f"기존 설정 형식이 올바르지 않아 변경하지 않았습니다: {path}")
    return value


def _servers(document: dict, source: Path) -> dict:
    value = document.get("mcpServers", {})
    if not isinstance(value, dict):
        raise RegistrationError(f"기존 MCP 설정 형식이 올바르지 않아 변경하지 않았습니다: {source}")
    return value


def make_server_entry(config_path: str | Path) -> dict:
    config = Path(config_path).expanduser().resolve()
    server = APP_DIR / "server.py"
    bundled = APP_DIR / "runtime" / "python.exe"
    console = Path(sys.executable).with_name("python.exe")
    python = bundled if bundled.is_file() else console
    if not python.is_file() or not server.is_file():
        raise RegistrationError("MCP 실행파일이 없습니다. 배포 ZIP 전체를 압축 해제해 주세요.")
    entry = _entry_for_paths(python, server, config)
    bridge = APP_DIR / "Computer Use MCP 관리자 연결.exe"
    if bridge.is_file():
        from consent import _plain_chain
        if not _plain_chain(bridge):
            raise RegistrationError("관리자 연결 실행파일의 실제 위치를 확인하지 못했습니다.")
        entry["command"] = str(bridge.resolve())
        entry["args"] = ["--config", str(config)]
    elif bundled.is_file() and (APP_DIR / "BUILD-MANIFEST.json").is_file():
        manifest = _read_object(APP_DIR / "BUILD-MANIFEST.json")
        if manifest.get("product") == "Computer-Use-MCP" and manifest.get("version") in {"0.7.0", "0.7.1", "0.8.0"}:
            raise RegistrationError("관리자 연결 실행파일이 없습니다. ZIP 전체를 다시 압축 해제해 주세요. 일반 권한 연결로 대체하지 않았습니다.")
    return entry


def _entry_for_paths(python: Path, server: Path, config: Path) -> dict:
    runtime = python.parent.resolve()
    return {"type": "stdio", "command": str(python.resolve()),
            "args": ["-B", "-s", str(server.resolve()), "--config", str(config)],
            "env": {"PYTHONHOME": str(runtime), "PYTHONPATH": "", "PYTHONSTARTUP": "",
                    "PYTHONUSERBASE": "", "PYTHONINSPECT": "", "PYTHONSAFEPATH": "",
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TCL_LIBRARY": str(runtime / "tcl" / "tcl8.6"),
                    "TK_LIBRARY": str(runtime / "tcl" / "tk8.6")}}


def _previous_entry(existing) -> dict | None:
    """Prove an intact known distribution and its exact generated CLI arguments."""
    from consent import _plain_chain
    if not isinstance(existing, dict):
        return None
    try:
        args = existing.get("args")
        if not isinstance(args, list):
            return None
        bridge = None
        command = Path(existing["command"])
        if len(args) == 5 and args[:2] == ["-B", "-s"] and args[3] == "--config":
            python, server, config = command, Path(args[2]), Path(args[4])
        elif len(args) == 2 and args[0] == "--config" and command.name == "Computer Use MCP 관리자 연결.exe":
            bridge = command
            python, server, config = command.parent / "runtime/python.exe", command.parent / "server.py", Path(args[1])
        else:
            return None
        if not all(p.is_absolute() for p in (python, server, config)):
            return None
        base = server.parent
        if server.name != "server.py" or python != base / "runtime" / "python.exe" or not _plain_chain(base):
            return None
        expected_entry = _entry_for_paths(python, server, config)
        if bridge is not None:
            expected_entry.update(command=str(bridge), args=["--config", str(config)])
        if existing != expected_entry:
            return None
        manifest = base / "SHA256SUMS.txt"
        if not _plain_chain(manifest) or manifest.stat().st_size > 1_000_000:
            return None
        manifest_bytes = manifest.read_bytes()
        version = PREVIOUS_MANIFESTS.get(hashlib.sha256(manifest_bytes).hexdigest())
        if not version:
            return None
        if bridge is not None and version not in {"0.7.0", "0.7.1"}:
            return None
        checked_folders = set()
        checked_names = set()
        for line in manifest_bytes.decode("utf-8").splitlines():
            digest, name = line.split("  ", 1)
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                return None
            file = base / name
            if file.parent not in checked_folders:
                if not _plain_chain(file.parent):
                    return None
                checked_folders.add(file.parent)
            info = file.lstat()
            if file.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or not file.is_file():
                return None
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                return None
            checked_names.add(name)
        if bridge is not None and bridge.name not in checked_names:
            return None
        return {"version": version, "previous_command": str(python), "previous_server": str(server),
                "previous_config": str(config)}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def export_config(config_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Write a new standalone MCP JSON file; never merge into a user's profile."""
    config = Path(config_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve() if output_path is not None else config.with_name("computer-use.mcp.json")
    if output == config or output == user_config_file().resolve() or output.name in {"settings.json", ".claude.json", ".mcp.json"}:
        raise RegistrationError("기존 프로그램 설정파일 대신 별도의 JSON 파일 이름을 선택해 주세요.")
    document = {"mcpServers": {SERVER_NAME: make_server_entry(config)}}
    if output.exists():
        if _read_object(output) == document:
            return output
        raise RegistrationError("선택한 파일에 다른 내용이 있습니다. 새 파일 이름으로 내보내 주세요.")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError as error:
        raise RegistrationError("같은 이름의 파일이 생겼습니다. 새 파일 이름으로 내보내 주세요.") from error
    return output


def _scope_project(scope: str, project_dir: str | Path | None) -> Path | None:
    if scope not in {"user", "local"}:
        raise RegistrationError("등록 범위는 user 또는 local만 선택할 수 있습니다.")
    if scope == "user":
        if project_dir is not None:
            raise RegistrationError("--project는 local 범위에서만 사용합니다.")
        return None
    if project_dir is None:
        raise RegistrationError("local 범위에는 --project로 작업 폴더를 명시해 주세요.")
    project = Path(project_dir).expanduser()
    if not project.is_absolute() or not project.is_dir():
        raise RegistrationError("local 작업 폴더는 실제 존재하는 폴더의 절대경로여야 합니다.")
    project = project.resolve()
    _check_local_project_boundary(project)
    return project


def _check_local_project_boundary(project: Path) -> None:
    """Claude's local scope uses the nearest Git root, not always its cwd.

    Do not silently widen a requested child folder to an ancestor repository.
    Treat .git files (worktrees/submodules) as boundaries as well as directories.
    Inspect paths only; never invoke Git, edit its settings, or initialize a repo.
    """
    overrides = [name for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
                 if os.environ.get(name, "").strip()]
    if overrides:
        raise RegistrationError("Git 작업 위치를 바꾸는 환경변수가 있어 local 적용 범위를 확인하지 못했습니다: "
                                + ", ".join(overrides)
                                + ". 환경변수와 기존 설정은 변경하지 않았습니다. 해당 환경변수가 없는 실행 환경에서 다시 확인해 주세요.")
    for folder in (project, *project.parents):
        marker = folder / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RegistrationError("Git 프로젝트 경계를 확인하지 못해 local 등록을 변경하지 않았습니다: " + str(folder)) from error
        if folder != project:
            raise RegistrationError("이 폴더의 local 연결은 상위 Git 프로젝트에 적용될 수 있어 등록하지 않았습니다: "
                                    + str(folder)
                                    + ". 해당 프로젝트 전체 사용이 맞으면 정확한 프로젝트 root를 선택하거나, Git 저장소 밖의 전용 독립 폴더를 선택해 주세요. 적용 범위는 자동 변경하지 않았습니다.")
        return


def _projects(document: dict) -> dict:
    projects = document.get("projects", {})
    if not isinstance(projects, dict):
        raise RegistrationError("기존 Claude 프로젝트 설정을 확인하지 못해 변경하지 않았습니다.")
    return projects


def _project_details(document: dict, project: Path) -> tuple[str | None, dict]:
    """Match Claude's forward-slash keys without confusing other projects."""
    expected = os.path.normcase(str(project))
    matches = []
    for name, details in _projects(document).items():
        candidate = Path(name)
        if candidate.is_absolute() and os.path.normcase(str(candidate.resolve())) == expected:
            matches.append((name, details))
    if len(matches) > 1:
        raise RegistrationError("같은 작업 폴더를 가리키는 Claude 프로젝트 설정이 여러 개여서 변경하지 않았습니다.")
    if not matches:
        return None, {}
    name, details = matches[0]
    if not isinstance(details, dict):
        raise RegistrationError("선택한 Claude 프로젝트 설정 형식이 올바르지 않아 변경하지 않았습니다.")
    return name, details


def _scoped_servers(document: dict, path: Path, project: Path | None) -> dict:
    return _servers(document if project is None else _project_details(document, project)[1], path)


def _scope_fields(scope: str, project: Path | None) -> dict:
    return {"scope": scope, "project_dir": str(project) if project is not None else None}


def _other_scopes(document: dict, user_file: Path, project: Path | None = None) -> list[str]:
    """Inspect known local/project scopes without loading any MCP process."""
    found = []
    projects = _projects(document)
    if project is None:
        for name, details in projects.items():
            if isinstance(details, dict) and SERVER_NAME in _servers(details, user_file):
                found.append("local: " + str(name))
        bases = (APP_DIR.resolve(), Path.cwd().resolve())
    else:
        # A different project's local connection is not active in this project.
        if SERVER_NAME in _servers(document, user_file):
            found.append("user: " + str(user_file))
        bases = (project,)
    seen = set()
    for base in bases:
        for folder in (base, *base.parents):
            candidate = folder / ".mcp.json"
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file() and SERVER_NAME in _servers(_read_object(candidate), candidate):
                found.append("project: " + str(candidate))
    return found


def registration_status(config_path: str | Path, scope: str = "user",
                        project_dir: str | Path | None = None) -> dict:
    project = _scope_project(scope, project_dir)
    entry = make_server_entry(config_path)
    path = user_config_file()
    document = _read_object(path)
    servers = _scoped_servers(document, path, project)
    existing = servers.get(SERVER_NAME)
    others = _other_scopes(document, path, project)
    upgrade = None
    label = "현재 사용자 범위" if project is None else "지정한 작업 폴더의 local 범위"
    if project is not None and others:
        status, message = "scope_conflict", "다른 범위에 같은 이름의 MCP가 있습니다. 기존 등록 범위를 먼저 확인해 주세요."
    elif existing == entry:
        status, message = "registered", f"{label}에 이 MCP가 등록되어 있습니다. 새 Claude 연결에서 사용할 수 있습니다."
        if others:
            message += " 다른 범위에도 같은 이름의 등록이 있어, 해당 작업 폴더에서는 그 등록이 우선할 수 있습니다."
    elif SERVER_NAME in servers:
        upgrade = _previous_entry(existing) if not others else None
        if upgrade:
            status, message = "upgrade_available", "이 도구의 이전 배포본 연결을 확인했습니다. '이전 연결을 새 버전으로 바꾸기'에서 경로를 확인한 뒤 갱신할 수 있습니다."
        else:
            status, message = "conflict", "같은 이름의 다른 MCP가 있거나 이전 배포본을 확인하지 못해 자동 변경하지 않습니다."
    elif others:
        status, message = "scope_conflict", "다른 범위에 같은 이름의 MCP가 있습니다. 기존 등록 범위를 먼저 확인해 주세요."
    else:
        status, message = "not_registered", f"{label}에 아직 등록하지 않았습니다."
    return {"ok": status not in {"conflict", "scope_conflict"}, "status": status, "message": message,
            "config_file": str(path), "other_scopes": others, **_scope_fields(scope, project),
            "can_upgrade": bool(upgrade), **(upgrade or {})}


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
                "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"})
    if env.get("CLAUDE_CONFIG_DIR", "").strip():
        env["CLAUDE_CONFIG_DIR"] = str(Path(env["CLAUDE_CONFIG_DIR"]).expanduser().resolve())
    return env


def _run_cli(executable: str, arguments: list[str], project: Path | None = None) -> subprocess.CompletedProcess:
    if project is not None:
        # A Git marker or scope override can appear while probing CLI support.
        _check_local_project_boundary(project)
    try:
        result = subprocess.run([executable, "mcp", *arguments], cwd=project if project is not None else APP_DIR, env=_cli_env(),
                                shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistrationError("Claude 등록 명령을 완료했는지 확인하지 못했습니다. 현재 등록 상태를 다시 확인해 주세요.") from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "명령 실패").strip()[:1500]
        raise RegistrationError("Claude 등록 명령이 거절되었습니다: " + detail)
    return result


def _check_cli_support(executable: str, operation: str, scope: str = "user", project: Path | None = None) -> None:
    help_text = _run_cli(executable, [operation, "--help"], project).stdout
    if "--scope" not in help_text or scope not in help_text or (operation == "add-json" and "<json>" not in help_text):
        raise RegistrationError(f"설치된 Claude Code의 {scope} 범위 등록 옵션을 확인하지 못했습니다. JSON 내보내기를 사용해 주세요.")


def _verify_unrelated(before: dict, after: dict, path: Path, project: Path | None = None) -> None:
    excluded = "mcpServers" if project is None else "projects"
    for key, value in before.items():
        if key != excluded and (key not in after or after[key] != value):
            raise RegistrationError("CLI 명령 후 다른 사용자 설정의 변화가 발견되었습니다. 등록 상태와 기존 설정을 확인해 주세요. 자동 복원은 하지 않았습니다.")
    if project is not None:
        old_name, old_details = _project_details(before, project)
        new_name, new_details = _project_details(after, project)
        new_projects = _projects(after)
        if _servers(before, path) != _servers(after, path):
            raise RegistrationError("CLI 명령 후 사용자 범위 MCP 등록이 바뀌었습니다. 자동 복원은 하지 않았습니다.")
        if {name: details for name, details in _projects(before).items() if name != old_name} != {
                name: details for name, details in new_projects.items() if name != new_name}:
            raise RegistrationError("CLI 명령 후 다른 프로젝트 설정의 변화가 발견되었습니다. 자동 복원은 하지 않았습니다.")
        if old_name is not None and old_name != new_name:
            raise RegistrationError("CLI 명령 후 기존 프로젝트 설정 경로가 바뀌었습니다. 자동 복원은 하지 않았습니다.")
        for key, value in old_details.items():
            if key != "mcpServers" and (key not in new_details or new_details[key] != value):
                raise RegistrationError("CLI 명령 후 해당 프로젝트의 다른 설정이 바뀌었습니다. 자동 복원은 하지 않았습니다.")
    old_servers, new_servers = _scoped_servers(before, path, project), _scoped_servers(after, path, project)
    if project is not None and {name: value for name, value in old_servers.items() if name != SERVER_NAME} != {
            name: value for name, value in new_servers.items() if name != SERVER_NAME}:
        raise RegistrationError("CLI 명령 후 해당 프로젝트의 다른 MCP 등록이 바뀌었습니다. 자동 복원은 하지 않았습니다.")
    if any(name != SERVER_NAME and (name not in new_servers or new_servers[name] != value) for name, value in old_servers.items()):
        raise RegistrationError("CLI 명령 후 다른 MCP 등록의 변화가 발견되었습니다. 기존 설정을 확인해 주세요. 자동 복원은 하지 않았습니다.")


def register_claude(config_path: str | Path, scope: str = "user",
                    project_dir: str | Path | None = None) -> dict:
    """Register exactly one selected-scope entry through the official CLI."""
    project = _scope_project(scope, project_dir)
    config = Path(config_path).expanduser().resolve()
    if not config.is_file():
        raise RegistrationError("먼저 Computer Use MCP 설정을 저장해 주세요.")
    entry = make_server_entry(config)
    path = user_config_file()
    before = _read_object(path)
    servers = _scoped_servers(before, path, project)
    existing = servers.get(SERVER_NAME)
    conflicts = _other_scopes(before, path, project)
    if project is not None and conflicts:
        raise RegistrationError(f"다른 범위에 {SERVER_NAME} 등록이 있어 변경하지 않았습니다: " + "; ".join(conflicts))
    if existing == entry:
        return {**registration_status(config, scope, project), "changed": False}
    if SERVER_NAME in servers:
        raise RegistrationError(f"{SERVER_NAME} 이름에 다른 MCP가 등록되어 있어 변경하지 않았습니다.")
    if conflicts:
        raise RegistrationError(f"다른 범위에 {SERVER_NAME} 등록이 있어 변경하지 않았습니다: " + "; ".join(conflicts))
    executable = _native_claude()
    _check_cli_support(executable, "add-json", scope, project)
    # Recheck after probing the CLI, before any registration write.
    current = _read_object(path)
    if current != before:
        raise RegistrationError("확인 중 Claude 설정이 변경되었습니다. 등록 상태를 다시 확인해 주세요.")
    if _other_scopes(current, path, project):
        raise RegistrationError("확인 중 다른 범위에 같은 이름의 MCP가 생겨 등록하지 않았습니다.")
    _run_cli(executable, ["add-json", SERVER_NAME, json.dumps(entry, ensure_ascii=False), "--scope", scope], project)
    after = _read_object(path)
    if _scoped_servers(after, path, project).get(SERVER_NAME) != entry:
        raise RegistrationError("명령 실행 후 선택한 범위의 동일한 등록을 확인하지 못했습니다. 자동으로 다시 등록하지 않습니다.")
    _verify_unrelated(before, after, path, project)
    return {**registration_status(config, scope, project), "changed": True}


def unregister_claude(config_path: str | Path, scope: str = "user",
                      project_dir: str | Path | None = None) -> dict:
    """Remove only an exact entry in the selected scope; preserve all others."""
    project = _scope_project(scope, project_dir)
    entry = make_server_entry(config_path)
    path = user_config_file()
    before = _read_object(path)
    servers = _scoped_servers(before, path, project)
    if SERVER_NAME not in servers:
        return {"ok": True, "status": "not_registered", "changed": False, **_scope_fields(scope, project),
                "config_file": str(path), "message": "삭제할 선택 범위 등록이 없습니다. 다른 범위의 등록은 변경하지 않았습니다."}
    if servers[SERVER_NAME] != entry:
        raise RegistrationError("현재 등록은 이 배포본의 실행파일·인수·환경과 다릅니다. 다른 등록을 삭제하지 않았습니다.")
    executable = _native_claude()
    _check_cli_support(executable, "remove", scope, project)
    if _read_object(path) != before:
        raise RegistrationError("확인 중 Claude 설정이 변경되었습니다. 등록 상태를 다시 확인해 주세요.")
    _run_cli(executable, ["remove", SERVER_NAME, "--scope", scope], project)
    after = _read_object(path)
    if SERVER_NAME in _scoped_servers(after, path, project):
        raise RegistrationError("선택한 범위의 등록 해제를 확인하지 못했습니다. 자동으로 다시 삭제하지 않습니다.")
    _verify_unrelated(before, after, path, project)
    label = "사용자 범위" if project is None else "지정한 작업 폴더의 local 범위"
    return {"ok": True, "status": "removed", "changed": True, **_scope_fields(scope, project), "config_file": str(path),
            "message": f"이 배포본의 {label} 연결만 해제했습니다. 이미 열린 Claude 연결은 다시 시작해 주세요."}


def upgrade_claude(config_path: str | Path, scope: str = "user",
                   project_dir: str | Path | None = None) -> dict:
    """Explicit UI action; replace only a proven, intact previous distribution."""
    project = _scope_project(scope, project_dir)
    config = Path(config_path).expanduser().resolve()
    if not config.is_file():
        raise RegistrationError("먼저 새 설정을 저장해 주세요.")
    entry = make_server_entry(config)
    path = user_config_file()
    before = _read_object(path)
    existing = _scoped_servers(before, path, project).get(SERVER_NAME)
    if existing == entry:
        return {**registration_status(config, scope, project), "changed": False}
    proof = _previous_entry(existing)
    if not proof or _other_scopes(before, path, project):
        raise RegistrationError("이 도구의 선택한 범위 이전 연결을 확인하지 못해 변경하지 않았습니다.")
    executable = _native_claude()
    _check_cli_support(executable, "remove")
    _check_cli_support(executable, "add-json")
    if _read_object(path) != before:
        raise RegistrationError("확인 중 Claude 설정이 변경되었습니다. 연결 상태를 다시 확인해 주세요.")
    try:
        _run_cli(executable, ["remove", SERVER_NAME, "--scope", scope], project)
        removed = _read_object(path)
        _verify_unrelated(before, removed, path, project)
        if SERVER_NAME in _scoped_servers(removed, path, project):
            raise RegistrationError("이전 연결 해제를 확인하지 못했습니다.")
        # Recheck immediately before adding; never overwrite a concurrent entry.
        if _read_object(path) != removed:
            raise RegistrationError("갱신 중 설정이 바뀌어 새 연결을 추가하지 않았습니다.")
        _run_cli(executable, ["add-json", SERVER_NAME, json.dumps(entry, ensure_ascii=False), "--scope", scope], project)
        after = _read_object(path)
        _verify_unrelated(before, after, path, project)
        if _scoped_servers(after, path, project).get(SERVER_NAME) != entry:
            raise RegistrationError("새 연결을 확인하지 못했습니다.")
    except RegistrationError as error:
        # A CLI failure may occur after writing. Inspect before any recovery.
        current = _read_object(path)
        _verify_unrelated(before, current, path, project)
        current_servers = _scoped_servers(current, path, project)
        if current_servers.get(SERVER_NAME) == entry:
            return {**registration_status(config, scope, project), "changed": True, "previous_version": proof["version"]}
        if SERVER_NAME not in current_servers and _read_object(path) == current:
            try:
                _run_cli(executable, ["add-json", SERVER_NAME, json.dumps(existing, ensure_ascii=False), "--scope", scope], project)
                restored = _read_object(path)
                _verify_unrelated(before, restored, path, project)
                if _scoped_servers(restored, path, project).get(SERVER_NAME) == existing:
                    raise RegistrationError("새 연결 갱신에 실패하여 이전 연결을 복원했습니다. 기존 Claude 연결은 유지됩니다.") from error
            except RegistrationError as restore_error:
                if _scoped_servers(_read_object(path), path, project).get(SERVER_NAME) == existing:
                    raise RegistrationError("새 연결 갱신에 실패하여 이전 연결을 복원했습니다.") from error
                raise RegistrationError("연결 갱신과 이전 연결 복원을 확인하지 못했습니다. 현재 연결 상태를 확인한 뒤 다시 연결해 주세요.") from restore_error
        raise RegistrationError("연결 갱신을 완료하지 못했습니다. 다른 등록은 덮어쓰지 않았습니다.") from error
    return {**registration_status(config, scope, project), "changed": True, "previous_version": proof["version"],
            "message": "이 도구의 이전 연결만 새 배포본으로 바꿨습니다. Claude Code를 다시 열어 주세요."}


def main() -> int:
    parser = argparse.ArgumentParser(description="Computer Use MCP 연결 설정")
    parser.add_argument("action", choices=("export", "status", "register", "unregister", "upgrade"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scope", choices=("user", "local"), default="user")
    parser.add_argument("--project", type=Path, help="local 등록을 사용할 기존 작업 폴더의 절대경로")
    args = parser.parse_args()
    try:
        if args.action == "export":
            if args.scope != "user" or args.project is not None:
                raise RegistrationError("export는 별도 연결 파일만 만들며 --scope와 --project를 사용하지 않습니다.")
            print(export_config(args.config, args.output))
        else:
            method = {"status": registration_status, "register": register_claude, "unregister": unregister_claude,
                      "upgrade": upgrade_claude}[args.action]
            print(json.dumps(method(args.config, scope=args.scope, project_dir=args.project), ensure_ascii=False, indent=2))
    except (RegistrationError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
