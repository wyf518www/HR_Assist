"""Real Windows DPAPI / OpenSSH askpass plumbing, using synthetic secrets only.

No SSH server or NAS is contacted. ssh-keygen shares OpenSSH's askpass launcher;
its key-passphrase prompt is deliberately bridged to a synthetic SSH password
prompt for that plumbing check. The production helper rejects key passphrases.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(os.name != 'nt' or not POWERSHELL,
                                reason='Requires Windows PowerShell and current-user DPAPI')


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


@pytest.fixture
def askpass(tmp_path):
    scripts = tmp_path / 'distribution with spaces' / 'scripts' / 'deploy'
    scripts.mkdir(parents=True)
    for name in ('Local-Credentials.ps1', 'Read-SshPassword.ps1', 'ssh-askpass.cmd'):
        shutil.copyfile(ROOT / 'scripts' / 'deploy' / name, scripts / name)
    private = tmp_path / 'local credentials'
    target = dict(Host='synthetic.invalid', Port=2222, User='synthetic-user')
    secret = 'Synthetic-中文-"\'$&%<>-7391'
    environment = {**os.environ,
                   'HR_ASSIST_ASKPASS_ACTIVE': '1',
                   'HR_ASSIST_ASKPASS_ROOT': str(private),
                   'HR_ASSIST_ASKPASS_TARGET': json.dumps(target),
                   'HR_SYNTHETIC_SECRET': secret}
    setup = tmp_path / 'setup-synthetic.ps1'
    setup.write_text(
        "$ErrorActionPreference='Stop'\n"
        f". {ps_quote(scripts / 'Local-Credentials.ps1')}\n"
        "$target=ConvertFrom-Json $env:HR_ASSIST_ASKPASS_TARGET\n"
        "$password=ConvertTo-SecureString $env:HR_SYNTHETIC_SECRET -AsPlainText -Force\n"
        "try { Save-HRLocalCredential $target $password -Root $env:HR_ASSIST_ASKPASS_ROOT } "
        "finally { $password.Dispose() }\n", encoding='utf-8-sig')
    result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(setup)], env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert secret.encode() not in result.stdout + result.stderr
    environment.pop('HR_SYNTHETIC_SECRET')
    return scripts, private, target, secret, environment


def invoke(askpass, prompt, *, changes=None, extra=()):
    scripts, _, _, _, environment = askpass
    environment = environment.copy()
    for key, value in (changes or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-File', str(scripts / 'Read-SshPassword.ps1'), prompt, *extra],
                          env=environment, capture_output=True, timeout=20)


def test_password_only_stdout_is_exact_utf8_without_bom_and_no_persistent_plaintext(askpass):
    _, private, _, secret, _ = askpass
    before = {path.name: path.read_bytes() for path in private.rglob('*') if path.is_file()}
    result = invoke(askpass, "synthetic-user@synthetic.invalid's password: ")
    assert result.returncode == 0
    assert result.stdout == secret.encode('utf-8') + b'\r\n'
    assert not result.stderr and not result.stdout.startswith(b'\xef\xbb\xbf')
    after = {path.name: path.read_bytes() for path in private.rglob('*') if path.is_file()}
    assert after == before
    assert not any(secret.encode('utf-8') in data or secret.encode('utf-16-le') in data for data in after.values())


def test_standard_password_prompt_does_not_misclassify_words_in_host_or_user(askpass):
    result = invoke(askpass, "code@token.example's password: ")
    assert result.returncode == 0 and result.stdout == askpass[3].encode('utf-8') + b'\r\n'
    assert not result.stderr


@pytest.mark.parametrize('prompt', [
    'Are you sure you want to continue connecting (yes/no/[fingerprint])?',
    'Enter passphrase for key synthetic:', 'Verification code:', 'MFA password:',
    'One-time password:', 'Two-factor password:', '2FA password:',
    'Password and token:', 'Password:\n', '',
])
def test_host_key_private_key_mfa_and_malformed_prompts_never_release_secret(askpass, prompt):
    result = invoke(askpass, prompt)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr == b'Stored SSH password is unavailable or this prompt is not supported.\r\n'
    assert askpass[3].encode() not in result.stderr


@pytest.mark.parametrize('changes', [
    {'HR_ASSIST_ASKPASS_ACTIVE': None}, {'HR_ASSIST_ASKPASS_ACTIVE': '0'},
    {'HR_ASSIST_ASKPASS_ROOT': None}, {'HR_ASSIST_ASKPASS_TARGET': None},
    {'HR_ASSIST_ASKPASS_TARGET': 'not-json'},
    {'HR_ASSIST_ASKPASS_TARGET': '{"Host":"different.invalid","Port":2222,"User":"synthetic-user"}'},
])
def test_missing_context_or_wrong_target_fails_without_plaintext_or_internal_error(askpass, changes):
    result = invoke(askpass, 'Password:', changes=changes)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr == b'Stored SSH password is unavailable or this prompt is not supported.\r\n'


def test_additional_prompt_arguments_fail_closed(askpass):
    result = invoke(askpass, 'Password:', extra=('untrusted extra',))
    assert result.returncode == 1 and not result.stdout
    assert b'untrusted extra' not in result.stderr and askpass[3].encode() not in result.stderr


def test_real_openssh_launches_cmd_and_powershell_askpass_with_synthetic_prompt_bridge(askpass, tmp_path):
    keygen = shutil.which('ssh-keygen.exe')
    if not keygen:
        pytest.skip('Requires Windows OpenSSH ssh-keygen')
    scripts, _, _, secret, environment = askpass
    key = tmp_path / 'synthetic_private_key'
    created = subprocess.run([keygen, '-t', 'ed25519', '-N', secret, '-f', str(key)],
                             capture_output=True, timeout=20)
    assert created.returncode == 0
    # The actual ssh-keygen prompt is correctly refused by production code.
    env = {**environment, 'SSH_ASKPASS': str(scripts / 'ssh-askpass.cmd'),
           'SSH_ASKPASS_REQUIRE': 'force', 'DISPLAY': 'hr-synthetic:0'}
    refused = subprocess.run([keygen, '-y', '-f', str(key)], input=b'', env=env,
                             capture_output=True, timeout=20)
    assert refused.returncode != 0 and not refused.stdout
    assert secret.encode() not in refused.stderr
    # Only this test adapter converts a key prompt to the intended password
    # request. It does not implement an SSH server or a network login.
    bridge = tmp_path / 'synthetic prompt bridge.cmd'
    bridge.write_text('@echo off\r\ncall "' + str(scripts / 'ssh-askpass.cmd')
                      + '" "synthetic-user@synthetic.invalid\'s password: "\r\nexit /b %ERRORLEVEL%\r\n')
    env['SSH_ASKPASS'] = str(bridge)
    result = subprocess.run([keygen, '-y', '-f', str(key)], input=b'', env=env,
                            capture_output=True, timeout=20)
    assert result.returncode == 0 and result.stdout.startswith(b'ssh-ed25519 ')
    assert not result.stderr and secret.encode() not in result.stdout
