"""Assemble the offline Windows bundle using an already installed CPython.

No downloads, package installation, registry writes, or administrator elevation.
Run --check for a source/compiler check without assembling a distribution.
Successful builds retain older distributions under release/previous-*.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile


SOURCE = Path(__file__).resolve().parent
RELEASE = SOURCE / "release"
BUNDLE_NAME = "Computer-Use-MCP"


def source_version() -> str:
    # Read the literal without importing settings or any app/platform module.
    tree = ast.parse((SOURCE / "settings.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and len(value.split(".")) == 3 and all(piece.isdigit() for piece in value.split(".")):
                return value
    raise ValueError("settings.VERSION must contain a literal three-part version.")


VERSION = source_version()
ZIP_NAME = f"Computer-Use-MCP-{VERSION}.zip"
SOURCE_ZIP_NAME = f"Computer-Use-MCP-{VERSION}-source.zip"
LAUNCHER_NAME = "Computer Use MCP 설정.exe"
APP_FILES = (
    "server.py", "settings.py", "setup.py", "consent.py", "register.py", "README.html", "VALIDATION.html",
    "maintenance.py", "diagnostics.py", "install.py", "programs.py", "INSTALL.md", "vendor/__init__.py", "vendor/guard.py", "vendor/windows.py",
    "session_runtime.py", "configuration_state.py", "operations.py", "workflows.py", "inspection.py", "accessibility_tree.py", "closing.py", "close_actions.py",
)
OPTIONAL_APP_FILES = ()
SOURCE_SUPPORT_FILES = (
    "Launcher.cs", "build_portable.py", ".gitignore", "test_consent.py", "test_register.py",
    "test_maintenance.py", "test_server.py", "test_settings.py", "test_diagnostics.py",
    "test_setup.py", "test_task_store.py", "test_install.py", "test_install_review.py", "test_install_live.py", "test_acceptance_workflows.py", "acceptance_workflows.py", "live_validation.py",
    "test_browser_defaults.py", "test_consent_ui.py", "test_programs.py", "test_setup_programs.py", "fixtures/blank.xlsx",
    "test_guard_performance.py", "test_configuration_state.py", "test_register_upgrades.py", "test_operations.py", "test_workflows.py", "performance_validation.py",
    "test_inspection.py", "test_named_windows.py", "test_accessibility_tree.py", "native_fixture.py", "generic_validation.py", "desktop_apps_validation.py", "closure_validation.py", "test_closing.py", "test_close_actions.py", "test_close_tools.py",
)


def source_files() -> tuple[str, ...]:
    return APP_FILES + tuple(name for name in OPTIONAL_APP_FILES if (SOURCE / name).is_file())


def archive_source_files() -> tuple[str, ...]:
    return source_files() + tuple(name for name in SOURCE_SUPPORT_FILES if (SOURCE / name).is_file())


COMPILER = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
SKIP_DIRS = {
    "__pycache__", "site-packages", "test", "tests", "ensurepip", "idlelib",
    "turtledemo", ".git", ".data", "demos", "nmake",
}
SKIP_FILES = {"sitecustomize.py", "usercustomize.py"}
IMPORT_CHECK = (
    "import tkinter,ssl,json,subprocess,sys,os; "
    "assert os.path.normcase(os.path.realpath(sys.prefix)) == os.path.normcase(os.path.realpath(os.environ['PYTHONHOME'])); "
    "print(json.dumps({'ok':True,'python':sys.version.split()[0],'executable':sys.executable,'prefix':sys.prefix,'tcl':tkinter.Tcl().eval('info patchlevel'),'imports':['tkinter','ssl','json','subprocess']},ensure_ascii=False))"
)


def own_release_path(path: Path) -> Path:
    """Only mutate generated paths under this source tree's release directory."""
    resolved = path.resolve()
    root = RELEASE.resolve()
    if root != SOURCE.resolve() / "release":
        raise ValueError(f"Release directory redirects outside the source tree: {root}")
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Not a generated release child: {resolved}")
    return resolved


