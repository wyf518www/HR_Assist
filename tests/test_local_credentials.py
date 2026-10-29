"""Real Windows DPAPI with synthetic secrets only; never connects to SSH."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(os.name != 'nt' or not POWERSHELL,
                                reason='Requires Windows PowerShell and current-user DPAPI')


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def execute(tmp_path, body):
    script = tmp_path / 'credential-test.ps1'
    script.write_text(f"""$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. {quote(ROOT / 'scripts/deploy/Local-Credentials.ps1')}
$root = {quote(tmp_path / 'isolated credentials')}
$target = [PSCustomObject]@{{ Host='SYNTHETIC.invalid'; Port=2222; User='synthetic-user' }}
function Assert-Test($Condition) {{ if (-not $Condition) {{ throw 'Synthetic assertion failed' }} }}
function Assert-Secret($Value, [string]$Expected) {{
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {{ Assert-Test ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) -ceq $Expected) }}
    finally {{ [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }}
}}
{body}
Write-Output 'SYNTHETIC_CREDENTIAL_TEST_OK'
""", encoding='utf-8-sig')
    result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'SYNTHETIC_CREDENTIAL_TEST_OK', result.stdout
    return tmp_path / 'isolated credentials'


def test_dpapi_round_trip_target_binding_acl_and_atomic_replacement(tmp_path):
    directory = execute(tmp_path, r'''
$plain = 'Synthetic-秘密-$&%"''-7391'
$secret = ConvertTo-SecureString $plain -AsPlainText -Force
Assert-Test (-not (Test-HRLocalCredential $target $root))
Save-HRLocalCredential $target $secret -Root $root
Assert-Test (Test-HRLocalCredential $target $root)
$path = Get-HRCredentialPath $target $root
$saved = Read-HRLocalCredential $target $root
Assert-Secret $saved.Password $plain
Assert-Secret $saved.SudoPassword $plain
$saved.Password.Dispose(); $saved.SudoPassword.Dispose()
$record = [IO.File]::ReadAllText($path)
Assert-Test (-not $record.Contains($plain))
Assert-Test (($record | ConvertFrom-Json).Host -ceq 'synthetic.invalid')
$allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
foreach ($entry in @($root, $path)) {
    $acl = Get-Acl -LiteralPath $entry
    Assert-Test $acl.AreAccessRulesProtected
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    Assert-Test ($rules.Count -eq 2)
    foreach ($rule in $rules) {
        Assert-Test ($rule.IdentityReference.Value -in $allowed)
        Assert-Test ($rule.AccessControlType -eq 'Allow' -and -not $rule.IsInherited)
    }
}
$other = [PSCustomObject]@{Host='synthetic.invalid'; Port=2223; User='synthetic-user'}
Assert-Test ((Get-HRCredentialPath $target $root) -ne (Get-HRCredentialPath $other $root))
Assert-Test (-not (Test-HRLocalCredential $other $root))
$updated = ConvertTo-SecureString 'Replacement-safe-7391' -AsPlainText -Force
$sudo = ConvertTo-SecureString 'Separate-sudo-4829' -AsPlainText -Force
Save-HRLocalCredential $target $updated $sudo $root
$saved = Read-HRLocalCredential $target $root
Assert-Secret $saved.Password 'Replacement-safe-7391'
Assert-Secret $saved.SudoPassword 'Separate-sudo-4829'
$saved.Password.Dispose(); $saved.SudoPassword.Dispose()
Assert-Test (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 1)
Remove-HRLocalCredential $target $root
Remove-HRLocalCredential $target $root
Assert-Test (-not (Test-HRLocalCredential $target $root))
$secret.Dispose(); $updated.Dispose(); $sudo.Dispose()
''')
    assert not list(directory.iterdir())


@pytest.mark.parametrize('damage', ['json', 'target', 'ciphertext', 'oversize', 'version_type', 'extra'])
def test_invalid_saved_file_fails_without_exposing_inputs(tmp_path, damage):
    changes = {
        'json': "[IO.File]::WriteAllText($path, 'INVALID Synthetic-secret-must-not-leak')",
        'target': "$record.Host='wrong.invalid'; [IO.File]::WriteAllText($path, ($record | ConvertTo-Json))",
        'ciphertext': "$record.Password='Synthetic-secret-must-not-leak'; [IO.File]::WriteAllText($path, ($record | ConvertTo-Json))",
        'oversize': "[IO.File]::WriteAllText($path, ('x' * 65537))",
        'version_type': "$record.Version='1'; [IO.File]::WriteAllText($path, ($record | ConvertTo-Json))",
        'extra': "$record | Add-Member -NotePropertyName Unexpected -NotePropertyValue 'Synthetic-secret-must-not-leak'; [IO.File]::WriteAllText($path, ($record | ConvertTo-Json))",
    }
    execute(tmp_path, r'''
$secret = ConvertTo-SecureString 'Synthetic-secret-must-not-leak' -AsPlainText -Force
Save-HRLocalCredential $target $secret -Root $root
$path = Get-HRCredentialPath $target $root
$record = [IO.File]::ReadAllText($path) | ConvertFrom-Json
''' + changes[damage] + r'''
$failed = $false
try { $unexpected = Read-HRLocalCredential $target $root }
catch {
    $failed = $true
    Assert-Test ($_.Exception.Message -ceq 'The saved SSH password is unavailable or belongs to another target or Windows profile. Save it again on this computer.')
}
Assert-Test $failed
Remove-HRLocalCredential $target $root
$secret.Dispose()
''')


def test_missing_and_unsafe_password_inputs_leave_existing_file_unchanged(tmp_path):
    execute(tmp_path, r'''
$secret = ConvertTo-SecureString 'Synthetic-original-7391' -AsPlainText -Force
Save-HRLocalCredential $target $secret -Root $root
$path = Get-HRCredentialPath $target $root
$before = [IO.File]::ReadAllText($path)
foreach ($value in @('', "with`nnewline", "with`rcarriage", ("with" + [char]0 + 'nul'))) {
    $bad = New-Object Security.SecureString
    foreach ($character in $value.ToCharArray()) { $bad.AppendChar($character) }
    $failed = $false
    try { Save-HRLocalCredential $target $bad -Root $root }
    catch { $failed = $true; Assert-Test (-not $_.Exception.Message.Contains('Synthetic-original')) }
    Assert-Test $failed
    Assert-Test ([IO.File]::ReadAllText($path) -ceq $before)
    $bad.Dispose()
}
Assert-Test (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 1)
Remove-HRLocalCredential $target $root
$failed = $false
try { $unexpected = Read-HRLocalCredential $target $root }
catch { $failed = $true }
Assert-Test $failed
$secret.Dispose()
''')


def test_network_relative_roots_and_invalid_targets_are_rejected(tmp_path):
    execute(tmp_path, r'''
foreach ($badRoot in @('relative\credentials', 'C:relative', '\\synthetic.invalid\share\credentials', 'C:\')) {
    $failed = $false
    try { $unexpected = Get-HRCredentialPath $target $badRoot }
    catch { $failed = $true }
    Assert-Test $failed
}
foreach ($badTarget in @(
    [PSCustomObject]@{Host='valid.invalid';Port=0;User='name'},
    [PSCustomObject]@{Host='valid.invalid';Port=22;User='user; command'},
    [PSCustomObject]@{Host='https://invalid';Port=22;User='name'}
)) {
    $failed = $false
    try { $unexpected = Get-HRCredentialPath $badTarget $root }
    catch { $failed = $true }
    Assert-Test $failed
}
Assert-Test (-not (Test-Path -LiteralPath $root))
''')


def test_credential_directory_junction_is_rejected(tmp_path):
    execute(tmp_path, r'''
$ordinary = Join-Path ([IO.Path]::GetDirectoryName($root)) 'ordinary'
[void][IO.Directory]::CreateDirectory($ordinary)
$null = New-Item -ItemType Junction -Path $root -Target $ordinary -ErrorAction Stop
$secret = ConvertTo-SecureString 'Synthetic-only-7391' -AsPlainText -Force
$failed = $false
try { Save-HRLocalCredential $target $secret -Root $root }
catch { $failed = $true }
Assert-Test $failed
Assert-Test (@(Get-ChildItem -LiteralPath $ordinary -Force).Count -eq 0)
$secret.Dispose()
''')


def test_atomic_replacement_failure_keeps_previous_secret_and_cleans_own_temporary_file(tmp_path):
    execute(tmp_path, r'''
$secret = ConvertTo-SecureString 'Synthetic-original-7391' -AsPlainText -Force
$replacement = ConvertTo-SecureString 'Synthetic-replacement-4829' -AsPlainText -Force
Save-HRLocalCredential $target $secret -Root $root
$path = Get-HRCredentialPath $target $root
$before = [IO.File]::ReadAllText($path)
$held = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$failed = $false
try {
    try { Save-HRLocalCredential $target $replacement -Root $root }
    catch { $failed = $true }
} finally { $held.Dispose() }
Assert-Test $failed
Assert-Test ([IO.File]::ReadAllText($path) -ceq $before)
Assert-Test (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 1)
$saved = Read-HRLocalCredential $target $root
Assert-Secret $saved.Password 'Synthetic-original-7391'
$saved.Password.Dispose(); $saved.SudoPassword.Dispose()
Remove-HRLocalCredential $target $root
$secret.Dispose(); $replacement.Dispose()
''')
