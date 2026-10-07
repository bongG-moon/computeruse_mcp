"""Add the reviewed, manually supplied Cua Driver to an intact MCP ZIP.

No downloads, installations, configuration changes, or executable launches.
The original MCP files and integrity manifest remain byte-for-byte unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

DRIVER_VERSION = "0.28.2"
DRIVER_TAG = "cua-driver-rs-v" + DRIVER_VERSION
DRIVER_ARCHIVE_SHA256 = "3c1fcf10ff9513b94e4af78ad6a216ab62aa95b2c9a3b70dfbdba9f04e021533"
DRIVER_EXE_SHA256 = "dbbd52d75759900155fbf3d5f0a13c759a12d06ef17338b88b3f2b8b9c1ef8dc"
DRIVER_FILES = {
    "cua_driver_abi.h", "cua_driver_node_runtime.node", "cua_driver_sdk.dll",
    "cua-cursor-theme.exe", "cua-driver-uia.exe", "cua-driver.exe",
}
LICENSE_SHA256 = "c0779290c1d4783169aa3dbfb55feb505e563ef8a004bbf55298ceffcfbda8d9"
BASE_PREFIX = "Computer-Use-MCP/"
DRIVER_PREFIX = f"cua-driver-rs-{DRIVER_VERSION}-windows-x86_64/"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_archive(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member")
        contents = {}
        for entry in entries:
            name = entry.filename
            rel = PurePosixPath(name)
            if rel.is_absolute() or ".." in rel.parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe ZIP path")
            if entry.is_dir():
                continue
            if entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Encrypted or linked ZIP member")
            contents[name] = archive.read(entry)
        return contents


def create_bundle(base_zip: Path, driver_zip: Path, license_file: Path, output: Path) -> dict:
    inputs = [base_zip, driver_zip, license_file]
    if output.exists() or any(output.resolve() == path.resolve() for path in inputs):
        raise ValueError("Output must be a new file, separate from the inputs")
    original = read_archive(base_zip)
    if any(not name.startswith(BASE_PREFIX) for name in original):
        raise ValueError("Unexpected MCP ZIP layout")
    build = json.loads(original[BASE_PREFIX + "BUILD-MANIFEST.json"])
    if build.get("product") != "Computer-Use-MCP" or build.get("version") not in {"0.6.0", "0.7.0", "0.7.1", "0.8.0", "0.9.0", "0.10.0", "0.11.0", "0.11.1", "0.12.0"}:
        raise ValueError("Unexpected MCP build identity")
    base_manifest = original[BASE_PREFIX + "SHA256SUMS.txt"].decode("utf-8")
    listed = {}
    for line in base_manifest.splitlines():
        expected, name = line.split("  ", 1)
        if name in listed or digest(original[BASE_PREFIX + name]) != expected:
            raise ValueError("MCP ZIP integrity mismatch")
        listed[name] = expected
    if set(original) != {BASE_PREFIX + name for name in listed} | {BASE_PREFIX + "SHA256SUMS.txt"}:
        raise ValueError("Unexpected files outside the MCP integrity manifest")
    for name, expected in build["source_files"].items():
        full_name = BASE_PREFIX + name
        if full_name in original and digest(original[full_name]) != expected:
            raise ValueError("MCP source differs from its build manifest")
    archive_bytes = driver_zip.read_bytes()
    if digest(archive_bytes) != DRIVER_ARCHIVE_SHA256:
        raise ValueError("Driver ZIP is not the reviewed official 0.28.2 x86_64 archive")
    driver = read_archive(driver_zip)
    if set(driver) != {DRIVER_PREFIX + name for name in DRIVER_FILES}:
        raise ValueError("Unexpected Driver components")
    if digest(driver[DRIVER_PREFIX + "cua-driver.exe"]) != DRIVER_EXE_SHA256:
        raise ValueError("Driver executable differs from the tested binary")
    license_bytes = license_file.read_bytes()
    if digest(license_bytes) != LICENSE_SHA256:
        raise ValueError("Driver license differs from the pinned upstream license")

    additions = {BASE_PREFIX + "driver/" + name.removeprefix(DRIVER_PREFIX): data
                 for name, data in driver.items()}
    additions[BASE_PREFIX + "driver/LICENSE.md"] = license_bytes
    documents = {BASE_PREFIX + "CUA-DRIVER-LICENSE.md": license_bytes}
    for name in ("README.md", "DRIVER-BUNDLE.md"):
        documents[BASE_PREFIX + name] = Path(__file__).with_name(name).read_bytes()
    for name, data in documents.items():
        if name in original:
            if original[name] != data:
                raise ValueError("Driver documentation differs from the original MCP package")
        else:
            additions[name] = data
    manifest = {
        "format": 1, "mcp_version": build["version"],
        "mcp_base_zip_sha256": digest(base_zip.read_bytes()),
        "mcp_base_files_unchanged": True,
        "driver": {
            "version": DRIVER_VERSION, "platform": "windows-x86_64",
            "upstream_repository": "https://github.com/trycua/cua",
            "upstream_release": "https://github.com/trycua/cua/releases/tag/" + DRIVER_TAG,
            "upstream_archive": "cua-driver-rs-0.28.2-windows-x86_64.zip",
            "upstream_archive_sha256": DRIVER_ARCHIVE_SHA256,
            "license": "MIT", "license_file": "driver/LICENSE.md",
            "license_sha256": LICENSE_SHA256,
            "files": {name.removeprefix(BASE_PREFIX): digest(data)
                      for name, data in sorted(additions.items())
                      if name.startswith(BASE_PREFIX + "driver/")},
        },
        "integrity": {
            "SHA256SUMS.txt": "Unchanged hashes of the original MCP package",
            "BUNDLE-SHA256SUMS.txt": "All combined-package files except this hash list",
        },
        "downloads_performed_by_builder": False,
        "executable_launches_performed_by_builder": False,
    }
    additions[BASE_PREFIX + "DRIVER-BUNDLE-MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if set(original).intersection(set(additions) | {BASE_PREFIX + "BUNDLE-SHA256SUMS.txt"}):
        raise ValueError("Driver additions would replace original MCP package files")
    contents = {**original, **additions}
    sums = "".join(digest(data) + "  " + name.removeprefix(BASE_PREFIX) + "\n"
                   for name, data in sorted(contents.items()))
    contents[BASE_PREFIX + "BUNDLE-SHA256SUMS.txt"] = sums.encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in sorted(contents.items()):
                entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        if read_archive(temporary) != contents:
            raise ValueError("Combined package verification failed")
        # Exclusive creation keeps an existing download/package from being replaced.
        with output.open("xb") as target, temporary.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
    finally:
        temporary.unlink(missing_ok=True)
    return {"passed": True, "version": build["version"], "driver_version": DRIVER_VERSION,
            "original_mcp_files_verified": len(original), "driver_components": len(driver),
            "zip_sha256": digest(output.read_bytes()), "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-zip", required=True, type=Path)
    parser.add_argument("--driver-zip", required=True, type=Path)
    parser.add_argument("--license-file", default=Path(__file__).with_name("CUA-DRIVER-LICENSE.md"), type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(create_bundle(args.base_zip, args.driver_zip, args.license_file, args.output),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