def runtime_environment(runtime: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Windows environment keys are case-insensitive even if this dict is not.
    removed = {"TCL_LIBRARY", "TK_LIBRARY"}
    env = {key: value for key, value in env.items() if not key.upper().startswith("PYTHON") and key.upper() not in removed}
    env.update({
        "PYTHONHOME": str(runtime), "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TCL_LIBRARY": str(runtime / "tcl" / "tcl8.6"),
        "TK_LIBRARY": str(runtime / "tcl" / "tk8.6"),
    })
    return env


def run_hidden(command: list[str], *, cwd: Path, env=None, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=cwd, env=env, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def validate_sources(python_root: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("This bundle must be built on Windows.")
    for name in source_files() + ("Launcher.cs",):
        path = SOURCE / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Required source file missing or linked: {path}")
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    for name in ("python.exe", "pythonw.exe", "python313.dll", "python3.dll", "LICENSE.txt"):
        if not (python_root / name).is_file():
            raise FileNotFoundError(f"Required CPython 3.13 file missing: {python_root / name}")
    for name in ("Lib", "DLLs", "tcl"):
        if not (python_root / name).is_dir():
            raise FileNotFoundError(f"Required CPython directory missing: {python_root / name}")
    if not COMPILER.is_file():
        raise FileNotFoundError(f"Windows .NET Framework compiler missing: {COMPILER}")


def copy_runtime_tree(source: Path, target: Path) -> None:
    """Copy only the installed runtime; never follow links or include local data."""
    for current, dirs, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not (current_path / name).is_symlink())
        relative = current_path.relative_to(source)
        destination = target / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            item = current_path / name
            if item.is_symlink() or name in SKIP_FILES or item.suffix.lower() in {".pyc", ".pyo", ".lib", ".a", ".sh"}:
                continue
            shutil.copy2(item, destination / name)


def copy_runtime(python_root: Path, target: Path) -> None:
    target.mkdir()
    for name in ("python.exe", "pythonw.exe", "python313.dll", "python3.dll", "LICENSE.txt"):
        shutil.copy2(python_root / name, target / name)
    vc_files = sorted(python_root.glob("vcruntime*.dll"))
    if not vc_files:
        raise FileNotFoundError("The installed Python runtime has no vcruntime DLLs.")
    for path in vc_files:
        shutil.copy2(path, target / path.name)
    dll_target = target / "DLLs"
    dll_target.mkdir()
    for path in sorted((python_root / "DLLs").iterdir()):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".pyd", ".dll"}:
            if path.name.startswith("_test") or path.name.startswith("_ctypes_test"):
                continue
            shutil.copy2(path, dll_target / path.name)
    copy_runtime_tree(python_root / "Lib", target / "Lib")
    copy_runtime_tree(python_root / "tcl", target / "tcl")


def compile_launcher(destination: Path) -> None:
    command = [
        str(COMPILER), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
        "/codepage:65001", "/reference:System.Windows.Forms.dll",
        f"/out:{destination}", str(SOURCE / "Launcher.cs"),
    ]
    result = run_hidden(command, cwd=SOURCE)
    if result.stdout.strip():
        print(result.stdout.strip())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle_manifest(bundle: Path) -> None:
    entries = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            entries.append(f"{sha256(path)}  {path.relative_to(bundle).as_posix()}")
    (bundle / "SHA256SUMS.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")


def write_source_archive(staging: Path, bundle: Path, snapshot: dict[str, bytes], python_proof: dict) -> Path:
    """Archive the actual working sources, independently of an unrelated Git HEAD."""
    source_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(snapshot.items())}
    runtime_hashes = {str(path.relative_to(bundle)).replace("\\", "/"): sha256(path)
                      for path in sorted((bundle / "runtime").rglob("*")) if path.is_file()}
    manifest = {
        "format": 1, "product": BUNDLE_NAME, "version": VERSION,
        "source_snapshot_sha256": hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest(),
        "source_files": source_hashes, "runtime_files": runtime_hashes,
        "toolchain": {"python_version": python_proof["python"], "tcl_version": python_proof["tcl"],
                      "csharp_compiler_sha256": sha256(COMPILER), "compiler": "Windows .NET Framework csc.exe"},
        "rebuild": {"command": "python build_portable.py --python-root C:\\Python313",
                    "requires": "Windows, an existing CPython 3.13 with Tcl/Tk, and the Windows .NET Framework compiler. No package downloads.",
                    "source_authority": "This source archive and the file hashes, not the containing repository HEAD.",
                    "byte_identical_binary_expected": False,
                    "reason": "ZIP metadata and .NET executable timestamps may differ; source and runtime hashes identify the tested contents."},
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (bundle / "BUILD-MANIFEST.json").write_bytes(manifest_bytes)
    destination = staging / SOURCE_ZIP_NAME
    source_prefix = BUNDLE_NAME + "-source/"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted({**snapshot, "BUILD-MANIFEST.json": manifest_bytes}.items()):
            # Source archive metadata is stable and contains no local host paths.
            member = zipfile.ZipInfo(source_prefix + name, date_time=(2026, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Source archive CRC verification failed.")
        for name, data in snapshot.items():
            if archive.read(source_prefix + name) != data:
                raise RuntimeError(f"Source archive mismatch: {name}")
    return destination


def preserve_previous(path: Path, suffix: str) -> None:
    if path.exists():
        target = own_release_path(RELEASE / f"previous-{suffix}-{path.name}")
        own_release_path(path).rename(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, default=Path(r"C:\Python313"))
    parser.add_argument("--check", action="store_true", help="Check sources/compiler and installed Python only; no bundle or ZIP.")
    args = parser.parse_args()
    python_root = args.python_root.resolve()
    validate_sources(python_root)
    if RELEASE.resolve() != SOURCE.resolve() / "release":
        raise ValueError("The release directory must not redirect outside the source tree.")
    RELEASE.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    staging = own_release_path(RELEASE / (("check-" if args.check else ".staging-") + stamp))
    staging.mkdir()
    if args.check:
        compile_launcher(staging / LAUNCHER_NAME)
        result = run_hidden([str(python_root / "python.exe"), "-B", "-s", "-c", IMPORT_CHECK], cwd=staging, env=runtime_environment(python_root))
        print(result.stdout.strip())
        print(f"Source check passed. Compiler output retained at: {staging}")
        return 0

    bundle = staging / BUNDLE_NAME
    bundle.mkdir()
    snapshot = {}
    for name in archive_source_files():
        path = SOURCE / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Source archive path must be a regular source file: {name}")
        snapshot[name] = path.read_bytes()
    for name in source_files():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_bytes(snapshot[name])
    print("Copying the existing CPython runtime...", flush=True)
    runtime = bundle / "runtime"
    copy_runtime(python_root, runtime)
    compile_launcher(bundle / LAUNCHER_NAME)
    result = run_hidden([str(runtime / "python.exe"), "-B", "-s", "-c", IMPORT_CHECK], cwd=bundle, env=runtime_environment(runtime))
    proof = json.loads(result.stdout)
    if not proof.get("ok"):
        raise RuntimeError("Bundled Python import check failed.")
    # Deliberately poison the parent variables: the launcher must replace them.
    launcher_env = os.environ.copy()
    launcher_env.update({"PYTHONHOME": r"Z:\not-a-python-runtime", "PYTHONPATH": r"Z:\not-an-app-path"})
    launched = run_hidden([str(bundle / LAUNCHER_NAME), "--self-test"], cwd=bundle, env=launcher_env)
    if not json.loads(launched.stdout).get("ok"):
        raise RuntimeError("Launcher self-test did not return a successful result.")
    run_hidden([str(runtime / "python.exe"), "-B", "-s", "-c",
        "import server,settings,consent,register,maintenance,diagnostics,install,programs,vendor.guard,vendor.windows; print(\"Application imports passed\")"],
        cwd=bundle, env=runtime_environment(runtime))
    print("Bundled Python, application imports, and launcher checks passed.", flush=True)
    source_zip = write_source_archive(staging, bundle, snapshot, proof)
    write_bundle_manifest(bundle)
    staged_zip = staging / ZIP_NAME
    with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging).as_posix())
    with zipfile.ZipFile(staged_zip) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"ZIP verification failed: {bad_member}")
        names = archive.namelist()
        if any("/.data/" in name or "/site-packages/" in name or name.endswith("startup-log.txt") for name in names):
            raise RuntimeError("Unexpected local data found in distribution.")
    final_bundle = own_release_path(RELEASE / BUNDLE_NAME)
    final_zip = own_release_path(RELEASE / staged_zip.name)
    final_source_zip = own_release_path(RELEASE / source_zip.name)
    for name, data in snapshot.items():
        if (SOURCE / name).read_bytes() != data:
            raise RuntimeError(f"Source changed during build; leaving staging for review: {name}")
    preserve_previous(final_bundle, stamp)
    preserve_previous(final_zip, stamp)
    preserve_previous(final_source_zip, stamp)
    bundle.rename(final_bundle)
    staged_zip.rename(final_zip)
    source_zip.rename(final_source_zip)
    sums = f"{sha256(final_zip)}  {final_zip.name}\n{sha256(final_source_zip)}  {final_source_zip.name}\n"
    (RELEASE / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    (RELEASE / f"SHA256SUMS-{VERSION}.txt").write_text(sums, encoding="utf-8")
    staging.rmdir()  # Empty generated folder only; no recursive deletion.
    print(json.dumps({"folder": str(final_bundle), "zip": str(final_zip), "sha256": sha256(final_zip), "bytes": final_zip.stat().st_size,
                      "source_zip": str(final_source_zip), "source_sha256": sha256(final_source_zip),
                      "source_bytes": final_source_zip.stat().st_size}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        raise SystemExit(1)
