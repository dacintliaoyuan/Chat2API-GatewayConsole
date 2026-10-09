# Public gateway runtime: chat2api (8000) and usage console (8096).
# Sharing has its own isolated backend (8110) and guarded edge (8100).

$script:GatewayServiceDescriptors = [ordered]@{
    'chat2api' = [pscustomobject]@{
        Key          = 'chat2api'
        DisplayName  = 'chat2api'
        Port         = 8000
        HealthPath   = '/health'
        StartOrder   = 2
        OpenUrl      = $null
        StartAction  = 'HiddenBatch'
        StopAction   = 'CommandLine'
        StartPath    = 'start_chat2api.bat'
        StartTimeoutSeconds = 20
        StopStrategy = 'GracefulHttp'
        StopWhenUnhealthy = $true
        StopProcessName = 'python.exe'
        StopCommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
        # Round 18 (2026-09-29) stop-scope identity. The entrypoint token above
        # also matched the dedicated share backend (chat2api.py --port 8110
        # --profile <share>\edge-profile, started by
        # console_share_production.py:1226-1234), so a private stop force-stopped
        # a share instance. The private launcher (start_chat2api.bat) passes only
        # --base-url, so these two facts positively identify the private
        # instance; --port, when declared, must agree with Port. See
        # Get-GatewayStopScopeClass for the full rule.
        StopInstanceScript = 'chat2api\chat2api.py'
        StopForeignArgumentPatterns = @('--profile')
        OwnerProcessName = 'python.exe'
        OwnerCommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
        AuthStatusUrl = 'http://127.0.0.1:8000/admin/auth/status'
        ReauthenticationUrl = 'http://127.0.0.1:8000/admin/auth/login'
        ShutdownUrl = 'http://127.0.0.1:8000/admin/shutdown'
        # Round 19 (2026-09-30): Start/Stop/Restart record the operator's
        # intent (Write-GatewayDesiredState) so lib\watch-chat2api.ps1 never
        # brings back an instance someone stopped on purpose, and the launch
        # tells chat2api who started it (its startup log line names the source).
        DesiredStateTracked = $true
        LaunchSourceEnv = 'CHAT2API_LAUNCH_SOURCE'
        # Rev13-W (2026-10-04, maintainer-approved option A): the opening
        # intent includes the independent watch. A runtime start arms this
        # scheduled task, a deliberate stop releases it, an abnormal exit
        # never touches it (the recovery entry survives). The task name must
        # stay identical to lib\register-chat2api-watch.ps1's registration.
        WatchTaskName = 'AI Gateway Public Chat2API Watch'
    }
    'usage' = [pscustomobject]@{
        Key          = 'usage'
        DisplayName  = '费用统计'
        Port         = 8096
        HealthPath   = '/health'
        StartOrder   = 6
        OpenUrl      = 'http://127.0.0.1:8096/'
        StartAction  = 'HiddenBatch'
        StopAction   = 'CommandLine'
        StartPath    = 'start_usage_service.bat'
        StartTimeoutSeconds = 10
        StopStrategy = 'CommandLine'
        StopWhenUnhealthy = $true
        StopProcessName = 'python.exe'
        StopCommandLinePattern = '(^|\s|[\\/])usagesrv\.py(\s|$)'
        StopInstanceScript = 'usagesrv.py'
        OwnerProcessName = 'python.exe'
        OwnerCommandLinePattern = '(^|\s)usagesrv\.py(\s|$)'
    }
}

# Startup order keeps all model sources ahead of consumers.
$script:GatewayStartOrder = @('chat2api', 'usage')
$script:GatewayStopOrder = @('usage', 'chat2api')

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

function Get-GatewayRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-LocalControlHeader {
    # X-Admin-Control header for chat2api management/passthrough endpoints.
    # The token file lives in the repo data dir, which the share identity
    # cannot read (repo-root deny ACE) - that asymmetry is the boundary.
    param([string]$TokenFile = (Join-Path (Get-GatewayRoot) 'data\admin-control-token'))
    if (Test-Path -LiteralPath $TokenFile) {
        $token = (Get-Content -LiteralPath $TokenFile -Raw -ErrorAction SilentlyContinue)
        if ($token) { return @{ 'X-Admin-Control' = $token.Trim() } }
    }
    return @{}
}

function Test-Chat2ApiControlUri {
    # The control token may only be sent to chat2api (port 8000) management or
    # passthrough paths. Every other local service - and every other path on
    # chat2api itself - must never receive the secret.
    param([Parameter(Mandatory = $true)][string]$Uri)
    try {
        $u = [uri]$Uri
    } catch {
        return $false
    }
    if ($u.Host -notin @('127.0.0.1', 'localhost', '::1')) { return $false }
    if ($u.Port -ne 8000) { return $false }
    $path = $u.AbsolutePath
    return ($path -eq '/admin' -or $path -like '/admin/*' -or
            $path -eq '/api' -or $path -like '/api/*')
}

function Test-GatewayServiceArchived {
    param([Parameter(Mandatory = $true)][string]$Key)
    $desc = $script:GatewayServiceDescriptors[$Key]
    if ($null -eq $desc) { return $false }
    $archived = $desc.PSObject.Properties['Archived']
    return ($null -ne $archived -and [bool]$archived.Value)
}

function Resolve-GatewayStartPath {
    param([Parameter(Mandatory = $true)][string]$Key)

    $desc = $script:GatewayServiceDescriptors[$Key]
    switch ($desc.StartAction) {
        'HiddenBatch' { return (Join-Path (Get-GatewayRoot) $desc.StartPath) }
        default {
            throw "Unknown start action for $Key"
        }
    }
}

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

function Test-GatewayServiceHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [int]$TimeoutSeconds = 2,
        [scriptblock]$HttpChecker
    )

    $desc = $script:GatewayServiceDescriptors[$Key]
    $url = "http://127.0.0.1:$($desc.Port)$($desc.HealthPath)"

    if ($null -ne $HttpChecker) {
        return & $HttpChecker $url $TimeoutSeconds
    }

    try {
        $previousProgressPreference = $ProgressPreference
        try {
            # Windows PowerShell renders Invoke-WebRequest progress as a blue
            # overlay. Five sequential health checks made that overlay flash
            # repeatedly over the interactive console.
            $ProgressPreference = 'SilentlyContinue'
            $response = Invoke-WebRequest -Uri $url -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
            return $response.StatusCode -lt 400
        } finally {
            $ProgressPreference = $previousProgressPreference
        }
    } catch {
        # Any HTTP response proves listener liveness; ownership is checked separately.
        if ($null -ne $_.Exception.Response) {
            return $true
        }
        return $false
    }
}

