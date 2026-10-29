"""Exercise the exact updater authentication header with synthetic stdin."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from test_remote_update import SCRIPTS, bash_path


@pytest.mark.parametrize('mode', ['root', 'password_sudo', 'passwordless_sudo', 'sudo_failure'])
def test_remote_sudo_consumption_and_root_stdin_closure(tmp_path, mode):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('Bash unavailable')
    password = 'Synthetic 中文 $&% "quote" \\password'
    expected_hash = hashlib.sha256(password.encode()).hexdigest()
    # Keep the real shell header byte-for-byte (after newline normalization),
    # stopping before any Docker/project logic. Only the safe assertion body
    # below is synthetic. This cannot reach a real Docker command.
    source = (SCRIPTS / 'update_remote.sh').read_text(encoding='utf-8')
    boundary = '[ "$#" = 5 ]'
    assert boundary in source
    header = source.split(boundary, 1)[0]
    assert "exec sudo -S -p ''" in header and 'exec </dev/null' in header
    remote = tmp_path / 'update_remote.sh'
    remote.write_text(header + '''
if IFS= read -r unexpected; then
    echo 'Unexpected inherited stdin' >&2
    exit 42
fi
printf '%s\n' reached > "$SYNTHETIC_BODY_MARKER"
''', encoding='utf-8', newline='\n')
    binaries = tmp_path / 'fake-bin'
    binaries.mkdir()
    (binaries / 'id').write_text('''#!/bin/sh
[ "$1" = -u ] || exit 44
if [ "${SYNTHETIC_ROOT:-0}" = 1 ]; then echo 0; else echo 1000; fi
''', encoding='utf-8', newline='\n')
    (binaries / 'sudo').write_text(f'''#!/bin/sh
set -eu
[ "$1" = -S ] && [ "$2" = -p ] && [ "$3" = '' ] && [ "$4" = sh ] || exit 45
printf '%s\\n' invoked > "$SYNTHETIC_SUDO_MARKER"
[ "$SYNTHETIC_MODE" != sudo_failure ] || exit 37
if [ "$SYNTHETIC_MODE" = password_sudo ]; then
    IFS= read -r supplied || exit 46
    actual=$(printf '%s' "$supplied" | sha256sum | cut -d ' ' -f 1)
    [ "$actual" = '{expected_hash}' ] || exit 47
    unset supplied actual
    printf '%s\\n' checked > "$SYNTHETIC_CHECKED_MARKER"
fi
shift 3
export SYNTHETIC_ROOT=1
exec "$@"
''', encoding='utf-8', newline='\n')
    # A mistaken expansion of the tested header cannot call a real Docker.
    (binaries / 'docker').write_text('#!/bin/sh\nexit 99\n', newline='\n')
    for file in binaries.iterdir():
        file.chmod(0o755)
    body = tmp_path / 'body-entered'
    sudo = tmp_path / 'sudo-entered'
    checked = tmp_path / 'password-checked'
    env = {**os.environ, 'MSYS_NO_PATHCONV': '1', 'HR_ASSIST_SUDO_STDIN': '1',
           'SYNTHETIC_ROOT': '1' if mode == 'root' else '0', 'SYNTHETIC_MODE': mode,
           'SYNTHETIC_BODY_MARKER': bash_path(body), 'SYNTHETIC_SUDO_MARKER': bash_path(sudo),
           'SYNTHETIC_CHECKED_MARKER': bash_path(checked)}
    command = [str(bash), '-c', 'export PATH="$1:/usr/bin:/bin"; shift; exec bash "$@"',
               'synthetic-auth', bash_path(binaries), bash_path(remote)]
    result = subprocess.run(command, env=env, input=(password + '\n').encode(),
                            capture_output=True, timeout=15)
    output = result.stdout + result.stderr
    assert password.encode() not in output
    assert result.returncode == (37 if mode == 'sudo_failure' else 0), output
    assert body.exists() == (mode != 'sudo_failure')
    assert sudo.exists() == (mode != 'root')
    assert checked.exists() == (mode == 'password_sudo')
    # All test files contain only code, fixed markers, and the expected digest.
    # The synthetic password was supplied through stdin, never a local file.
    assert all(password.encode() not in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
