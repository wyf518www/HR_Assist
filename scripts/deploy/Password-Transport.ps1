# Windows OpenSSH uses SSH_ASKPASS for authentication. Only sudo needs stdin.
# These helpers never place a password in process arguments or environment.
function ConvertTo-HRProcessArgument([string]$Value) {
    if ($null -eq $Value) { $Value = '' }
    # CommandLineToArgvW / Microsoft CRT quoting, including trailing slashes.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-HRWithSecureInput {
    param([string]$Executable, [string[]]$Arguments, [Security.SecureString]$Password)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-HRProcessArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.CreateNoWindow = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    $pointer = [IntPtr]::Zero
    $started = $false
    $inputBytes = $null
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if (-not $plain -or $plain.IndexOfAny([char[]]"`r`n`0") -ge 0) {
            throw 'Saved password must be a nonempty single line. Save it again.'
        }
        if (-not $process.Start()) { throw 'Could not start SSH.' }
        $started = $true
        # Windows PowerShell 5.1 / .NET Framework has no StandardInputEncoding.
        # Write explicit UTF-8 without BOM and Linux LF directly to the pipe.
        $inputBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($plain + "`n")
        $process.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)
        $process.StandardInput.BaseStream.Flush()
        $process.StandardInput.Close()
        [Array]::Clear($inputBytes, 0, $inputBytes.Length)
        $inputBytes = $null
        $plain = $null
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $pointer = [IntPtr]::Zero
        $process.WaitForExit()
        return $process.ExitCode
    } catch {
        # A failed write may mean SSH rejected the host/password before stdin.
        # Never include the native exception or password in diagnostics.
        if ($started) {
            try { $process.StandardInput.Close() } catch {}
            try { $process.WaitForExit() } catch {}
        }
        throw 'SSH execution failed. Check the host key, saved password, SSH access and sudo permission.'
    } finally {
        $plain = $null
        if ($null -ne $inputBytes) { [Array]::Clear($inputBytes, 0, $inputBytes.Length) }
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        $process.Dispose()
    }
}