function Get-GatewayServiceStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker
    )

    if ($null -eq $HttpChecker) {
        return (Get-GatewayServiceInspection -Key $Key).State
    }
    if (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker) {
        return 'Running'
    }
    return 'Stopped'
}

function Test-GatewayProcessOwnership {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Process
    )
    return ($Process.Name -eq $Descriptor.OwnerProcessName -and
        (Get-GatewayStopScopeClass -Descriptor $Descriptor -Process $Process -GatewayRoot (Get-GatewayRoot)) -eq 'Private')
}

# ---------------------------------------------------------------------------
# Stop scope (round 18, 2026-09-29)
#
# The stop scope answers ONE question before anything is terminated: which running
# processes are POSITIVELY identified as this descriptor's own private instance?
# Test-GatewayProcessOwnership is deliberately NOT reused here: for chat2api its
# OwnerCommandLinePattern is the same broad entrypoint token that caused the
# defect, so it cannot separate the private instance from the dedicated share
# backend or from another checkout's same-named script.
# ---------------------------------------------------------------------------

function Get-GatewayDeclaredPort {
    # --port <n> / --port=<n> declared on a command line. $null means the
    # process declares no port at all (the private launcher passes none).
    param([AllowNull()][string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $null }
    $match = [regex]::Match($CommandLine, '--port(?:=|\s+)(?<port>\d{1,5})(?=\s|$)')
    if (-not $match.Success) { return $null }
    return [int]$match.Groups['port'].Value
}

function Get-GatewayStopScopeClass {
    <#
    Classify one candidate process against a descriptor's PRIVATE stop scope.

      Private     - positively identified as this descriptor's private instance.
      Foreign     - positively identified as a DIFFERENT instance; never stopped.
      Unconfirmed - the descriptor has no positive identity rule, or a qualified
                    script path cannot be resolved; never stopped (fail-closed).

    The private instance is the process that runs the descriptor's own entrypoint
    (StopInstanceScript) and declares no foreign port and none of the argument
    markers the private launcher never passes. A same-named script from another
    checkout is only distinguishable when its command line qualifies the script
    with a path; a bare relative name from another directory is NOT
    distinguishable from the command line alone, which is why the port and
    foreign-argument rules are mandatory and why the caller refuses instead of
    force-stopping when nothing confirms the identity.
    #>
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$GatewayRoot
    )
    if ([string]::IsNullOrWhiteSpace([string]$Process.CommandLine)) { return 'Unconfirmed' }
    $commandLine = [string]$Process.CommandLine

    if (-not $Descriptor.PSObject.Properties['StopInstanceScript'] -or
            -not $Descriptor.StopInstanceScript) {
        return 'Unconfirmed'
    }
    $expected = $null
    try {
        $expected = [IO.Path]::GetFullPath(
            (Join-Path $GatewayRoot ([string]$Descriptor.StopInstanceScript)))
    } catch {
        return 'Unconfirmed'
    }
    $leaf = [regex]::Escape([IO.Path]::GetFileName($expected))

    $references = @()
    $tokenPattern = '(?:"(?<quoted>[^"\r\n]+)"|(?<plain>[^\s"]+))'
    foreach ($match in [regex]::Matches($commandLine, $tokenPattern)) {
        $reference = if ($match.Groups['quoted'].Success) { $match.Groups['quoted'].Value } else { $match.Groups['plain'].Value }
        if ([IO.Path]::GetFileName($reference) -eq [IO.Path]::GetFileName($expected)) { $references += $reference }
    }
    if ($references.Count -eq 0) { return 'Foreign' }
    foreach ($reference in $references) {
        if (-not [IO.Path]::IsPathRooted($reference)) { return 'Unconfirmed' }
        try { $resolved = [IO.Path]::GetFullPath($reference) } catch { return 'Unconfirmed' }
        if (-not $resolved.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) { return 'Foreign' }
    }

    $declaredPort = Get-GatewayDeclaredPort -CommandLine $commandLine
    if ($null -ne $declaredPort -and $declaredPort -ne [int]$Descriptor.Port) {
        return 'Foreign'
    }

    $foreignPatterns = @()
    if ($Descriptor.PSObject.Properties['StopForeignArgumentPatterns']) {
        $foreignPatterns = @($Descriptor.StopForeignArgumentPatterns)
    }
    foreach ($pattern in $foreignPatterns) {
        if ($pattern -and $commandLine -match $pattern) { return 'Foreign' }
    }

    return 'Private'
}

