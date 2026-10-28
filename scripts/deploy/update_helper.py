"""Offline helpers run with the existing image's Python, never the NAS's Python."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile


class UpdateError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise UpdateError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(package, expected_hash, destination):
    require(sha256(package) == expected_hash, 'Package SHA256 differs from the Windows copy.')
    require(package.stat().st_size <= 100 * 1024 * 1024, 'Package exceeds 100 MiB.')
    with tarfile.open(package, 'r:gz') as archive:
        members = archive.getmembers()
        require(len(members) <= 1000 and sum(m.size for m in members) <= 100 * 1024 * 1024, 'Archive exceeds limits.')
        files, roots = {}, set()
        for member in members:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in member.name,
                    'Unsafe archive path.')
            require(member.isfile() or member.isdir(), 'Links and special files are not accepted.')
            require(bool(path.parts), 'Empty archive path.')
            roots.add(path.parts[0])
            if member.isdir():
                continue
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            require(relative not in files, 'Duplicate archive path.')
            allowed = relative in {'SHA256SUMS', 'UPDATE.txt', 'Dockerfile.jd-update', '.dockerignore', 'JD批量导入.md'}
            allowed |= (relative.startswith('hr_assist/') and path.suffix in {'.py', '.js', '.css', '.html'}
                        and all(not part.startswith('.') for part in path.parts[1:]))
            require(allowed, 'Only application-only HR Assist cumulative packages are supported.')
            files[relative] = member
        require(len(roots) == 1 and 'SHA256SUMS' in files, 'Package must have one root and a checksum manifest.')
        require({'hr_assist/__init__.py', 'hr_assist/main.py', 'hr_assist/service.py'} <= files.keys(), 'Incomplete application package.')
        checksums = {}
        for line in archive.extractfile(files['SHA256SUMS']).read().decode('utf-8-sig').splitlines():
            match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
            require(match is not None, 'Invalid checksum manifest.')
            digest, name = match.groups()
            require(name not in checksums, 'Duplicate checksum manifest entry.')
            checksums[name] = digest
        require(set(checksums) == set(files) - {'SHA256SUMS'}, 'Manifest does not cover the whole package.')
        for name, digest in checksums.items():
            require(hashlib.sha256(archive.extractfile(files[name]).read()).hexdigest() == digest,
                    'Package file checksum mismatch.')
        # Nothing is extracted until the entire manifest has passed.
        destination.mkdir()
        for name, member in files.items():
            if not name.startswith('hr_assist/'):
                continue
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.extractfile(member).read())
            output.chmod(0o644)
        # The helper uses umask 077 for private backup files. Application source
        # needs searchable directories even when Compose uses a custom UID.
        for directory in (destination/'hr_assist', *(p for p in destination.rglob('*') if p.is_dir())):
            directory.chmod(0o755)


def host_path(value):
    require(isinstance(value, str) and value.startswith('/') and value != '/' and
            not any(char in value for char in '\r\n\x00:,|') and '..' not in PurePosixPath(value).parts,
            'Unsupported host path; use an absolute directory without colon/comma or control characters.')
    return value.rstrip('/')


def deployment_plan(inspect, compose, project, service, target_image):
    require(len(inspect) == 1 and inspect[0]['State']['Running'], 'The existing service must be running.')
    container = inspect[0]
    config = compose.get('services', {}).get(service)
    require(config is not None, 'Service is missing from the current Compose configuration.')
    require(config.get('image') == target_image and container['Config']['Image'] == target_image,
            'Configured image differs from the running service; resolve it before updating.')
    require(not any(v.get('target') == '/app' or v.get('target', '').startswith('/app/')
                    for v in config.get('volumes', [])), 'Mounted /app source is not supported.')
    actual = {mount['Destination']: mount for mount in container['Mounts']}
    planned = {mount['target']: mount for mount in config.get('volumes', [])}
    paths = {}
    for destination, key in (('/state', 'state'), ('/data', 'data'), ('/run/secrets/admin_password', 'secret')):
        mount, desired = actual.get(destination), planned.get(destination)
        require(mount is not None and mount['Type'] == 'bind', 'Expected state, data and administrator secret bind mounts.')
        require(desired is not None and desired.get('type') == 'bind', 'Compose bind mount is missing.')
        source = host_path(mount['Source'])
        require(host_path(desired['source']) == source, 'Compose mounts differ from the running container; refusing to switch storage.')
        paths[key] = source
    for path in (paths['state'], paths['data']):
        require(project != path and not project.startswith(path + '/'), 'Update/backup directory would be inside a data mount.')
    require(paths['state'] != paths['data'] and not paths['state'].startswith(paths['data'] + '/') and
            not paths['data'].startswith(paths['state'] + '/'), 'Overlapping state/data mounts are not supported.')
    return paths


def prepare(work, project, service, image, digest, base_tag):
    paths = deployment_plan(json.loads((work/'container.json').read_text()),
                            json.loads((work/'compose.json').read_text()), project, service, image)
    context = work/'context'
    validate_package(work/'package.tar.gz', digest, context)
    (context/'Dockerfile').write_text(f'FROM {base_tag}\nCOPY --chown=10001:10001 hr_assist /app/hr_assist\n')
    (context/'.dockerignore').write_text('**\n!hr_assist/\n!hr_assist/**\n')
    for key, value in paths.items():
        (work/(key+'-path')).write_text(value)
    print('Package and existing Compose mounts verified.', flush=True)


def idle(state):
    database = state/'hr-assist.db'
    require(database.is_file(), 'Application database is missing; refusing an ambiguous backup.')
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as con:
        require(not con.execute("SELECT 1 FROM runs WHERE status='RUNNING' LIMIT 1").fetchone(),
                'A resume task is running. Wait for it to finish, then update again.')
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jd_imports'").fetchone()
        if exists:
            require(not con.execute("SELECT 1 FROM jd_imports WHERE status IN ('QUEUED','EXTRACTING','ANALYZING') LIMIT 1").fetchone(),
                    'JD import tasks are active. Finish them before updating.')


def size_of(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(size_of(child) for child in path.iterdir())


def capacity(work, state, data, project):
    required = 2 * size_of(state) + size_of(data) + size_of(project/'hr_assist') + 256 * 1024 * 1024
    require(shutil.disk_usage(work).free >= required, 'Not enough free space for a complete backup.')


def backup(work, state, data, project, secret):
    # Called only after the old container is fully stopped, so SQLite and file data agree.
    capacity(work, state, data, project)
    # A stopped WAL database may have no -shm file. Validate a consistent offline
    # copy in a writable scratch directory, never reopen the live mount writable.
    with tempfile.TemporaryDirectory(prefix='db-check-', dir=work) as scratch:
        check = Path(scratch)
        require((state/'hr-assist.db').is_file(), 'Application database is missing.')
        for suffix in ('', '-wal'):
            source = state/('hr-assist.db'+suffix)
            if source.exists():
                shutil.copyfile(source, check/source.name)
        idle(check)
        with closing(sqlite3.connect(check/'hr-assist.db')) as connection:
            require(connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'Database integrity check failed.')
    folder = work/'backup'
    folder.mkdir(mode=0o700)
    sources = [('state', state), ('data', data), ('source', project/'hr_assist'), ('admin-secret', secret)]
    for name, source in sources:
        require(source.exists(), 'Required backup source is missing.')
        with tarfile.open(folder/(name+'.tar'), 'w') as archive:
            archive.add(source, arcname=name, recursive=True)
    with tarfile.open(folder/'project-config.tar', 'w') as archive:
        selected = {project/'.env'}
        for pattern in ('compose*.yml', 'compose*.yaml', 'docker-compose*.yml', 'docker-compose*.yaml'):
            selected.update(project.glob(pattern))
        if (project/'secrets').is_dir():
            selected.add(project/'secrets')
        for path in sorted(selected):
            if path.exists():
                archive.add(path, arcname=path.name)
    checksums = ''.join(f'{sha256(path)}  {path.name}\n' for path in sorted(folder.glob('*.tar')))
    (folder/'SHA256SUMS').write_text(checksums)
    (folder/'COMPLETE').write_text('Offline state/data/config/source backup completed.\n')
    print('Offline data, key, configuration and source backup completed.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'idle', 'capacity', 'backup'])
    parser.add_argument('--work', type=Path, default=Path('/update'))
    parser.add_argument('--project', default='')
    parser.add_argument('--service', default='hr-assist')
    parser.add_argument('--image', default='hr-assist:0.1.0')
    parser.add_argument('--sha256', default='')
    parser.add_argument('--base-tag', default='')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.work, host_path(args.project), args.service, args.image, args.sha256, args.base_tag)
    elif args.action == 'idle':
        idle(Path('/state'))
    elif args.action == 'capacity':
        capacity(args.work, Path('/state'), Path('/data'), Path('/project'))
    else:
        backup(args.work, Path('/state'), Path('/data'), Path('/project'), Path('/admin-secret'))


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (UpdateError, OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        # Do not echo arbitrary archive filenames, configuration values or database contents.
        print('UPDATE ERROR: '+str(error) if isinstance(error, UpdateError) else 'UPDATE ERROR: helper validation or file operation failed.', flush=True)
        raise SystemExit(1)
