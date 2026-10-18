"""Build the optional offline PNG matcher from reviewed, hash-pinned archives.

No downloads, package installers, or setup.py execution. Build/release tooling
supplies these archives; end users never run this script or configure engines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import zipfile

ARCHIVES = {
    "airtest-1.4.3.tar.gz": "6208e83ca8d3618e32b8eee23b3e857a0077cd59accf158dd567a81df2a3b84c",
    "numpy-2.2.6-cp313-cp313-win_amd64.whl": "b0544343a702fa80c95ad5d3d608ea3599dd54d4632df855e4c8d24eb6ecfa1c",
    "opencv_python_headless-4.12.0.88-cp37-abi3-win_amd64.whl": "86b413bdd6c6bf497832e346cd5371995de148e579b9774f8eba686dee3f5528",
    "pillow-11.3.0-cp313-cp313-win_amd64.whl": "0bce5c4fd0921f99d2e858dc4d4d64193407e1b99478bc5cacecba2311abde51",
}
AIRTEST_FILES = (
    "airtest/__init__.py", "airtest/utils/__init__.py", "airtest/utils/version.py",
    "airtest/utils/logger.py", "airtest/aircv/template_matching.py", "airtest/aircv/utils.py",
    "airtest/aircv/cal_confidence.py", "airtest/aircv/error.py",
)


def _checked_path(path: Path, *, allow_missing=False):
    """Reject links/junctions before canonicalization, including ancestors."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError(f"Image backend paths cannot contain links or junctions: {component}")
    return path.resolve()


def _manifest_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name or "\0" in name
            or PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name
            or any(part in (".", "..") or part.endswith((".", " ")) for part in name.split("/"))):
        raise ValueError("Image backend manifest has a noncanonical file name")
    return name


def build_runtime(archives: Path, destination: Path):
    archives, destination = _checked_path(archives), _checked_path(destination, allow_missing=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to replace existing runtime: {destination}")
    for name, digest in ARCHIVES.items():
        archive_path = _checked_path(archives/name)
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Pinned archive hash mismatch: {name}")
    destination.mkdir(parents=True)
    for name in ARCHIVES:
        if not name.endswith(".whl"):
            continue
        with zipfile.ZipFile(archives/name) as archive:
            for item in archive.infolist():
                target = (destination/item.filename).resolve()
                if not target.is_relative_to(destination) or item.filename.startswith(("/", "\\")):
                    raise ValueError("Archive entry escapes runtime")
            archive.extractall(destination)
    with tarfile.open(archives/"airtest-1.4.3.tar.gz") as archive:
        for relative in (*AIRTEST_FILES, "LICENSE"):
            entry = archive.getmember("airtest-1.4.3/"+relative)
            if not entry.isfile():
                raise ValueError("Expected regular Airtest source")
            target = destination/("AIRTEST-LICENSE.txt" if relative == "LICENSE" else relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    (destination/"airtest/aircv/__init__.py").write_text(
        "# Selected Airtest 1.4.3 image-only module. Full device/input package is not included.\n", encoding="utf-8")
    files = {str(p.relative_to(destination)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(destination.rglob("*")) if p.is_file()}
    manifest = {"format": "computer-image-backend/v1", "airtest": "1.4.3", "numpy": "2.2.6",
                "opencv": "4.12.0.88", "pillow": "11.3.0", "python": "3.13", "platform": "win_amd64",
                "archives": ARCHIVES, "files": files, "network_at_runtime": False, "desktop_input": False}
    (destination/"BACKEND-MANIFEST.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    return manifest


def copy_runtime(source: Path, destination: Path):
    """Release builder entry point: verify every payload file before copying."""
    source, destination = _checked_path(source), _checked_path(destination, allow_missing=True)
    manifest_path = _checked_path(source/"BACKEND-MANIFEST.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "computer-image-backend/v1" or manifest.get("archives") != ARCHIVES:
        raise ValueError("Unrecognized image backend build")
    for name, expected in manifest["files"].items():
        name = _manifest_name(name)
        item = _checked_path(source/name)
        if not item.is_relative_to(source) or not stat.S_ISREG(item.stat().st_mode) or hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Image backend payload mismatch: {name}")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    for name in [*manifest["files"], "BACKEND-MANIFEST.json"]:
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recheck immediately before copying instead of trusting the earlier
        # scan after another process could have replaced a directory.
        item = _checked_path(source/name)
        target = _checked_path(target, allow_missing=True)
        if not item.is_relative_to(source) or not target.is_relative_to(destination):
            raise ValueError("Image backend copy left its declared root")
        shutil.copy2(item, target)
        if name != "BACKEND-MANIFEST.json" and hashlib.sha256(target.read_bytes()).hexdigest() != manifest["files"][name]:
            raise ValueError(f"Image backend changed during copying: {name}")
    return {"files": len(manifest["files"]), "bytes": sum((destination/p).stat().st_size for p in manifest["files"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = build_runtime(args.archives, args.destination)
    print(json.dumps({"ok": True, "files": len(result["files"]), "destination": str(args.destination)}))
