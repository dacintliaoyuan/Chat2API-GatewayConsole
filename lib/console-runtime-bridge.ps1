# console-runtime-bridge.ps1 - PowerShell side of the console runtime JSON
# bridge. Gateway-runtime remains the single service authority; this file only
# adds the cross-process resource lock (same protocol as
# console.operations.ResourceFileLock in Python) and JSON-shaped snapshots.
#
# Functions only; invoke-console-runtime-json.ps1, invoke-gateway-service.ps1 and
# scripts/Open-GatewayConsole.ps1 dot-source this file and dispatch. Scriptblock
# parameters (Inspector/Starter/Stopper/HttpChecker/Sleeper/Opener) are TEST-ONLY
# seams, never passed by the production entries.

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

# ---------------------------------------------------------------------------
# #118: one readiness boundary for the desktop shortcut and the CLI.
#
# 2026-11-20: 8001 had no listener but an old managed console process was
# still alive. The shortcut treated that state as residual and aborted; the CLI
# start asked the runtime to clean it up and got AlreadyStopped back
# ("受管残留进程清理失败"). Both entries now go through this helper, which:
#   1. rechecks health briefly (a starting instance may become ready);
#   2. takes the same service:<key> resource lock the console uses;
#   3. rechecks under the lock (another opener may have started it);
#   4. stops ONLY a positively identified private residual instance, then starts
#      once; a listening-but-unhealthy port, a foreign listener or an
#      unconfirmable process is reported and every process is kept.
# The state taxonomy stays in gateway-runtime.ps1; this file only composes it
# with the cross-process lock.
# ---------------------------------------------------------------------------

function Wait-ConsoleServiceHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [int]$TimeoutSeconds = 5,
        [int]$PollMilliseconds = 500,
        [scriptblock]$HttpChecker,  # TEST-ONLY seam
        [scriptblock]$Sleeper       # TEST-ONLY seam
    )
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0, $TimeoutSeconds))
    $interval = [Math]::Max(1, $PollMilliseconds)
    while ($true) {
        if (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker) {
            return $true
        }
        if ([DateTime]::UtcNow -ge $deadline) { return $false }
        if ($null -ne $Sleeper) {
            & $Sleeper ($interval / 1000.0)
        } else {
            Start-Sleep -Milliseconds $interval
        }
    }
}

function Format-ConsoleServiceDetail {
    # Names the PIDs a refusal or a kept instance refers to, without ever
    # echoing a command line (the process table may carry a control token).
    param(
        [Parameter(Mandatory = $true)][string]$Prefix,
        [object[]]$ProcessIds = @(),
        [string]$Suffix = ''
    )
    $ids = @()
    foreach ($processId in @($ProcessIds)) {
        if ($null -ne $processId) { $ids += [int]$processId }
    }
    $text = $Prefix
    if ($ids.Count -gt 0) { $text += '（PID ' + ($ids -join ',') + '）' }
    if ($Suffix) { $text += '，' + $Suffix }
    return $text
}

function New-ConsoleServiceReadyResult {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Detail = '',
        [object[]]$ProcessIds = @(),
        [object[]]$TerminatedProcessIds = @()
    )
    $pids = @()
    foreach ($processId in @($ProcessIds)) {
        if ($null -ne $processId) { $pids += [int]$processId }
    }
    $terminated = @()
    foreach ($processId in @($TerminatedProcessIds)) {
        if ($null -ne $processId) { $terminated += [int]$processId }
    }
    return [pscustomobject]@{
        Key                   = $Key
        Status                = $Status
        Detail                = $Detail
        Reused                = ($Status -eq 'AlreadyRunning')
        Started               = ($Status -eq 'Started')
        Healthy               = ($Status -in @('AlreadyRunning', 'Started'))
        ProcessIds            = $pids
        TerminatedProcessIds  = $terminated
    }
}

