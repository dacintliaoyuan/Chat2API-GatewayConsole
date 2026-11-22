[CmdletBinding()]
param(
    [string]$ReadyUrl = 'http://127.0.0.1:8000/ready',
    [ValidateRange(1, 30)][int]$ProbeTimeoutSeconds = 3,
    [ValidateRange(1, 20)][int]$FailureThreshold = 2,
    [ValidateRange(1, 20)][int]$MaxStartsPerHour = 3,
    [string]$StatePath,
    [string]$LogPath,
    # The private instance's service log (exit records L1-L4, round 19).
    [string]$ServiceLogPath,
    [scriptblock]$Probe,
    [scriptblock]$ProcessQuery,
    [scriptblock]$StartService,
    [scriptblock]$BootIdProvider,
    [scriptblock]$NowProvider,
    [scriptblock]$DesiredStateProvider,
    [scriptblock]$ExitEvidenceProvider,
    [scriptblock]$SchedulingControl,
    [scriptblock]$HttpChecker,
    [scriptblock]$Launcher,
    [scriptblock]$Stopper,
    [scriptblock]$Sleeper,
    [switch]$SkipServiceLock,
    [switch]$SelfTest
)

# Private chat2api (8000) watch - round 19 (2026-09-30), Rev13-W (2026-10-04),
# maintainer-approved option A.
#
# Brings the PRIVATE instance back only when all of these hold:
#   1. the operator's desired state (lib\gateway-runtime.ps1 marker) is
#      "running" and was written during THIS boot - a console/CLI stop, or a
#      reboot, is never undone;
#   2. once the deployment window has opted in (the lifecycle control marker,
#      gateway-runtime.ps1 Test-GatewayWatchLifecycleControl), the marker's
#      start_outcome must say this boot actually had a health-confirmed start
#      ('succeeded') - a never-confirmed or failed start, or an old v1 marker
#      without the field, then authorizes nothing (fail closed). WITHOUT the
#      control marker this gate does not exist and condition 1 alone governs,
#      exactly as round 19 did: a disk that still holds a v1 marker keeps the
#      guard over a this-boot "running" instance instead of silently losing it
#      to a newer watch build;
#   3. /ready failed on FailureThreshold consecutive runs;
#   4. both the private chat2api worker and the ingress identities exist (the
#      round 18 stop-scope identity - any other chat2api.py never counts as
#      "alive") and the loopback endpoint is owned by the ingress;
#   5. the last private instance did not end in a graceful /admin/shutdown
#      that is newer than the desired state (a deliberate stop that bypassed
#      the runtime, e.g. a direct control call);
#   6. fewer than MaxStartsPerHour starts in the last hour of this boot.
# Readiness failures of a LIVE process (upstream 401, 5xx, network) never
# restart it: re-authentication is chat2api's own gate. Any query that cannot
# be confirmed means "do nothing". The start takes the same service:chat2api
# resource lock as the console and CLI, and re-checks 1, 2 and 4 inside it.
#
# Option A scheduling: while this boot authorizes recovery the watch task
# stays scheduled - probe failures, identity doubts and a exhausted budget
# never disarm it, so the sole recovery entry survives an abnormal exit. When
# nothing in this boot authorizes recovery (stopped / unknown / previous-boot
# intent, or - once opted in - a start outcome that failed without any
# confirmed start), the watch releases its own scheduling once, so a machine
# that never opened chat2api does not keep a once-a-minute idle task. The
# release re-checks the intent under the service lock, never runs while
# recovery is authorized, and is itself inert until the control marker exists:
# pre-opt-in the watch never mutates the real scheduled task (round-19
# compatibility - the outcome gate and the scheduling release ship dark
# together).

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:ServiceKey = 'chat2api'
$script:RuntimePath = Join-Path $PSScriptRoot 'gateway-runtime.ps1'
$script:BridgePath = Join-Path $PSScriptRoot 'console-runtime-bridge.ps1'
. $script:RuntimePath

$script:WatchRoot = [string]$env:SELFUSEAI_GATEWAY_WATCH_DIR
if (-not $script:WatchRoot) {
    $script:WatchRoot = Join-Path $env:LOCALAPPDATA 'SelfUseAI\ai-gateway\watch'
}
if (-not $StatePath) { $StatePath = Join-Path $script:WatchRoot 'chat2api-watch-state.json' }
if (-not $LogPath) { $LogPath = Join-Path $script:WatchRoot 'chat2api-watch.log' }
if (-not $ServiceLogPath) {
    $ServiceLogPath = Join-Path (Get-GatewayRoot) 'chat2api\chat2api.log'
}
$script:LogMaxBytes = 1MB

