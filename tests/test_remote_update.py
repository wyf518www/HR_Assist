import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'scripts/deploy'
spec = importlib.util.spec_from_file_location('update_helper', SCRIPTS/'update_helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def make_package(tmp_path, extra=None, wrong_hash=False):
    files = {f'hr_assist/{name}.py': b'# synthetic\n' for name in ('__init__', 'main', 'service')}
    files.update(extra or {})
    manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in files.items())
    if wrong_hash:
        manifest = '0' * 64 + manifest[64:]
    files['SHA256SUMS'] = manifest.encode()
    package = tmp_path/'package.tar.gz'
    with tarfile.open(package, 'w:gz') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo('release/'+name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return package


def test_package_verified_before_extraction(tmp_path):
    package = make_package(tmp_path)
    target = tmp_path/'context'
    helper.validate_package(package, helper.sha256(package), target)
    assert (target/'hr_assist/main.py').read_bytes() == b'# synthetic\n'


@pytest.mark.parametrize('extra', [{'../escape.py': b'x'}, {'hr_assist/../../escape.py': b'x'}, {'.env': b'private'}, {'requirements.txt': b'new-dependency'}, {'hr_assist/.env.py': b'private'}])
def test_dangerous_or_dependency_package_rejected(tmp_path, extra):
    package = make_package(tmp_path, extra)
    with pytest.raises(helper.UpdateError):
        helper.validate_package(package, helper.sha256(package), tmp_path/'context')
    assert not (tmp_path/'context').exists()


def test_corrupt_checksums_and_links_rejected(tmp_path):
    package = make_package(tmp_path, wrong_hash=True)
    with pytest.raises(helper.UpdateError, match='checksum mismatch'):
        helper.validate_package(package, helper.sha256(package), tmp_path/'context')
    with pytest.raises(helper.UpdateError, match='Windows copy'):
        helper.validate_package(package, 'f' * 64, tmp_path/'context')
    with tarfile.open(package, 'w:gz') as archive:
        link = tarfile.TarInfo('release/hr_assist/link.py')
        link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
        archive.addfile(link)
    with pytest.raises(helper.UpdateError, match='Links'):
        helper.validate_package(package, helper.sha256(package), tmp_path/'context')


def deployment():
    mounts = [{'Type': 'bind', 'Destination': dest, 'Source': source} for dest, source in (
        ('/state', '/volume1/hr/storage/state'), ('/data', '/volume1/hr/storage/data'),
        ('/run/secrets/admin_password', '/volume1/hr/secrets/admin_password.txt'))]
    container = [{'State': {'Running': True}, 'Config': {'Image': 'hr-assist:0.1.0'}, 'Mounts': mounts}]
    compose = {'services': {'hr-assist': {'image': 'hr-assist:0.1.0',
               'volumes': [{'type': 'bind', 'target': m['Destination'], 'source': m['Source']} for m in mounts]}}}
    return container, compose


def test_mount_plan_uses_actual_sources_and_blocks_changed_storage():
    container, compose = deployment()
    args = (container, compose, '/volume1/hr', 'hr-assist', 'hr-assist:0.1.0')
    assert helper.deployment_plan(*args)['data'] == '/volume1/hr/storage/data'
    compose['services']['hr-assist']['volumes'][0]['source'] = '/volume1/other/state'
    with pytest.raises(helper.UpdateError, match='differ'):
        helper.deployment_plan(*args)
    container, compose = deployment()
    with pytest.raises(helper.UpdateError, match='inside a data mount'):
        helper.deployment_plan(container, compose, '/volume1/hr/storage/state/project', 'hr-assist', 'hr-assist:0.1.0')


@pytest.mark.parametrize('problem', ['overlap', 'volume', 'source-overlay'])
def test_unsupported_mounts_are_rejected(problem):
    container, compose = deployment()
    volumes = compose['services']['hr-assist']['volumes']
    if problem == 'overlap':
        container[0]['Mounts'][1]['Source'] = volumes[1]['source'] = '/volume1/hr/storage/state/data'
    elif problem == 'volume':
        container[0]['Mounts'][0]['Type'] = volumes[0]['type'] = 'volume'
    else:
        volumes.append({'type': 'bind', 'target': '/app/hr_assist', 'source': '/other'})
    with pytest.raises(helper.UpdateError):
        helper.deployment_plan(container, compose, '/volume1/hr', 'hr-assist', 'hr-assist:0.1.0')


def setup_backup_tree(tmp_path):
    work, state, data, project = [tmp_path/name for name in ('update', 'state', 'data', 'project')]
    for path in (work, state, data, project/'hr_assist'):
        path.mkdir(parents=True)
    with sqlite3.connect(state/'hr-assist.db') as con:
        con.execute('CREATE TABLE runs(status TEXT)')
        con.execute('CREATE TABLE jd_imports(status TEXT)')
    (state/'vault.key').write_bytes(b'synthetic-key')
    (data/'resume.pdf').write_bytes(b'synthetic-resume')
    (project/'hr_assist/main.py').write_text('# old source')
    (project/'.env').write_text('SYNTHETIC=1')
    (project/'docker-compose.yml').write_text('services: {}')
    secret = tmp_path/'admin-secret'
    secret.write_bytes(b'synthetic-password')
    return work, state, data, project, secret


def test_offline_backup_covers_keys_originals_config_and_source(tmp_path):
    args = setup_backup_tree(tmp_path)
    work, state, data, project, secret = args
    original = (state/'hr-assist.db').read_bytes()
    helper.backup(*args)
    assert (work/'backup/COMPLETE').exists()
    for name, member, value in [('state', 'state/vault.key', b'synthetic-key'), ('data', 'data/resume.pdf', b'synthetic-resume'),
                                 ('admin-secret', 'admin-secret', b'synthetic-password'), ('project-config', '.env', b'SYNTHETIC=1')]:
        with tarfile.open(work/f'backup/{name}.tar') as archive:
            assert archive.extractfile(member).read() == value
    assert (state/'hr-assist.db').read_bytes() == original
    assert not list(work.glob('db-check-*'))


def test_busy_database_blocks_backup_without_touching_original(tmp_path):
    args = setup_backup_tree(tmp_path)
    with sqlite3.connect(args[1]/'hr-assist.db') as con:
        con.execute("INSERT INTO runs VALUES ('RUNNING')")
    with pytest.raises(helper.UpdateError, match='running'):
        helper.backup(*args)
    assert not (args[0]/'backup/COMPLETE').exists()


def test_insufficient_space_prevents_backup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    args = setup_backup_tree(tmp_path)
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(helper.UpdateError, match='free space'):
        helper.backup(*args)
    assert not (args[0]/'backup').exists()


def test_plan_mode_and_invalid_configuration_never_connect(tmp_path):
    executable = shutil.which('powershell.exe') or shutil.which('pwsh')
    if not executable:
        pytest.skip('PowerShell unavailable')
    package = make_package(tmp_path)
    config = tmp_path/'private.json'
    data = dict(Host='synthetic.invalid', Port=22, User='test', ProjectDir='/volume1/hr',
                Service='hr-assist', Image='hr-assist:0.1.0', PackageDirectory=str(tmp_path))
    config.write_text(json.dumps(data))
    command = [executable, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPTS/'Update-HRAssist.ps1'),
               '-Plan', '-Config', str(config), '-Package', str(package)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0 and 'PLAN ONLY' in result.stdout, result.stdout + result.stderr
    for field, invalid in [('Host', 'evil;touch'), ('ProjectDir', '/volume1/hr;touch'), ('Port', '22;id')]:
        changed = {**data, field: invalid}
        config.write_text(json.dumps(changed))
        failed = subprocess.run(command, capture_output=True, text=True)
        assert failed.returncode == 1 and 'UPDATE ERROR' in failed.stdout


FAKE_DOCKER = r'''
import json, os, re, sys
from pathlib import Path
def native(value):
    if os.name == 'nt' and re.match(r'^/[a-zA-Z]/', value):
        value = value[1].upper() + ':' + value[2:]
    return Path(value)
root = Path(os.environ['FAKE_ROOT'])
record = root/'docker-state.json'
state = json.loads(record.read_text()) if record.exists() else {'active': False, 'running': True, 'calls': []}
args = sys.argv[1:]
state['calls'].append(args)
failure = os.environ.get('FAKE_FAILURE', '')
def done(code=0, text=''):
    record.write_text(json.dumps(state))
    if text: sys.stdout.buffer.write((text + '\n').encode('utf-8'))
    sys.exit(code)
if args == ['--version']: done(text='Docker version 24.0 synthetic')
if args == ['info']: done(1 if failure == 'daemon' else 0)
if args[:2] == ['compose', 'version']: done(1 if os.environ.get('FAKE_NO_COMPOSE_PLUGIN') == '1' else 0, text='Docker Compose synthetic')
if args[:3] == ['compose', 'ps', '-q']: done(text='new-id' if state['active'] else 'old-id')
if args[:2] == ['compose', 'config']: done(text='{}')
if args[:2] == ['compose', 'stop']:
    state['running'] = False
    done()
if args[:2] == ['compose', 'up']:
    state.update(active=True, running=True)
    done(1 if failure == 'activate' else 0)
if args[0] == 'inspect':
    if '--format' not in args: done(text='[]')
    fmt = args[args.index('--format') + 1]
    if '.Health' in fmt: done(text='unhealthy' if failure == 'health' else 'healthy')
    if '.Running' in fmt: done(text=str(state['running']).lower())
    done(text='sha256:new' if state['active'] else 'sha256:old')
if args[:2] == ['image', 'inspect']: done(text='sha256:new')
if args[:2] == ['image', 'tag']:
    done(1 if failure == 'tag' and args[2].startswith('hr-assist:update-') else 0)
if args[0] == 'start':
    state.update(active=False, running=True)
    done()
if args[0] == 'build': done(1 if failure == 'build' else 0)
if args[0] == 'exec':
    busy = failure == 'busy' or (failure == 'busy-after-build' and any(c[0] == 'build' for c in state['calls']))
    done(1 if busy else 0)
if args[0] == 'run':
    if '/update/update_helper.py' not in args: done(1 if failure == 'smoke' else 0)
    action = args[args.index('/update/update_helper.py') + 1]
    bindings = [args[i+1] for i, arg in enumerate(args) if arg == '-v']
    work = native(next(value[:-len(':/update')] for value in bindings if value.endswith(':/update')))
    if action == failure: done(1)
    if action == 'prepare':
        context = work/'context/hr_assist'
        context.mkdir(parents=True)
        (context/'main.py').write_text('# new source')
        for key in ('state', 'data', 'secret'): (work/(key+'-path')).write_text('/synthetic/'+key)
    if action == 'backup':
        (work/'backup').mkdir()
        (work/'backup/COMPLETE').write_text('synthetic backup')
    done()
done(99, 'Unhandled fake Docker call')
'''


def bash_path(path):
    value = path.as_posix()
    return '/'+value[0].lower()+value[2:] if os.name == 'nt' else value


@pytest.mark.parametrize('failure', ['', 'locked', 'prepare', 'busy', 'capacity', 'build', 'smoke', 'busy-after-build', 'backup', 'tag', 'activate', 'health'])
def test_remote_update_failure_boundaries_with_mock_docker(tmp_path, failure):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('Bash unavailable')
    project, upload, bin_dir = [tmp_path/name for name in ('project', 'upload', 'fake-bin')]
    for path in (project/'hr_assist', project/'.git/info', upload, bin_dir):
        path.mkdir(parents=True)
    (project/'hr_assist/main.py').write_text('# original source')
    if failure == 'locked':
        (project/'.hr-assist-update.lock').mkdir()
    (upload/'package.tar.gz').write_bytes(b'fake transport: package validation tested separately')
    for file in ('update_remote.sh', 'update_helper.py'):
        shutil.copyfile(SCRIPTS/file, upload/file)
    mock = tmp_path/'fake_docker.py'
    mock.write_text(FAKE_DOCKER)
    (bin_dir/'docker').write_text(f'#!/bin/sh\nexec "{sys.executable.replace(chr(92), "/")}" "{mock.as_posix()}" "$@"\n', newline='\n')
    (bin_dir/'id').write_text('#!/bin/sh\necho 0\n', newline='\n')
    for file in bin_dir.iterdir(): file.chmod(0o755)
    env = {**os.environ, 'PATH': str(bin_dir)+os.pathsep+os.environ['PATH'], 'MSYS_NO_PATHCONV': '1',
           'FAKE_ROOT': str(tmp_path), 'FAKE_FAILURE': failure}
    # Prove no real Docker executable can be reached by this integration test.
    lookup = subprocess.run([str(bash), '-c', 'command -v docker'], env=env, capture_output=True, text=True)
    assert 'fake-bin' in lookup.stdout
    command = [str(bash), '-c', 'export PATH="$1:$PATH"; shift; exec bash "$@"', 'mock-update', bash_path(bin_dir),
               bash_path(upload/'update_remote.sh'), bash_path(project), 'hr-assist', 'hr-assist:0.1.0', 'synthetic-1', 'a'*64]
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
    assert (tmp_path/'docker-state.json').exists(), result.stdout + result.stderr
    state = json.loads((tmp_path/'docker-state.json').read_text())
    assert (result.returncode == 0) == (failure == ''), result.stdout + result.stderr
    assert (project/'.hr-assist-update.lock').exists() == (failure == 'locked')
    if not failure:
        assert state['active'] and state['running']
        assert (project/'hr_assist/main.py').read_text() == '# new source'
        assert (project/'.hr-assist-updates/synthetic-1/source-before/main.py').read_text() == '# original source'
        assert 'UPDATE SUCCESS' in result.stdout
    elif failure in {'activate', 'health'}:
        assert state['active'] and not state['running']
        assert (project/'.hr-assist-updates/synthetic-1/backup/COMPLETE').exists()
        assert not any(call[0] == 'start' for call in state['calls'])
        assert (project/'hr_assist/main.py').read_text() == '# original source'
    elif failure in {'backup', 'tag'}:
        assert not state['active'] and state['running']
        assert any(call == ['start', 'old-id'] for call in state['calls'])
    else:
        assert not state['active'] and state['running']
        assert not any(call[:2] == ['compose', 'stop'] for call in state['calls'])
