"""Current-user Windows application catalog; never launches or installs apps."""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess

BUILTIN_PACKAGES = {"notepad": "Microsoft.WindowsNotepad", "calculator": "Microsoft.WindowsCalculator", "store": "Microsoft.WindowsStore"}
BUILTIN_NAMES = {"notepad": "메모장", "calculator": "계산기", "store": "Microsoft Store", "chrome": "Google Chrome"}
BUILTIN_PROTOCOLS = {"calculator": "calculator:", "store": "ms-windows-store:"}

# No interpolated path, user query, remote request, or all-user inventory. Windows
# supplies the exact installed executable from each current user's manifest.
PACKAGE_QUERY = r"""[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$rows = @(foreach ($name in @('Microsoft.WindowsNotepad','Microsoft.WindowsCalculator','Microsoft.WindowsStore')) {
  foreach ($package in @(Get-AppxPackage -Name $name -ErrorAction SilentlyContinue)) {
    if ($package.PublisherId -ne '8wekyb3d8bbwe' -or $package.IsFramework -or $package.IsResourcePackage) { continue }
    try {
      $manifest = Get-AppxPackageManifest -Package $package.PackageFullName -ErrorAction Stop
      foreach ($app in @($manifest.Package.Applications.Application)) {
        if ($app.Executable) {
          [pscustomobject]@{Name=$package.Name; PublisherId=$package.PublisherId; InstallLocation=$package.InstallLocation; Executable=[string]$app.Executable; ApplicationId=[string]$app.Id}
        }
      }
    } catch {}
  }
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
"""


def query_packages(*, run=subprocess.run):
    if os.name != "nt": return []
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file(): return []
    try:
        result = run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", PACKAGE_QUERY],
            capture_output=True, timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or len(result.stdout) > 256000: return []
        rows = json.loads(result.stdout.decode("utf-8-sig", "strict"))
        return rows if isinstance(rows, list) and len(rows) <= 100 else []
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
        return []


def package_executables(rows):
    found = {name: [] for name in BUILTIN_PACKAGES}
    if not isinstance(rows, list): return found
    reverse = {package: builtin for builtin, package in BUILTIN_PACKAGES.items()}
    for row in rows[:100]:
        if not isinstance(row, dict) or row.get("Name") not in reverse or row.get("PublisherId") != "8wekyb3d8bbwe": continue
        folder, raw = row.get("InstallLocation"), row.get("Executable")
        if not isinstance(folder, str) or not isinstance(raw, str) or len(folder) > 32767 or len(raw) > 1000: continue
        root, relative = PureWindowsPath(folder), PureWindowsPath(raw)
        if (not root.is_absolute() or folder.startswith(("\\\\", "//")) or relative.is_absolute() or relative.drive or relative.root
                or ".." in relative.parts or relative.suffix.lower() != ".exe"
                or any(ord(c) < 32 or c in '<>"|*?%' for c in folder+raw)):
            continue
        path = str(root / relative)
        key = reverse[row["Name"]]
        if path.casefold() not in {p.casefold() for p in found[key]}: found[key].append(path)
    return found


def builtin_catalog(*, package_rows=None, first=None, app_path=None):
    from vendor.windows import _first, _app_path
    first, app_path = first or _first, app_path or _app_path
    found = package_executables(query_packages() if package_rows is None else package_rows)
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    notepad = first(app_path("notepad.exe"), windir / "System32/notepad.exe")
    chrome = first(app_path("chrome.exe"), pf / "Google/Chrome/Application/chrome.exe",
        pf86 / "Google/Chrome/Application/chrome.exe", local / "Google/Chrome/Application/chrome.exe")
    results = {}
    for builtin in BUILTIN_NAMES:
        paths = found.get(builtin, [])
        # Multiple package application executables are explicit control targets,
        # never a broad folder or wildcard allowlist.
        exe = notepad if builtin == "notepad" else chrome if builtin == "chrome" else paths[0] if paths else ""
        controls = [path for path in paths if path.casefold() != exe.casefold()]
        if builtin == "calculator" and not exe:
            exe = first(windir / "System32/calc.exe")
        entry = {"id": "browser" if builtin == "chrome" else builtin, "builtin": builtin,
            "name": BUILTIN_NAMES[builtin], "exe": exe, "control_exes": controls,
            "available": bool(exe), "hints": "사용자가 요청한 화면 작업을 수행하고 실제 결과를 확인합니다."}
        if builtin in BUILTIN_PROTOCOLS and paths: entry["launch"] = {"kind": "builtin", "id": builtin}
        results[builtin] = entry
    return results


def resolve_builtin(name):
    if name not in BUILTIN_NAMES: raise ValueError("기본 프로그램은 notepad, calculator, store, chrome 중에서 지정하세요.")
    item = builtin_catalog()[name]
    if not item["available"]:
        raise ValueError("이 Windows 사용자에게 설치된 프로그램을 찾지 못했습니다. 프로그램을 한 번 연 뒤 열린 창 목록에서 선택하세요.")
    return item


def is_previous_package_path(path, builtin, current_paths):
    """Recognize only this family's versioned WindowsApps paths in the same
    root as a current-user manifest. Never match a basename or folder prefix
    alone. This lets explicit registration retire an obsolete package version.
    """
    package = BUILTIN_PACKAGES.get(builtin)
    if package is None or not isinstance(path, str): return False
    pattern = re.compile(re.escape(package) + r"_\d+\.\d+\.\d+\.\d+_(?:x86|x64|arm64|neutral)_[^\\/]*_8wekyb3d8bbwe", re.I)
    def root(value):
        if not isinstance(value, str): return None
        item = PureWindowsPath(value)
        if not item.is_absolute() or item.suffix.casefold() != ".exe" or ".." in item.parts: return None
        for parent in item.parents:
            if pattern.fullmatch(parent.name) and parent.parent.name.casefold() == "windowsapps":
                return str(parent.parent).casefold()
        return None
    previous_root = root(path)
    return previous_root is not None and previous_root in {root(p) for p in current_paths}
