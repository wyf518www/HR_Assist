"""Run the remote updater with a simulated DSM package tree, never real Docker."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from test_remote_update import FAKE_DOCKER, SCRIPTS, bash_path


@pytest.mark.parametrize('scenario', ['container_manager', 'legacy_docker', 'sudo_container_manager',
                                    'standalone_compose', 'missing_docker', 'missing_compose', 'compose_v1', 'daemon'])
def test_dsm_discovery_without_docker_in_path(tmp_path, scenario):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('Bash unavailable')
    project, upload, bin_dir = [tmp_path/name for name in ('project', 'upload', 'fake-bin')]
    for path in (project/'hr_assist', upload, bin_dir):
        path.mkdir(parents=True)
    (project/'hr_assist/main.py').write_text('# original source')
    (upload/'package.tar.gz').write_bytes(b'synthetic package validated by separate tests')
    shutil.copyfile(SCRIPTS/'update_helper.py', upload/'update_helper.py')
    source = (SCRIPTS/'update_remote.sh').read_text()
    # Relocate every fixed Docker/Compose executable candidate into a synthetic
    # filesystem, and make PATH lookups miss. Even a Docker-equipped test host
    # cannot accidentally reach its real Docker daemon.
    fixed_paths = set(re.findall(r'"(/[^"\n]*docker(?:-compose)?)"', source))
    mapped = {path: tmp_path/'dsm'/path.lstrip('/') for path in fixed_paths}
    for path, target in mapped.items():
        source = source.replace('"'+path+'"', '"'+bash_path(target)+'"')
    source = source.replace('command -v docker-compose', 'command -v synthetic-absent-compose')
    source = source.replace('command -v docker ', 'command -v synthetic-absent-docker ')
    (upload/'update_remote.sh').write_text(source, newline='\n')
    fake = tmp_path/'fake_docker.py'
    fake.write_text(FAKE_DOCKER)
    package_name = 'Docker' if scenario == 'legacy_docker' else 'ContainerManager'
    docker = mapped[f'/var/packages/{package_name}/target/usr/bin/docker']
    if scenario != 'missing_docker':
        docker.parent.mkdir(parents=True)
        docker.write_text(f'#!/bin/sh\nexec "{sys.executable.replace(chr(92), "/")}" "{fake.as_posix()}" "$@"\n', newline='\n')
        docker.chmod(0o755)
    if scenario in {'standalone_compose', 'compose_v1'}:
        compose = docker.parent/'docker-compose'
        version = '1.29.2' if scenario == 'compose_v1' else 'v2.24.0'
        compose.write_text(f'''#!/bin/sh
if [ "$1" = version ]; then
    printf '%s\\n' '{version}'
    exit 0
fi
exec "{bash_path(docker)}" compose "$@"
''', newline='\n')
        compose.chmod(0o755)
    elevated = scenario == 'sudo_container_manager'
    (bin_dir/'id').write_text('#!/bin/sh\nif [ "${SYNTHETIC_ROOT:-0}" = 1 ]; then echo 0; else echo 1000; fi\n'
                             if elevated else '#!/bin/sh\necho 0\n', newline='\n')
    (bin_dir/'sudo').write_text(f'''#!/bin/sh
export SYNTHETIC_ROOT=1
export PATH="{bash_path(bin_dir)}:/usr/bin:/bin"
printf '%s\\n' called > "{bash_path(tmp_path/'sudo-called')}"
exec "$@"
''', newline='\n')
    for file in bin_dir.iterdir():
        file.chmod(0o755)
    env = {**os.environ, 'MSYS_NO_PATHCONV': '1', 'FAKE_ROOT': str(tmp_path),
           'FAKE_FAILURE': 'daemon' if scenario == 'daemon' else '',
           'FAKE_NO_COMPOSE_PLUGIN': '1' if scenario in {'standalone_compose', 'missing_compose', 'compose_v1'} else '0'}
    command = [str(bash), '-c', 'export PATH="$1:/usr/bin:/bin"; shift; exec bash "$@"', 'mock-dsm', bash_path(bin_dir),
               bash_path(upload/'update_remote.sh'), bash_path(project), 'hr-assist', 'hr-assist:0.1.0', 'synthetic-dsm', 'a'*64]
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
    output = result.stdout+result.stderr
    state_file = tmp_path/'docker-state.json'
    success = scenario in {'container_manager', 'legacy_docker', 'sudo_container_manager', 'standalone_compose'}
    assert (result.returncode == 0) == success, output
    assert not (project/'.hr-assist-update.lock').exists()
    if success:
        state = json.loads(state_file.read_text())
        assert 'UPDATE SUCCESS' in output and 'Docker CLI:' in output and 'Compose command:' in output
        assert state['active'] and state['running']
        assert (project/'hr_assist/main.py').read_text() == '# new source'
        if elevated:
            assert (tmp_path/'sudo-called').exists()
    else:
        assert 'No service was changed' in output
        assert not (project/'.hr-assist-updates').exists()
        assert (project/'hr_assist/main.py').read_text() == '# original source'
        if state_file.exists():
            state = json.loads(state_file.read_text())
            assert not state['active'] and state['running']
            assert all(call in (['--version'], ['info'], ['compose', 'version']) for call in state['calls'])
