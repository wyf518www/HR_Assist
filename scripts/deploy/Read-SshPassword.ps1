param([string]$Prompt = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# OpenSSH is the sole intended caller. Parent transports disable public-key and
# keyboard-interactive authentication and require an already trusted host key.
# Never answer host-key confirmation, private-key passphrase or MFA prompts.
$credential = $null
$buffer = [IntPtr]::Zero
$plain = $null
try {
    if ($env:HR_ASSIST_ASKPASS_ACTIVE -ne '1' -or
        [string]::IsNullOrWhiteSpace($env:HR_ASSIST_ASKPASS_TARGET) -or
        [string]::IsNullOrWhiteSpace($env:HR_ASSIST_ASKPASS_ROOT)) {
        throw 'Inactive credential request'
    }
    # Accept the two fixed OpenSSH password forms, rather than searching for
    # words that may also legitimately occur in a username or host name.
    $passwordPrompt = $Prompt -match '(?i)^password:\s*$' -or
        $Prompt -match "(?i)^[a-z0-9_][a-z0-9_.-]*@[a-z0-9][a-z0-9.:-]*'s password:\s*$"
    if ($args.Count -ne 0 -or $Prompt.Length -gt 1024 -or
        $Prompt -match '[\r\n\x00]' -or -not $passwordPrompt) {
        throw 'Unexpected credential prompt'
    }
    $target = ConvertFrom-Json -InputObject $env:HR_ASSIST_ASKPASS_TARGET -ErrorAction Stop
    . (Join-Path $PSScriptRoot 'Local-Credentials.ps1')
    $credential = Read-HRLocalCredential $target $env:HR_ASSIST_ASKPASS_ROOT
    $buffer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($credential.Password)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($buffer)
    if ([string]::IsNullOrEmpty($plain) -or $plain -match '[\r\n\x00]') { throw 'Invalid credential' }
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    [Console]::Out.WriteLine($plain)
} catch {
    # Raw exceptions, settings, prompts and DPAPI content must never be logged.
    [Console]::Error.WriteLine('Stored SSH password is unavailable or this prompt is not supported.')
    exit 1
} finally {
    $plain = $null
    if ($buffer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($buffer) }
    if ($null -ne $credential) {
        if ($null -ne $credential.Password) { $credential.Password.Dispose() }
        if ($null -ne $credential.SudoPassword) { $credential.SudoPassword.Dispose() }
    }
}
exit 0
