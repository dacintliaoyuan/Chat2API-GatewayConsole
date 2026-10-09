# console-runtime-bridge.ps1 - PowerShell side of the console runtime JSON
# bridge. Gateway-runtime remains the single service authority; this file only
# adds the cross-process resource lock (same protocol as
# console_operations.ResourceFileLock in Python) and JSON-shaped snapshots.
#
# Functions only; the CLI entry (invoke-console-runtime-json.ps1) dot-sources
# this file and dispatches. Scriptblock parameters (Inspector/Starter/Stopper)
# are TEST-ONLY seams, never passed by the production entry.

function Get-ConsoleLockDir {
    param([string]$LockDir = '')
    if ($LockDir -ne '') { return $LockDir }
    return (Join-Path (Get-GatewayRoot) 'data\console-locks')
}

function ConvertTo-ConsoleLockFileName {
    # Must stay identical to Python _sanitize_resource: keep [A-Za-z0-9_-],
    # replace everything else with '_', cap at 64 chars.
    param([Parameter(Mandatory = $true)][string]$Resource)
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Resource.ToCharArray()) {
        # PS 5.1 / .NET Framework has no char.IsAsciiLetterOrDigit; use a
        # regex so the sanitize rule matches the Python side exactly.
        if ("$ch" -cmatch '^[A-Za-z0-9_-]$') {
            [void]$sb.Append($ch)
        } else {
            [void]$sb.Append('_')
        }
    }
    $cleaned = $sb.ToString()
    if ($cleaned.Length -gt 64) { $cleaned = $cleaned.Substring(0, 64) }
    return $cleaned
}

function Enter-ConsoleResourceLock {
    # Exclusive-handle file lock: the open stream IS the lock (FileShare.None).
    # A dead owner releases automatically because the OS closes its handles.
    param(
        [Parameter(Mandatory = $true)][string]$Resource,
        [string]$Owner = 'console-bridge',
        [string]$LockDir = '',
        [double]$TimeoutSeconds = 15
    )
    $dir = Get-ConsoleLockDir -LockDir $LockDir
    $path = Join-Path $dir ((ConvertTo-ConsoleLockFileName -Resource $Resource) + '.lock')
    [void][System.IO.Directory]::CreateDirectory($dir)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $stream = $null
    while ($true) {
        try {
            $stream = [System.IO.File]::Open($path,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None)
            break
        } catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw (New-Object System.TimeoutException(
                    'console resource lock busy: ' + $Resource))
            }
            Start-Sleep -Milliseconds 50
        }
    }
    try {
        $info = @{ pid = $PID; owner = $Owner; resource = $Resource;
                   created_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
        $json = ConvertTo-Json $info -Compress
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $stream.SetLength(0)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } catch { }  # owner info is diagnostic; the stream is the lock
    return @{ Stream = $stream; Path = $path; Resource = $Resource }
}

function Exit-ConsoleResourceLock {
    param($Lock)
    if ($null -ne $Lock -and $null -ne $Lock.Stream) {
        try { $Lock.Stream.Dispose() } catch { }
    }
}

