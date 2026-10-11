"""Generate and verify publication metadata from the final, tested release assets.

Standard library only. Does not import or run the product/Driver. `publish`
performs a read-only check of the committed tree before calling GitHub CLI.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

SUPPORT_FILES = (
    'tools/publication.py', 'tools/test_publication.py', 'PUBLICATION.md',
    '.github/workflows/publication.yml',
)
MANIFESTS = {'BUILD-MANIFEST.json', 'DRIVER-BUNDLE-MANIFEST.json', 'PUBLICATION-MANIFEST.json'}
PRODUCT = 'Computer-Use-MCP'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def safe_name(name):
    require(isinstance(name, str) and bool(name), 'Empty file name')
    path = PurePosixPath(name)
    require(not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and ':' not in name and path.as_posix() == name, 'Unsafe file name: ' + name)
    return name


def owned_file(root, name):
    safe_name(name)
    root = root.resolve()
    path = root / name
    require(root in path.resolve().parents, 'File escapes root: ' + name)
    require(not any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents),
            'Linked file: ' + name)
    return path


def version_from(data: bytes):
    values = []
    for node in ast.parse(data.decode('utf-8-sig')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            values.append(ast.literal_eval(node.value))
    require(len(values) == 1 and isinstance(values[0], str)
            and re.fullmatch(r'\d+\.\d+\.\d+', values[0]), 'Expected one literal VERSION')
    return values[0]


def asset_names(version):
    return {f'{PRODUCT}-{version}{suffix}.zip' for suffix in ('', '-source', '-with-driver')} | {
        'cua-driver.exe', 'CUA-DRIVER-LICENSE.md', 'DRIVER-BUNDLE-MANIFEST.json', 'SHA256SUMS.txt'}


def read_zip(path, prefix):
    result = {}
    with zipfile.ZipFile(path) as archive:
        names = set()
        for item in archive.infolist():
            require(item.filename not in names, 'Duplicate ZIP member: ' + item.filename)
            names.add(item.filename)
            require(item.filename.startswith(prefix), 'Unexpected ZIP prefix')
            if item.is_dir():
                safe_name(item.filename.rstrip('/'))
                continue
            name = safe_name(item.filename[len(prefix):])
            require(not item.flag_bits & 1 and (item.external_attr >> 16) & 0o170000 != 0o120000,
                    'Encrypted or linked ZIP member')
            result[name] = archive.read(item)  # ZIP CRC is checked while reading.
    return result


def hash_list(data):
    result = {}
    for line in data.decode('utf-8-sig').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        require(match is not None, 'Invalid checksum line')
        expected, name = match.groups()
        safe_name(name)
        require(name not in result, 'Duplicate checksum entry: ' + name)
        result[name] = expected
    return result


def verify_hashes(contents, hashes, label):
    require(set(contents) == set(hashes), label + ' inventory mismatch')
    for name, body in contents.items():
        require(digest(body) == hashes[name], label + ' checksum mismatch: ' + name)


def audit_assets(assets: Path, version: str):
    names = asset_names(version)
    require(assets.is_dir() and {p.name for p in assets.iterdir()} == names, 'Release asset inventory/version mismatch')
    bodies = {name: owned_file(assets, name).read_bytes() for name in sorted(names)}
    verify_hashes({n: b for n, b in bodies.items() if n != 'SHA256SUMS.txt'},
                  hash_list(bodies['SHA256SUMS.txt']), 'Release assets')
    base_name = f'{PRODUCT}-{version}.zip'
    source = read_zip(assets / f'{PRODUCT}-{version}-source.zip', PRODUCT + '-source/')
    base = read_zip(assets / base_name, PRODUCT + '/')
    combined = read_zip(assets / f'{PRODUCT}-{version}-with-driver.zip', PRODUCT + '/')
    build_bytes = source.pop('BUILD-MANIFEST.json')
    build = json.loads(build_bytes)
    require(build.get('product') == PRODUCT and build.get('version') == version, 'Build version mismatch')
    require(base.get('BUILD-MANIFEST.json') == build_bytes, 'Build manifests differ')
    verify_hashes(source, build['source_files'], 'Source ZIP')
    require(build.get('source_snapshot_sha256') == digest(json.dumps(build['source_files'], sort_keys=True).encode()),
            'Source snapshot checksum mismatch')
    require(version_from(source['settings.py']) == version, 'Source VERSION mismatch')
    verify_hashes({n: b for n, b in base.items() if n != 'SHA256SUMS.txt'},
                  hash_list(base['SHA256SUMS.txt']), 'Base ZIP')
    verify_hashes({n: b for n, b in base.items() if n.startswith('runtime/')},
                  build['runtime_files'], 'Runtime')
    require(base.get('settings.py') == source['settings.py'], 'Runtime settings differ')
    for name in source.keys() & base.keys():
        require(source[name] == base[name], 'Runtime/source mismatch: ' + name)
    for name, body in base.items():
        require(combined.get(name) == body, 'Driver bundle changed base: ' + name)
    verify_hashes({n: b for n, b in combined.items() if n != 'BUNDLE-SHA256SUMS.txt'},
                  hash_list(combined['BUNDLE-SHA256SUMS.txt']), 'Combined ZIP')
    driver_bytes = bodies['DRIVER-BUNDLE-MANIFEST.json']
    require(combined.get('DRIVER-BUNDLE-MANIFEST.json') == driver_bytes, 'Driver manifests differ')
    driver = json.loads(driver_bytes)
    require(driver.get('mcp_version') == version, 'Driver manifest version mismatch')
    require(driver.get('mcp_base_zip_sha256') == digest(bodies[base_name])
            and driver.get('mcp_base_files_unchanged') is True, 'Driver base checksum mismatch')
    driver_hashes = driver['driver']['files']
    require(all(n.startswith('driver/') for n in driver_hashes), 'Driver inventory outside driver directory')
    require(set(combined) == set(base) | set(driver_hashes) | {'DRIVER-BUNDLE-MANIFEST.json', 'BUNDLE-SHA256SUMS.txt'},
            'Unexpected combined ZIP file')
    verify_hashes({n: combined[n] for n in driver_hashes}, driver_hashes, 'Driver files')
    require(bodies['cua-driver.exe'] == combined['driver/cua-driver.exe'], 'Standalone Driver differs')
    require(bodies['CUA-DRIVER-LICENSE.md'] == combined['driver/LICENSE.md']
            and digest(bodies['CUA-DRIVER-LICENSE.md']) == driver['driver']['license_sha256'], 'Driver license differs')
    rows = [{'name': n, 'size': len(bodies[n]), 'sha256': digest(bodies[n])} for n in sorted(names)]
    return build_bytes, driver_bytes, source, rows


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def repository_reader(repo, git_ref=None):
    if git_ref is None:
        tracked = set(git(repo, 'ls-files', '-z').decode('utf-8').strip('\0').split('\0')) - {''}
        def read(name):
            path = owned_file(repo, name)
            require(path.is_file(), 'Missing or non-regular repository file: ' + name)
            return path.read_bytes()
        return tracked, read
    commit = git(repo, 'rev-parse', '--verify', '--end-of-options', git_ref + '^{commit}').decode().strip()
    entries = git(repo, 'ls-tree', '-rz', commit).split(b'\0')
    tracked = set()
    for entry in entries:
        if not entry:
            continue
        header, raw_name = entry.split(b'\t', 1)
        require(header.split()[0] in {b'100644', b'100755'}, 'Non-regular Git file')
        tracked.add(safe_name(raw_name.decode('utf-8')))
    return tracked, lambda name: git(repo, 'show', commit + ':' + safe_name(name))


def expected_metadata(repo, assets, release_status, git_ref=None, preparing=False):
    require(release_status in {'release', 'prerelease'}, 'Invalid release status')
    tracked, read = repository_reader(repo, git_ref)
    version = version_from(read('settings.py'))
    build_bytes, driver_bytes, source, rows = audit_assets(assets, version)
    inventory = set(source) | MANIFESTS | set(SUPPORT_FILES)
    require(not tracked - inventory, 'Unexpected tracked publication files: ' + ', '.join(sorted(tracked - inventory)))
    missing = inventory - tracked
    require(not missing or preparing and missing <= MANIFESTS | set(SUPPORT_FILES),
            'Missing tracked publication files: ' + ', '.join(sorted(missing)))
    for name, body in source.items():
        require(read(name) == body, 'Repository/source checksum mismatch: ' + name)
    support_hashes = {n: digest(read(n)) for n in sorted(set(SUPPORT_FILES) - source.keys())}
    generated = {'BUILD-MANIFEST.json': build_bytes, 'DRIVER-BUNDLE-MANIFEST.json': driver_bytes}
    payload = {**source, **generated}
    publication = {
        'format': 2, 'product': PRODUCT, 'version': version, 'release_tag': 'v' + version,
        'release_status': release_status,
        'publication_note': 'Generated from final release assets. Source bytes match the tested source ZIP exactly. '
            'Publication tooling is listed separately and is not part of the tested application payload. '
            'This manifest excludes itself to avoid a circular checksum.',
        'source_snapshot_sha256': json.loads(build_bytes)['source_snapshot_sha256'],
        'source_files': {n: digest(b) for n, b in sorted(payload.items())},
        'publication_support_files': support_hashes, 'release_assets': rows,
    }
    generated['PUBLICATION-MANIFEST.json'] = json_bytes(publication)
    return generated, read


def prepare(repo: Path, assets: Path, *, release_status='release'):
    generated, _ = expected_metadata(repo, assets, release_status, preparing=True)
    destinations = {name: owned_file(repo, name) for name in generated}
    # All inputs and all destinations are checked before the first write.
    for name, body in generated.items():
        destinations[name].write_bytes(body)
    return json.loads(generated['PUBLICATION-MANIFEST.json'])


def check(repo: Path, assets: Path, *, release_status='release', git_ref=None, release_json=None):
    generated, read = expected_metadata(repo, assets, release_status, git_ref)
    for name, expected in generated.items():
        require(read(name) == expected, 'Stale or mismatched publication metadata: ' + name)
    manifest = json.loads(generated['PUBLICATION-MANIFEST.json'])
    if release_json is not None:
        remote = json.loads(Path(release_json).read_text(encoding='utf-8-sig')) if not isinstance(release_json, dict) else release_json
        require(remote.get('tag_name') == manifest['release_tag'] and remote.get('draft') is False
                and remote.get('prerelease') is (release_status == 'prerelease'), 'Published release version/status mismatch')
        # A separately attached correction notice/manifest may accompany immutable release assets.
        # Every primary asset must still exist exactly once with the verified bytes.
        rows = remote['assets']
        require(len({r['name'] for r in rows}) == len(rows), 'Duplicate remote asset')
        remote_assets = {r['name']: r for r in rows}
        for asset in manifest['release_assets']:
            actual = remote_assets.get(asset['name'], {})
            require(actual.get('size') == asset['size'] and actual.get('digest') == 'sha256:' + asset['sha256'],
                    'Published release checksum mismatch: ' + asset['name'])
    return {'passed': True, 'version': manifest['version'], 'source_files': len(manifest['source_files']),
            'assets': len(manifest['release_assets']), 'git_ref': git_ref, 'writes_performed': False}


def remote_tag(github_repo, tag):
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github_repo), 'Invalid GitHub repository')
    require(re.fullmatch(r'v\d+\.\d+\.\d+', tag), 'Invalid release tag')
    raw = subprocess.check_output(['git', 'ls-remote', '--tags',
        'https://github.com/' + github_repo + '.git', 'refs/tags/' + tag, 'refs/tags/' + tag + '^{}'],
        stderr=subprocess.PIPE).decode('utf-8')
    refs = dict(reversed(line.split('\t', 1)) for line in raw.splitlines())
    return refs.get('refs/tags/' + tag + '^{}', refs.get('refs/tags/' + tag))


def publish(repo, assets, notes_file, github_repo, release_status):
    repo, assets, notes_file = repo.resolve(), assets.resolve(), notes_file.resolve()
    require(notes_file.is_file(), 'Missing release notes')
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    _, read = repository_reader(repo, commit)
    version = version_from(read('settings.py'))
    require({p.name for p in assets.iterdir()} == asset_names(version), 'Release asset inventory/version mismatch')
    # Upload the same private snapshot that passed validation, not mutable
    # caller-relative paths or a HEAD that could move between commands.
    with tempfile.TemporaryDirectory(prefix='computer-use-publication-') as tmp:
        snapshot = Path(tmp) / 'assets'
        snapshot.mkdir()
        for name in sorted(asset_names(version)):
            shutil.copyfile(owned_file(assets, name), snapshot / name)
        notes = Path(tmp) / 'release-notes.md'
        shutil.copyfile(notes_file, notes)
        result = check(repo, snapshot, release_status=release_status, git_ref=commit)
        tag = 'v' + version
        require(remote_tag(github_repo, tag) is None,
                'Release tag already exists; never reuse or move a published tag')
        command = ['gh', 'release', 'create', tag, '--repo', github_repo,
                   '--target', commit, '--title', PRODUCT + ' ' + version,
                   '--notes-file', str(notes)]
        command.append('--prerelease' if release_status == 'prerelease' else '--latest')
        command.extend(str(snapshot / n) for n in sorted(asset_names(version)))
        subprocess.run(command, cwd=repo, check=True)
        require(remote_tag(github_repo, tag) == commit, 'Published tag does not point at the verified commit')
        remote = json.loads(subprocess.check_output(['gh', 'api', 'repos/' + github_repo + '/releases/tags/' + tag]))
        result = check(repo, snapshot, release_status=release_status, git_ref=commit, release_json=remote)
        result['published'] = True
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'check', 'publish'])
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--release-status', choices=['release', 'prerelease'], default='release')
    parser.add_argument('--git-ref')
    parser.add_argument('--release-json', type=Path)
    parser.add_argument('--notes-file', type=Path)
    parser.add_argument('--github-repo')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'prepare':
            require(args.git_ref is None, 'prepare writes the working tree; do not pass --git-ref')
            result = prepare(args.repo, args.assets, release_status=args.release_status)
            result = {'prepared': True, 'version': result['version'], 'manifests': sorted(MANIFESTS)}
        elif args.mode == 'check':
            result = check(args.repo, args.assets, release_status=args.release_status,
                           git_ref=args.git_ref, release_json=args.release_json)
        else:
            require(args.github_repo and args.notes_file, 'publish requires --github-repo and --notes-file')
            require(args.git_ref is None, 'publish always uses the resolved current HEAD')
            result = publish(args.repo, args.assets, args.notes_file, args.github_repo, args.release_status)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, SyntaxError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print('Publication verification failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
