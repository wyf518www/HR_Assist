[CmdletBinding()]
param(
    [string]$Config = '',
    [string]$Package = '',
    [switch]$Plan,
    [switch]$Yes,
    [switch]$Configure,
    [switch]$SavePassword,
    [switch]$ForgetPassword,
    [switch]$SeparateSudoPassword,
    [switch]$Interactive
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$privateRoot = Join-Path $repoRoot '.local-deployment'
if (-not $Config) { $Config = Join-Path $privateRoot 'remote-update.json' }
$Config = [IO.Path]::GetFullPath($Config)
$localCredentials = $null
$askpassEnvironment = @{}
. (Join-Path $PSScriptRoot 'Local-Credentials.ps1')
. (Join-Path $PSScriptRoot 'Password-Transport.ps1')

function Read-Setting([string]$Label, [string]$Default = '') {
    $value = Read-Host "$Label [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Check-Configuration($Value) {
    foreach ($key in @('Host', 'Port', 'User', 'ProjectDir', 'Service', 'Image', 'PackageDirectory')) {
        if (-not ($Value.PSObject.Properties.Name -contains $key)) { throw "Missing configuration field: $key" }
    }
    if ($Value.PSObject.Properties.Name -match '(?i)password|token|secret') { throw 'Do not store passwords or tokens in this configuration.' }
    if ($Value.Host -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Host must be a hostname, IPv4 address, or SSH alias; do not enter a URL or port.' }
    if ($Value.User -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$') { throw 'Invalid SSH username.' }
    $portNumber = 0
    if (-not [int]::TryParse([string]$Value.Port, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) { throw 'SSH port must be 1-65535.' }
    if ($Value.ProjectDir -notmatch '^/[a-zA-Z0-9_./-]+$' -or $Value.ProjectDir.Split('/') -contains '..') { throw 'ProjectDir must be an absolute NAS path using letters, digits, dot, slash, underscore or hyphen.' }
    if ($Value.Service -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$') { throw 'Invalid Compose service name.' }
    if ($Value.Image -notmatch '^[a-z0-9][a-z0-9_.-]*:[a-zA-Z0-9_.-]+$') { throw 'Use the existing local image name and tag, such as hr-assist:0.1.0.' }
    if ([string]::IsNullOrWhiteSpace($Value.PackageDirectory)) { throw 'PackageDirectory is required.' }
}

try {
    if (@($Plan, $Configure, $SavePassword, $ForgetPassword).Where({ $_ }).Count -gt 1) {
        throw 'Choose only one of -Plan, -Configure, -SavePassword or -ForgetPassword.'
    }
    if ($SeparateSudoPassword -and -not $SavePassword) { throw '-SeparateSudoPassword requires -SavePassword.' }
    if ($Configure -or -not (Test-Path -LiteralPath $Config)) {
        if ($Plan -and -not $Configure) { throw 'Configuration is missing. Run once with -Configure, then use -Plan.' }
        Write-Host 'First-time connection setup. Passwords never go in this JSON configuration.'
        $defaultPackages = Join-Path $repoRoot 'updates'
        if (-not (Test-Path -LiteralPath $defaultPackages)) { $defaultPackages = $privateRoot }
        $settings = [PSCustomObject]@{
            Host = (Read-Setting 'NAS SSH host or existing SSH alias')
            Port = (Read-Setting 'SSH port (not the HTTPS website port)' '22')
            User = (Read-Setting 'SSH username')
            ProjectDir = (Read-Setting 'Existing NAS project directory containing docker-compose.yml')
            Service = 'hr-assist'
            Image = 'hr-assist:0.1.0'
            PackageDirectory = (Read-Setting 'Local folder containing cumulative .tar.gz packages' $defaultPackages)
        }
        Check-Configuration $settings
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Config)) | Out-Null
        [IO.File]::WriteAllText($Config, ($settings | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Write-Host "Private configuration saved: $Config"
        if ($Configure) { exit 0 }
    } else {
        $settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
        Check-Configuration $settings
    }
    if ($ForgetPassword) {
        Remove-HRLocalCredential -Settings $settings
        Write-Host 'Saved SSH/sudo passwords removed for this target. Interactive login remains available.'
        exit 0
    }
    if ($SavePassword) {
        Write-Host "Save password locally for $($settings.User)@$($settings.Host):$($settings.Port)."
        Write-Host 'Windows encrypts it for this Windows account on this computer. Future double-clicks start the update without y/N or password prompts.'
        $sshPassword = Read-Host 'SSH password (hidden)' -AsSecureString
        $sudoPassword = $null
        try {
            if ($SeparateSudoPassword) { $sudoPassword = Read-Host 'sudo password (hidden)' -AsSecureString }
            Save-HRLocalCredential -Settings $settings -Password $sshPassword -SudoPassword $sudoPassword
        } finally {
            if ($null -ne $sshPassword) { $sshPassword.Dispose() }
            if ($null -ne $sudoPassword) { $sudoPassword.Dispose() }
        }
        Write-Host 'Encrypted local passwords saved. No SSH connection or update was performed.'
        Write-Host 'Keep only the intended cumulative package in updates, then double-click Update-HRAssist.cmd.'
        exit 0
    }
    if (-not $Package) {
        $choices = @(Get-ChildItem -LiteralPath $settings.PackageDirectory -Filter 'hr-assist-*.tar.gz' -File | Sort-Object LastWriteTimeUtc -Descending)
        if (-not $choices.Count) { throw 'No cumulative update package found. Put the new .tar.gz in PackageDirectory, or pass -Package.' }
        $Package = $choices[0].FullName
    }
    $packageFile = Get-Item -LiteralPath $Package
    if ($packageFile.PSIsContainer -or $packageFile.Name -notlike '*.tar.gz' -or $packageFile.Length -gt 100MB) { throw 'Select an application cumulative .tar.gz package no larger than 100 MiB.' }
    $hasher = [Security.Cryptography.SHA256]::Create()
    $packageStream = [IO.File]::OpenRead($packageFile.FullName)
    try { $digest = ([BitConverter]::ToString($hasher.ComputeHash($packageStream))).Replace('-', '').ToLowerInvariant() }
    finally { $packageStream.Dispose(); $hasher.Dispose() }
    Write-Host "Package: $($packageFile.FullName)"
    Write-Host "SHA256:  $digest"
    Write-Host "Target:  $($settings.User)@$($settings.Host):$($settings.Port)"
    Write-Host "Project: $($settings.ProjectDir)"
    Write-Host 'Plan: upload -> validate -> offline build -> stop -> complete backup -> activate -> health check.'
    Write-Host 'Configuration and volume paths are preserved. Backups stay on the NAS. No GitHub access.'
    if ($Plan) { Write-Host 'PLAN ONLY: no SSH connection or remote changes were made.'; exit 0 }
    $savedPasswordMode = -not $Interactive -and (Test-HRLocalCredential -Settings $settings)
    if ($savedPasswordMode) {
        $localCredentials = Read-HRLocalCredential -Settings $settings
        Write-Host 'Saved-password mode: starting automatically. Stop using the workbench until the update finishes.'
        $askpass = Join-Path $PSScriptRoot 'ssh-askpass.cmd'
        if (-not (Test-Path -LiteralPath $askpass -PathType Leaf)) { throw 'Missing ssh-askpass.cmd. Extract the complete updater package again.' }
        $targetJson = @{ Host=$settings.Host; Port=[int]$settings.Port; User=$settings.User } | ConvertTo-Json -Compress
        $values = @{
            SSH_ASKPASS=$askpass; SSH_ASKPASS_REQUIRE='force'; DISPLAY='hr-assist-local:0';
            HR_ASSIST_ASKPASS_ACTIVE='1'; HR_ASSIST_ASKPASS_TARGET=$targetJson;
            HR_ASSIST_ASKPASS_ROOT=[IO.Path]::GetDirectoryName((Get-HRCredentialPath -Settings $settings))
        }
        foreach ($entry in $values.GetEnumerator()) {
            $askpassEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
            [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
        }
    }
    if (-not $savedPasswordMode -and -not $Yes -and (Read-Host 'Stop using the workbench until the update finishes. Start now? [y/N]') -notmatch '^(?i:y|yes)$') {
        Write-Host 'Cancelled. No remote changes.'
        exit 0
    }
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $scp = (Get-Command scp.exe -ErrorAction Stop).Source
    $tar = (Get-Command tar.exe -ErrorAction Stop).Source
    $release = (Get-Date -Format 'yyyyMMddHHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $stage = Join-Path $privateRoot "update-staging/$release"
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    Copy-Item -LiteralPath $packageFile.FullName -Destination (Join-Path $stage 'package.tar.gz')
    foreach ($script in @('update_remote.sh', 'update_helper.py')) {
        # Force UTF-8 without BOM and LF for BusyBox/Python, even after a Windows checkout.
        $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $script)).Replace("`r`n", "`n")
        [IO.File]::WriteAllText((Join-Path $stage $script), $text, (New-Object Text.UTF8Encoding($false)))
    }
    $transport = Join-Path $stage 'transport.tar'
    & $tar -cf $transport -C $stage package.tar.gz update_remote.sh update_helper.py
    if ($LASTEXITCODE -ne 0) { throw 'Could not build the upload archive.' }
    $target = "$($settings.User)@$($settings.Host)"
    $remoteArchive = ".hr-assist-upload-$release.tar"
    $remoteStage = ".hr-assist-upload-$release"
    Write-Host 'Uploading via legacy SCP over SSH (no SFTP required).'
    # OpenSSH 9+ defaults to SFTP. DSM can allow SSH while its SFTP subsystem
    # is disabled; -O selects the traditional SCP protocol over the same SSH.
    if ($savedPasswordMode) {
        # A saved password must never approve an unknown/replaced host key or
        # answer a private-key passphrase / MFA challenge.
        $sshOptions = @('-o','StrictHostKeyChecking=yes','-o','BatchMode=no','-o','NumberOfPasswordPrompts=1',
            '-o','PreferredAuthentications=password','-o','PasswordAuthentication=yes','-o','PubkeyAuthentication=no',
            '-o','KbdInteractiveAuthentication=no','-o','ConnectTimeout=20')
        & $scp -O -P ([string]$settings.Port) @sshOptions $transport "${target}:$remoteArchive"
    } else {
        & $scp -O -P ([string]$settings.Port) -o StrictHostKeyChecking=ask $transport "${target}:$remoteArchive"
    }
    if ($LASTEXITCODE -ne 0) { throw 'Upload failed. The Docker service was not changed. For an unknown host key, use -Interactive and verify the fingerprint; for a changed password, run -SavePassword again.' }
    # All remote arguments have been restricted to non-shell syntax above.
    $remoteCommand = "set -eu`numask 077`nmkdir '$remoteStage'`ntar -xf '$remoteArchive' -C '$remoteStage'`nrm -f '$remoteArchive'`nexec sh '$remoteStage/update_remote.sh' '$($settings.ProjectDir)' '$($settings.Service)' '$($settings.Image)' '$release' '$digest'"
    if ($savedPasswordMode) {
        # Only this fixed flag crosses SSH; the sudo password travels through
        # redirected stdin, with no terminal echo, arguments, or disk copy.
        $remoteCommand = $remoteCommand.Replace('exec sh ', 'exec env HR_ASSIST_SUDO_STDIN=1 sh ')
        Write-Host 'Starting update with encrypted local credentials.'
        $sshArgs = @('-T','-p',([string]$settings.Port)) + $sshOptions + @('-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4',$target,$remoteCommand)
        $updateExit = Invoke-HRWithSecureInput -Executable $ssh -Arguments $sshArgs -Password $localCredentials.SudoPassword
    } else {
        Write-Host 'Starting update. SSH may ask again; a non-root account may also receive a sudo password prompt.'
        & $ssh -tt -p ([string]$settings.Port) -o StrictHostKeyChecking=ask -o ServerAliveInterval=15 -o ServerAliveCountMax=4 $target $remoteCommand
        $updateExit = $LASTEXITCODE
    }
    if ($updateExit -ne 0) { throw 'Remote update did not report success. Read the last message and NAS backup/status before retrying; do not delete the update lock blindly.' }
    Write-Host 'Update finished successfully. Refresh the workbench with Ctrl+F5.' -ForegroundColor Green
} catch {
    Write-Host ("UPDATE ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    foreach ($entry in $askpassEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    if ($null -ne $localCredentials) {
        $localCredentials.Password.Dispose()
        if (-not [Object]::ReferenceEquals($localCredentials.Password, $localCredentials.SudoPassword)) {
            $localCredentials.SudoPassword.Dispose()
        }
    }
}
