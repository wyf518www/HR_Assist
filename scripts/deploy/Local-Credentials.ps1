# Windows-only, current-user DPAPI storage. This module never emits plaintext.
# Root is injectable for isolated tests; normal callers leave it empty.
# Load this runtime's module explicitly: a PowerShell 7 parent can otherwise
# pass its PSModulePath to a Windows PowerShell 5.1 subprocess.
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop

function Get-HRCredentialTarget {
    param($Settings)
    try {
        $targetHost = ([string]$Settings.Host).Trim().ToLowerInvariant()
        $targetUser = [string]$Settings.User
        $targetPort = 0
        if ($targetHost -notmatch '^[a-z0-9][a-z0-9.-]*$' -or
            $targetUser -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$' -or
            -not [int]::TryParse([string]$Settings.Port, [ref]$targetPort) -or
            $targetPort -lt 1 -or $targetPort -gt 65535) { throw 'Invalid target' }
        return [PSCustomObject]@{ Host = $targetHost; Port = $targetPort; User = $targetUser }
    } catch { throw 'The SSH credential target is invalid.' }
}

function Assert-HRNoReparsePath {
    param([string]$Path)
    $currentPath = [IO.Path]::GetFullPath($Path)
    while ($currentPath) {
        if (Test-Path -LiteralPath $currentPath) {
            $entry = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Credential paths cannot contain links or junctions.'
            }
        }
        $parentPath = [IO.Path]::GetDirectoryName($currentPath.TrimEnd('\'))
        if ($parentPath -eq $currentPath) { break }
        $currentPath = $parentPath
    }
}

function Get-HRCredentialRoot {
    param([string]$Root = '')
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Local password storage requires Windows.' }
    if (-not $Root) {
        $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
        if (-not $localData) { throw 'The local Windows profile directory is unavailable.' }
        $Root = Join-Path $localData 'HR-Assist-Updater\credentials'
    }
    # Reject UNC and drive-relative paths, including redirected network drives.
    if ($Root -notmatch '^[A-Za-z]:[\\/]') { throw 'Credential storage must use a local absolute Windows path.' }
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($resolvedRoot))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Credential storage must be on a local fixed drive.' }
    if ($resolvedRoot.Length -le 3) { throw 'Credential storage cannot be a drive root.' }
    Assert-HRNoReparsePath $resolvedRoot
    return $resolvedRoot
}

function Get-HRCredentialPath {
    param($Settings, [string]$Root = '')
    $target = Get-HRCredentialTarget $Settings
    $resolvedRoot = Get-HRCredentialRoot $Root
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes("$($target.Host)`n$($target.Port)`n$($target.User)")
        $key = ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose() }
    $path = Join-Path $resolvedRoot ($key + '.json')
    Assert-HRNoReparsePath $path
    return $path
}

function Set-HRPrivateCredentialAcl {
    param([string]$Path, [switch]$Directory)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'
    if ($Directory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($sid)
    foreach ($identity in @($sid, $systemSid)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity,
            [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
    }
    if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) }
    else { [IO.File]::SetAccessControl($Path, $acl) }
}

function Test-HRLocalCredential {
    param($Settings, [string]$Root = '')
    $path = Get-HRCredentialPath $Settings $Root
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    if (-not [IO.File]::Exists($path)) { throw 'The saved SSH credential is not an ordinary file.' }
    return $true
}

function Assert-HRSingleLineSecret {
    param([Security.SecureString]$Value)
    if ($null -eq $Value -or $Value.Length -eq 0) { throw 'A nonempty password is required.' }
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        for ($offset = 0; $offset -lt $Value.Length; $offset++) {
            $character = [Runtime.InteropServices.Marshal]::ReadInt16($pointer, $offset * 2)
            if ($character -in @(0, 10, 13)) { throw 'The password must be a single line without NUL characters.' }
        }
    } finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
}