function Initialize-WatchDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $dir = Split-Path -Parent $Path
    if (-not $dir -or (Test-Path -LiteralPath $dir)) { return }
    New-Item -ItemType Directory -Path $dir -Force | Out-Null

    # Best effort: the decision file controls bounded recovery. Keep it private
    # to the current identity and SYSTEM instead of inheriting broad ACLs.
    try {
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls.exe $dir /inheritance:r /grant:r `
            "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
    }
    catch {
        # Logging must still work if ACL hardening is unavailable.
    }
}

function Write-WatchLog {
    param(
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    # Bounded: one previous generation is kept; the watch runs every minute
    # for months, so an unbounded log is not an option.
    try {
        if ((Test-Path -LiteralPath $LogPath) -and
                (Get-Item -LiteralPath $LogPath).Length -gt $script:LogMaxBytes) {
            Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
        }
    } catch { }
    $stamp = [DateTimeOffset]::UtcNow.ToString('o')
    $line = '{0} {1} {2}' -f $stamp, $Level.ToUpperInvariant(), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Get-WatchNow {
    if ($NowProvider) {
        $value = & $NowProvider
        if ($value -is [DateTimeOffset]) { return $value.ToUniversalTime() }
        if ($value -is [DateTime]) {
            return [DateTimeOffset]::new($value).ToUniversalTime()
        }
        return [DateTimeOffset]::Parse(
            [string]$value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    }
    return [DateTimeOffset]::UtcNow
}

function Get-BootIdentity {
    if ($BootIdProvider) {
        try { return [string](& $BootIdProvider) }
        catch { return $null }
    }
    return Get-GatewayBootIdentity
}

function ConvertFrom-WatchTimestamp {
    param([AllowNull()]$Value)
    if (-not $Value) { return $null }
    if ($Value -is [DateTimeOffset]) { return $Value.ToUniversalTime() }
    if ($Value -is [DateTime]) { return [DateTimeOffset]::new($Value).ToUniversalTime() }
    try {
        return [DateTimeOffset]::Parse(
            [string]$Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    } catch {
        return $null
    }
}

function New-WatchState {
    param([string]$BootId)
    return [pscustomobject]@{
        version = 2
        boot_id = $BootId
        consecutive_failures = 0
        restart_attempts_utc = @()
        idle_reason = ''
    }
}

function Read-WatchState {
    param([string]$BootId)
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return New-WatchState -BootId $BootId
    }
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if (-not $state.PSObject.Properties['version'] -or
                [int]$state.version -ne 2) {
            throw 'unsupported state version'
        }
        if ($BootId -and [string]$state.boot_id -ne $BootId) {
            Write-WatchLog 'INFO' (
                'boot_changed old={0} new={1}; resetting failure/recovery budget' -f
                [string]$state.boot_id, $BootId)
            return New-WatchState -BootId $BootId
        }
        return $state
    }
    catch {
        Write-WatchLog 'WARNING' (
            'state_read_failed type={0}; starting conservative empty state' -f
            $_.Exception.GetType().Name)
        return New-WatchState -BootId $BootId
    }
}

function Write-WatchState {
    param([Parameter(Mandatory = $true)]$State)
    $dir = Split-Path -Parent $StatePath
    if ($dir) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = '{0}.{1}.tmp' -f $StatePath, $PID
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $StatePath -Force
}

function Invoke-DefaultProbe {
    # #117: the probe separates "no HTTP response at all" (connection
    # refused, probe timeout, DNS/connect failure) from "an HTTP response that
    # is not 200". A PS7 transport exception has no Response property, so
    # reading it directly used to raise PropertyNotFoundStrict and end the watch
    # run with exit code 1 before any process or intent check.
    return Invoke-GatewayReadyProbe -Url $ReadyUrl -TimeoutSeconds $ProbeTimeoutSeconds
}

function Invoke-ReadyProbe {
    if ($Probe) { return & $Probe }
    return Invoke-DefaultProbe
}

function Test-LegacyWorkerListener {
    param($Descriptor, [int[]]$WorkerPids, [int[]]$IngressPids)

    $path = Get-GatewayWorkerPortFilePath -Descriptor $Descriptor
    if (!$path -or !(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $text = [IO.File]::ReadAllText($path)
    # The old binary publishes one decimal port and its ingress can only read
    # that format. Leave the file as written and verify its owner separately.
    if ($text -notmatch '\A[0-9]{1,5}(?:\r?\n)?\z') { return $false }
    $workerPort = [int]$text.Trim()
    if ($workerPort -lt 1 -or $workerPort -gt 65535 -or $workerPort -eq [int]$Descriptor.Port) {
        return $false
    }

    foreach ($target in @(
        @{ Port = $workerPort; Pids = $WorkerPids }
        @{ Port = [int]$Descriptor.Port; Pids = $IngressPids }
    )) {
        $snapshot = Get-GatewayListenerSnapshot -Port $target.Port
        if (@($snapshot.Unconfirmed).Count -gt 0) { throw 'legacy listener identity unconfirmed' }
        $listeners = @($snapshot.Overlapping)
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or
                [int]$listeners[0].OwningProcess -notin $target.Pids) {
            return $false
        }
    }
    return $true
}

function Invoke-DefaultProcessQuery {
    # Round 18 stop-scope identity, not the bare "chat2api.py" token: the
    # other chat2api.py instances must never
    # make the private instance look alive. A port 8000 listener that is not a
    # private instance is a conflict the watch must not start into.
    #
    # #133 A2: in component mode the watch confirms BOTH the worker and the
    # ingress identities; either missing means the pair is absent. The component
    # start fills only the missing one. A recorded worker port is only a hint:
    # with no worker identity process it is stale (the worker crashed and the
    # port may have been reused by an unrelated process), so it is ignored
    # instead of becoming a permanent "unconfirmed" that blocks every recovery.
    # The component start deletes the file before launching a fresh worker.
    try {
        $desc = (Get-GatewayServiceDescriptors)[$script:ServiceKey]
        $startPath = $null
        try { $startPath = Resolve-GatewayStartPath -Key $script:ServiceKey } catch { }
        if (Test-GatewayComponentMode -Descriptor $desc -StartPath $startPath) {
            # #133 A2: the watch must keep the whole entry alive, not only the
            # worker. A live worker with a dead ingress would leave 8000
            # unserved forever, so both identities are required before the pair
            # counts as verified. The component start fills only the missing one.
            $workerIdentity = $desc.RecoveryIdentity
            $ingressIdentity = Get-GatewayComponentIdentity -Descriptor $desc -Name 'ingress'
            if ($null -eq $ingressIdentity) {
                return [pscustomobject]@{
                    State = 'unconfirmed'
                    Pids = @()
                    Error = 'ingress_identity_missing'
                }
            }
            $workerScope = Select-GatewayStopScope -Descriptor $desc `
                -GatewayRoot (Get-GatewayRoot) -Identities @($workerIdentity)
            if (@($workerScope.Unconfirmed).Count -gt 0) {
                return [pscustomobject]@{
                    State = 'unconfirmed'
                    Pids = @($workerScope.Unconfirmed |
                        ForEach-Object { [int]$_.ProcessId })
                    Error = 'identity_unconfirmed'
                }
            }
            $ingressScope = Select-GatewayStopScope -Descriptor $desc `
                -GatewayRoot (Get-GatewayRoot) -Identities @($ingressIdentity)
            if (@($ingressScope.Unconfirmed).Count -gt 0) {
                return [pscustomobject]@{
                    State = 'unconfirmed'
                    Pids = @($ingressScope.Unconfirmed |
                        ForEach-Object { [int]$_.ProcessId })
                    Error = 'identity_unconfirmed'
                }
            }
            $workerPids = @($workerScope.Private |
                ForEach-Object { [int]$_.ProcessId })
            $ingressPids = @($ingressScope.Private |
                ForEach-Object { [int]$_.ProcessId })
            if ($workerPids.Count -eq 0 -or $ingressPids.Count -eq 0) {
                # The pair is incomplete: the component start fills the missing
                # component and leaves the live one alone. A recorded worker port
                # is only a hint and never blocks recovery.
                return [pscustomobject]@{ State = 'absent'; Pids = @(); Error = $null }
            }
            if ($null -eq (Get-GatewayWorkerPort -Descriptor $desc) -and
                    (Test-GatewayWorkerStartingExpired -Descriptor $desc `
                        -Processes $workerScope.Private) -and
                    !(Test-LegacyWorkerListener -Descriptor $desc `
                        -WorkerPids $workerPids -IngressPids $ingressPids)) {
                # A worker past the startup deadline without a usable record
                # or verified legacy listener can be replaced.
                return [pscustomobject]@{ State = 'absent'; Pids = @(); Error = $null }
            }
            $pids = @()
            foreach ($processId in @($workerPids) + @($ingressPids)) {
                if ([int]$processId -notin $pids) { $pids += [int]$processId }
            }
            return [pscustomobject]@{
                State = 'verified'; Pids = @($pids); Error = $null
            }
        }
        $scope = Select-GatewayStopScope -Descriptor $desc -GatewayRoot (Get-GatewayRoot)
        if (@($scope.Private).Count -gt 0) {
            return [pscustomobject]@{
                State = 'verified'
                Pids = @($scope.Private | ForEach-Object { [int]$_.ProcessId })
                Error = $null
            }
        }
        if (@($scope.Unconfirmed).Count -gt 0) {
            return [pscustomobject]@{
                State = 'unconfirmed'
                Pids = @($scope.Unconfirmed | ForEach-Object { [int]$_.ProcessId })
                Error = 'identity_unconfirmed'
            }
        }
        $port = [int]$desc.Port
        # A Tailscale or LAN listener on another address does not occupy
        # the gateway's 127.0.0.1 socket. Use the runtime's bind-scope rules.
        $listeners = Get-GatewayListenerSnapshot -Port $port
        if (@($listeners.Unconfirmed).Count -gt 0) {
            return [pscustomobject]@{
                State = 'unconfirmed'
                Pids = @()
                Error = 'port_{0}_listener_unconfirmed' -f $port
            }
        }
        if (@($listeners.Overlapping).Count -gt 0) {
            return [pscustomobject]@{
                State = 'unconfirmed'
                Pids = @($listeners.Overlapping | ForEach-Object { [int]$_.OwningProcess })
                Error = 'port_{0}_held_by_non_private_process' -f $port
            }
        }
        return [pscustomobject]@{ State = 'absent'; Pids = @(); Error = $null }
    }
    catch {
        return [pscustomobject]@{
            State = 'unconfirmed'
            Pids = @()
            Error = $_.Exception.GetType().Name
        }
    }
}

function Invoke-ProcessQuery {
    if ($ProcessQuery) { return & $ProcessQuery }
    return Invoke-DefaultProcessQuery
}

function Get-DesiredState {
    if ($DesiredStateProvider) { return & $DesiredStateProvider }
    return Read-GatewayDesiredState -Key $script:ServiceKey
}

function Get-DefaultExitEvidence {
    # Reads the tail of the private instance's service log for the last port
    # 8000 instance: its "listening" pid, and whether that pid later logged a
    # graceful "shutdown requested". $null = no evidence either way.
    if (-not (Test-Path -LiteralPath $ServiceLogPath)) { return $null }
    $text = $null
    $stream = $null
    try {
        $stream = [IO.File]::Open($ServiceLogPath, [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        $take = [Math]::Min($stream.Length, 1MB)
        [void]$stream.Seek(-$take, [IO.SeekOrigin]::End)
        $buffer = New-Object byte[] $take
        $read = 0
        while ($read -lt $take) {
            $n = $stream.Read($buffer, $read, $take - $read)
            if ($n -le 0) { break }
            $read += $n
        }
        $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
    } catch {
        return $null
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    # #133 A2: the worker binds an ephemeral port and never logs port=8000.
    # Read the WorkerPortFile value so a record from the worker still binds the
    # pid; the descriptor port stays accepted for the Python rollback. No
    # matching record means no evidence - never a fabricated pid or port.
    $ports = @()
    $desc = (Get-GatewayServiceDescriptors)[$script:ServiceKey]
    if ($null -ne $desc) {
        $startPath = $null
        try { $startPath = Resolve-GatewayStartPath -Key $script:ServiceKey } catch { }
        if (Test-GatewayComponentMode -Descriptor $desc -StartPath $startPath) {
            $workerPort = Get-GatewayWorkerPort -Descriptor $desc
            if ($null -ne $workerPort) { $ports += [int]$workerPort }
        }
        $ports += [int]$desc.Port
    }
    $ports = @($ports | Select-Object -Unique)
    if ($ports.Count -eq 0) { return $null }
    $portPattern = (@($ports | ForEach-Object { [string]$_ }) -join '|')
    $stampPattern = '^(?<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) INFO '
    $lastPid = $null
    $shutdownAt = $null
    foreach ($rawLine in ($text -split "`r?`n")) {
        # A byte-order mark (a file written by another tool) must not hide the
        # first record from the anchored patterns below.
        $line = $rawLine.TrimStart([char]0xFEFF)
        # R32-KEY-AUTH: the line may carry the instance's own
        # downstream_key=... after pid (chat2api.py:6419-6421); the suffix is
        # optional so records written by either deployment still match.
        $listen = [regex]::Match($line, $stampPattern +
            ('listening host=\S+ port=(?:{0}) pid=(?<pid>\d+)(?: downstream_key=\S+)?$' -f $portPattern))
        if ($listen.Success) {
            $lastPid = [int]$listen.Groups['pid'].Value
            $shutdownAt = $null
            continue
        }
        if ($null -eq $lastPid) { continue }
        # A graceful control-token stop is "exit begin ... reason=admin_shutdown".
        # The instance deployed first on 2026-09-30 wrote the same fact as
        # "shutdown requested ... by=" / "exit begin ... reason=normal"; both
        # forms are accepted so that instance's stop is read the same way.
        $stop = [regex]::Match($line, $stampPattern + ((
            '(?:exit begin port=(?:{0}) pid=(?<pid>\d+) reason=(?:admin_shutdown|normal)$|' +
            'shutdown requested port=(?:{0}) pid=(?<pid>\d+) by=)') -f $portPattern))
        if ($stop.Success -and [int]$stop.Groups['pid'].Value -eq $lastPid -and
                $null -eq $shutdownAt) {
            # The service log is written in local time (logging's asctime).
            $local = [DateTime]::ParseExact($stop.Groups['at'].Value,
                'yyyy-MM-dd HH:mm:ss,fff', [Globalization.CultureInfo]::InvariantCulture)
            $shutdownAt = [DateTimeOffset]::new($local).ToUniversalTime()
        }
    }
    if ($null -eq $lastPid) { return $null }
    return [pscustomobject]@{ Pid = $lastPid; GracefulShutdownUtc = $shutdownAt }
}

function Get-ExitEvidence {
    if ($ExitEvidenceProvider) { return & $ExitEvidenceProvider }
    return Get-DefaultExitEvidence
}

function Test-DesiredRunning {
    # Returns '' when the operator wants the instance running in this boot,
    # otherwise the reason not to act.
    param($Desired, [string]$BootId)
    if ($null -eq $Desired) { return 'desired_state_unknown' }
    if ([string]$Desired.state -ne 'running') {
        return 'desired_stopped source={0}' -f [string]$Desired.source
    }
    if (-not $BootId -or [string]$Desired.boot_id -ne $BootId) {
        return 'desired_state_from_previous_boot'
    }
    return ''
}

function Test-StartOutcomeRecoverable {
    # Rev13-W: auto-recovery is authorized only for a boot whose desired-state
    # marker records a health-confirmed start - but ONLY once the deployment
    # window opted the lifecycle in (the control marker). Before the opt-in
    # this gate does not exist and the round-19 rule governs alone: a this-boot
    # state=running intent authorizes recovery, and start_outcome is not a
    # criterion. That keeps the guard over an instance round 19 would have
    # recovered when the disk still holds an old v1 marker (no start_outcome
    # field) or a failed attempt; nothing here writes a success intent by
    # process existence.
    # With the control marker present: anything but 'succeeded' - the field
    # missing (an old v1-style marker), 'pending', 'failed', 'never' - returns
    # a reason and the watch must not start a never-confirmed instance from a
    # transient probe failure or a stale running intent (fail closed).
    #
    # Exit condition for the round-19 compatibility branch (AGENTS.md §4): it
    # stays only while the deployment window has not opted in (no lifecycle
    # control marker on this machine). Once the real scheduled task is
    # re-registered - Enable-GatewayWatchLifecycleControl in
    # register-chat2api-watch.ps1 - and a start through the runtime has
    # recorded a fresh start_outcome, no pre-opt-in machine remains and the
    # branch (this control-marker test) is deleted with its fixtures.
    param($Desired)
    if ($null -eq $Desired) { return 'start_outcome_unknown' }
    if (-not (Test-GatewayWatchLifecycleControl)) { return '' }
    $prop = $Desired.PSObject.Properties['start_outcome']
    if ($null -eq $prop -or $null -eq $prop.Value) { return 'start_outcome_unknown' }
    $outcome = [string]$prop.Value
    if ($outcome -eq 'succeeded') { return '' }
    return 'start_outcome_{0}' -f $outcome
}

function Invoke-GatewayStart {
    if ($StartService) { return & $StartService }

    # Same runtime primitive as the console and CLI; the start records
    # "running" with source=watch, and chat2api's startup line says so too.
    # #133 A2: a start (not a restart) is what fills a missing worker while
    # the ingress stays up; a restart would tear down the healthy ingress.
    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = 'watch'
    Enable-GatewayDesiredState
    $startArgs = @{ Key = $script:ServiceKey }
    if ($HttpChecker) { $startArgs.HttpChecker = $HttpChecker }
    if ($Launcher) { $startArgs.Launcher = $Launcher }
    if ($Sleeper) { $startArgs.Sleeper = $Sleeper }
    return Start-GatewayService @startArgs
}

function Enter-ServiceLock {
    if ($SkipServiceLock) { return @{ Stream = $null } }
    . $script:BridgePath
    return Enter-ConsoleResourceLock -Resource ('service:' + $script:ServiceKey) `
        -Owner 'chat2api-watch' -TimeoutSeconds 3
}