function Select-GatewayStopScope {
    # One pass over the process table, split into the verified private targets
    # and everything else. A process carrying the descriptor's stop process name
    # whose command line is unreadable is NOT skipped silently: it could be the
    # private instance, so it lands in Unconfirmed and the caller refuses rather
    # than reporting a stop it cannot confirm.
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$GatewayRoot
    )
    $private = @()
    $foreign = @()
    $unconfirmed = @()
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ([string]$process.Name -ne [string]$Descriptor.StopProcessName) { continue }
        if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
            # A name match with no readable command line could be the private
            # instance; classify it (Unconfirmed) instead of skipping it.
            $unconfirmed += $process
            continue
        }
        switch (Get-GatewayStopScopeClass -Descriptor $Descriptor -Process $process `
                -GatewayRoot $GatewayRoot) {
            'Private' { $private += $process }
            'Unconfirmed' { $unconfirmed += $process }
            default { $foreign += $process }
        }
    }
    return [pscustomobject]@{
        Private = $private; Foreign = $foreign; Unconfirmed = $unconfirmed
    }
}

function ConvertTo-GatewayCreationTicks {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) { return $Value.Ticks }
    try { return ([datetime]$Value).Ticks } catch { return $null }
}

function Test-GatewayProcessIdentityUnchanged {
    # A PID is only a handle: between selection and termination the OS may have
    # reused it for an unrelated process. Re-verify name, command line AND start
    # identity (creation time) before killing; anything unreadable fails closed.
    param(
        [Parameter(Mandatory = $true)]$Target,
        [AllowNull()]$Current
    )
    if ($null -eq $Current) { return $false }
    if ([int]$Current.ProcessId -ne [int]$Target.ProcessId) { return $false }
    if ([string]$Current.Name -ne [string]$Target.Name) { return $false }
    if ([string]$Current.CommandLine -ne [string]$Target.CommandLine) { return $false }
    $targetTicks = ConvertTo-GatewayCreationTicks -Value $Target.CreationDate
    $currentTicks = ConvertTo-GatewayCreationTicks -Value $Current.CreationDate
    if ($null -eq $targetTicks -or $null -eq $currentTicks) { return $false }
    return ($targetTicks -eq $currentTicks)
}

function Stop-GatewayScopedProcesses {
    <#
    Terminate ONLY the processes positively identified as this descriptor's
    private instance.

    Round 18 (2026-09-29): the GracefulHttp/CommandLine branches used to
    force-stop every python.exe whose command line carried the entrypoint token,
    which selected the dedicated share backend (chat2api.py --port 8110) too. Now
    the scope is classified first, each selected PID is re-verified against its
    startup identity right before the kill, and an unconfirmable scope terminates
    NOTHING and reports StopRefused instead of a broad force-stop or a fake
    "stopped".
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)]$Descriptor
    )
    try {
        $scope = Select-GatewayStopScope -Descriptor $Descriptor -GatewayRoot (Get-GatewayRoot)
    } catch {
        return [pscustomobject]@{
            Key = $Key; Status = 'StopRefused'; Action = 'Stop'
            TerminatedProcessIds = @()
            Detail = '进程归属无法确认（进程清单查询失败），未终止任何进程'
        }
    }
    if (@($scope.Unconfirmed).Count -gt 0) {
        # Observable refusal: the PIDs whose identity could not be confirmed are
        # named, so the operator can inspect them instead of a silent no-op.
        $unconfirmedPids = @($scope.Unconfirmed |
            ForEach-Object { [int]$_.ProcessId })
        return [pscustomobject]@{
            Key = $Key; Status = 'StopRefused'; Action = 'Stop'
            TerminatedProcessIds = @()
            Detail = '进程归属无法确认（PID ' + ($unconfirmedPids -join ',') +
                '），未终止任何进程'
        }
    }
    $terminated = @()
    $skipped = @()
    foreach ($target in @($scope.Private)) {
        $current = $null
        try {
            $current = @(Get-CimInstance Win32_Process `
                -Filter "ProcessId=$([int]$target.ProcessId)" -ErrorAction Stop) |
                Select-Object -First 1
        } catch {
            return [pscustomobject]@{
                Key = $Key; Status = 'StopRefused'; Action = 'Stop'
                TerminatedProcessIds = @($terminated)
                Detail = '进程归属无法确认（PID 复核查询失败），未终止其余目标'
            }
        }
        if (-not (Test-GatewayProcessIdentityUnchanged -Target $target -Current $current)) {
            $skipped += [int]$target.ProcessId
            continue
        }
        Stop-Process -Id ([int]$target.ProcessId) -Force
        $terminated += [int]$target.ProcessId
    }
    if ($skipped.Count -gt 0) {
        return [pscustomobject]@{
            Key = $Key; Status = 'StopRefused'; Action = 'Stop'
            TerminatedProcessIds = @($terminated)
            Detail = '目标 PID 启动身份已变化（PID 复用或进程已消失），未终止：' +
                ($skipped -join ',')
        }
    }
    return [pscustomobject]@{
        Key = $Key; Status = 'Stopped'; Action = 'Stop'
        TerminatedProcessIds = @($terminated)
    }
}

function Get-GatewayServiceInspection {
    param([Parameter(Mandatory = $true)][string]$Key)

    $desc = $script:GatewayServiceDescriptors[$Key]
    $listeners = @(Get-NetTCPConnection -LocalPort $desc.Port -State Listen `
        -ErrorAction SilentlyContinue)
    $owners = @()
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process `
            -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if ($null -ne $process) { $owners += $process }
    }
    $unknown = @($owners | Where-Object { -not (Test-GatewayProcessOwnership $desc $_) })
    if ($unknown.Count -gt 0) {
        return [pscustomobject]@{
            Key = $Key; State = 'Conflict'; Healthy = $false
            ProcessIds = @($unknown.ProcessId); Detail = '端口被未知程序占用'
        }
    }
    if ($owners.Count -gt 0) {
        $healthy = Test-GatewayServiceHealth -Key $Key
        return [pscustomobject]@{
            Key = $Key; State = if ($healthy) { 'Running' } else { 'Degraded' }
            Healthy = $healthy; ProcessIds = @($owners.ProcessId)
            Detail = if ($healthy) { '运行中' } else { '程序在运行但健康检查失败' }
        }
    }
    $residual = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { Test-GatewayProcessOwnership $desc $_ })
    # Empty $residual makes $residual.ProcessId evaluate to $null, so
    # @($null) would surface as process id 0 downstream; build the array
    # per-element so a stopped service reports an empty PID list.
    $residualPids = @()
    foreach ($proc in $residual) { $residualPids += [int]$proc.ProcessId }
    return [pscustomobject]@{
        Key = $Key; State = if ($residual.Count) { 'Residual' } else { 'Stopped' }
        Healthy = $false; ProcessIds = $residualPids
        Detail = if ($residual.Count) { '发现受管残留进程' } else { '已停止' }
    }
}

# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Desired state and launch source (round 19, 2026-09-30)
# ---------------------------------------------------------------------------
# The desired-state marker is the operator's last start/stop intent for a
# tracked service. Its only consumer is lib\watch-chat2api.ps1, which may bring
# an instance back only while the marker says "running" for the CURRENT boot:
# a stop through any runtime path (console, CLI, menu) writes "stopped" before
# it acts, so a manual stop is never undone; a marker from an earlier boot is
# stale, so nothing is auto-started merely because the machine rebooted.
#
# Writing is OFF unless the entry point opts in (Enable-GatewayDesiredState):
# the operator-facing entries do (invoke-gateway-service.ps1, the console's
# invoke-console-runtime-json.ps1 without test hooks, the watch itself). A test
# that dot-sources this file, or invoke-model-sync.ps1 borrowing a service for
# one sync and stopping it again, never moves the real operator intent.

$script:GatewayDesiredStateEnabled = $false

function Enable-GatewayDesiredState {
    $script:GatewayDesiredStateEnabled = $true
}

function Get-GatewayActionSource {
    # Who is driving this runtime call: the console bridge and the watch set
    # AIGATEWAY_PUBLIC_ACTION_SOURCE; any other caller is a script ("cli").
    $raw = [string]$env:AIGATEWAY_PUBLIC_ACTION_SOURCE
    if ($raw -match '^[a-z][a-z0-9_-]{0,31}$') { return $raw }
    return 'cli'
}

function Get-GatewayBootIdentity {
    # Stable for one boot of this machine; $null when it cannot be read.
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $boot = ([DateTime]$os.LastBootUpTime).ToUniversalTime().ToString('o')
        return '{0}|{1}' -f $env:COMPUTERNAME, $boot
    } catch {
        return $null
    }
}

