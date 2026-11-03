"""Offline allowlist packaging and verification for the standalone add-on only."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.1'
BASE = 'Agent-Desktop-Local-' + VERSION
FILES = ('SKILL.md', 'README.md', '사용가이드.html', 'VALIDATION.md', 'LICENSE', 'NOTICE.txt', 'UPSTREAM.json', 'THIRD_PARTY_NOTICES.txt',
         'references/recognition.md', 'scripts/observe.py', 'scripts/local_core.py',
         'scripts/recognition.py', 'tests/test_observer.py', 'tools/build.py')
PIN = '2c8e9a627d87649a4562bfe5678450d045b793ab6f3fdec8af05c9571f32d5ef'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source():
    files = {name: (ROOT / name).read_bytes() for name in FILES}
    # Public source must not contain workstation paths, credentials or captures.
    forbidden = [rb'C:\\Users\\', rb'(?i)gh[pousr]_[A-Za-z0-9]{25,}',
                 rb'(?i)sk-[A-Za-z0-9]{30,}', rb'-----BEGIN [A-Z ]*PRIVATE KEY-----']
    for name, data in files.items():
        if name != 'tools/build.py' and any(re.search(rule, data) for rule in forbidden):
            raise ValueError('publication_scan_failed:' + name)
    return files


def archive(path, files):
    manifest = {'version': VERSION, 'files': {n: sha(b) for n, b in sorted(files.items())}}
    files = dict(files, **{'MANIFEST.json': json.dumps(manifest, indent=2).encode()})
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo('agent-desktop-local/' + name, date_time=(2026, 11, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)


def check(assets):
    expected = source()
    names = (BASE + '-source.zip', BASE + '-windows-x64.zip', BASE + '-Guide.html')
    listed = {}
    for line in (assets / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split('  ', 1)
        if name in listed or name not in names or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('invalid_checksum_manifest')
        listed[name] = digest
    if set(listed) != set(names):
        raise ValueError('missing_asset')
    for name in names:
        if sha((assets / name).read_bytes()) != listed[name]:
            raise ValueError('asset_hash_mismatch')
        if name.endswith('-Guide.html'):
            if (assets / name).read_bytes() != expected['사용가이드.html']:
                raise ValueError('guide_source_mismatch')
            continue
        with zipfile.ZipFile(assets / name) as z:
            if z.testzip() is not None or len(set(z.namelist())) != len(z.namelist()):
                raise ValueError('invalid_archive')
            content = {p.removeprefix('agent-desktop-local/'): z.read(p) for p in z.namelist()}
        manifest = json.loads(content.pop('MANIFEST.json'))
        if manifest['version'] != VERSION or manifest['files'] != {p: sha(b) for p, b in content.items()}:
            raise ValueError('manifest_mismatch')
        binary = content.pop('bin/agent-desktop.exe', None)
        if name.endswith('windows-x64.zip') and (binary is None or sha(binary) != PIN):
            raise ValueError('upstream_binary_mismatch')
        if name.endswith('source.zip') and binary is not None:
            raise ValueError('binary_in_source')
        if content != expected:
            raise ValueError('source_or_allowlist_mismatch')
    print(json.dumps({'verified': True, 'assets': 4, 'source_files': len(expected)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', required=True, type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not args.check:
        files = source()
        binary = args.binary.read_bytes() if args.binary else b''
        if sha(binary) != PIN:
            raise ValueError('pinned_upstream_binary_required')
        args.assets.mkdir(parents=True, exist_ok=True)
        archive(args.assets / (BASE + '-source.zip'), files)
        archive(args.assets / (BASE + '-windows-x64.zip'), dict(files, **{'bin/agent-desktop.exe': binary}))
        (args.assets / (BASE + '-Guide.html')).write_bytes(files['사용가이드.html'])
        names = (BASE + '-source.zip', BASE + '-windows-x64.zip', BASE + '-Guide.html')
        (args.assets / 'SHA256SUMS.txt').write_text(''.join(sha((args.assets / n).read_bytes()) + '  ' + n + '\n' for n in names), encoding='ascii')
    check(args.assets)


if __name__ == '__main__':
    main()