function Exit-ServiceLock {
    param($Lock)
    if ($null -ne $Lock -and $null -ne $Lock.Stream) {
        try { $Lock.Stream.Dispose() } catch { }
    }
}

function Set-WatchScheduling {
    # The watch's ONLY scheduling action: release its own task when this boot
    # no longer authorizes recovery. The runtime's start/stop paths own the
    # arming and the deliberate-stop release; the watch never disarms while
    # recovery is authorized, so the sole recovery entry survives probe
    # failures, identity doubts and budget exhaustion.
    param([Parameter(Mandatory = $true)][bool]$Enabled)
    if ($SchedulingControl) { return & $SchedulingControl $Enabled }
    return Set-GatewayWatchScheduling -Key $script:ServiceKey -Enabled $Enabled
}

function Test-WatchSchedulingReleasable {
    # $true only when the idle reason means NOTHING in this boot authorizes
    # recovery, so releasing the scheduling cannot remove a live recovery
    # entry. 'start_outcome_pending' (a start may be in flight) and
    # 'start_outcome_unknown' (indeterminate - fail closed) keep the task.
    param([Parameter(Mandatory = $true)][string]$IdleReason)
    if ($IdleReason -eq 'start_outcome_pending') { return $false }
    if ($IdleReason -eq 'start_outcome_unknown') { return $false }
    return $true
}

function Invoke-WatchSchedulingRelease {
    # Releases the watch's own scheduling when this boot authorizes no
    # recovery, so a machine that never opened chat2api does not keep a
    # once-a-minute idle task (option A; never mode B "always scheduled,
    # just idle"). Returns 'released', 'kept', 'deferred' (service lock busy,
    # retry next run) or 'intent_recovered' (a runtime start raced this run
    # and the boot just became recoverable again - keep going actively).
    param([Parameter(Mandatory = $true)][string]$IdleReason)
    if (-not (Test-GatewayWatchLifecycleControl)) { return 'control_off' }
    if (-not (Test-WatchSchedulingReleasable -IdleReason $IdleReason)) { return 'kept' }
    $lock = $null
    try {
        $lock = Enter-ServiceLock
    } catch {
        Write-WatchLog 'INFO' 'watch_scheduling_release_deferred reason=service_lock_busy'
        return 'deferred'
    }
    try {
        # Re-check under the lock: a runtime start that raced this run has
        # either finished (intent running with a confirmed start) or not
        # happened; only then may the watch release its own entry.
        $desired = Get-DesiredState
        $bootId = Get-BootIdentity
        $stillIdle = Test-DesiredRunning -Desired $desired -BootId $bootId
        if (-not $stillIdle) {
            $stillIdle = Test-StartOutcomeRecoverable -Desired $desired
        }
        if (-not $stillIdle) { return 'intent_recovered' }
        $report = Set-WatchScheduling -Enabled $false
        $actionText = ''
        $detailText = ''
        if ($null -ne $report) {
            $actionProp = $report.PSObject.Properties['Action']
            if ($null -ne $actionProp) { $actionText = [string]$actionProp.Value }
            $detailProp = $report.PSObject.Properties['Detail']
            if ($null -ne $detailProp) { $detailText = [string]$detailProp.Value }
        }
        if ($actionText -eq 'Disable') {
            # Log once per real transition; an already-disabled or absent task
            # stays quiet (the desired scheduler state is already reached).
            Write-WatchLog 'INFO' (
                'watch_scheduling_released reason={0}' -f $IdleReason)
        } elseif ($null -ne $report -and
                $report.PSObject.Properties['SchedulingEnabled'] -and
                $null -eq $report.SchedulingEnabled -and $detailText) {
            # An indeterminate toggle failure deserves repeated visibility.
            Write-WatchLog 'WARNING' (
                'watch_scheduling_release issue={0} reason={1}' -f $detailText, $IdleReason)
        }
        return 'released'
    } finally {
        Exit-ServiceLock -Lock $lock
    }
}

function Get-RecentRestartAttempts {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][DateTimeOffset]$Now
    )
    $cutoff = $Now.AddHours(-1)
    $recent = @()
    foreach ($raw in @($State.restart_attempts_utc)) {
        if (-not $raw) { continue }
        $parsed = ConvertFrom-WatchTimestamp -Value $raw
        if ($null -eq $parsed) {
            Write-WatchLog 'WARNING' 'discarded malformed restart timestamp from state'
            continue
        }
        if ($parsed -ge $cutoff -and $parsed -le $Now.AddMinutes(1)) {
            $recent += $parsed.ToString('o')
        }
    }
    return @($recent)
}

function Set-IdleReason {
    # Quiet idling: log a skip reason only when it changes, so a deliberately
    # stopped service does not add a line every minute.
    param($State, [string]$Reason)
    if ([string]$State.idle_reason -ne $Reason) {
        if ($Reason) {
            Write-WatchLog 'INFO' ('idle reason={0}' -f $Reason)
        } else {
            Write-WatchLog 'INFO' ('watching reason_cleared previous={0}' -f [string]$State.idle_reason)
        }
        $State.idle_reason = $Reason
    }
}