function Invoke-ConsoleServiceReady {
    <#
    Ensure one local service is ready, recovering a verified residual instance.

    Status taxonomy:
      AlreadyRunning - reused an instance that was or became healthy.
      Started        - the verified residual instance (if any) was stopped and
                       exactly one start was launched.
      Degraded       - the port is listening but /health fails; processes kept.
      Conflict       - the port/process ownership is unknown; processes kept.
      Unconfirmed    - a name-matching process cannot be identified; processes kept.
      StartBlocked   - the scoped residual cleanup refused; processes kept.
      StartPending   - the start returned before the health wait completed.
      Failed         - the runtime returned an unexpected status.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$LockDir = '',
        [int]$ReadyWaitSeconds = 0,
        [int]$ReadyPollMilliseconds = 500,
        [double]$LockTimeoutSeconds = 0,
        # Caller-holds-lock contract: the console task layer acquires the
        # resource lock BEFORE invoking this helper. Standalone entries omit the
        # switch and the helper takes the lock itself.
        [switch]$CallerHoldsLock,
        [scriptblock]$HttpChecker,  # TEST-ONLY seam
        [scriptblock]$Sleeper       # TEST-ONLY seam
    )
    $descriptors = Get-GatewayServiceDescriptors
    if (-not $descriptors.Contains($Key)) {
        return (New-ConsoleServiceReadyResult -Key $Key -Status 'Failed' `
            -Detail ('未知服务：' + $Key))
    }
    $desc = $descriptors[$Key]
    if ($LockTimeoutSeconds -le 0) {
        $LockTimeoutSeconds = [Math]::Max(15, $ReadyWaitSeconds + 30)
    }

    # 1) Brief recheck BEFORE taking the lock: a managed instance that is
    #    already starting may become ready, in which case it is reused without
    #    terminating or launching anything.
    if (Wait-ConsoleServiceHealth -Key $Key -TimeoutSeconds $ReadyWaitSeconds `
            -PollMilliseconds $ReadyPollMilliseconds -HttpChecker $HttpChecker `
            -Sleeper $Sleeper) {
        return (New-ConsoleServiceReadyResult -Key $Key -Status 'AlreadyRunning' `
            -Detail '复用已就绪实例')
    }

    $lock = $null
    try {
        if (-not $CallerHoldsLock) {
            $lock = Enter-ConsoleResourceLock -Resource ('service:' + $Key) `
                -Owner 'console-entry' -LockDir $LockDir `
                -TimeoutSeconds $LockTimeoutSeconds
        }
        # 2) Recheck under the lock: a concurrent opener may have started the
        #    service while this one waited for the lock.
        if (Wait-ConsoleServiceHealth -Key $Key -TimeoutSeconds 0 `
                -HttpChecker $HttpChecker) {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'AlreadyRunning' `
                -Detail '复用已就绪实例')
        }

        $inspection = Get-GatewayServiceInspection -Key $Key
        if ($inspection.State -eq 'Running') {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'AlreadyRunning' `
                -Detail '复用已就绪实例' -ProcessIds @($inspection.ProcessIds))
        }
        if ($inspection.State -eq 'Degraded') {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'Degraded' `
                -Detail (Format-ConsoleServiceDetail -Prefix '端口已监听但健康检查失败' `
                    -ProcessIds @($inspection.ProcessIds) -Suffix '已保留进程') `
                -ProcessIds @($inspection.ProcessIds))
        }
        if ($inspection.State -eq 'Conflict') {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'Conflict' `
                -Detail (Format-ConsoleServiceDetail -Prefix ([string]$inspection.Detail) `
                    -ProcessIds @($inspection.ProcessIds) -Suffix '已保留进程') `
                -ProcessIds @($inspection.ProcessIds))
        }

        # 3) Residual or Stopped: the stop scope is the ONLY authority that
        #    may classify a name-matching process as this instance's. An
        #    unconfirmed candidate keeps every process; a foreign candidate is
        #    left alone and never blocks a fresh local start.
        try {
            $scope = Select-GatewayStopScope -Descriptor $desc `
                -GatewayRoot (Get-GatewayRoot)
        } catch {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'Unconfirmed' `
                -Detail '进程归属无法确认（进程清单查询失败），已保留进程')
        }
        if (@($scope.Unconfirmed).Count -gt 0) {
            $unconfirmedPids = @($scope.Unconfirmed |
                ForEach-Object { [int]$_.ProcessId })
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'Unconfirmed' `
                -Detail (Format-ConsoleServiceDetail -Prefix '进程归属无法确认' `
                    -ProcessIds $unconfirmedPids -Suffix '已保留进程') `
                -ProcessIds $unconfirmedPids)
        }

        $terminated = @()
        if (@($scope.Private).Count -gt 0) {
            $cleanup = Stop-GatewayService -Key $Key -KeepDesiredState -Sleeper $Sleeper
            if ($cleanup.PSObject.Properties['TerminatedProcessIds']) {
                $terminated = @($cleanup.TerminatedProcessIds)
            }
            if ($cleanup.Status -eq 'StopRefused') {
                $cleanupDetail = ''
                if ($cleanup.PSObject.Properties['Detail']) {
                    $cleanupDetail = [string]$cleanup.Detail
                }
                $detail = '受管残留进程清理失败'
                if ($cleanupDetail) { $detail += '：' + $cleanupDetail }
                return (New-ConsoleServiceReadyResult -Key $Key -Status 'StartBlocked' `
                    -Detail $detail -ProcessIds $terminated `
                    -TerminatedProcessIds $terminated)
            }
            if ($cleanup.Status -notin @('Stopped', 'AlreadyStopped')) {
                return (New-ConsoleServiceReadyResult -Key $Key -Status 'StartBlocked' `
                    -Detail ('受管残留进程清理失败：' + [string]$cleanup.Status) `
                    -TerminatedProcessIds $terminated)
            }
        }

        # 4) Start exactly once.
        $start = Start-GatewayService -Key $Key -HttpChecker $HttpChecker -Sleeper $Sleeper
        if ($start.Status -eq 'Started') {
            $processIds = @()
            try {
                $post = Get-GatewayServiceInspection -Key $Key
                if ($post.State -eq 'Running') {
                    $processIds = @($post.ProcessIds)
                }
            } catch { }
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'Started' `
                -Detail '已启动' -ProcessIds $processIds `
                -TerminatedProcessIds $terminated)
        }
        if ($start.Status -eq 'AlreadyRunning') {
            return (New-ConsoleServiceReadyResult -Key $Key -Status 'AlreadyRunning' `
                -Detail '复用已就绪实例' -TerminatedProcessIds $terminated)
        }
        $startDetail = ''
        if ($start.PSObject.Properties['Detail']) { $startDetail = [string]$start.Detail }
        if (-not $startDetail) { $startDetail = [string]$start.Status }
        $status = 'Failed'
        if ($start.Status -in @('StartPending', 'StartBlocked', 'Conflict')) {
            $status = [string]$start.Status
        }
        return (New-ConsoleServiceReadyResult -Key $Key -Status $status `
            -Detail $startDetail -TerminatedProcessIds $terminated)
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
}