function Get-GatewayDesiredStatePath {
    param([Parameter(Mandatory = $true)][string]$Key)
    $root = [string]$env:AIGATEWAY_PUBLIC_WATCH_DIR
    if (-not $root) {
        $root = Join-Path $env:LOCALAPPDATA 'AIGatewayPublic\watch'
    }
    return Join-Path $root ('{0}-desired.json' -f $Key)
}

function Test-GatewayDesiredStateTracked {
    param([Parameter(Mandatory = $true)][string]$Key)
    $desc = $script:GatewayServiceDescriptors[$Key]
    if ($null -eq $desc) { return $false }
    $prop = $desc.PSObject.Properties['DesiredStateTracked']
    return ($null -ne $prop -and [bool]$prop.Value)
}

function Write-GatewayDesiredState {
    # Record intent BEFORE acting. Returns $true when the marker holds the new
    # state. If "stopped" cannot be written, the old marker is removed instead:
    # a missing marker makes the watch refuse to act, which is the safe side.
    #
    # Rev13-W (2026-10-04): version 2 adds start_outcome - whether THIS boot's
    # running intent has been realized by a health-confirmed start. Once the
    # lifecycle is opted in, the watch may auto-recover only 'succeeded'; a v1
    # marker (no field, still surfaced by the reader) is indeterminate and
    # refuses recovery. Before the opt-in round 19 governs (R27-LOAD): a this-boot
    # running intent recovers on its own and start_outcome is not a criterion,
    # so an old v1 marker cannot silently strip the guard of an instance round
    # 19 would have recovered. 'running' keeps a prior same-boot 'succeeded'
    # (a re-start or a watch recovery attempt must not erase the boot's
    # confirmed start) and otherwise starts as 'pending'; 'stopped' keeps a
    # same-boot 'succeeded' for the record and writes 'never' otherwise.
    # at_utc stays the intent write time: the watch compares a graceful
    # shutdown against it, so confirming a start later must not move that anchor.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][ValidateSet('running', 'stopped')][string]$State
    )
    if (-not $script:GatewayDesiredStateEnabled) { return $true }
    if (-not (Test-GatewayDesiredStateTracked -Key $Key)) { return $true }
    $path = Get-GatewayDesiredStatePath -Key $Key
    $bootId = Get-GatewayBootIdentity
    $outcome = 'never'
    if ($State -eq 'running') { $outcome = 'pending' }
    try {
        $existing = [IO.File]::ReadAllText($path) | ConvertFrom-Json
        $existingOutcome = $null
        if ($null -ne $existing) {
            $bootProp = $existing.PSObject.Properties['boot_id']
            $outcomeProp = $existing.PSObject.Properties['start_outcome']
            if ($null -ne $bootProp -and $null -ne $outcomeProp -and $bootId -and
                    [string]$bootProp.Value -eq $bootId) {
                $existingOutcome = [string]$outcomeProp.Value
            }
        }
        if ($existingOutcome -eq 'succeeded') { $outcome = 'succeeded' }
    } catch {
        # Absent or unreadable old marker: keep the defaults computed above.
    }
    try {
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $path))
        $record = [ordered]@{
            version = 2
            key = $Key
            state = $State
            at_utc = [DateTimeOffset]::UtcNow.ToString('o')
            boot_id = $bootId
            source = Get-GatewayActionSource
            writer_pid = $PID
            start_outcome = $outcome
        }
        $tmp = '{0}.{1}.tmp' -f $path, $PID
        [IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $path -Force
        return $true
    } catch {
        if ($State -eq 'stopped') {
            try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop } catch { }
        }
        return $false
    }
}

function Read-GatewayDesiredState {
    # $null when absent, unreadable or malformed: callers treat that as
    # "intent unknown" and do nothing.
    #
    # Version 1 (round 19) records are still surfaced: they carry the intent,
    # the boot identity and the write time, only the Rev13-W start_outcome is
    # absent. The watch's outcome gate then fails closed once the lifecycle is
    # opted in, while before the opt-in the round-19 authorization applies.
    # Dropping a v1 record to "unknown" would silently strip the guard over an
    # instance round 19 would have recovered (R27-LOAD).
    param([Parameter(Mandatory = $true)][string]$Key)
    $path = Get-GatewayDesiredStatePath -Key $Key
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $record = [IO.File]::ReadAllText($path) | ConvertFrom-Json
        if ([int]$record.version -notin @(1, 2) -or [string]$record.key -ne $Key -or
                [string]$record.state -notin @('running', 'stopped') -or
                -not $record.at_utc) {
            return $null
        }
        return $record
    } catch {
        return $null
    }
}

