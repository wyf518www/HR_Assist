"""Exercise the Windows launcher with local SCP/SSH stand-ins; never connect."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


MOCK_TRANSPORT = r'''
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ['MOCK_TRANSPORT_ROOT'])
kind, args = sys.argv[1], sys.argv[2:]
with (root/'transport-calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'kind': kind, 'args': args})+'\n')
if kind == 'scp':
    # Reproduce a NAS with SSH available but no SFTP subsystem.
    if '-O' not in args:
        print('subsystem request failed on channel 0')
        sys.exit(1)
    assert args[args.index('-P')+1] == '2222'
    assert 'StrictHostKeyChecking=ask' in args
    if os.environ.get('MOCK_UPLOAD_FAIL') == '1':
        print('Synthetic upload failure')
        sys.exit(1)
    shutil.copyfile(args[-2], root/'received.tar')
else:
    raise AssertionError('Unexpected tool')
'''


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize('upload_fails', [False, True])
def test_no_sftp_upload_and_failure_stops_before_remote_update(tmp_path, upload_fails):
    powershell = shutil.which('powershell.exe')
    tar = shutil.which('tar.exe')
    if os.name != 'nt' or not powershell or not tar:
        pytest.skip('Requires Windows PowerShell and tar')
    # Use an isolated distribution, including a path with spaces. Real tools
    # except tar are never resolved, even if a test stub is missing/broken.
    distribution = tmp_path/'updater with spaces'
    scripts = distribution/'scripts/deploy'
    scripts.mkdir(parents=True)
    for name in ('Update-HRAssist.ps1', 'update_remote.sh', 'update_helper.py',
                 'Local-Credentials.ps1', 'Password-Transport.ps1'):
        shutil.copyfile(ROOT/'scripts/deploy'/name, scripts/name)
    package = tmp_path/'hr-assist-synthetic.tar.gz'
    with tarfile.open(package, 'w:gz') as archive:
        info = tarfile.TarInfo('synthetic/binary')
        data = bytes(range(256))*4
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    config = tmp_path/'private.json'
    config.write_text(json.dumps(dict(Host='synthetic.invalid', Port=2222, User='synthetic-user',
                                     ProjectDir='/volume1/hr', Service='hr-assist', Image='hr-assist:0.1.0',
                                     PackageDirectory=str(tmp_path))))
    original_config = config.read_bytes()
    mock = tmp_path/'mock_transport.py'
    mock.write_text(MOCK_TRANSPORT)
    for kind in ('scp',):
        (tmp_path/(kind+'.cmd')).write_text(
            f'@echo off\n"{sys.executable}" "{mock}" {kind} %*\nexit /b %ERRORLEVEL%\n')
    wrapper = tmp_path/'run-isolated.ps1'
    wrapper.write_text(f'''
function Invoke-SyntheticSsh {{
    $record = @{{kind='ssh'; args=@($args)}} | ConvertTo-Json -Compress
    [IO.File]::AppendAllText({ps_quote(tmp_path/'transport-calls.jsonl')}, $record + "`n")
    $global:LASTEXITCODE = 0
    Write-Output 'UPDATE SUCCESS. Synthetic remote execution only.'
}}
function Get-Command {{
    param([string]$Name, [string]$ErrorAction)
    switch ($Name) {{
        'ssh.exe' {{ [PSCustomObject]@{{Source='Invoke-SyntheticSsh'}}; return }}
        'scp.exe' {{ [PSCustomObject]@{{Source={ps_quote(tmp_path/'scp.cmd')}}}; return }}
        'tar.exe' {{ [PSCustomObject]@{{Source={ps_quote(tar)}}}; return }}
        default {{ throw 'Test blocked unexpected executable lookup' }}
    }}
}}
& {ps_quote(scripts/'Update-HRAssist.ps1')} -Config {ps_quote(config)} -Package {ps_quote(package)} -Yes
exit $LASTEXITCODE
''')
    result = subprocess.run([powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper)],
                            env={**os.environ, 'MOCK_TRANSPORT_ROOT': str(tmp_path), 'MOCK_UPLOAD_FAIL': str(int(upload_fails))},
                            capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    records = [json.loads(line) for line in (tmp_path/'transport-calls.jsonl').read_text().splitlines()]
    assert config.read_bytes() == original_config
    if upload_fails:
        assert result.returncode == 1 and 'Docker service was not changed' in output
        assert [row['kind'] for row in records] == ['scp']
    else:
        assert result.returncode == 0 and 'Update finished successfully' in output, output
        assert [row['kind'] for row in records] == ['scp', 'ssh']
        ssh_args = records[1]['args']
        assert '-tt' in ssh_args and 'StrictHostKeyChecking=ask' in ssh_args
        assert ssh_args[ssh_args.index('-p')+1] == '2222'
        assert 'synthetic-user@synthetic.invalid' in ssh_args and 'update_remote.sh' in ssh_args[-1]
        with tarfile.open(tmp_path/'received.tar') as archive:
            assert set(archive.getnames()) == {'package.tar.gz', 'update_remote.sh', 'update_helper.py'}
            assert archive.extractfile('package.tar.gz').read() == package.read_bytes()
            for name in ('update_remote.sh', 'update_helper.py'):
                assert archive.extractfile(name).read() == (scripts/name).read_bytes().replace(b'\r\n', b'\n')