function Invoke-GatewayConsoleOpen {
    # Desktop entry: ensure the local console is ready, then open its home page
    # with the one-time bootstrap code (if one was obtained).
    param(
        [string]$BaseUrl = 'http://127.0.0.1:8001',
        [string]$Key = 'usage',
        [string]$Label = '控制台（8001）',
        [string]$BootstrapCode = '',
        [string]$LockDir = '',
        [int]$ReadyWaitSeconds = 5,
        [int]$ReadyPollMilliseconds = 500,
        [scriptblock]$HttpChecker,  # TEST-ONLY seam
        [scriptblock]$Sleeper,      # TEST-ONLY seam
        [scriptblock]$Opener        # TEST-ONLY seam
    )
    $ready = Invoke-ConsoleServiceReady -Key $Key -LockDir $LockDir `
        -ReadyWaitSeconds $ReadyWaitSeconds `
        -ReadyPollMilliseconds $ReadyPollMilliseconds `
        -HttpChecker $HttpChecker -Sleeper $Sleeper
    if ($ready.Status -notin @('AlreadyRunning', 'Started')) {
        throw ('{0}：{1}' -f $Label, $ready.Detail)
    }
    $url = $BaseUrl
    if ($BootstrapCode) {
        $url = '{0}#s={1}' -f $url, $BootstrapCode.Trim()
    }
    if ($null -ne $Opener) {
        & $Opener $url
    } else {
        Start-Process $url
    }
    return [pscustomobject]@{
        Key                  = $Key
        Status               = 'Opened'
        Url                  = $url
        ReadyStatus          = $ready.Status
        Reused               = $ready.Reused
        Started              = $ready.Started
        Detail               = $ready.Detail
        ProcessIds           = @($ready.ProcessIds)
        TerminatedProcessIds = @($ready.TerminatedProcessIds)
    }
}

function Invoke-GatewayServiceCommand {
    # CLI entry: Start and Stop share the same readiness/cleanup boundary as
    # the desktop shortcut. Stop keeps its own lock acquisition; Start lets the
    # readiness helper take the lock so a concurrent entry cannot launch twice.
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Start', 'Stop')][string]$Action,
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$LockDir = '',
        [int]$ReadyWaitSeconds = 5,
        [int]$ReadyPollMilliseconds = 500,
        [scriptblock]$HttpChecker,  # TEST-ONLY seam
        [scriptblock]$Sleeper       # TEST-ONLY seam
    )
    $descriptors = Get-GatewayServiceDescriptors
    if (-not $descriptors.Contains($Key)) {
        return [pscustomobject]@{ ok = $false; error = 'invalid_key'; detail = $Key }
    }
    if (Test-GatewayServiceArchived -Key $Key) {
        return [pscustomobject]@{ ok = $false; error = 'archived'; detail = $Key }
    }
    if ($Action -eq 'Start') {
        $ready = Invoke-ConsoleServiceReady -Key $Key -LockDir $LockDir `
            -ReadyWaitSeconds $ReadyWaitSeconds `
            -ReadyPollMilliseconds $ReadyPollMilliseconds `
            -HttpChecker $HttpChecker -Sleeper $Sleeper
        if ($ready.Status -in @('AlreadyRunning', 'Started')) {
            return [pscustomobject]@{
                ok = $true; action = 'Start'; result = $ready
            }
        }
        return [pscustomobject]@{
            ok = $false; action = 'Start'; error = 'not_ready'
            detail = $ready.Detail; result = $ready
        }
    }
    $lock = Enter-ConsoleResourceLock -Resource ('service:' + $Key) `
        -Owner 'cli-invoker' -LockDir $LockDir
    try {
        $result = Stop-GatewayService -Key $Key -HttpChecker $HttpChecker `
            -Sleeper $Sleeper
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
    # Round 18 (2026-09-29): a refused stop terminated nothing, so the CLI
    # entry must not exit 0 as if the service had stopped.
    if ([string]$result.Status -eq 'StopRefused') {
        return [pscustomobject]@{
            ok = $false; action = 'Stop'; error = 'stop_scope_unconfirmed'
            detail = [string]$result.Detail; result = $result
        }
    }
    return [pscustomobject]@{ ok = $true; action = 'Stop'; result = $result }
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
