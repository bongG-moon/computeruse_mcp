"""Read-only teaching feature and package-file diagnostics; never launch helpers."""
from __future__ import annotations

from pathlib import Path

from settings import VERSION


ELEMENT_PICKER_FILE = "Computer Use MCP 요소 선택.exe"
PROCESS_EDITOR_FILE = "Computer Use MCP 프로세스 만들기.exe"
VISUAL_HELPER_FILE = "Computer Use MCP 이미지 도구.exe"
TEACHING_HELPER_FILES = (ELEMENT_PICKER_FILE, PROCESS_EDITOR_FILE, VISUAL_HELPER_FILE)


def _helper_file(app_dir: Path, filename: str) -> dict:
    path = app_dir / filename
    try:
        present = path.is_file()
        state = "present" if present else "missing"
    except OSError:
        present, state = False, "unavailable"
    return {"filename": filename, "path": str(path), "file_present": present,
            "inspection_state": state}


def teaching_capabilities(app_dir: Path) -> dict:
    """Report support and file presence, not whether Windows can show a picker.

    No Driver, desktop, user settings, registered app, or client connection is
    accessed. Even an empty helper file is only `file_present`; successful
    native startup must be established by its actual teaching/editor request.
    """
    root = Path(app_dir).resolve()
    helpers = {name: _helper_file(root, name) for name in TEACHING_HELPER_FILES}
    missing = [name for name, item in helpers.items() if item["inspection_state"] == "missing"]
    unreadable = [name for name, item in helpers.items() if item["inspection_state"] == "unavailable"]
    code = ("helper_files_unavailable" if unreadable else
            "helper_files_missing" if missing else "helper_files_present_unverified")
    if unreadable:
        message = "일부 도우미 파일의 존재를 확인하지 못했습니다. UIA 선택 기능 미지원으로 판단하지 말고 배포 폴더 접근 상태를 확인하세요."
    elif missing:
        message = "일부 도우미 파일이 없습니다. UIA 선택 기능 미지원이 아닙니다. 같은 버전의 배포 ZIP 전체를 풀고 해당 폴더로 MCP를 다시 연결하세요."
    else:
        message = "도우미 파일이 있습니다. 실행이나 창 표시는 아직 검사하지 않았습니다. 실제 학습 요청의 단계와 진단 코드를 확인하세요."
    return {
        "format": "computer-teaching-capabilities/v1",
        "version": VERSION,
        "probe_scope": "files_only",
        "readiness": "not_tested",
        "screen_accessed": False,
        "identity": {"app_directory": str(root), "server_path": str(root / "server.py"),
                     "module_path": str(Path(__file__).resolve())},
        "direct_picker": {
            "supported": True, "supported_modes": ["uia"],
            "tool": "computer_teach_element", "status_tool": "computer_teach_status",
            "helper": helpers[ELEMENT_PICKER_FILE], "launch_verified": False,
            "readiness": "not_tested",
            "visible_confirmation": "awaiting_selection with picker_visible=true",
        },
        "image_process": {
            "supported": True, "tool": "computer_process_editor",
            "status_tool": "computer_process_status",
            "helpers": [helpers[PROCESS_EDITOR_FILE], helpers[VISUAL_HELPER_FILE]],
            "launch_verified": False, "readiness": "not_tested",
            "message": "이미지 선택·동작 녹화는 프로세스 편집기에서 사용합니다. computer_teach_element의 UIA 학습과 구분하세요.",
        },
        "diagnostics": {"code": code, "missing_files": missing,
                        "unavailable_files": unreadable, "message": message},
    }