function Set-GatewayDesiredStartOutcome {
    # Rev13-W: record how THIS boot's start attempt ended, separately from the
    # intent itself (the intent is never cleared by an outcome).
    #   succeeded - a health-confirmed start; the watch may auto-recover this
    #               boot within its hourly budget.
    #   failed    - the attempt ended without health (StartPending, Conflict,
    #               StartBlocked). A prior same-boot 'succeeded' is preserved:
    #               a failed recovery attempt must not erase the boot's
    #               confirmed start; the budget governs instead.
    # Never touches state or at_utc: the intent and its graceful-shutdown
    # comparison anchor stay untouched. Returns the EFFECTIVE outcome
    # ('succeeded'/'failed'), or $null when nothing was recorded (tracking
    # off, marker missing/stale/from another boot) - callers treat $null as
    # "undecidable, do not act on it".
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][ValidateSet('succeeded', 'failed')][string]$Outcome
    )
    if (-not $script:GatewayDesiredStateEnabled) { return $null }
    if (-not (Test-GatewayDesiredStateTracked -Key $Key)) { return $null }
    $path = Get-GatewayDesiredStatePath -Key $Key
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $bootId = Get-GatewayBootIdentity
    try {
        $record = [IO.File]::ReadAllText($path) | ConvertFrom-Json
        $versionProp = $record.PSObject.Properties['version']
        $keyProp = $record.PSObject.Properties['key']
        $stateProp = $record.PSObject.Properties['state']
        if ($null -eq $versionProp -or [int]$versionProp.Value -ne 2 -or
                $null -eq $keyProp -or [string]$keyProp.Value -ne $Key -or
                $null -eq $stateProp -or [string]$stateProp.Value -ne 'running') {
            return $null
        }
        $recordBootProp = $record.PSObject.Properties['boot_id']
        if ($null -eq $recordBootProp) { return $null }
        $recordBoot = [string]$recordBootProp.Value
        if (-not $bootId -or -not $recordBoot -or $recordBoot -ne $bootId) {
            return $null
        }
        $outcomeProp = $record.PSObject.Properties['start_outcome']
        $current = $null
        if ($null -ne $outcomeProp) { $current = [string]$outcomeProp.Value }
        if ($Outcome -eq 'failed' -and $current -eq 'succeeded') { return 'succeeded' }
        if ($current -eq $Outcome) { return $Outcome }
        if ($null -ne $outcomeProp) {
            $record.start_outcome = $Outcome
        } else {
            Add-Member -InputObject $record -NotePropertyName start_outcome `
                -NotePropertyValue $Outcome
        }
        $tmp = '{0}.{1}.tmp' -f $path, $PID
        [IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $path -Force
        return $Outcome
    } catch {
        return $null
    }
}

# ---------------------------------------------------------------------------
# Watch scheduling lifecycle (Rev13-W, 2026-10-04; maintainer-approved A)
# ---------------------------------------------------------------------------
# Opening intent for the private 8000 instance now includes its independent
# watch:
#   - a runtime start publishes the "running" intent FIRST, then makes the
#     watch task ready, launches, and records the start outcome afterwards;
#   - a deliberate stop commits the "stopped" intent first (racing a recovery
#     is decided by this order under the caller's service:chat2api lock), then
#     releases the watch task on the operator path itself - never inside the
#     watch's own processing;
#   - a controlled restart keeps both the intent and the scheduling;
#   - an abnormal exit clears NOTHING: the recovery entry stays armed and the
#     per-boot budget governs recovery attempts.
# The whole lifecycle is INERT until the deployment window opts in by creating
# the control marker (Enable-GatewayWatchLifecycleControl, done by
# lib\register-chat2api-watch.ps1): shipping this code must not mutate the
# real scheduled task outside that window. Status reading never mutates.
# The RECOVERY semantics of the marker are opt-in too: the strict
# start_outcome gate (watch-chat2api.ps1 Test-StartOutcomeRecoverable) applies
# only while the control marker exists; before that the watch keeps round 19's
# authorization (a this-boot "running" intent recovers on its own, start
# outcome is not a criterion), so an old v1 marker on disk cannot silently
# strip the guard of an instance round 19 would have recovered.
# Read-GatewayDesiredState therefore keeps surfacing version 1 records: the
# opt-in gate then fails closed on the missing start_outcome, and the boot check
# still rejects a previous-boot intent. The compatibility path is covered by
# isolated fixtures in watch-chat2api.ps1 -SelfTest, which write REAL v1/v2
# marker files and drive the production reader (control marker pointed at its own
# temp directory).
# Registration, scheduling and current execution are presented as separate
# facts - never collapsed into one "running" word.

$script:GatewayWatchSchedulerHook = $null

function Get-GatewayWatchTaskName {
    param([Parameter(Mandatory = $true)][string]$Key)
    $desc = $script:GatewayServiceDescriptors[$Key]
    if ($null -eq $desc) { return $null }
    $prop = $desc.PSObject.Properties['WatchTaskName']
    if ($null -eq $prop -or -not $prop.Value) { return $null }
    return [string]$prop.Value
}

function Get-GatewayWatchControlPath {
    $root = [string]$env:AIGATEWAY_PUBLIC_WATCH_DIR
    if (-not $root) {
        $root = Join-Path $env:LOCALAPPDATA 'AIGatewayPublic\watch'
    }
    return Join-Path $root 'chat2api-watch-lifecycle-control.enabled'
}

function Test-GatewayWatchLifecycleControl {
    # Deployment opt-in: the scheduled-task lifecycle is only driven once the
    # control marker exists; before that the runtime behaves exactly as
    # round 19 did.
    return (Test-Path -LiteralPath (Get-GatewayWatchControlPath))
}

function Enable-GatewayWatchLifecycleControl {
    $path = Get-GatewayWatchControlPath
    $dir = Split-Path -Parent $path
    if ($dir) { [void][IO.Directory]::CreateDirectory($dir) }
    if (Test-Path -LiteralPath $path) { return $true }
    $record = [ordered]@{
        version = 1
        enabled_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        writer_pid = $PID
    }
    [IO.File]::WriteAllText($path, ($record | ConvertTo-Json -Compress),
        (New-Object System.Text.UTF8Encoding($false)))
    return (Test-Path -LiteralPath $path)
}

function Disable-GatewayWatchLifecycleControl {
    $path = Get-GatewayWatchControlPath
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
    return -not (Test-Path -LiteralPath $path)
}

function New-GatewayWatchSchedulingReport {
    # Uniform report for a scheduling attempt. $null Registered/SchedulingEnabled
    # means "not determinable" - never reported as enabled.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$TaskName,
        # Untyped on purpose: $null means "not determinable" and must survive
        # (a [bool] cast would coerce it to $false and fake a decision).
        [AllowNull()]$Registered,
        [AllowNull()]$SchedulingEnabled,
        [Parameter(Mandatory = $true)][bool]$ControlEnabled,
        [Parameter(Mandatory = $true)][string]$Action,
        [string]$Detail = ''
    )
    return [pscustomobject]@{
        Key = $Key; TaskName = $TaskName
        Registered = $Registered; SchedulingEnabled = $SchedulingEnabled
        ControlEnabled = $ControlEnabled; Action = $Action; Detail = $Detail
    }
}

function Add-GatewayWatchToResult {
    # Carries the watch-scheduling report on a lifecycle result so the console
    # and CLI can show "running but unwatched" honestly instead of one status
    # word. No-op for untracked services ($null report).
    param($Result, $Watch)
    if ($null -ne $Watch -and $null -ne $Result -and
            -not $Result.PSObject.Properties['Watch']) {
        Add-Member -InputObject $Result -NotePropertyName Watch -NotePropertyValue $Watch
    }
    return $Result
}

function Set-GatewayWatchScheduling {
    # Ensures the independent watch entry is ready ($true) or released
    # ($false). No-op (returns $null) unless: desired-state tracking is opted
    # in, the descriptor carries a watch task name, and the deployment control
    # marker exists. A scheduling failure is honest report material ("running
    # but unwatched"), never a start/stop failure.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][bool]$Enabled,
        [scriptblock]$TaskScheduler   # TEST-ONLY seam; production passes nothing
    )
    if (-not $script:GatewayDesiredStateEnabled) { return $null }
    if (-not (Test-GatewayDesiredStateTracked -Key $Key)) { return $null }
    $taskName = Get-GatewayWatchTaskName -Key $Key
    if (-not $taskName) { return $null }
    if (-not (Test-GatewayWatchLifecycleControl)) {
        return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
            -ControlEnabled $false -Action 'None' `
            -Detail '守护调度控制未启用（部署窗口未开启，不改动计划任务）'
    }
    $scheduler = $TaskScheduler
    if ($null -eq $scheduler) { $scheduler = $script:GatewayWatchSchedulerHook }
    try {
        if ($null -ne $scheduler) { return & $scheduler $taskName $Enabled }
        try {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        } catch {
            return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
                -Registered $false -SchedulingEnabled $false -ControlEnabled $true `
                -Action 'None' -Detail '守护计划任务未注册'
        }
        $taskState = 'Unknown'
        $stateProp = $task.PSObject.Properties['State']
        if ($null -ne $stateProp) { $taskState = [string]$stateProp.Value }
        if ($Enabled -and $taskState -eq 'Disabled') {
            Enable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null
            return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
                -Registered $true -SchedulingEnabled $true -ControlEnabled $true `
                -Action 'Enable'
        }
        if (-not $Enabled -and $taskState -ne 'Disabled') {
            Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null
            return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
                -Registered $true -SchedulingEnabled $false -ControlEnabled $true `
                -Action 'Disable'
        }
        return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
            -Registered $true -SchedulingEnabled $Enabled -ControlEnabled $true `
            -Action 'None'
    } catch {
        return New-GatewayWatchSchedulingReport -Key $Key -TaskName $taskName `
            -ControlEnabled $true -Action 'None' `
            -Detail ('守护调度切换失败：' + $_.Exception.GetType().Name +
                '；可能存在运行但无守护')
    }
}

function Add-GatewayStartFailureRelease {
    # After a start attempt that did not confirm health: when this boot's
    # record still authorizes recovery (a preserved 'succeeded'), the watch
    # entry stays armed; otherwise the entry is released - there is nothing
    # the watch may recover, and an unconfirmed start must not keep a
    # once-a-minute probing task alive.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [AllowNull()]$Watch
    )
    $effective = Set-GatewayDesiredStartOutcome -Key $Key -Outcome 'failed'
    if ($effective -ne 'failed') { return $Watch }
    return (Set-GatewayWatchScheduling -Key $Key -Enabled $false)
}