function Save-HRLocalCredential {
    param($Settings, [Security.SecureString]$Password, [Security.SecureString]$SudoPassword = $null, [string]$Root = '')
    $temporaryPath = $null
    try {
        Assert-HRSingleLineSecret $Password
        if ($null -eq $SudoPassword) { $SudoPassword = $Password }
        Assert-HRSingleLineSecret $SudoPassword
        $target = Get-HRCredentialTarget $Settings
        $path = Get-HRCredentialPath $Settings $Root
        $directory = [IO.Path]::GetDirectoryName($path)
        [void][IO.Directory]::CreateDirectory($directory)
        Assert-HRNoReparsePath $directory
        Set-HRPrivateCredentialAcl $directory -Directory
        if (Test-Path -LiteralPath $path) {
            Assert-HRNoReparsePath $path
            if (-not [IO.File]::Exists($path)) { throw 'Not a file' }
            Set-HRPrivateCredentialAcl $path
        }
        $record = [ordered]@{
            Version = 1
            Host = $target.Host
            Port = $target.Port
            User = $target.User
            Password = (ConvertFrom-SecureString -SecureString $Password -ErrorAction Stop)
            SudoPassword = (ConvertFrom-SecureString -SecureString $SudoPassword -ErrorAction Stop)
        }
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($record | ConvertTo-Json -Compress))
        $temporaryPath = Join-Path $directory ('.' + [Guid]::NewGuid().ToString('N') + '.tmp')
        $stream = New-Object IO.FileStream($temporaryPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Set-HRPrivateCredentialAcl $temporaryPath
        Assert-HRNoReparsePath $path
        # PowerShell 5.1 binds plain $null to an empty string for this overload;
        # NullString supplies the real null backup path required by File.Replace.
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temporaryPath, $path, [NullString]::Value) }
        else { [IO.File]::Move($temporaryPath, $path) }
        $temporaryPath = $null
        Set-HRPrivateCredentialAcl $path
    } catch { throw 'Could not save the local SSH password. Check this Windows profile and the local credential folder permissions.' }
    finally {
        if ($temporaryPath -and [IO.File]::Exists($temporaryPath)) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Read-HRLocalCredential {
    param($Settings, [string]$Root = '')
    $passwordValue = $null
    $sudoValue = $null
    try {
        $target = Get-HRCredentialTarget $Settings
        $path = Get-HRCredentialPath $Settings $Root
        if (-not [IO.File]::Exists($path)) { throw 'Missing credential' }
        $file = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($file.Length -gt 65536) { throw 'Oversized credential' }
        $record = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
        $expectedKeys = @('Host', 'Password', 'Port', 'SudoPassword', 'User', 'Version')
        if ($record -isnot [PSCustomObject] -or
            @(Compare-Object @($record.PSObject.Properties.Name | Sort-Object) $expectedKeys).Count -ne 0) { throw 'Invalid credential record' }
        if (($record.Version -isnot [int] -and $record.Version -isnot [long]) -or $record.Version -ne 1 -or
            $record.Host -isnot [string] -or $record.Host -cne $target.Host -or
            $record.User -isnot [string] -or $record.User -cne $target.User -or
            ($record.Port -isnot [int] -and $record.Port -isnot [long]) -or $record.Port -ne $target.Port -or
            $record.Password -isnot [string] -or $record.SudoPassword -isnot [string]) { throw 'Credential target does not match' }
        $passwordValue = ConvertTo-SecureString -String $record.Password -ErrorAction Stop
        $sudoValue = ConvertTo-SecureString -String $record.SudoPassword -ErrorAction Stop
        Assert-HRSingleLineSecret $passwordValue
        Assert-HRSingleLineSecret $sudoValue
        return [PSCustomObject]@{ Password = $passwordValue; SudoPassword = $sudoValue }
    } catch {
        if ($null -ne $passwordValue) { $passwordValue.Dispose() }
        if ($null -ne $sudoValue) { $sudoValue.Dispose() }
        throw 'The saved SSH password is unavailable or belongs to another target or Windows profile. Save it again on this computer.'
    }
}

function Remove-HRLocalCredential {
    param($Settings, [string]$Root = '')
    try {
        $path = Get-HRCredentialPath $Settings $Root
        if (Test-Path -LiteralPath $path) {
            if (-not [IO.File]::Exists($path)) { throw 'Not a file' }
            Remove-Item -LiteralPath $path -Force -ErrorAction Stop
        }
    } catch { throw 'Could not remove the saved SSH password. Check the local credential folder permissions.' }
}
