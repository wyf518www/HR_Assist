"""Saved-password launcher tests: local executable stand-ins, no network."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts/deploy'
SYNTHETIC = 'Synthetic 中文 "quote" $&%\\end'
ENV_KEYS = ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'DISPLAY', 'HR_ASSIST_ASKPASS_ACTIVE',
            'HR_ASSIST_ASKPASS_TARGET', 'HR_ASSIST_ASKPASS_ROOT')


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps_run(script, env=None, timeout=40):
    shell = shutil.which('powershell.exe')
    if os.name != 'nt' or not shell:
        pytest.skip('Requires Windows PowerShell')
    return subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                          env=env, capture_output=True, timeout=timeout)


@pytest.fixture(scope='module')
def transport_executable(tmp_path_factory):
    if os.name != 'nt' or not shutil.which('powershell.exe') or not shutil.which('tar.exe'):
        pytest.skip('Requires Windows PowerShell and tar')
    folder = tmp_path_factory.mktemp('saved-transport-exe')
    source = folder / 'MockTransport.cs'
    source.write_text(r'''
using System;
using System.IO;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
class Program {
  static int Main(string[] args) {
    var root = Environment.GetEnvironmentVariable("MOCK_TRANSPORT_ROOT");
    var scp = Array.IndexOf(args, "-O") >= 0;
    var saved = Array.IndexOf(args, "-T") >= 0;
    var record = new Dictionary<string, object>();
    record["kind"] = scp ? "scp" : "ssh";
    record["args"] = args;
    var values = new Dictionary<string, string>();
    foreach (var key in new [] {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY", "HR_ASSIST_ASKPASS_ACTIVE", "HR_ASSIST_ASKPASS_TARGET", "HR_ASSIST_ASKPASS_ROOT"})
      values[key] = Environment.GetEnvironmentVariable(key);
    record["env"] = values;
    if (saved) {
      using (var input = Console.OpenStandardInput())
      using (var buffer = new MemoryStream()) {
        input.CopyTo(buffer);
        using (var sha = SHA256.Create())
          record["stdin_hash"] = BitConverter.ToString(sha.ComputeHash(buffer.ToArray())).Replace("-", "").ToLowerInvariant();
      }
    }
    File.AppendAllText(Path.Combine(root, "transport.jsonl"), new JavaScriptSerializer().Serialize(record) + "\n");
    if (scp) {
      if (Environment.GetEnvironmentVariable("MOCK_UPLOAD_FAIL") == "1") return 3;
      File.Copy(args[args.Length-2], Path.Combine(root, "received.tar"), true);
    } else if (Environment.GetEnvironmentVariable("MOCK_SSH_FAIL") == "1") return 7;
    Console.WriteLine("SYNTHETIC TRANSPORT ONLY");
    return 0;
  }
}
''', encoding='utf-8')
    executable = folder / 'MockTransport.exe'
    build = folder / 'build.ps1'
    build.write_text(f"$ErrorActionPreference='Stop'\nAdd-Type -Path {quote(source)} -ReferencedAssemblies 'System.Web.Extensions','System.Core' -OutputAssembly {quote(executable)} -OutputType ConsoleApplication\n", encoding='utf-8')
    result = ps_run(build)
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def distribution(tmp_path, executable, mode='success', flags=()):
    scripts = tmp_path / 'updater with spaces/scripts/deploy'
    scripts.mkdir(parents=True)
    for name in ('Update-HRAssist.ps1', 'Password-Transport.ps1', 'update_remote.sh', 'update_helper.py'):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    # This marker is checked for presence only; mock SSH never invokes it.
    (scripts / 'ssh-askpass.cmd').write_text('@exit /b 99\n')
    chars = ','.join(str(ord(c)) for c in SYNTHETIC)
    (scripts / 'Local-Credentials.ps1').write_text(f'''
function Test-HRLocalCredential {{ param($Settings)
    [IO.File]::AppendAllText((Join-Path $env:MOCK_TRANSPORT_ROOT 'credential-calls'), "test`n")
    return $true
}}
function Get-HRCredentialPath {{ param($Settings)
    return (Join-Path $env:MOCK_TRANSPORT_ROOT 'local-credentials/profile.json')
}}
function Read-HRLocalCredential {{ param($Settings)
    [IO.File]::AppendAllText((Join-Path $env:MOCK_TRANSPORT_ROOT 'credential-calls'), "read`n")
    if ($env:MOCK_CREDENTIAL_BAD -eq '1') {{ throw 'Synthetic encrypted credential is corrupt.' }}
    $first = New-Object Security.SecureString
    $second = New-Object Security.SecureString
    foreach ($char in ([char[]]@({chars}))) {{ $first.AppendChar($char); $second.AppendChar($char) }}
    return [PSCustomObject]@{{Password=$first; SudoPassword=$second}}
}}
function Save-HRLocalCredential {{ param($Settings, $Password, $SudoPassword)
    if ($Password.Length -ne {len(SYNTHETIC)}) {{ throw 'Synthetic password length mismatch' }}
    [IO.File]::AppendAllText((Join-Path $env:MOCK_TRANSPORT_ROOT 'credential-calls'), "save`n")
}}
function Remove-HRLocalCredential {{ param($Settings)
    [IO.File]::AppendAllText((Join-Path $env:MOCK_TRANSPORT_ROOT 'credential-calls'), "remove`n")
}}
''', encoding='utf-8')
    package = tmp_path / 'hr-assist-synthetic.tar.gz'
    package.write_bytes(b'Synthetic package, never used by a remote host')
    config = tmp_path / 'connection.json'
    config.write_text(json.dumps(dict(Host='synthetic.invalid', Port=2222, User='synthetic-user',
        ProjectDir='/volume1/hr', Service='hr-assist', Image='hr-assist:0.1.0', PackageDirectory=str(tmp_path))))
    wrapper = tmp_path / 'run.ps1'
    original_env = {key: f'synthetic-original-{key}' for key in ENV_KEYS}
    wrapper.write_text(f'''
$ErrorActionPreference='Stop'
function Read-Host {{ param([string]$Prompt, [switch]$AsSecureString)
  if ($env:MOCK_ALLOW_SAVE_PROMPT -ne '1' -or -not $AsSecureString) {{ throw 'Unexpected interactive prompt' }}
  $value = New-Object Security.SecureString
  foreach ($char in ([char[]]@({chars}))) {{ $value.AppendChar($char) }}
  return $value
}}
function Get-Command {{ param([string]$Name, [string]$ErrorAction)
  switch ($Name) {{
    'ssh.exe' {{ [PSCustomObject]@{{Source={quote(executable)}}}; return }}
    'scp.exe' {{ [PSCustomObject]@{{Source={quote(executable)}}}; return }}
    'tar.exe' {{ [PSCustomObject]@{{Source={quote(shutil.which('tar.exe'))}}}; return }}
    default {{ throw 'Unexpected executable lookup' }}
  }}
}}
try {{
  & {quote(scripts / 'Update-HRAssist.ps1')} -Config {quote(config)} -Package {quote(package)} {' '.join(flags)}
  $status = $LASTEXITCODE
}} finally {{
  $snapshot = @{{}}
  foreach ($name in @({','.join(quote(k) for k in ENV_KEYS)})) {{ $snapshot[$name] = [Environment]::GetEnvironmentVariable($name,'Process') }}
  [IO.File]::WriteAllText({quote(tmp_path / 'environment-after.json')}, ($snapshot | ConvertTo-Json -Compress))
}}
exit $status
''', encoding='utf-8')
    env = {**os.environ, **original_env, 'MOCK_TRANSPORT_ROOT': str(tmp_path),
        'MOCK_UPLOAD_FAIL': str(int(mode == 'upload_failure')),
        'MOCK_SSH_FAIL': str(int(mode == 'ssh_failure')),
        'MOCK_CREDENTIAL_BAD': str(int(mode == 'corrupt')),
        'MOCK_ALLOW_SAVE_PROMPT': str(int('-SavePassword' in flags))}
    before = config.read_bytes()
    result = ps_run(wrapper, env)
    output = result.stdout + result.stderr
    assert config.read_bytes() == before
    assert SYNTHETIC.encode() not in output and SYNTHETIC.encode('utf-16le') not in output
    records = [json.loads(line) for line in (tmp_path / 'transport.jsonl').read_text().splitlines()] if (tmp_path / 'transport.jsonl').exists() else []
    assert SYNTHETIC not in json.dumps(records, ensure_ascii=False)
    if (tmp_path / 'environment-after.json').exists():
        assert json.loads((tmp_path / 'environment-after.json').read_text(encoding='utf-8-sig')) == original_env
    else:
        pytest.fail('Wrapper did not verify environment restoration')
    return result, records


@pytest.mark.parametrize('mode', ['success', 'upload_failure', 'ssh_failure'])
def test_saved_mode_uses_checked_host_and_stdin_without_interactive_password(tmp_path, transport_executable, mode):
    result, rows = distribution(tmp_path, transport_executable, mode)
    output = result.stdout + result.stderr
    assert [r['kind'] for r in rows] == (['scp'] if mode == 'upload_failure' else ['scp', 'ssh']), output
    assert result.returncode == (0 if mode == 'success' else 1), output
    for row in rows:
        args = row['args']
        assert 'StrictHostKeyChecking=yes' in args
        assert 'BatchMode=no' in args and 'NumberOfPasswordPrompts=1' in args
        assert 'PreferredAuthentications=password' in args
        assert 'PubkeyAuthentication=no' in args and 'KbdInteractiveAuthentication=no' in args
        assert row['env']['SSH_ASKPASS_REQUIRE'] == 'force'
        assert row['env']['HR_ASSIST_ASKPASS_ACTIVE'] == '1'
        assert json.loads(row['env']['HR_ASSIST_ASKPASS_TARGET']) == {'Host':'synthetic.invalid', 'Port':2222, 'User':'synthetic-user'}
    assert '-O' in rows[0]['args']
    if mode != 'upload_failure':
        ssh = rows[-1]
        assert '-T' in ssh['args'] and '-tt' not in ssh['args']
        assert 'HR_ASSIST_SUDO_STDIN=1' in ssh['args'][-1]
        assert ssh['stdin_hash'] == hashlib.sha256((SYNTHETIC + '\n').encode()).hexdigest()


@pytest.mark.parametrize('mode,flags', [('corrupt', ()), ('corrupt', ('-Plan',)), ('success', ('-Interactive','-Yes'))])
def test_corrupt_plan_and_interactive_paths(tmp_path, transport_executable, mode, flags):
    result, rows = distribution(tmp_path, transport_executable, mode, flags)
    output = result.stdout + result.stderr
    if '-Plan' in flags:
        assert result.returncode == 0 and not rows and b'PLAN ONLY' in output
        assert not (tmp_path / 'credential-calls').exists()
    elif '-Interactive' in flags:
        assert result.returncode == 0 and [r['kind'] for r in rows] == ['scp','ssh'], output
        assert '-tt' in rows[-1]['args'] and 'StrictHostKeyChecking=ask' in rows[-1]['args']
        assert not (tmp_path / 'credential-calls').exists()
    else:
        assert result.returncode == 1 and not rows and b'corrupt' in output


def test_secure_stdin_and_windows_quoting_preserve_special_characters(tmp_path):
    script = tmp_path / 'child with spaces.py'
    received = tmp_path / 'received.json'
    script.write_text('import hashlib,json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({"args":sys.argv[2:],"stdin_hash":hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}))\n')
    values = ['', 'two words', 'quote"inside', 'end\\', '\\"mixed', 'line\nbreak', '$&%name']
    runner = tmp_path / 'stdin.ps1'
    chars = ','.join(str(ord(c)) for c in SYNTHETIC)
    runner.write_text(f'''
$ErrorActionPreference='Stop'
. {quote(SCRIPTS / 'Password-Transport.ps1')}
$secret = New-Object Security.SecureString
foreach ($char in ([char[]]@({chars}))) {{ $secret.AppendChar($char) }}
try {{
  $status = Invoke-HRWithSecureInput -Executable {quote(sys.executable)} -Arguments @({','.join(quote(v) for v in [str(script), str(received), *values])}) -Password $secret
  exit $status
}} finally {{ $secret.Dispose() }}
''', encoding='utf-8', newline='\n')
    result = ps_run(runner)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(received.read_text())
    assert record['args'] == values
    assert record['stdin_hash'] == hashlib.sha256((SYNTHETIC + '\n').encode()).hexdigest()
    assert SYNTHETIC.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize('flags,expected', [
    (('-SavePassword',), 'save'),
    (('-SavePassword', '-SeparateSudoPassword'), 'save'),
    (('-ForgetPassword',), 'remove'),
])
def test_password_management_is_local_only(tmp_path, transport_executable, flags, expected):
    result, rows = distribution(tmp_path, transport_executable, flags=flags)
    assert result.returncode == 0 and not rows, result.stdout + result.stderr
    assert (tmp_path / 'credential-calls').read_text().splitlines() == [expected]
    assert not (tmp_path / 'updater with spaces/.local-deployment/update-staging').exists()


@pytest.mark.parametrize('value', ['', 'line\nbreak', 'line\rbreak', 'null\0byte'])
def test_invalid_password_cannot_start_child_process(tmp_path, value):
    marker = tmp_path / 'child-started'
    runner = tmp_path / 'invalid.ps1'
    chars = ','.join(str(ord(c)) for c in value)
    runner.write_text(f'''
$ErrorActionPreference='Stop'
. {quote(SCRIPTS / 'Password-Transport.ps1')}
$secret = New-Object Security.SecureString
foreach ($char in ([char[]]@({chars}))) {{ $secret.AppendChar($char) }}
try {{
  Invoke-HRWithSecureInput -Executable {quote(sys.executable)} -Arguments @('-c', {quote("from pathlib import Path; Path(" + repr(str(marker)) + ").touch()")}) -Password $secret
  throw 'Expected rejection'
}} catch {{
  if ($_.Exception.Message -notlike 'SSH execution failed.*') {{ throw }}
}} finally {{ $secret.Dispose() }}
''', encoding='utf-8')
    result = ps_run(runner)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


def test_child_start_failure_is_fixed_message_without_native_exception(tmp_path):
    runner = tmp_path / 'missing.ps1'
    runner.write_text(f'''
$ErrorActionPreference='Stop'
. {quote(SCRIPTS / 'Password-Transport.ps1')}
$secret = New-Object Security.SecureString
$secret.AppendChar('x')
try {{
  Invoke-HRWithSecureInput -Executable {quote(tmp_path / 'missing.exe')} -Arguments @() -Password $secret
  throw 'Expected rejection'
}} catch {{
  if ($_.Exception.Message -notlike 'SSH execution failed.*') {{ throw }}
}} finally {{ $secret.Dispose() }}
''', encoding='utf-8')
    result = ps_run(runner)
    assert result.returncode == 0, result.stdout + result.stderr