function Get-GatewayWatchLifecycleStatus {
    # Four separate facts about the private watch, never one "running" word:
    # registration, scheduling, current execution, and the service itself.
    # "运行但无守护" is named explicitly when the service runs unwatched.
    param(
        [string]$Key = 'chat2api',
        [scriptblock]$TaskStateProvider,   # TEST-ONLY seam: (taskName) -> { Registered; State }
        [scriptblock]$HttpChecker
    )
    $taskName = Get-GatewayWatchTaskName -Key $Key
    $serviceRunning = [bool](Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker)
    $control = Test-GatewayWatchLifecycleControl
    $registered = $null
    $schedulingEnabled = $null
    $executing = $null
    if ($taskName) {
        $taskState = $null
        if ($TaskStateProvider) {
            $taskState = & $TaskStateProvider $taskName
        } else {
            try {
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
                $taskState = [pscustomobject]@{ Registered = $true; State = [string]$task.State }
            } catch {
                $taskState = [pscustomobject]@{ Registered = $false; State = $null }
            }
        }
        if ($null -ne $taskState) {
            $registeredProp = $taskState.PSObject.Properties['Registered']
            if ($null -ne $registeredProp) { $registered = [bool]$registeredProp.Value }
            if ($registered) {
                $stateText = ''
                $stateProp = $taskState.PSObject.Properties['State']
                if ($null -ne $stateProp) { $stateText = [string]$stateProp.Value }
                $schedulingEnabled = ($stateText -ne 'Disabled')
                $executing = ($stateText -eq 'Running')
            } else {
                $schedulingEnabled = $false
                $executing = $false
            }
        }
    }
    $issues = @()
    if ($serviceRunning -and ($registered -eq $false -or $schedulingEnabled -eq $false)) {
        if ($registered -eq $false) { $issues += '运行但无守护：服务在运行，守护未注册' }
        else { $issues += '运行但无守护：服务在运行，守护调度未启用' }
    }
    if ($null -ne $schedulingEnabled -and $schedulingEnabled -and -not $serviceRunning) {
        $issues += '守护调度已启用但服务未运行（等待探测/恢复判定）'
    }
    if (-not $control) { $issues += '守护调度控制未启用（部署窗口未开启）' }
    return [pscustomobject]@{
        Key = $Key; TaskName = $taskName
        ControlEnabled = $control
        Registered = $registered
        SchedulingEnabled = $schedulingEnabled
        Executing = $executing
        ServiceRunning = $serviceRunning
        Detail = ($issues -join '；')
    }
}