function Get-ConsoleRuntimeSnapshot {
    # JSON-shaped, authority-backed snapshot. State taxonomy comes from
    # Get-GatewayServiceInspection (Running/Degraded/Conflict/Residual/Stopped);
    # all timestamps are numeric epochs, never DateTime objects.
    param(
        [string]$Key = '',
        [scriptblock]$Inspector  # TEST-ONLY seam; production passes nothing
    )
    $descriptors = Get-GatewayServiceDescriptors
    $keys = @()
    if ($Key -ne '') {
        if (-not $descriptors.Contains($Key)) {
            return @{ ok = $false; error = 'invalid_key'; detail = $Key }
        }
        $keys = @($Key)
    } else {
        $keys = @($descriptors.Keys)
    }
    $services = @()
    foreach ($key in $keys) {
        $desc = $descriptors[$key]
        if ($null -ne $Inspector) {
            $insp = & $Inspector $key
        } else {
            $insp = Get-GatewayServiceInspection -Key $key
        }
        $pids = @()
        foreach ($p in @($insp.ProcessIds)) {
            # A null entry (e.g. member access on an empty collection) must
            # not surface as process id 0 for a stopped service.
            if ($null -ne $p) { $pids += [int]$p }
        }
        $services += [ordered]@{
            key          = [string]$insp.Key
            display_name = [string]$desc.DisplayName
            port         = [int]$desc.Port
            state        = [string]$insp.State
            healthy      = [bool]$insp.Healthy
            detail       = [string]$insp.Detail
            process_ids  = $pids
        }
    }
    return [ordered]@{
        ok       = $true
        services = $services
        meta     = [ordered]@{ source = 'gateway-runtime';
                               generated_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    }
}

function Invoke-ConsoleRuntimeStart {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$LockDir = '',
        # Caller-holds-lock contract: the console task layer acquires the
        # resource lock BEFORE invoking this wrapper, so the bridge must not
        # open the same lock file again (a second exclusive open in the child
        # process would wait on its own parent until timeout). Standalone
        # callers omit the switch and the wrapper takes the lock itself.
        [switch]$CallerHoldsLock,
        [scriptblock]$Starter  # TEST-ONLY seam
    )
    $descriptors = Get-GatewayServiceDescriptors
    if (-not $descriptors.Contains($Key)) {
        return @{ ok = $false; error = 'invalid_key'; detail = $Key }
    }
    $lock = $null
    try {
        if (-not $CallerHoldsLock) {
            $lock = Enter-ConsoleResourceLock -Resource ('service:' + $Key) `
                -Owner 'console-bridge-start' -LockDir $LockDir
        }
        if ($null -ne $Starter) {
            $result = & $Starter $Key
        } else {
            $result = Start-GatewayService -Key $Key
        }
        # Nest under "result" so the Python adapter can consume a uniform
        # shape: { ok, result: { key, status, action, detail? } }.
        $out = [ordered]@{ ok = $true
                           result = [ordered]@{ key = [string]$result.Key
                                                status = [string]$result.Status
                                                action = [string]$result.Action } }
        if ($result.PSObject.Properties['Detail'] -and $result.Detail) {
            $out.result['detail'] = [string]$result.Detail
        }
        return $out
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
}

function Invoke-ConsoleRuntimeStop {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$LockDir = '',
        # Same caller-holds-lock contract as Invoke-ConsoleRuntimeStart.
        [switch]$CallerHoldsLock,
        [scriptblock]$Stopper  # TEST-ONLY seam
    )
    $descriptors = Get-GatewayServiceDescriptors
    if (-not $descriptors.Contains($Key)) {
        return @{ ok = $false; error = 'invalid_key'; detail = $Key }
    }
    $lock = $null
    try {
        if (-not $CallerHoldsLock) {
            $lock = Enter-ConsoleResourceLock -Resource ('service:' + $Key) `
                -Owner 'console-bridge-stop' -LockDir $LockDir
        }
        if ($null -ne $Stopper) {
            $result = & $Stopper $Key
        } else {
            $result = Stop-GatewayService -Key $Key
        }
        if ([string]$result.Status -eq 'StopRefused') {
            # Round 18 (2026-09-29): a stop whose scope could not be confirmed
            # terminated nothing, so it must not be reported as a completed stop
            # (the console would otherwise show a stop that never happened).
            return @{ ok = $false; error = 'stop_scope_unconfirmed'
                      detail = [string]$result.Detail }
        }
        $out = [ordered]@{ ok = $true
                           result = [ordered]@{ key = [string]$result.Key
                                                status = [string]$result.Status
                                                action = [string]$result.Action } }
        if ($result.PSObject.Properties['Detail'] -and $result.Detail) {
            $out.result['detail'] = [string]$result.Detail
        }
        # Exit observability (round 18): the operation record names the PIDs the
        # stop actually terminated, so a later "the instance is gone" question
        # has an instance-bound answer instead of a status word alone.
        if ($result.PSObject.Properties['TerminatedProcessIds']) {
            $out.result['terminated_process_ids'] = @($result.TerminatedProcessIds |
                ForEach-Object { [int]$_ })
        }
        return $out
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
}

function Invoke-ConsoleRuntimeRestart {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$LockDir = '',
        # Same caller-holds-lock contract as Start/Stop — and restart NEEDS it:
        # stop and start run inside ONE lock acquisition so no other console
        # action can start the service between the two halves.
        [switch]$CallerHoldsLock,
        [scriptblock]$HttpChecker,  # TEST-ONLY seam (same queue contract as the runtime tests)
        [scriptblock]$Starter,      # TEST-ONLY seam
        [scriptblock]$Stopper,      # TEST-ONLY seam
        [scriptblock]$Sleeper       # TEST-ONLY seam
    )
    $descriptors = Get-GatewayServiceDescriptors
    if (-not $descriptors.Contains($Key)) {
        return @{ ok = $false; error = 'invalid_key'; detail = $Key }
    }
    $lock = $null
    try {
        if (-not $CallerHoldsLock) {
            $lock = Enter-ConsoleResourceLock -Resource ('service:' + $Key) `
                -Owner 'console-bridge-restart' -LockDir $LockDir
        }
        if ($null -ne $HttpChecker -or $null -ne $Starter -or $null -ne $Stopper) {
            $result = Restart-GatewayService -Key $Key -HttpChecker $HttpChecker `
                -Launcher $Starter -Stopper $Stopper -Sleeper $Sleeper
        } else {
            $result = Restart-GatewayService -Key $Key
        }
        if ([string]$result.Status -eq 'StopRefused') {
            # Same contract as stop: a restart whose stop half could not confirm
            # the private instance never launched anything (round 18).
            return @{ ok = $false; error = 'stop_scope_unconfirmed'
                      detail = [string]$result.Detail }
        }
        $out = [ordered]@{ ok = $true
                           result = [ordered]@{ key = [string]$result.Key
                                                status = [string]$result.Status
                                                action = [string]$result.Action } }
        if ($result.PSObject.Properties['Detail'] -and $result.Detail) {
            $out.result['detail'] = [string]$result.Detail
        }
        # Exit observability (round 18): a restart record carries the PIDs the
        # stop half terminated, same contract as stop.
        if ($result.PSObject.Properties['TerminatedProcessIds']) {
            $out.result['terminated_process_ids'] = @($result.TerminatedProcessIds |
                ForEach-Object { [int]$_ })
        }
        return $out
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
}