function Invoke-Chat2ApiWatch {
    param([switch]$SkipMutex)

    $mutex = $null
    $locked = $false
    try {
        if (-not $SkipMutex) {
            $mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList `
                $false, 'Local\SelfUseAI.Chat2API.Watch'
            $locked = $mutex.WaitOne(0)
            if (-not $locked) {
                Write-WatchLog 'INFO' 'skip reason=another_watch_instance_running'
                return 0
            }
        }

        $now = Get-WatchNow
        $bootId = Get-BootIdentity
        $state = Read-WatchState -BootId $bootId
        if ($bootId -and [string]$state.boot_id -ne $bootId) {
            $state.boot_id = $bootId
        }

        # Operator intent first: a stopped or previous-boot intent means there
        # is nothing to watch, so do not even probe. Rev13-W adds the start
        # outcome: a boot without a health-confirmed start authorizes nothing.
        $desired = Get-DesiredState
        $idle = Test-DesiredRunning -Desired $desired -BootId $bootId
        if (-not $idle) { $idle = Test-StartOutcomeRecoverable -Desired $desired }
        if ($idle) {
            $release = Invoke-WatchSchedulingRelease -IdleReason $idle
            if ($release -eq 'intent_recovered') {
                # A runtime start raced this run and just made the boot
                # recoverable again; fall through and run actively.
                $idle = ''
            } else {
                $state.consecutive_failures = 0
                Set-IdleReason -State $state -Reason $idle
                Write-WatchState -State $state
                return 0
            }
        }
        Set-IdleReason -State $state -Reason ''

        $probeResult = Invoke-ReadyProbe
        if ($probeResult.Ready) {
            $oldFailures = [int]$state.consecutive_failures
            $state.consecutive_failures = 0
            $state.restart_attempts_utc = @(
                Get-RecentRestartAttempts -State $state -Now $now)
            Write-WatchState -State $state
            if ($oldFailures -gt 0) {
                Write-WatchLog 'INFO' (
                    'probe_recovered previous_failures={0} http_status={1}' -f
                    $oldFailures, $probeResult.HttpStatus)
            }
            return 0
        }

        $nextFailures = [int]$state.consecutive_failures + 1
        $state.consecutive_failures = [Math]::Min($nextFailures, $FailureThreshold)
        $state.restart_attempts_utc = @(
            Get-RecentRestartAttempts -State $state -Now $now)
        Write-WatchState -State $state

        $httpStatusText = if ($null -eq $probeResult.HttpStatus) {
            '-'
        } else {
            [string]$probeResult.HttpStatus
        }
        # #117: the exception type is a class name (never the message), so the
        # no-response reason stays diagnosable without leaking a URL, path or
        # proxy setting. Custom probes may omit the field.
        $errorTypeText = '-'
        $errorTypeProperty = $probeResult.PSObject.Properties['ErrorType']
        if ($null -ne $errorTypeProperty -and $errorTypeProperty.Value) {
            $errorTypeText = [string]$errorTypeProperty.Value
        }
        Write-WatchLog 'WARNING' (
            'probe_failed count={0}/{1} responded={2} http_status={3} reason={4} error_type={5}' -f
            $state.consecutive_failures, $FailureThreshold,
            [bool]$probeResult.Responded, $httpStatusText,
            [string]$probeResult.Reason, $errorTypeText)

        if ([int]$state.consecutive_failures -lt $FailureThreshold) {
            return 0
        }

        $processResult = Invoke-ProcessQuery
        switch ([string]$processResult.State) {
            'verified' {
                Write-WatchLog 'WARNING' (
                    'no_restart reason=process_alive pids={0}; readiness failure is not a restart trigger' -f
                    ((@($processResult.Pids) -join ',')))
                return 0
            }
            'unconfirmed' {
                Write-WatchLog 'WARNING' (
                    'no_restart reason=process_state_unconfirmed error={0}' -f
                    [string]$processResult.Error)
                return 0
            }
            'absent' { }
            default {
                Write-WatchLog 'WARNING' (
                    'no_restart reason=invalid_process_query_state state={0}' -f
                    [string]$processResult.State)
                return 0
            }
        }

        # PID absence is the only automatic-start trigger. A missing boot id
        # means the per-boot recovery budget cannot be enforced safely.
        if (-not $bootId) {
            Write-WatchLog 'WARNING' (
                'no_restart reason=boot_identity_unconfirmed pid_state=absent')
            return 0
        }

        # A graceful shutdown newer than the operator's intent was deliberate
        # (a stop that bypassed the runtime); a vanished instance with no
        # shutdown record is the case this watch exists for.
        $desired = Get-DesiredState
        $desiredAt = ConvertFrom-WatchTimestamp -Value $desired.at_utc
        $evidence = Get-ExitEvidence
        if ($null -ne $evidence -and $null -ne $evidence.GracefulShutdownUtc -and
                ($null -eq $desiredAt -or $evidence.GracefulShutdownUtc -gt $desiredAt)) {
            Write-WatchLog 'WARNING' (
                'no_restart reason=graceful_shutdown_outside_runtime pid={0} shutdown_utc={1}' -f
                $evidence.Pid, $evidence.GracefulShutdownUtc.ToString('o'))
            return 0
        }

        $recent = @(Get-RecentRestartAttempts -State $state -Now $now)
        if ($recent.Count -ge $MaxStartsPerHour) {
            $state.restart_attempts_utc = $recent
            Write-WatchState -State $state
            Write-WatchLog 'WARNING' (
                'no_restart reason=hourly_start_budget_exhausted attempts={0}/{1} pid_state=absent' -f
                $recent.Count, $MaxStartsPerHour)
            return 0
        }

        $lock = $null
        try {
            $lock = Enter-ServiceLock
        } catch {
            Write-WatchLog 'INFO' (
                'no_restart reason=service_lock_busy type={0}; retry next run' -f
                $_.Exception.GetType().Name)
            return 0
        }
        try {
            # Re-check inside the lock: a console/CLI stop that raced this run
            # has now either finished (marker says stopped) or not started.
            # Rev13-W: the start outcome is re-checked too - a stop wins over
            # the recovery, and an unconfirmed start never authorizes one.
            $recheckDesired = Get-DesiredState
            $recheck = Test-DesiredRunning -Desired $recheckDesired -BootId $bootId
            if (-not $recheck) {
                $recheck = Test-StartOutcomeRecoverable -Desired $recheckDesired
            }
            if ($recheck) {
                Write-WatchLog 'INFO' ('no_restart reason={0} (rechecked under lock)' -f $recheck)
                return 0
            }
            $processAgain = Invoke-ProcessQuery
            if ([string]$processAgain.State -ne 'absent') {
                Write-WatchLog 'INFO' (
                    'no_restart reason=process_state_changed_under_lock state={0}' -f
                    [string]$processAgain.State)
                return 0
            }

            # Count the recovery attempt before invoking it. A crash in the
            # launcher must not erase budget consumption and create a loop.
            $recent += $now.ToString('o')
            $state.restart_attempts_utc = @($recent)
            $state.consecutive_failures = 0
            Write-WatchState -State $state
            $evidenceText = if ($null -eq $evidence) { 'none' }
                elseif ($null -eq $evidence.GracefulShutdownUtc) {
                    'last_pid={0} graceful_shutdown=none' -f $evidence.Pid }
                else {
                    'last_pid={0} graceful_shutdown={1}_older_than_intent' -f
                        $evidence.Pid, $evidence.GracefulShutdownUtc.ToString('o') }
            Write-WatchLog 'WARNING' (
                'auto_start attempt={0}/{1} reason=pid_absent exit_evidence={2}' -f
                $recent.Count, $MaxStartsPerHour, $evidenceText)

            try {
                $result = Invoke-GatewayStart
                $status = ''
                if ($null -ne $result -and $result.PSObject.Properties['Status']) {
                    $status = [string]$result.Status
                }
                $statusText = if ($status) { $status } else { 'unknown' }
                Write-WatchLog 'WARNING' (
                    'auto_start_result status={0}' -f $statusText)
                if ($status -and $status -notin @('Restarted', 'Started', 'AlreadyRunning')) {
                    return 1
                }
                return 0
            }
            catch {
                Write-WatchLog 'ERROR' (
                    'auto_start_failed type={0}' -f $_.Exception.GetType().Name)
                return 1
            }
        }
        finally {
            Exit-ServiceLock -Lock $lock
        }
    }
    finally {
        if ($locked -and $null -ne $mutex) {
            try { $mutex.ReleaseMutex() } catch { }
        }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

function Invoke-WatchSelfTest {
    $testRoot = Join-Path $env:TEMP (
        'SelfUseAI-chat2api-watch-selftest-{0}' -f [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    $script:StatePath = Join-Path $testRoot 'state.json'
    $script:LogPath = Join-Path $testRoot 'watch.log'
    $script:SkipServiceLock = $true
    $savedWatchDir = $env:SELFUSEAI_GATEWAY_WATCH_DIR
    $env:SELFUSEAI_GATEWAY_WATCH_DIR = $testRoot
    # The control marker decides the recovery semantics below; the self-test must
    # only ever touch its own copy. The guard runs BEFORE the first enable - a
    # guard placed after the action it protects would create (or delete) the real
    # deployment marker first and only then fail.
    $selfTestControlPath = Get-GatewayWatchControlPath
    if (-not $selfTestControlPath.StartsWith(
            $testRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw ('self-test failed: the control marker is outside the test directory ({0})' -f
            $selfTestControlPath)
    }

    $script:selfTestNowIndex = 0
    $script:selfTestBase = [DateTimeOffset]::Parse('2026-09-21T12:00:00Z')
    $script:NowProvider = {
        $value = $script:selfTestBase.AddMinutes($script:selfTestNowIndex)
        $script:selfTestNowIndex += 1
        return $value
    }
    $script:BootIdProvider = { 'selftest-boot' }
    $script:selfTestProbeReady = $false
    # #117: the probe stub can return the real probe's no-response result
    # (a bound-but-unlistening loopback port) or a non-200 HTTP response, so
    # the decision table can prove both branches end to end.
    $script:selfTestProbeMode = 'failure'
    $script:selfTestClosedProbeUrl = $null
    $script:selfTestProbeResults = New-Object System.Collections.ArrayList
    $script:Probe = {
        $result = $null
        if ($script:selfTestProbeMode -eq 'default') {
            # The production probe entry point, against the closed port set in
            # $script:ReadyUrl (the #117 strict-mode regression).
            $result = Invoke-DefaultProbe
        } elseif ($script:selfTestProbeMode -eq 'closed') {
            $result = Invoke-GatewayReadyProbe -Url $script:selfTestClosedProbeUrl -TimeoutSeconds 5
        } elseif ($script:selfTestProbeMode -eq 'http_not_ready') {
            $result = [pscustomobject]@{
                Ready = $false; Responded = $true; HttpStatus = 503
                Reason = 'http_not_ready'; ErrorType = 'HttpResponseException' }
        } elseif ($script:selfTestProbeReady) {
            $result = [pscustomobject]@{
                Ready = $true; Responded = $true; HttpStatus = 200
                Reason = 'ready'; ErrorType = $null }
        } else {
            $result = [pscustomobject]@{
                Ready = $false; Responded = $false; HttpStatus = $null
                Reason = 'SelfTestProbeFailure'; ErrorType = $null }
        }
        [void]$script:selfTestProbeResults.Add($result)
        return $result
    }
    & {
        function Select-GatewayStopScope {
            param($Descriptor, $GatewayRoot, [object[]]$Identities = @())
            $component = ''
            if (@($Identities).Count -gt 0) {
                $componentProp = $Identities[0].PSObject.Properties['Component']
                if ($null -ne $componentProp) {
                    $component = [string]$componentProp.Value
                }
            }
            if ($component -eq 'ingress') {
                return [pscustomobject]@{
                    Private = @($script:selfTestIngressScope); Unconfirmed = @()
                }
            }
            return [pscustomobject]@{
                Private = @($script:selfTestWorkerScope); Unconfirmed = @()
            }
        }

        # #133 A2: the split worker publishes its real port in WorkerPortFile;
        # the self-test fixes that port so the stale-record and listener-address
        # decision table below exercises the same port source as production
        # without reading a real data/chat2api-worker.port.
        function Get-GatewayWorkerPort {
            param($Descriptor)
            return 5678
        }

        function Get-NetTCPConnection {
            [CmdletBinding()]
            param([int]$LocalPort, [string]$State)
            [pscustomobject]@{ LocalAddress = $fixtureAddress; OwningProcess = 5678 }
        }

        $descriptor = (Get-GatewayServiceDescriptors)['chat2api']
        $script:selfTestWorkerScope = @()
        $script:selfTestIngressScope = @()
        foreach ($fixture in @(
            @{ Address = '100.64.0.1'; Expected = 'absent' }
            @{ Address = 'fd00::1'; Expected = 'absent' }
            @{ Address = '127.0.0.1'; Expected = 'absent' }
            @{ Address = '0.0.0.0'; Expected = 'absent' }
            @{ Address = '::'; Expected = 'absent' }
            @{ Address = '::ffff:127.0.0.1'; Expected = 'absent' }
            @{ Address = 'unreadable'; Expected = 'absent' }
        )) {
            # No worker or ingress identity process: the recorded port is
            # stale even when an unrelated process (loopback, wildcard or
            # tailnet) now listens on it. It must never block recovery.
            $fixtureAddress = $fixture.Address
            $result = Invoke-DefaultProcessQuery
            if ($result.State -ne $fixture.Expected) {
                throw ('self-test failed: stale worker port with listener {0} expected {1}, got {2}' -f
                    $fixtureAddress, $fixture.Expected, $result.State)
            }
            if ($null -ne (Get-GatewayWorkerRuntimePort -Descriptor $descriptor)) {
                throw ('self-test failed: stale worker port with listener {0} was treated as the worker port' -f
                    $fixtureAddress)
            }
        }

        # A live worker with a dead ingress is not a verified pair: the watch
        # must start the service so the component start fills the ingress only.
        $script:selfTestWorkerScope = @([pscustomobject]@{
            ProcessId = 4242; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode worker --port 0'
        })
        $fixtureAddress = '100.64.0.1'
        $workerOnly = Invoke-DefaultProcessQuery
        if ($workerOnly.State -ne 'absent') {
            throw ('self-test failed: a live worker without an ingress was not treated as incomplete (state={0})' -f
                $workerOnly.State)
        }
        if ($null -ne (Get-GatewayWorkerRuntimePort -Descriptor $descriptor)) {
            throw 'self-test failed: a tailnet-only listener was treated as the worker runtime port'
        }

        # Ingress only is incomplete too: the worker must be started.
        $script:selfTestWorkerScope = @()
        $script:selfTestIngressScope = @([pscustomobject]@{
            ProcessId = 4243; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode ingress --port 8000'
        })
        $ingressOnly = Invoke-DefaultProcessQuery
        if ($ingressOnly.State -ne 'absent') {
            throw ('self-test failed: an ingress without a worker was not treated as incomplete (state={0})' -f
                $ingressOnly.State)
        }

        # Both identities alive: the pair is verified and both PIDs are named.
        $script:selfTestWorkerScope = @([pscustomobject]@{
            ProcessId = 4242; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode worker --port 0'
        })
        $both = Invoke-DefaultProcessQuery
        if ($both.State -ne 'verified' -or
                (@($both.Pids) -join ',') -ne '4242,4243') {
            throw ('self-test failed: a live worker and ingress were not verified together (state={0}, pids={1})' -f
                $both.State, (@($both.Pids) -join ','))
        }
        $script:selfTestWorkerScope = @()
        $script:selfTestIngressScope = @()
    }

    # P2: a worker process alive past StartTimeoutSeconds without a port file
    # is stuck, not starting: the watch returns absent so the component start
    # can replace it.
    & {
        $legacyPortFile = Join-Path $testRoot 'legacy-worker.port'
        function Get-GatewayWorkerPortFilePath {
            param($Descriptor)
            return $legacyPortFile
        }
        function Get-NetTCPConnection {
            [CmdletBinding()]
            param([int]$LocalPort, [string]$State)
            if ($LocalPort -eq 8000) {
                [pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 4243 }
            } elseif ($LocalPort -eq 5678 -and $null -ne $fixture.Owner) {
                [pscustomobject]@{ LocalAddress = $fixture.Address; OwningProcess = $fixture.Owner }
            }
        }
        function Get-GatewayWorkerPort {
            param($Descriptor)
            return $null
        }
        function Select-GatewayStopScope {
            param($Descriptor, $GatewayRoot, [object[]]$Identities = @())
            $component = ''
            if (@($Identities).Count -gt 0) {
                $componentProp = $Identities[0].PSObject.Properties['Component']
                if ($null -ne $componentProp) {
                    $component = [string]$componentProp.Value
                }
            }
            if ($component -eq 'ingress') {
                return [pscustomobject]@{
                    Private = @($script:selfTestIngressScope); Unconfirmed = @()
                }
            }
            return [pscustomobject]@{
                Private = @($script:selfTestWorkerScope); Unconfirmed = @()
            }
        }
        $script:selfTestWorkerScope = @([pscustomobject]@{
            ProcessId = 4242; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode worker --port 0'
            CreationDate = [datetime]::UtcNow.AddSeconds(-30)
        })
        $script:selfTestIngressScope = @([pscustomobject]@{
            ProcessId = 4243; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode ingress --port 8000'
            CreationDate = [datetime]::UtcNow
        })
        $expired = Invoke-DefaultProcessQuery
        if ($expired.State -ne 'absent') {
            throw ('self-test failed: a worker past the start budget was not treated as absent (state={0})' -f
                $expired.State)
        }
        foreach ($fixture in @(
            @{ Owner = 4242; Address = '127.0.0.1'; Content = "5678`n"; Expected = 'verified' }
            @{ Owner = 9999; Address = '127.0.0.1'; Content = "5678`n"; Expected = 'absent' }
            @{ Owner = 4242; Address = '100.64.0.1'; Content = "5678`n"; Expected = 'absent' }
            @{ Owner = $null; Address = '127.0.0.1'; Content = "5678`n"; Expected = 'absent' }
            @{ Owner = 4242; Address = '127.0.0.1'; Content = "5678`ninvalid`n"; Expected = 'absent' }
        )) {
            [IO.File]::WriteAllText($legacyPortFile, $fixture.Content)
            $legacy = Invoke-DefaultProcessQuery
            if ($legacy.State -ne $fixture.Expected) {
                throw ('self-test failed: legacy worker port expected {0}, got {1} (owner={2}, address={3})' -f
                    $fixture.Expected, $legacy.State, $fixture.Owner, $fixture.Address)
            }
        }
        $script:selfTestWorkerScope = @([pscustomobject]@{
            ProcessId = 4242; Name = 'um-gateway-candidate.exe'
            CommandLine = 'um-gateway-candidate.exe --mode worker --port 0'
            CreationDate = [datetime]::UtcNow
        })
        $starting = Invoke-DefaultProcessQuery
        if ($starting.State -ne 'verified' -or
                (@($starting.Pids) -join ',') -ne '4242,4243') {
            throw ('self-test failed: a worker within the start budget was not verified (state={0}, pids={1})' -f
                $starting.State, (@($starting.Pids) -join ','))
        }
        $script:selfTestWorkerScope = @()
        $script:selfTestIngressScope = @()
    }

    # J7: Invoke-GatewayStart must not forward the watch-only -Stopper seam to
    # Start-GatewayService (it has no such parameter). A local override proves
    # the binding without launching or stopping anything.
    & {
        function Start-GatewayService {
            param($Key, $HttpChecker, $Launcher, $Sleeper)
            return [pscustomobject]@{ Status = 'Started' }
        }
        $savedStartService = $script:StartService
        $savedStopper = $script:Stopper
        $savedLauncher = $script:Launcher
        $savedHttpChecker = $script:HttpChecker
        $savedSleeper = $script:Sleeper
        $savedSource = $env:SELFUSEAI_GATEWAY_ACTION_SOURCE
        $savedDesiredEnabled = $script:GatewayDesiredStateEnabled
        try {
            $script:StartService = $null
            $script:Stopper = { param($key, $desc) }
            $script:Launcher = $null
            $script:HttpChecker = $null
            $script:Sleeper = $null
            $result = Invoke-GatewayStart
            if ($null -eq $result -or [string]$result.Status -ne 'Started') {
                throw 'self-test failed: Invoke-GatewayStart with a Stopper seam did not reach the runtime start'
            }
        } finally {
            $script:StartService = $savedStartService
            $script:Stopper = $savedStopper
            $script:Launcher = $savedLauncher
            $script:HttpChecker = $savedHttpChecker
            $script:Sleeper = $savedSleeper
            $script:GatewayDesiredStateEnabled = $savedDesiredEnabled
            if ($null -eq $savedSource) {
                Remove-Item Env:\SELFUSEAI_GATEWAY_ACTION_SOURCE -ErrorAction SilentlyContinue
            } else {
                $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = $savedSource
            }
        }
    }

    $script:selfTestProcessState = 'verified'
    $script:selfTestProcessPid = 4242
    $script:ProcessQuery = {
        if ($script:selfTestProcessState -eq 'verified') {
            return [pscustomobject]@{
                State = 'verified'; Pids = @($script:selfTestProcessPid); Error = $null }
        }
        if ($script:selfTestProcessState -eq 'unconfirmed') {
            return [pscustomobject]@{ State = 'unconfirmed'; Pids = @(); Error = 'port_8000_held_by_non_private_process' }
        }
        return [pscustomobject]@{ State = 'absent'; Pids = @(); Error = $null }
    }
    $script:selfTestDesired = [pscustomobject]@{
        state = 'stopped'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'never' }
    $script:DesiredStateProvider = { $script:selfTestDesired }
    $script:selfTestEvidence = $null
    $script:ExitEvidenceProvider = { $script:selfTestEvidence }
    $script:selfTestStarts = 0
    $script:selfTestStartModelsInstance = $false
    $script:StartService = {
        $script:selfTestStarts += 1
        # #117: model a launcher whose new instance answers /health, so the
        # next run must see the new PID and never start a second time.
        if ($script:selfTestStartModelsInstance) {
            $script:selfTestProcessState = 'verified'
            $script:selfTestProcessPid = 5555
            $script:selfTestProbeReady = $true
            $script:selfTestProbeMode = 'ready'
        }
        [pscustomobject]@{ Status = 'Restarted' }
    }
    $script:selfTestSchedulingCalls = New-Object System.Collections.ArrayList
    $script:selfTestTaskDisabled = $false
    $script:SchedulingControl = {
        param([bool]$Enabled)
        [void]$script:selfTestSchedulingCalls.Add([bool]$Enabled)
        # Model the real scheduler: an actual transition reports Action
        # Disable/Enable; a no-op (already in the wanted state) reports None.
        if (-not $Enabled -and -not $script:selfTestTaskDisabled) {
            $script:selfTestTaskDisabled = $true
            return [pscustomobject]@{
                Registered = $true; SchedulingEnabled = $false
                Action = 'Disable'; Detail = '' }
        }
        return [pscustomobject]@{
            Registered = $true; SchedulingEnabled = $Enabled
            Action = 'None'; Detail = '' }
    }
    # Rev13-W: the deployment control marker opts the lifecycle in; the
    # self-test points it at its own temporary directory.
    [void](Enable-GatewayWatchLifecycleControl)
    function Assert-Starts([int]$Expected, [string]$Why) {
        if ($script:selfTestStarts -ne $Expected) {
            throw ('self-test failed: {0} (starts={1}, expected={2})' -f
                $Why, $script:selfTestStarts, $Expected)
        }
    }
    function Assert-StartsNone {
        # Delta form for evaluations after the budget block (the running total
        # is already 3 there): the body must start nothing.
        param(
            [Parameter(Mandatory = $true)][scriptblock]$Body,
            [Parameter(Mandatory = $true)][string]$Why
        )
        $before = $script:selfTestStarts
        [void](& $Body)
        if ($script:selfTestStarts -ne $before) {
            throw ('self-test failed: {0} (started {1} time(s))' -f
                $Why, ($script:selfTestStarts - $before))
        }
    }
    function Assert-Releases([int]$ExpectedDelta, [string]$Why) {
        # The watch never ARMS: every scheduling call must be a release.
        foreach ($enabled in $script:selfTestSchedulingCalls) {
            if ($enabled) {
                throw ('self-test failed: the watch armed the task itself ({0})' -f $Why)
            }
        }
        if ($script:selfTestSchedulingCalls.Count -ne $ExpectedDelta) {
            throw ('self-test failed: {0} (releases={1}, expected={2})' -f
                $Why, $script:selfTestSchedulingCalls.Count, $ExpectedDelta)
        }
    }

    # A deliberate stop is never undone, and idling does not probe or log
    # every run. Rev13-W: the boot authorizes no recovery, so the watch
    # releases its own scheduling (option A; never mode B idle-every-minute).
    $script:selfTestProcessState = 'absent'
    1..3 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'desired=stopped started the service'
    Assert-Releases 3 'a deliberate stop must release the scheduling'
    $idleLines = @(Get-Content -LiteralPath $script:LogPath |
        Where-Object { $_ -match 'idle reason=desired_stopped' })
    if ($idleLines.Count -ne 1) {
        throw "self-test failed: idle reason logged $($idleLines.Count) times, expected once"
    }
    $releasedLines = @(Get-Content -LiteralPath $script:LogPath |
        Where-Object { $_ -match 'watch_scheduling_released' })
    if ($releasedLines.Count -ne 1) {
        throw "self-test failed: release logged $($releasedLines.Count) times, expected once"
    }

    # Unknown intent and an intent from a previous boot also mean "no" (and
    # release - nothing in this boot authorizes recovery).
    $script:selfTestDesired = $null
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'unknown desired state started the service'
    Assert-Releases 5 'unknown intent must release the scheduling'
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'older-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'succeeded' }
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'previous-boot intent started the service'
    Assert-Releases 7 'previous-boot intent must release the scheduling'

    # Rev13-W start-outcome gate: this boot's running intent without a
    # confirmed start authorizes nothing. Indeterminate (field missing) and
    # pending (a start may be in flight) KEEP the task - fail closed; a failed
    # start with no confirmed boot start releases it.
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z' }
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'a never-confirmed running intent started the service'
    Assert-Releases 7 'an indeterminate start outcome must keep the task'
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'pending' }
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'a pending start outcome started the service'
    Assert-Releases 7 'a pending start outcome must keep the task'
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'failed' }
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'a failed start outcome started the service'
    Assert-Releases 9 'a failed start outcome must release the task'

    # This boot DID have a confirmed start (marker start_outcome=succeeded):
    # the recovery path is live again from here on, and nothing below may
    # release the scheduling - an abnormal exit keeps the entry armed.
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'succeeded' }

    # Two failed probes with a live PID, or an unconfirmed identity, must not
    # start anything.
    $script:selfTestProcessState = 'verified'
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'live PID triggered a start'
    $script:selfTestProcessState = 'unconfirmed'
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'unconfirmed identity triggered a start'
    Assert-Releases 9 'identity doubts must not disarm the watch'

    # A graceful shutdown newer than the intent was deliberate; an older one
    # (superseded by a later start) is not.
    $script:selfTestProcessState = 'absent'
    $script:selfTestEvidence = [pscustomobject]@{
        Pid = 777; GracefulShutdownUtc = [DateTimeOffset]::Parse('2026-09-21T11:30:00Z') }
    1..2 | ForEach-Object { [void](Invoke-Chat2ApiWatch -SkipMutex) }
    Assert-Starts 0 'graceful shutdown outside the runtime was undone'
    Assert-Releases 9 'a deliberate bypass-runtime stop must not disarm via the watch'
    $script:selfTestEvidence = [pscustomobject]@{
        Pid = 777; GracefulShutdownUtc = [DateTimeOffset]::Parse('2026-09-21T10:30:00Z') }

    # Vanished with no newer graceful record: one evaluation may start once.
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    Assert-Starts 1 'first confirmed absence did not start exactly once'

    # Each start resets the two-failure gate. Permit attempts 2 and 3.
    1..2 | ForEach-Object {
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        [void](Invoke-Chat2ApiWatch -SkipMutex)
    }
    Assert-Starts 3 'expected three starts within the hourly budget'

    # A fourth candidate within the same hour must be warning-only, and the
    # budget exhaustion keeps the entry armed for the next hour.
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    Assert-Starts 3 'hourly budget allowed a fourth start'
    Assert-Releases 9 'budget exhaustion must not disarm the watch'

    $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if (@($state.restart_attempts_utc).Count -ne 3) {
        throw 'self-test failed: recovery budget counter is not three'
    }

    # Recovery vs deliberate-stop race: the intent flips to stopped between the
    # outer check and the under-lock recheck; the stop wins and nothing starts.
    $script:desiredCalls = 0
    $script:DesiredStateProvider = {
        $script:desiredCalls += 1
        if ($script:desiredCalls -le 2) {
            return [pscustomobject]@{
                state = 'running'; boot_id = 'selftest-boot'; source = 'console'
                at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'succeeded' }
        }
        return [pscustomobject]@{
            state = 'stopped'; boot_id = 'selftest-boot'; source = 'console'
            at_utc = '2026-09-21T11:59:00Z'; start_outcome = 'never' }
    }
    $raceState = [pscustomobject]@{
        version = 2; boot_id = 'selftest-boot'; consecutive_failures = 1
        restart_attempts_utc = @(); idle_reason = '' }
    $raceState | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    $script:selfTestProcessState = 'absent'
    $startsBeforeRace = $script:selfTestStarts
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    if ($script:selfTestStarts -ne $startsBeforeRace) {
        throw ('self-test failed: a stop that raced the recovery started anyway (starts={0})' -f
            $script:selfTestStarts)
    }
    Assert-Releases 9 'the racing run goes active, the operator stop path owns the disarm'
    $script:DesiredStateProvider = { $script:selfTestDesired }

    # #117: a probe with no HTTP response must not end the run. The REAL
    # probe runs against a bound-but-unlistening loopback port (the case that
    # used to raise PropertyNotFoundStrict under the production StrictMode and
    # exit the task with code 1). The watch records the sanitized reason and
    # continues into the existing process and intent checks: a live managed
    # process is kept, a vanished one is started only once, and an explicit stop
    # or an unconfirmed ownership starts nothing.
    function Reset-SelfTestFixtureState {
        param([int]$ConsecutiveFailures = 1)
        $fixture = [pscustomobject]@{
            version = 2; boot_id = 'selftest-boot'
            consecutive_failures = $ConsecutiveFailures
            restart_attempts_utc = @(); idle_reason = '' }
        $fixture | ConvertTo-Json -Depth 5 |
            Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    }
    function Get-NewSelfTestLogLines {
        param([int]$Skip)
        return @(Get-Content -LiteralPath $script:LogPath | Select-Object -Skip $Skip)
    }
    function Assert-NoStart([int]$StartsBefore, [string]$Why) {
        if ($script:selfTestStarts -ne $StartsBefore) {
            throw ('self-test failed: {0} (started {1} time(s))' -f
                $Why, ($script:selfTestStarts - $StartsBefore))
        }
    }
    $noResponseReason = 'reason=(connect_refused|connect_failed|probe_timeout|dns_failure|no_response)'
    $savedReadyUrl = $ReadyUrl

    $closedProbeSocket = [System.Net.Sockets.Socket]::new(
        [System.Net.Sockets.AddressFamily]::InterNetwork,
        [System.Net.Sockets.SocketType]::Stream,
        [System.Net.Sockets.ProtocolType]::Tcp)
    try {
        $closedProbeSocket.Bind([System.Net.IPEndPoint]::new(
            [System.Net.IPAddress]::Loopback, 0))
        $script:selfTestClosedProbeUrl = 'http://127.0.0.1:{0}/ready' -f
            $closedProbeSocket.LocalEndPoint.Port
        $script:selfTestProbeMode = 'default'
        $script:ReadyUrl = $script:selfTestClosedProbeUrl
        $script:selfTestEvidence = $null

        # (a) No response + a live managed process: recorded, no restart.
        Reset-SelfTestFixtureState
        $script:selfTestProcessState = 'verified'
        $script:selfTestProcessPid = 4242
        $logLinesBefore = @(Get-Content -LiteralPath $script:LogPath).Count
        $startsBefore = $script:selfTestStarts
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        Assert-NoStart $startsBefore 'a no-response probe over a live process started the service'
        $newLines = Get-NewSelfTestLogLines -Skip $logLinesBefore
        $probeLine = @($newLines | Where-Object {
            $_ -match 'probe_failed .*responded=False http_status=- ' + $noResponseReason })
        if ($probeLine.Count -ne 1) {
            throw ('self-test failed: a no-response probe logged {0} sanitized line(s), expected one: {1}' -f
                $probeLine.Count, ($newLines -join ' | '))
        }
        if ($probeLine[0] -notmatch 'error_type=(?!-)') {
            throw 'self-test failed: a no-response probe did not log the exception type'
        }
        if ($newLines -match 'PropertyNotFoundStrict') {
            throw 'self-test failed: the no-response probe raised PropertyNotFoundStrict'
        }
        if ($newLines -match '127\.0\.0\.1|http://') {
            throw 'self-test failed: the no-response probe leaked its URL into the watch log'
        }
        $keptLine = @($newLines | Where-Object {
            $_ -match 'no_restart reason=process_alive pids=4242' })
        if ($keptLine.Count -ne 1) {
            throw 'self-test failed: a no-response probe over a live process did not keep it'
        }

        # (b) No response + a vanished managed instance: exactly one start,
        # then the next run sees the NEW pid and /health 200.
        Reset-SelfTestFixtureState
        $script:selfTestProcessState = 'absent'
        $script:selfTestStartModelsInstance = $true
        $logLinesBefore = @(Get-Content -LiteralPath $script:LogPath).Count
        $startsBefore = $script:selfTestStarts
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        if ($script:selfTestStarts -ne ($startsBefore + 1)) {
            throw ('self-test failed: a no-response probe over a vanished instance started {0} time(s), expected one' -f
                ($script:selfTestStarts - $startsBefore))
        }
        $newLines = Get-NewSelfTestLogLines -Skip $logLinesBefore
        if (@($newLines | Where-Object {
                $_ -match 'auto_start attempt=1/3 reason=pid_absent' }).Count -ne 1) {
            throw 'self-test failed: the no-response auto-start was not logged with reason=pid_absent'
        }
        if ($script:selfTestProcessPid -ne 5555) {
            throw ('self-test failed: the started instance has pid {0}, expected the new pid 5555' -f
                $script:selfTestProcessPid)
        }
        $script:selfTestStartModelsInstance = $false
        $script:selfTestProbeMode = 'ready'
        $logLinesBefore = @(Get-Content -LiteralPath $script:LogPath).Count
        $startsBefore = $script:selfTestStarts
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        Assert-NoStart $startsBefore 'the recovered instance was started a second time'
        $newLines = Get-NewSelfTestLogLines -Skip $logLinesBefore
        if ($newLines -match 'probe_failed|auto_start ') {
            throw ('self-test failed: the recovered run was not quiet: {0}' -f ($newLines -join ' | '))
        }
        $lastProbe = $script:selfTestProbeResults[$script:selfTestProbeResults.Count - 1]
        if (-not [bool]$lastProbe.Ready -or [int]$lastProbe.HttpStatus -ne 200) {
            throw ('self-test failed: the recovered instance reported ready={0} http_status={1}' -f
                [bool]$lastProbe.Ready, $lastProbe.HttpStatus)
        }
        $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ([int]$state.consecutive_failures -ne 0) {
            throw 'self-test failed: the recovered instance did not clear the failure gate'
        }

        # (c) An HTTP 503 response with a live managed process: the status is
        # recorded and the process is kept, never restarted.
        Reset-SelfTestFixtureState
        $script:selfTestProbeMode = 'http_not_ready'
        $script:selfTestProcessState = 'verified'
        $script:selfTestProcessPid = 4242
        $logLinesBefore = @(Get-Content -LiteralPath $script:LogPath).Count
        $startsBefore = $script:selfTestStarts
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        Assert-NoStart $startsBefore 'an HTTP 503 over a live process started the service'
        $newLines = Get-NewSelfTestLogLines -Skip $logLinesBefore
        if (@($newLines | Where-Object {
                $_ -match 'responded=True http_status=503 reason=http_not_ready' }).Count -ne 1) {
            throw 'self-test failed: the HTTP 503 status was not recorded'
        }
        if (@($newLines | Where-Object {
                $_ -match 'no_restart reason=process_alive pids=4242' }).Count -ne 1) {
            throw 'self-test failed: the HTTP 503 did not keep the live process'
        }

        # (d) An explicit stop is never undone, even without an HTTP response
        # (the watch returns before probing).
        $script:selfTestProbeMode = 'closed'
        $script:selfTestDesired = [pscustomobject]@{
            state = 'stopped'; boot_id = 'selftest-boot'; source = 'console'
            at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'never' }
        $probeCallsBefore = $script:selfTestProbeResults.Count
        $startsBefore = $script:selfTestStarts
        [void](Disable-GatewayWatchLifecycleControl)
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        [void](Enable-GatewayWatchLifecycleControl)
        Assert-NoStart $startsBefore 'an explicit stop was undone by a no-response probe'
        if ($script:selfTestProbeResults.Count -ne $probeCallsBefore) {
            throw 'self-test failed: an explicit stop still probed /ready'
        }
        $script:selfTestDesired = [pscustomobject]@{
            state = 'running'; boot_id = 'selftest-boot'; source = 'console'
            at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'succeeded' }

        # (e) Unconfirmed process ownership is never started into.
        Reset-SelfTestFixtureState
        $script:selfTestProcessState = 'unconfirmed'
        $logLinesBefore = @(Get-Content -LiteralPath $script:LogPath).Count
        $startsBefore = $script:selfTestStarts
        [void](Invoke-Chat2ApiWatch -SkipMutex)
        Assert-NoStart $startsBefore 'an unconfirmed process state started the service'
        $newLines = Get-NewSelfTestLogLines -Skip $logLinesBefore
        if (@($newLines | Where-Object {
                $_ -match 'no_restart reason=process_state_unconfirmed' }).Count -ne 1) {
            throw 'self-test failed: an unconfirmed process state was not recorded'
        }
    } finally {
        $closedProbeSocket.Dispose()
        $script:ReadyUrl = $savedReadyUrl
        $script:selfTestProbeMode = 'failure'
        $script:selfTestProbeReady = $false
        $script:selfTestStartModelsInstance = $false
        $script:selfTestProcessState = 'verified'
        $script:selfTestProcessPid = 4242
    }

    # Corrupted watch state: conservative empty state, no action, no crash.
    Set-Content -LiteralPath $script:StatePath -Value '{ this is not json' -Encoding UTF8
    $script:selfTestDesired = [pscustomobject]@{
        state = 'stopped'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'never' }
    Assert-StartsNone -Why 'corrupted watch state started the service' -Body {
        [void](Invoke-Chat2ApiWatch -SkipMutex)
    }
    Assert-Releases 10 'a corrupt state still releases (stopped intent)'

    # Watch state from a previous boot: the budget resets with the boot.
    $oldBootState = [pscustomobject]@{
        version = 2; boot_id = 'older-boot'; consecutive_failures = 5
        restart_attempts_utc = @('2026-09-20T12:00:00Z'); idle_reason = '' }
    $oldBootState | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    Assert-StartsNone -Why 'previous-boot watch state started the service' -Body {
        [void](Invoke-Chat2ApiWatch -SkipMutex)
    }
    Assert-Releases 11 'a cross-boot idle run still releases (stopped intent)'
    $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ([string]$state.boot_id -ne 'selftest-boot' -or [int]$state.consecutive_failures -ne 0) {
        throw 'self-test failed: cross-boot watch state was not reset'
    }

    # Closing the usage console (8001) or its web page is invisible to this
    # watch: it reads only the operator intent and the private 8000 signals,
    # so a console-page/8001 exit neither disarms the watch nor restarts 8000,
    # and an armed watch over a healthy service stays quiet.
    $script:selfTestDesired = [pscustomobject]@{
        state = 'running'; boot_id = 'selftest-boot'; source = 'console'
        at_utc = '2026-09-21T11:00:00Z'; start_outcome = 'succeeded' }
    $script:selfTestProcessState = 'verified'
    $script:selfTestProbeReady = $true
    Assert-StartsNone -Why 'an 8001/console-page exit restarted 8000' -Body {
        [void](Invoke-Chat2ApiWatch -SkipMutex)
    }
    Assert-Releases 11 'an 8001/console-page exit disarmed the watch'
    $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ([int]$state.consecutive_failures -ne 0) {
        throw 'self-test failed: a healthy probe did not clear the failure gate'
    }

    # Rev13-W compatibility (R27-LOAD): the start-outcome gate exists only
    # AFTER the deployment window opts in. Before that the watch keeps the
    # round-19 authorization - a this-boot state=running intent recovers on
    # its own, whatever start_outcome says - so a disk still holding an old v1
    # marker (or a failed attempt) cannot silently lose the guard over an
    # instance round 19 would have recovered. These fixtures run against the
    # self-test's own control-marker directory (the guard at the top of this
    # function already proved the path is inside $testRoot), so the real
    # deployment marker is never read or toggled. Deltas are used: the running
    # totals of the scenarios above stay untouched.
    function Set-CompatibilityFixtureState {
        # Fresh per-boot budget and a single failure so one run reaches the
        # start path (the second failed probe).
        $fixtureState = [pscustomobject]@{
            version = 2; boot_id = 'selftest-boot'; consecutive_failures = 1
            restart_attempts_utc = @(); idle_reason = '' }
        $fixtureState | ConvertTo-Json -Depth 5 |
            Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    }
    function Set-CompatibilityDesiredMarker {
        # Write a REAL marker file in the shape round 19 / Rev13-W wrote it -
        # never inject a record object: the fixture must drive
        # Read-GatewayDesiredState, the reader the production watch uses, or
        # it cannot prove an old v1 marker keeps its round-19 recovery.
        param(
            [Parameter(Mandatory = $true)][int]$Version,
            [string]$State = 'running',
            $StartOutcome
        )
        $record = [ordered]@{
            version = $Version; key = $script:ServiceKey; state = $State
            at_utc = '2026-09-21T11:00:00Z'; boot_id = 'selftest-boot'
            source = 'console'; writer_pid = 1234
        }
        if ($null -ne $StartOutcome) { $record.start_outcome = $StartOutcome }
        [IO.File]::WriteAllText(
            (Get-GatewayDesiredStatePath -Key $script:ServiceKey),
            ($record | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
    }
    function Assert-CompatibilityMarkerReadable {
        param([int]$Version, [string]$Why)
        $readBack = Read-GatewayDesiredState -Key $script:ServiceKey
        if ($null -eq $readBack) {
            throw ('self-test failed: {0} (Read-GatewayDesiredState returned null)' -f $Why)
        }
        if ([int]$readBack.version -ne $Version -or
                [string]$readBack.state -ne 'running' -or
                [string]$readBack.boot_id -ne 'selftest-boot') {
            throw ('self-test failed: {0} (read back version={1} state={2} boot={3})' -f
                $Why, [int]$readBack.version, [string]$readBack.state, [string]$readBack.boot_id)
        }
    }
    $script:selfTestProcessState = 'absent'
    $script:selfTestProbeReady = $false
    $script:selfTestEvidence = $null
    # The compatibility fixtures read the real marker FILE through the production
    # reader; the object provider the scenarios above use is disconnected here.
    $script:DesiredStateProvider = $null

    # (1) Control marker OFF + v1 marker (state=running, no start_outcome):
    # the outcome gate does not apply yet - exactly one start, and the real
    # scheduling is never touched (pre-opt-in the watch mutates nothing).
    [void](Disable-GatewayWatchLifecycleControl)
    if (Test-GatewayWatchLifecycleControl) {
        throw 'self-test failed: the compatibility fixture did not turn the control marker off'
    }
    Set-CompatibilityDesiredMarker -Version 1
    Assert-CompatibilityMarkerReadable -Version 1 `
        -Why 'the v1 marker did not reach the compatibility decision'
    Set-CompatibilityFixtureState
    $startsBefore = $script:selfTestStarts
    $callsBefore = $script:selfTestSchedulingCalls.Count
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    if ($script:selfTestStarts -ne ($startsBefore + 1)) {
        throw ('self-test failed: control off + v1 marker lost the round-19 recovery (starts={0}, expected={1})' -f
            $script:selfTestStarts, ($startsBefore + 1))
    }
    if ($script:selfTestSchedulingCalls.Count -ne $callsBefore) {
        throw 'self-test failed: control off + v1 marker touched the scheduling (pre-opt-in)'
    }

    # (2) Control marker OFF + v2 marker start_outcome=failed: start_outcome
    # is not a criterion before the opt-in either - round 19 authorizes the
    # recovery of this boot's running intent.
    Set-CompatibilityDesiredMarker -Version 2 -StartOutcome 'failed'
    Assert-CompatibilityMarkerReadable -Version 2 `
        -Why 'the v2 marker did not reach the compatibility decision'
    Set-CompatibilityFixtureState
    $startsBefore = $script:selfTestStarts
    $callsBefore = $script:selfTestSchedulingCalls.Count
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    if ($script:selfTestStarts -ne ($startsBefore + 1)) {
        throw ('self-test failed: control off + failed outcome lost the round-19 recovery (starts={0}, expected={1})' -f
            $script:selfTestStarts, ($startsBefore + 1))
    }
    if ($script:selfTestSchedulingCalls.Count -ne $callsBefore) {
        throw 'self-test failed: control off + failed outcome touched the scheduling (pre-opt-in)'
    }

    # (3) Control marker back ON + v1 marker: fail closed again - nothing
    # starts and the entry stays armed (the 'start_outcome_unknown' rule of the
    # opt-in scenarios above, reached here through a missing field).
    [void](Enable-GatewayWatchLifecycleControl)
    if (-not (Test-GatewayWatchLifecycleControl)) {
        throw 'self-test failed: the compatibility fixture did not restore the control marker'
    }
    Set-CompatibilityDesiredMarker -Version 1
    Assert-CompatibilityMarkerReadable -Version 1 `
        -Why 'the opt-in v1 marker did not reach the fail-closed gate'
    Set-CompatibilityFixtureState
    $startsBefore = $script:selfTestStarts
    $callsBefore = $script:selfTestSchedulingCalls.Count
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    if ($script:selfTestStarts -ne $startsBefore) {
        throw 'self-test failed: opt-in + v1 marker started the service (must fail closed)'
    }
    if ($script:selfTestSchedulingCalls.Count -ne $callsBefore) {
        throw 'self-test failed: opt-in + v1 marker released the armed watch'
    }
    $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ([string]$state.idle_reason -ne 'start_outcome_unknown') {
        throw ('self-test failed: opt-in + v1 marker idled for "{0}", expected start_outcome_unknown' -f
            [string]$state.idle_reason)
    }

    # (4) Control marker OFF + a deliberate stop: the idle path runs, but the
    # scheduling release is inert pre-opt-in (round-19 compatibility - the watch
    # must not mutate the real scheduled task before the deployment window), so
    # this run makes no scheduling call at all.
    [void](Disable-GatewayWatchLifecycleControl)
    Set-CompatibilityDesiredMarker -Version 1 -State 'stopped'
    $callsBefore = $script:selfTestSchedulingCalls.Count
    [void](Invoke-Chat2ApiWatch -SkipMutex)
    if ($script:selfTestSchedulingCalls.Count -ne $callsBefore) {
        throw 'self-test failed: control off + stopped intent touched the scheduling (pre-opt-in)'
    }

    # R32-KEY-AUTH: the private instance's listening record now carries an
    # optional downstream_key=... suffix (chat2api.py:6419-6421); the default
    # exit-evidence parser must still bind the pid and the graceful stop from a
    # temp log (never the real chat2api.log: the path is overridden here).
    $savedServiceLogPath = $ServiceLogPath
    $ServiceLogPath = Join-Path $testRoot 'selftest-service.log'
    $script:ExitEvidenceProvider = $null
    Set-Content -LiteralPath $ServiceLogPath -Encoding UTF8 -Value @(
        '2026-09-21 11:30:00,000 INFO listening host=127.0.0.1 port=8000 pid=777 downstream_key=not_required',
        '2026-09-21 11:40:00,000 INFO exit begin port=8000 pid=777 reason=admin_shutdown')
    $parsedEvidence = Get-ExitEvidence
    if ($null -eq $parsedEvidence -or [int]$parsedEvidence.Pid -ne 777) {
        throw 'self-test failed: the downstream_key suffix hid the listening pid'
    }
    if ($null -eq $parsedEvidence.GracefulShutdownUtc) {
        throw 'self-test failed: the downstream_key suffix hid the graceful exit record'
    }
    $ServiceLogPath = $savedServiceLogPath
    $script:ExitEvidenceProvider = { $script:selfTestEvidence }

    $env:SELFUSEAI_GATEWAY_WATCH_DIR = $savedWatchDir
    Write-Host 'watch-chat2api self-test: PASS'
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    return 0
}

if ($SelfTest) {
    exit (Invoke-WatchSelfTest)
}

Initialize-WatchDirectory -Path $StatePath
Initialize-WatchDirectory -Path $LogPath
exit (Invoke-Chat2ApiWatch)