function Start-GatewayService {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker,
        [scriptblock]$Launcher,
        [scriptblock]$Sleeper
    )

    $desc = $script:GatewayServiceDescriptors[$Key]
    # Rev13-W order: publish the intent, then make the independent watch entry
    # ready, then launch; the start outcome is recorded after the launch is
    # verified (below). A failed intent write skips the scheduling step - the
    # marker stays unknown and the watch refuses to act, so arming it would
    # only invite an enable/disarm fight.
    $intentWritten = [bool](Write-GatewayDesiredState -Key $Key -State 'running')
    $watch = $null
    if ($intentWritten) {
        $watch = Set-GatewayWatchScheduling -Key $Key -Enabled $true
    }

    if ($null -eq $HttpChecker -and $null -eq $Launcher) {
        $inspection = Get-GatewayServiceInspection -Key $Key
        if ($inspection.State -eq 'Conflict') {
            $watch = Add-GatewayStartFailureRelease -Key $Key -Watch $watch
            return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                Key = $Key; Status = 'Conflict'; Action = 'None'; Detail = $inspection.Detail
            }) -Watch $watch)
        }
        if ($inspection.State -eq 'Running') {
            [void](Set-GatewayDesiredStartOutcome -Key $Key -Outcome 'succeeded')
            return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                Key = $Key; Status = 'AlreadyRunning'; Action = 'None'
            }) -Watch $watch)
        }
        if ($inspection.State -in @('Residual', 'Degraded')) {
            # Cleanup on the way to a start: intent stays "running".
            $cleanup = Stop-GatewayService -Key $Key -KeepDesiredState
            if ($cleanup.Status -ne 'Stopped') {
                $watch = Add-GatewayStartFailureRelease -Key $Key -Watch $watch
                return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                    Key = $Key; Status = 'StartBlocked'; Action = 'None'
                    Detail = '受管残留进程清理失败'
                }) -Watch $watch)
            }
        }
    }

    # Idempotent: don't restart if already healthy.
    if (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker) {
        [void](Set-GatewayDesiredStartOutcome -Key $Key -Outcome 'succeeded')
        return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
            Key = $Key; Status = 'AlreadyRunning'; Action = 'None'
        }) -Watch $watch)
    }

    $startPath = Resolve-GatewayStartPath -Key $Key

    if ($null -ne $Launcher) {
        & $Launcher $Key $startPath $desc
    } else {
        switch ($desc.StartAction) {
            'HiddenBatch' {
                # The child inherits this environment: the launcher learns it
                # runs hidden (so it records its exit instead of pausing in an
                # invisible window) and the service learns who started it.
                $childEnv = @{ AIGATEWAY_PUBLIC_HIDDEN_LAUNCH = '1' }
                $sourceProp = $desc.PSObject.Properties['LaunchSourceEnv']
                if ($null -ne $sourceProp -and $sourceProp.Value) {
                    $childEnv[[string]$sourceProp.Value] = Get-GatewayActionSource
                }
                $saved = @{}
                foreach ($name in $childEnv.Keys) {
                    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
                    [Environment]::SetEnvironmentVariable($name, $childEnv[$name], 'Process')
                }
                try {
                    Start-Process -FilePath 'cmd.exe' `
                        -ArgumentList @('/d', '/c', "`"$startPath`"") `
                        -WorkingDirectory (Split-Path -Parent $startPath) `
                        -WindowStyle Hidden
                } finally {
                    foreach ($name in $saved.Keys) {
                        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
                    }
                }
            }
        }
    }

    # Poll up to the service-specific timeout. External launchers can need
    # materially longer than two seconds on a cold start.
    $healthy = $false
    $timeoutSeconds = [Math]::Max(1, [int]$desc.StartTimeoutSeconds)
    for ($elapsed = 0; $elapsed -lt $timeoutSeconds; $elapsed++) {
        if ($null -ne $Sleeper) {
            & $Sleeper 1
        } else {
            Start-Sleep -Seconds 1
        }
        if (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker) {
            $healthy = $true
            break
        }
    }
    if ($healthy) {
        [void](Set-GatewayDesiredStartOutcome -Key $Key -Outcome 'succeeded')
    } else {
        # An unconfirmed start releases the watch entry only when this boot has
        # no preserved 'succeeded' (Add-GatewayStartFailureRelease decides).
        $watch = Add-GatewayStartFailureRelease -Key $Key -Watch $watch
    }
    return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
        Key = $Key
        Status = if ($healthy) { 'Started' } else { 'StartPending' }
        Action = 'Start'
    }) -Watch $watch)
}

function Stop-GatewayService {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker,
        [scriptblock]$Stopper,
        [scriptblock]$Sleeper,
        # Restart and start-time cleanup stop on the way to a start; only a
        # stop meant as a stop records the "stopped" intent.
        [switch]$KeepDesiredState
    )

    $desc = $script:GatewayServiceDescriptors[$Key]
    # Rev13-W: a deliberate stop commits the "stopped" intent FIRST (under the
    # caller's service:chat2api lock this order decides any racing recovery),
    # then releases the watch entry on the operator path itself - never inside
    # the watch's own processing. A cleanup/restart stop (-KeepDesiredState)
    # touches neither the intent nor the scheduling.
    $watch = $null
    if (-not $KeepDesiredState) {
        [void](Write-GatewayDesiredState -Key $Key -State 'stopped')
        $watch = Set-GatewayWatchScheduling -Key $Key -Enabled $false
    }

    if ($null -eq $HttpChecker -and $null -eq $Stopper) {
        $inspection = Get-GatewayServiceInspection -Key $Key
        if ($inspection.State -eq 'Conflict') {
            return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                Key = $Key; Status = 'Conflict'; Action = 'None'; Detail = $inspection.Detail
            }) -Watch $watch)
        }
        if ($inspection.State -eq 'Stopped') {
            return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                Key = $Key; Status = 'AlreadyStopped'; Action = 'None'
            }) -Watch $watch)
        }
    }

    $wasHealthy = Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker
    # Descriptors without StopWhenUnhealthy default to $false (the documented
    # edge in Restart-GatewayService); strict mode would throw on direct
    # property access, so resolve the optional field defensively.
    $stopWhenUnhealthy = $false
    $stopWhenUnhealthyProp = $desc.PSObject.Properties['StopWhenUnhealthy']
    if ($null -ne $stopWhenUnhealthyProp) { $stopWhenUnhealthy = [bool]$stopWhenUnhealthyProp.Value }
    if (-not $wasHealthy -and -not $stopWhenUnhealthy) {
        return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
            Key = $Key; Status = 'AlreadyStopped'; Action = 'None'
        }) -Watch $watch)
    }

    # Exit observability (round 18): the scoped stop reports the instances it
    # actually terminated; a null value means "this strategy did not report".
    $scoped = $null

    if ($null -ne $Stopper) {
        & $Stopper $Key $desc
    } else {
        switch ($desc.StopStrategy) {
            'GracefulHttp' {
                # The graceful request itself is already instance-scoped: only
                # the private instance's own control token opens its
                # /admin/shutdown (the share backend reads
                # CHAT2API_CONTROL_TOKEN_FILE from its own data dir,
                # console_share_production.py:1236-1246). The FORCE-KILL below
                # is the part that used to select the share backend, so it runs
                # through the verified stop scope.
                $gracefulRequested = $false
                try {
                    $previousProgressPreference = $ProgressPreference
                    try {
                        $ProgressPreference = 'SilentlyContinue'
                        Invoke-WebRequest -Method Post -Uri $desc.ShutdownUrl `
                            -Headers (Get-LocalControlHeader) `
                            -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop | Out-Null
                        $gracefulRequested = $true
                    } finally {
                        $ProgressPreference = $previousProgressPreference
                    }
                } catch { }

                # A process waiting for startup authentication has not opened
                # its port yet, so the graceful endpoint cannot receive the
                # request. Clean up only the verified private instance — the
                # same scope decision runs whether or not the graceful request
                # was accepted, so a successful shutdown does not fall back to
                # a broad force-stop.
                if ($gracefulRequested) {
                    Start-Sleep -Seconds 3
                }
                $scoped = Stop-GatewayScopedProcesses -Key $Key -Descriptor $desc
                if ($scoped.Status -eq 'StopRefused') {
                    return (Add-GatewayWatchToResult -Result $scoped -Watch $watch)
                }
            }
            'CommandLine' {
                $scoped = Stop-GatewayScopedProcesses -Key $Key -Descriptor $desc
                if ($scoped.Status -eq 'StopRefused') {
                    return (Add-GatewayWatchToResult -Result $scoped -Watch $watch)
                }
            }
        }
    }

    if ($null -ne $Sleeper) {
        & $Sleeper 2
    } else {
        Start-Sleep -Seconds 2
    }

    $healthy = Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker
    $out = [pscustomobject]@{
        Key = $Key
        Status = if ($healthy) { 'StopPending' } else { 'Stopped' }
        Action = 'Stop'
    }
    # Exit observability (round 18): carry the PIDs the scoped stop actually
    # terminated, so the console operation record can name the instances instead
    # of only a status word.
    if ($null -ne $scoped -and $scoped.PSObject.Properties['TerminatedProcessIds']) {
        Add-Member -InputObject $out -NotePropertyName TerminatedProcessIds `
            -NotePropertyValue @($scoped.TerminatedProcessIds)
    }
    # Rev13-W: a StopPending still released the watch entry above - the honest
    # picture ("service may still run, watch released") travels with the result.
    return (Add-GatewayWatchToResult -Result $out -Watch $watch)
}

function Restart-GatewayService {
    # Stop then Start through the same descriptor primitives: the way to load
    # new gateway code without a manual port kill. A failed stop surfaces
    # honestly (no launch attempt); an already-stopped service still gets the
    # start attempt (start's own Residual/Degraded cleanup applies). Note the
    # known edge: a Degraded-but-managed process without StopWhenUnhealthy
    # returns AlreadyStopped from stop and cannot be cleaned here — that
    # surfaces as StartBlocked from start's inspection, never as silent success.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker,
        [scriptblock]$Launcher,
        [scriptblock]$Stopper,
        [scriptblock]$Sleeper
    )

    # The stop half keeps the intent; Start-GatewayService records "running"
    # AFTER the stop, so an instance's graceful shutdown is never newer than
    # the intent that replaced it (lib\watch-chat2api.ps1 relies on that order).
    $stopped = Stop-GatewayService -Key $Key -HttpChecker $HttpChecker `
        -Stopper $Stopper -Sleeper $Sleeper -KeepDesiredState
    if ($stopped.Status -notin @('Stopped', 'AlreadyStopped')) {
        return [pscustomobject]@{
            Key = $Key; Status = $stopped.Status; Action = 'Restart'
            Detail = '停止未完成，未尝试重启'
        }
    }
    $started = Start-GatewayService -Key $Key -HttpChecker $HttpChecker `
        -Launcher $Launcher -Sleeper $Sleeper
    $out = [pscustomobject]@{
        Key = $Key
        Status = if ($started.Status -in @('Started', 'AlreadyRunning')) { 'Restarted' } else { $started.Status }
        Action = 'Restart'
    }
    # Exit observability (round 18): name the instances the stop half actually
    # terminated, so a restart record can be tied back to real PIDs instead of a
    # bare status word.
    if ($stopped.PSObject.Properties['TerminatedProcessIds']) {
        Add-Member -InputObject $out -NotePropertyName TerminatedProcessIds `
            -NotePropertyValue @($stopped.TerminatedProcessIds)
    }
    # Rev13-W: the restart's stop half keeps the intent and the scheduling
    # (-KeepDesiredState); the start half owns both, so its watch report - and
    # any "release because nothing is recoverable" it decided - travels here.
    if ($started.PSObject.Properties['Watch']) {
        Add-Member -InputObject $out -NotePropertyName Watch `
            -NotePropertyValue $started.Watch
    }
    return $out
}

function Start-AllGatewayServices {
    param(
        [scriptblock]$HttpChecker,
        [scriptblock]$Launcher,
        [scriptblock]$Sleeper,
        [scriptblock]$OnProgress
    )

    $results = @()
    foreach ($key in $script:GatewayStartOrder) {
        if ($null -ne $OnProgress) { & $OnProgress $key 'Starting' }
        try {
            $result = Start-GatewayService -Key $key -HttpChecker $HttpChecker -Launcher $Launcher -Sleeper $Sleeper
        } catch {
            $result = [pscustomobject]@{ Key = $key; Status = 'Failed'; Action = 'Start'; Detail = $_.Exception.Message }
        }
        $results += $result
        if ($null -ne $OnProgress) { & $OnProgress $key $result.Status }
        if ($result.Status -eq 'StartPending') {
            # Continue starting remaining services; a single failure is visible.
        }
    }
    return $results
}

function Stop-AllGatewayServices {
    param(
        [scriptblock]$HttpChecker,
        [scriptblock]$Stopper,
        [scriptblock]$Sleeper,
        [scriptblock]$OnProgress
    )

    $results = @()
    foreach ($key in $script:GatewayStopOrder) {
        if ($null -ne $OnProgress) { & $OnProgress $key 'Stopping' }
        try {
            $result = Stop-GatewayService -Key $key -HttpChecker $HttpChecker -Stopper $Stopper -Sleeper $Sleeper
        } catch {
            $result = [pscustomobject]@{ Key = $key; Status = 'Failed'; Action = 'Stop'; Detail = $_.Exception.Message }
        }
        $results += $result
        if ($null -ne $OnProgress) { & $OnProgress $key $result.Status }
    }
    return $results
}

# ---------------------------------------------------------------------------
# Proxy environment
# ---------------------------------------------------------------------------

function Get-GatewayProxyEnvironment {
    # All services started by the console clear proxy variables and set
    # NO_PROXY=* so traffic goes direct (no VPN/system proxy).
    # PowerShell hashtables are case-insensitive, so we return a list of
    # name/value pairs that callers set explicitly on the process environment.
    return @(
        [pscustomobject]@{ Name = 'HTTP_PROXY';  Value = '' }
        [pscustomobject]@{ Name = 'HTTPS_PROXY'; Value = '' }
        [pscustomobject]@{ Name = 'ALL_PROXY';   Value = '' }
        [pscustomobject]@{ Name = 'http_proxy';  Value = '' }
        [pscustomobject]@{ Name = 'https_proxy'; Value = '' }
        [pscustomobject]@{ Name = 'all_proxy';   Value = '' }
        [pscustomobject]@{ Name = 'NO_PROXY';    Value = '*' }
        [pscustomobject]@{ Name = 'no_proxy';    Value = '*' }
    )
}

# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

function Get-GatewayServiceDescriptors {
    return $script:GatewayServiceDescriptors
}

function Get-GatewayStartOrder {
    return $script:GatewayStartOrder
}

function Get-GatewayStopOrder {
    return $script:GatewayStopOrder
}
