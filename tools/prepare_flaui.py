"""Build the pinned offline FlaUI reader. Downloads occur only with --download.

No system installation, NuGet invocation, telemetry or runtime network access.
The portable build can call build(destination, cache) with a prepared cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HELPER_NAME = "Computer Use MCP FlaUI.exe"
PACKAGES = (
    ("flaui.core", "5.0.0", "191cc65ea82036b77f1872e6d4ebf743d3d120895bba2fa5248d126cd6f568a7", "lib/net48/FlaUI.Core.dll"),
    ("flaui.uia2", "5.0.0", "b17a549809c17cdc9d8eee450d145bb73a4caf7228c4af17d28c42316ce0d3ac", "lib/net48/FlaUI.UIA2.dll"),
    ("flaui.uia3", "5.0.0", "d5d2e083539a04bf6c9053781dfd47332854a76faeb8e9c3c68cc82109f711f2", "lib/net48/FlaUI.UIA3.dll"),
    ("interop.uiautomationclient", "10.19041.0", "0d2ed17db2cb13a262f0580ab992db87577651c25a9899053df4114b4d9ff3b1", "lib/net45/Interop.UIAutomationClient.dll"),
    # .NET Framework uses the OS System.Management assembly (package net462/_._).
    ("system.management", "8.0.0", "1f0a5f0dbfbeabbfefc51eaae7b986160979534bf1cbea1127aa2b14a3917cfd", None),
)


def build(destination: Path, cache: Path, *, download=False):
    destination, cache = Path(destination).resolve(), Path(cache).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    notices = destination / "licenses" / "flaui"
    notices.mkdir(parents=True, exist_ok=True)
    packages = []
    dlls = []
    for name, version, digest, member in PACKAGES:
        archive = cache / f"{name}.{version}.nupkg"
        url = f"https://api.nuget.org/v3-flatcontainer/{name}/{version}/{archive.name}"
        if not archive.is_file():
            if not download:
                raise FileNotFoundError(f"Missing pinned package {archive.name}; prepare_flaui.py --download once during build preparation")
            request = urllib.request.Request(url, headers={"User-Agent": "ComputerUseMCP-build"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Package checksum mismatch: {archive.name}")
            archive.write_bytes(data)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Package checksum mismatch: {archive.name}")
        with zipfile.ZipFile(archive) as package:
            if member:
                target = destination / Path(member).name
                target.write_bytes(package.read(member))
                dlls.append(target)
            for entry in package.namelist():
                if entry.upper() in ("LICENSE.TXT", "THIRD-PARTY-NOTICES.TXT"):
                    (notices / f"{name}-{version}-{Path(entry).name}").write_bytes(package.read(entry))
        packages.append({"name": name, "version": version, "sha256": digest, "source": url})
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    refs = ["System.Web.Extensions.dll", "System.Drawing.dll", *map(str, dlls)]
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/optimize+",
                    *[f"/reference:{ref}" for ref in refs], f"/out:{destination / HELPER_NAME}",
                    str(ROOT / "FlaUIProbe.cs")], check=True, cwd=str(ROOT))
    manifest = {"backend": "flaui", "read_only": True, "packages": packages,
                "runtime_network": False, "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [destination / HELPER_NAME, *dlls]}}
    (destination / "flaui-dependencies.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_point_baseline(destination: Path):
    """Compile existing picker with a non-interactive benchmark entry point."""
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    refs = ["System.Web.Extensions.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
            *[str(compiler.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]]
    destination = Path(destination).resolve(); destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/main:FlaUINativePointBaseline",
                    *[f"/reference:{ref}" for ref in refs], f"/out:{destination}",
                    str(ROOT / "ElementPicker.cs"), str(ROOT / "tools/FlaUINativePointBaseline.cs")],check=True,cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / ".data/flaui018/runtime")
    parser.add_argument("--cache", type=Path, default=ROOT / ".data/flaui018/packages")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.destination, args.cache, download=args.download), ensure_ascii=True))


if __name__ == "__main__":
    main()
