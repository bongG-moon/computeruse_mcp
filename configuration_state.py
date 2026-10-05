"""Read-only comparison of a running configuration with its exact source file.

Never reload settings, alter permissions, execute source code, or discover a
different configuration. Returned fingerprints identify validated configuration
content; no setting values or raw parse errors are included in the result.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import stat

from settings import VERSION as LOADED_VERSION, validate_config


SETTINGS_PATH = Path(__file__).with_name("settings.py")
MAX_CONFIG_BYTES = 1_000_000
MAX_SETTINGS_BYTES = 256_000


class _InvalidSource(ValueError):
    pass


def _read_local_file(path: Path, maximum: int) -> bytes:
    # Check before resolving: resolving a link or UNC path could read a remote
    # target rather than the local file named by the running server.
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise OSError("Untracked local path")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("Linked path")
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Not a regular file")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise _InvalidSource("Oversized source")
    return data


def _fingerprint(value: dict) -> str:
    checked = validate_config(value)
    encoded = json.dumps(checked, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _package_status() -> tuple[str | None, str]:
    try:
        tree = ast.parse(_read_local_file(SETTINGS_PATH, MAX_SETTINGS_BYTES).decode("utf-8-sig"))
        declarations = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
                declarations.append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "VERSION":
                declarations.append(node.value)
        if len(declarations) != 1 or not isinstance(declarations[0], ast.Constant):
            raise _InvalidSource("Version is not one literal")
        version = declarations[0].value
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?", version) or len(version) > 64:
            raise _InvalidSource("Invalid package version")
        return version, "current" if version == LOADED_VERSION else "changed"
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unavailable"
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return None, "invalid"


def configuration_status(config: dict, config_path: str | Path | None = None) -> dict:
    """Describe a config/package mismatch without applying anything.

    ``restart_required`` is true when a tracked source differs or cannot be
    validated. Invalid/missing files must be repaired before reconnecting. An
    absent ``config_path`` means untracked; no default path is guessed.
    """
    package_version, package_state = _package_status()
    value = {"source_state": "untracked", "restart_required": False,
             "loaded_fingerprint": None, "disk_fingerprint": None,
             "fingerprint_kind": "validated_config_sha256",
             "loaded_version": LOADED_VERSION, "package_version": package_version,
             "package_state": package_state, "message": "설정 파일 위치를 전달받지 않아 저장된 설정과 비교하지 않았습니다."}
    try:
        value["loaded_fingerprint"] = _fingerprint(config)
    except (ValueError, TypeError, KeyError, RecursionError):
        value.update(source_state="invalid", restart_required=True,
                     message="실행 중인 설정을 검증하지 못했습니다. 설정을 확인한 뒤 연결을 다시 시작하세요.")
        return value

    if config_path is not None:
        try:
            data = _read_local_file(Path(config_path), MAX_CONFIG_BYTES)
            value["disk_fingerprint"] = _fingerprint(json.loads(data.decode("utf-8-sig")))
            if value["loaded_fingerprint"] == value["disk_fingerprint"]:
                value.update(source_state="current", message="실행 중인 설정과 저장된 설정이 같습니다.")
            else:
                value.update(source_state="changed", restart_required=True,
                             message="저장된 설정이 실행 중인 설정과 다릅니다. 현재 작업을 끝내고 MCP 연결을 다시 열어 반영하세요. 설정은 자동 적용하지 않았습니다.")
        except FileNotFoundError:
            value.update(source_state="missing", restart_required=True,
                         message="사용하던 설정 파일이 없습니다. 실행 중인 설정은 유지됩니다. 파일을 복구·확인한 뒤 MCP 연결을 다시 여세요.")
        except OSError:
            value.update(source_state="unavailable", restart_required=True,
                         message="사용하던 로컬 설정 파일을 확인하지 못했습니다. 실행 중인 설정은 유지됩니다. 파일 위치와 접근 상태를 확인한 뒤 다시 연결하세요.")
        except (ValueError, TypeError, KeyError, RecursionError):
            value.update(source_state="invalid", restart_required=True,
                         message="저장된 설정이 올바르지 않습니다. 실행 중인 설정은 유지됩니다. 원본 설정을 복구·확인한 뒤 MCP 연결을 다시 여세요.")

    if package_state != "current":
        value["restart_required"] = True
        value["message"] += (" 배포 폴더의 버전이 실행 중인 버전과 다릅니다. 현재 작업을 끝낸 뒤 다시 연결하세요."
                             if package_state == "changed" else
                             " 배포 폴더의 버전을 확인하지 못했습니다. 배포 파일을 확인·복구한 뒤 다시 연결하세요.")
    return value
