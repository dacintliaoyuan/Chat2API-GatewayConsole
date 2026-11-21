# Gateway runtime module: single source of truth for service identity,
# health checks, start/stop strategies and startup order.
#
# Two active services:
#   chat2api    8000  /health       UM 模型直连
#   usage       8001  /health       用量统计聚合
#
# This module owns the descriptors and the start/stop/status implementation.
# Adapters (menu, BAT, VBS) delegate here and only handle presentation.

# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------

# #133 A2: the local 8000 shape is split into two private processes of the same
# Rust binary. The worker owns model processing and publishes its ephemeral
# loopback port in WorkerPortFile; the ingress owns 127.0.0.1:8000 and forwards
# to that port. Both identities are deliberately narrow: a worker is only the
# process carrying --mode worker, an ingress only the one carrying --mode ingress.
# The production single-process identity stays for the transition and the Python
# identity stays for the offline rollback.
$script:Chat2ApiWorkerIdentity = [pscustomobject]@{
    Component = 'worker'
    ProcessName = 'um-gateway-candidate.exe'
    CommandLinePattern = '(^|\s)um-gateway-candidate(\.exe)?(\s|$)'
    InstanceScript = 'rust\target\release\um-gateway-candidate.exe'
    RequiredArgumentPatterns = @('--mode worker(\s|$)')
    ForeignArgumentPatterns = @(
        '--mode ingress(\s|$)',
        '--mode candidate(\s|$)',
        '--mode production(\s|$)',
        '--role cloud(\s|$)',
        '--share-config'
    )
    # The worker binds --port 0; its real port is published in WorkerPortFile and
    # must never be compared with the descriptor's 8000.
    Portless = $true
}
$script:Chat2ApiIngressIdentity = [pscustomobject]@{
    Component = 'ingress'
    ProcessName = 'um-gateway-candidate.exe'
    CommandLinePattern = '(^|\s)um-gateway-candidate(\.exe)?(\s|$)'
    InstanceScript = 'rust\target\release\um-gateway-candidate.exe'
    RequiredArgumentPatterns = @('--mode ingress(\s|$)')
    ForeignArgumentPatterns = @(
        '--mode worker(\s|$)',
        '--mode candidate(\s|$)',
        '--mode production(\s|$)',
        '--role cloud(\s|$)',
        '--share-config'
    )
}

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
        # #80: the local 8000 launcher is the Rust gateway binary. The Python
        # entrypoint stays in start_chat2api.bat as the rollback target: reverting
        # this one field (StartPath) restores Python without a reinstall or a
        # port change. StartArguments/StartWorkingDirectory are harmless for a
        # batch launcher (the batch ignores them) so the rollback stays a
        # one-line change.
        StartPath    = 'rust\target\release\um-gateway-candidate.exe'
        # #108: the ledger and runtime log paths the local 8001 console reads.
        # Python wrote these two files by default; the Rust gateway keeps them
        # opt-in through --usage-log/--service-log so a missing flag would leave
        # today's request and log pages empty.
        # #133 A2: this single-process argument line stays as the transition
        # shape; a .exe StartPath with StartComponents uses the component
        # arguments below instead. A .bat StartPath (Python rollback) ignores it.
        StartArguments = '--mode production --port 8000 --role local --upstream-base-url https://chat.genai.um.edu.mo --control-token-file data/admin-control-token --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log'
        # #133 A2: worker first, ingress second. Placeholders are expanded at
        # launch: {worker-port-file} is the absolute WorkerPortFile path,
        # {desired-state-file} the operator intent marker, {boot-id} the current
        # boot identity. The ingress also reads the two gateway environment
        # variables the runtime injects into both children.
        StartComponents = @(
            [pscustomobject]@{
                Name = 'worker'
                StartArguments = @(
                    '--mode', 'worker', '--port', '0', '--role', 'local',
                    '--worker-port-file', '{worker-port-file}',
                    '--upstream-base-url', 'https://chat.genai.um.edu.mo',
                    '--control-token-file', 'data/admin-control-token',
                    '--usage-log', 'data/usage-chat2api.jsonl',
                    '--service-log', 'chat2api/chat2api.log')
                HealthUrl = 'http://127.0.0.1:{worker-port}/health'
            },
            [pscustomobject]@{
                Name = 'ingress'
                StartArguments = @(
                    '--mode', 'ingress', '--port', '8000',
                    '--worker-port-file', '{worker-port-file}',
                    '--desired-state-file', '{desired-state-file}',
                    '--boot-id', '{boot-id}',
                    '--control-token-file', 'data/admin-control-token')
                HealthUrl = 'http://127.0.0.1:8000/health'
            }
        )
        # Relative to the repository root; the worker atomically writes the
        # decimal port it actually bound (--port 0).
        WorkerPortFile = 'data/chat2api-worker.port'
        # The ingress /health proxies the worker and answers 503 while the worker
        # is missing. The 8001 console keeps the historical "any HTTP response
        # proves a listener" reachability semantics.
        HealthRequiresOk = $true
        # #133 A2: the watch's process query confirms both component identities
        # (worker and ingress); either missing means the pair is absent.
        RecoveryIdentity = $script:Chat2ApiWorkerIdentity
        # Repository root keeps the Rust role defaults identical to Python:
        # current_dir()/chat2api/token.json and current_dir()/chat2api/.edge-profile.
        StartWorkingDirectory = '.'
        StartTimeoutSeconds = 20
        # #133 A2: bounded wait for the worker to exit after its graceful
        # shutdown POST, before the verified force-stop fallback runs.
        StopTimeoutSeconds = 5
        StopStrategy = 'GracefulHttp'
        StopWhenUnhealthy = $true
        StopProcessName = 'python.exe'
        StopCommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
        # Round 18 (2026-09-29) stop-scope identity. The entrypoint token above
        # also matched another chat2api.py instance
        # --profile <share>\edge-profile, started by
        # console/share_production.py:1226-1234), so a private stop force-stopped
        # a share instance. The private launcher (start_chat2api.bat) passes only
        # --base-url, so these two facts positively identify the private
        # instance; --port, when declared, must agree with Port. See
        # Get-GatewayStopScopeClass for the full rule.
        StopInstanceScript = 'chat2api\chat2api.py'
        StopForeignArgumentPatterns = @('--profile')
        # #80: the private 8000 instance can be either runtime. The Rust
        # identity is deliberately narrower than the Python one: it requires the
        # production port (declared --port must equal 8000) and excludes the
        # candidate and cloud shapes. The Python identity remains for rollback.
        StopIdentities = @(
            [pscustomobject]@{
                ProcessName = 'python.exe'
                CommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
                InstanceScript = 'chat2api\chat2api.py'
                ForeignArgumentPatterns = @('--profile')
            },
            [pscustomobject]@{
                ProcessName = 'um-gateway-candidate.exe'
                CommandLinePattern = '(^|\s)um-gateway-candidate(\.exe)?(\s|$)'
                InstanceScript = 'rust\target\release\um-gateway-candidate.exe'
                ForeignArgumentPatterns = @(
                    '--profile',
                    '--mode candidate',
                    '--mode worker',
                    '--mode ingress',
                    '--role cloud',
                    '--share-config'
                )
            },
            $script:Chat2ApiWorkerIdentity,
            $script:Chat2ApiIngressIdentity
        )
        OwnerProcessName = 'python.exe'
        OwnerCommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
        OwnerIdentities = @(
            [pscustomobject]@{
                ProcessName = 'python.exe'
                CommandLinePattern = '(^|\s)chat2api\.py(\s|$)'
            },
            [pscustomobject]@{
                ProcessName = 'um-gateway-candidate.exe'
                # The two split forms are named explicitly so the owner test
                # proves both are recognized, then the broad pattern keeps the
                # legacy single-process shape recognizable.
                CommandLinePattern = 'um-gateway-candidate(\.exe)?\b(?=.*--mode worker(\s|$))'
            },
            [pscustomobject]@{
                ProcessName = 'um-gateway-candidate.exe'
                CommandLinePattern = 'um-gateway-candidate(\.exe)?\b(?=.*--mode ingress(\s|$))'
            },
            [pscustomobject]@{
                ProcessName = 'um-gateway-candidate.exe'
                # Owner matching is broad (the inspection path only asks whether
                # this is the managed program); a quoted or path-qualified
                # command line must match here.
                CommandLinePattern = 'um-gateway-candidate(\.exe)?\b'
            }
        )
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
        WatchTaskName = 'SelfUseAI AI Gateway Chat2API Watch'
    }
    'usage' = [pscustomobject]@{
        Key          = 'usage'
        DisplayName  = '用量统计'
        Port         = 8001
        HealthPath   = '/health'
        StartOrder   = 6
        OpenUrl      = 'http://127.0.0.1:8001/'
        StartAction  = 'HiddenBatch'
        StopAction   = 'Port'
        # #85: the local 8001 launcher is the Rust console binary. The Python
        # entrypoint stays in start_usage_service.bat as the rollback target:
        # reverting this one field (StartPath) restores Python without a
        # reinstall or a port change. StartArguments/StartWorkingDirectory are
        # harmless for a batch launcher (the batch ignores them) so the rollback
        # stays a one-line change.
        StartPath    = 'rust\target\release\um-console-candidate.exe'
        StartArguments = '--mode production --bind-port 8001 --report-port 8001 --role local --gateway-port 8000 --state-file data/console-state.sqlite3 --dist-dir web/dist --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log'
        # Repository root keeps the Rust local role defaults identical to
        # Python: data/console-state.sqlite3, data/usage-chat2api.jsonl,
        # data/usage-session-code, data/admin-control-token, web/dist.
        StartWorkingDirectory = '.'
        # `--role local` now applies the Rust local path defaults (state file,
        # lock dir, share store, control token, session code, share backend and
        # watch dir), so no UM_SHARE_ROLE environment override is needed. A
        # batch rollback ignores StartArguments/StartWorkingDirectory.
        StartTimeoutSeconds = 10
        StopStrategy = 'Port'
        # #85: the private 8001 instance can be either runtime. The Rust
        # identity is deliberately narrower than the Python one: it requires the
        # production local shape and excludes the candidate/cloud shapes used for
        # parity, so only the launcher's own instance is ever stopped.
        StopProcessName = 'python.exe'
        StopCommandLinePattern = '(^|\s)usagesrv\.py(\s|$)'
        StopInstanceScript = 'usagesrv.py'
        StopIdentities = @(
            [pscustomobject]@{
                ProcessName = 'python.exe'
                CommandLinePattern = '(^|\s)usagesrv\.py(\s|$)'
                InstanceScript = 'usagesrv.py'
                ForeignArgumentPatterns = @('--profile')
            },
            [pscustomobject]@{
                ProcessName = 'um-console-candidate.exe'
                CommandLinePattern = '(^|\s)um-console-candidate(\.exe)?(\s|$)'
                InstanceScript = 'rust\target\release\um-console-candidate.exe'
                ForeignArgumentPatterns = @(
                    '--mode candidate(\s|$)',
                    '--role cloud(\s|$)',
                    '--bind-port 8002(\s|$)',
                    '--report-port 8002(\s|$)'
                )
            }
        )
        OwnerProcessName = 'python.exe'
        OwnerCommandLinePattern = '(^|\s)usagesrv\.py(\s|$)'
        OwnerIdentities = @(
            [pscustomobject]@{
                ProcessName = 'python.exe'
                CommandLinePattern = '(^|\s)usagesrv\.py(\s|$)'
            },
            [pscustomobject]@{
                ProcessName = 'um-console-candidate.exe'
                # Owner matching is broad (the inspection path only asks
                # whether this is the managed program); a quoted or
                # path-qualified command line must match here.
                CommandLinePattern = 'um-console-candidate(\.exe)?\b'
            }
        )
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
        'HiddenExecutable' { return (Join-Path (Get-GatewayRoot) $desc.StartPath) }
        default {
            throw "Unknown start action for $Key"
        }
    }
}

function Test-GatewayCredentialsAcl {
    # #120: the machine-local credentials file is readable only by the current
    # user. Any other Allow entry - inherited or explicit - is too broad; an
    # unreadable ACL or an unresolved identity fails closed. A file with no
    # current-user Allow entry is unusable.
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        if ($null -eq $identity -or $null -eq $identity.User) { return $false }
        $currentSid = $identity.User.Value
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        $currentUserAllowed = $false
        foreach ($rule in @($acl.Access)) {
            if ($null -eq $rule -or $rule.AccessControlType -ne 'Allow') { continue }
            $ruleSid = $null
            try {
                $ruleSid = $rule.IdentityReference.Translate(
                    [System.Security.Principal.SecurityIdentifier]).Value
            } catch {
                return $false
            }
            if ($ruleSid -ne $currentSid) { return $false }
            $currentUserAllowed = $true
        }
        return $currentUserAllowed
    } catch {
        return $false
    }
}

function Get-GatewayLocalUmCredentials {
    # #120: the machine-local UM account for the private 8000 instance. The
    # explicit CHAT2API_UM_ACCOUNT / CHAT2API_UM_PASSWORD pair wins; the
    # credentials file (ADR 0012, shared with the desktop app) is read only
    # when that pair is incomplete. The file must be a complete version 1
    # record and its ACL must allow only the current user. The status is
    # desensitized: file content and parser errors never enter a message.
    $envAccount = [string]$env:CHAT2API_UM_ACCOUNT
    $envPassword = [string]$env:CHAT2API_UM_PASSWORD
    if ($envAccount.Trim() -and $envPassword.Trim()) {
        return [pscustomobject]@{
            Status = 'credentials_present'; Account = $envAccount; Password = $envPassword
        }
    }
    $localAppData = [string]$env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        return [pscustomobject]@{
            Status = 'credentials_missing'; Account = $null; Password = $null
        }
    }
    $path = Join-Path $localAppData 'Chat2API\credentials.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [pscustomobject]@{
            Status = 'credentials_missing'; Account = $null; Password = $null
        }
    }
    $errorCount = $Error.Count
    try {
        $record = [IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $record) { throw 'credentials record is null' }
        $versionProp = $record.PSObject.Properties['version']
        $accountProp = $record.PSObject.Properties['account']
        $passwordProp = $record.PSObject.Properties['password']
        if ($null -eq $versionProp -or $null -eq $accountProp -or
                $null -eq $passwordProp) {
            throw 'credentials record is incomplete'
        }
        if ([int]$versionProp.Value -ne 1) { throw 'credentials version is not 1' }
        if ($accountProp.Value -isnot [string] -or
                $passwordProp.Value -isnot [string]) {
            throw 'credentials fields are not strings'
        }
        $account = [string]$accountProp.Value
        $password = [string]$passwordProp.Value
        if (-not $account.Trim() -or -not $password.Trim()) {
            throw 'credentials fields are blank'
        }
        if (-not (Test-GatewayCredentialsAcl -Path $path)) {
            while ($Error.Count -gt $errorCount) { $Error.RemoveAt(0) }
            return [pscustomobject]@{
                Status = 'credentials_acl_too_broad'; Account = $null; Password = $null
            }
        }
        return [pscustomobject]@{
            Status = 'credentials_present'; Account = $account; Password = $password
        }
    } catch {
        while ($Error.Count -gt $errorCount) { $Error.RemoveAt(0) }
        return [pscustomobject]@{
            Status = 'credentials_invalid'; Account = $null; Password = $null
        }
    }
}

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

function Test-GatewayHealthUrl {
    # #133 A2: one health probe against an explicit URL. The descriptor flag
    # HealthRequiresOk tightens chat2api to HTTP 200 (the ingress /health
    # proxies the worker and answers 503 while it is missing); 8001 keeps the
    # historical "any HTTP response proves a listener" reachability semantics.
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 2,
        [bool]$RequireOk = $false
    )

    try {
        $previousProgressPreference = $ProgressPreference
        try {
            # Windows PowerShell renders Invoke-WebRequest progress as a blue
            # overlay. Five sequential health checks made that overlay flash
            # repeatedly over the interactive console.
            $ProgressPreference = 'SilentlyContinue'
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
            if ($RequireOk) { return ([int]$response.StatusCode -eq 200) }
            return ($response.StatusCode -lt 400)
        } finally {
            $ProgressPreference = $previousProgressPreference
        }
    } catch {
        # The previous curl-based console treated any HTTP response as proof
        # that the process was listening. A rejected HTTP response still
        # proves reachability and must not trigger a duplicate start attempt.
        # PS7 transport exceptions have no Response property; inspect the
        # optional member without raising again under the launcher's StrictMode.
        $httpResponseProperty = $_.Exception.PSObject.Properties['Response']
        if ($null -ne $httpResponseProperty -and $null -ne $httpResponseProperty.Value) {
            if ($RequireOk) {
                $status = 0
                try { $status = [int]$httpResponseProperty.Value.StatusCode } catch { $status = 0 }
                return ($status -eq 200)
            }
            return $true
        }
        return $false
    }
}

function Test-GatewayServiceHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [int]$TimeoutSeconds = 2,
        [scriptblock]$HttpChecker
    )

    if (Test-GatewayServiceArchived -Key $Key) { return $false }
    $desc = $script:GatewayServiceDescriptors[$Key]
    $url = "http://127.0.0.1:$($desc.Port)$($desc.HealthPath)"

    if ($null -ne $HttpChecker) {
        return [bool](& $HttpChecker $url $TimeoutSeconds)
    }

    $requireOk = $false
    $requireOkProp = $desc.PSObject.Properties['HealthRequiresOk']
    if ($null -ne $requireOkProp) { $requireOk = [bool]$requireOkProp.Value }
    return (Test-GatewayHealthUrl -Url $url -TimeoutSeconds $TimeoutSeconds `
        -RequireOk $requireOk)
}

function Get-GatewayProbeFailureReason {
    # #117: classify a failed probe without exposing the raw exception message
    # (it can carry URLs, proxy settings or paths). Returns one token from a
    # fixed vocabulary; every other failure is a plain "no_response".
    param([AllowNull()]$Exception)

    $current = $Exception
    $depth = 0
    $socketCodes = @()
    $webStatus = ''
    $timedOut = $false
    while ($null -ne $current -and $depth -lt 8) {
        if ($current -is [System.Net.Sockets.SocketException]) {
            $socketCodes += [string]$current.SocketErrorCode
        } elseif ($current -is [System.Net.WebException]) {
            $webStatus = [string]$current.Status
        } elseif ($current -is [System.TimeoutException]) {
            $timedOut = $true
        } elseif ($current -is [System.OperationCanceledException]) {
            $timedOut = $true
        }
        $current = $current.InnerException
        $depth += 1
    }
    if ($socketCodes -contains 'ConnectionRefused') { return 'connect_refused' }
    if ($timedOut -or $webStatus -eq 'Timeout' -or
            $socketCodes -contains 'TimedOut') {
        return 'probe_timeout'
    }
    if ($webStatus -in @('NameResolutionFailure', 'ProxyNameResolutionFailure') -or
            $socketCodes -contains 'HostNotFound' -or
            $socketCodes -contains 'NoData') {
        return 'dns_failure'
    }
    if ($webStatus -eq 'ConnectFailure' -or
            $socketCodes -contains 'NetworkUnreachable' -or
            $socketCodes -contains 'HostUnreachable') {
        return 'connect_failed'
    }
    return 'no_response'
}

function Invoke-GatewayReadyProbe {
    # #117: one probe of a readiness URL. The result separates "no HTTP
    # response at all" (connection refused, timeout, DNS/connect failure) from
    # "an HTTP response that is not 200": only the latter proves a listener
    # exists. Reading the optional Response member this way never raises
    # PropertyNotFoundStrict under StrictMode (PS7 transport exceptions have no
    # Response property at all).
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 3
    )

    try {
        $previousProgressPreference = $ProgressPreference
        try {
            $ProgressPreference = 'SilentlyContinue'
            $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing `
                -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        } finally {
            $ProgressPreference = $previousProgressPreference
        }
        $status = [int]$response.StatusCode
        $reason = if ($status -eq 200) { 'ready' } else { 'http_not_ready' }
        return [pscustomobject]@{
            Ready = ($status -eq 200)
            Responded = $true
            HttpStatus = $status
            Reason = $reason
            ErrorType = $null
        }
    } catch {
        $exception = $_.Exception
        $errorType = if ($null -eq $exception) { '' } else { $exception.GetType().Name }
        $responseProperty = $null
        if ($null -ne $exception) { $responseProperty = $exception.PSObject.Properties['Response'] }
        if ($null -ne $responseProperty -and $null -ne $responseProperty.Value) {
            $status = 0
            try { $status = [int]$responseProperty.Value.StatusCode }
            catch { $status = 0 }
            return [pscustomobject]@{
                Ready = $false
                Responded = $true
                HttpStatus = $status
                Reason = 'http_not_ready'
                ErrorType = $errorType
            }
        }
        return [pscustomobject]@{
            Ready = $false
            Responded = $false
            HttpStatus = $null
            Reason = (Get-GatewayProbeFailureReason -Exception $exception)
            ErrorType = $errorType
        }
    }
}

function Get-GatewayServiceStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker
    )

    if (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker) {
        return 'Running'
    }
    return 'Stopped'
}

function Get-GatewayStopIdentities {
    # #80: a descriptor may carry several private stop identities. The local
    # 8000 launcher switched from Python to the Rust binary, but the rollback is
    # only a StartPath change back to start_chat2api.bat: the Python identity
    # must stay recognized after that rollback. Descriptors without the array
    # keep their historical scalar identity.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $identitiesProp = $Descriptor.PSObject.Properties['StopIdentities']
    if ($null -ne $identitiesProp -and $null -ne $identitiesProp.Value) {
        return @($identitiesProp.Value)
    }
    $instanceScript = ''
    $instanceProp = $Descriptor.PSObject.Properties['StopInstanceScript']
    if ($null -ne $instanceProp) { $instanceScript = [string]$instanceProp.Value }
    $foreignPatterns = @()
    $foreignProp = $Descriptor.PSObject.Properties['StopForeignArgumentPatterns']
    if ($null -ne $foreignProp -and $null -ne $foreignProp.Value) {
        $foreignPatterns = @($foreignProp.Value)
    }
    $commandLinePattern = ''
    $patternProp = $Descriptor.PSObject.Properties['StopCommandLinePattern']
    if ($null -ne $patternProp) { $commandLinePattern = [string]$patternProp.Value }
    return @([pscustomobject]@{
        ProcessName = [string]$Descriptor.StopProcessName
        CommandLinePattern = $commandLinePattern
        InstanceScript = $instanceScript
        ForeignArgumentPatterns = $foreignPatterns
    })
}

function Get-GatewayOwnerIdentities {
    # Same shape as the stop identities, without the stop-only fields. The
    # inspection path must recognize both runtimes for the same reason.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $identitiesProp = $Descriptor.PSObject.Properties['OwnerIdentities']
    if ($null -ne $identitiesProp -and $null -ne $identitiesProp.Value) {
        return @($identitiesProp.Value)
    }
    $commandLinePattern = ''
    $patternProp = $Descriptor.PSObject.Properties['OwnerCommandLinePattern']
    if ($null -ne $patternProp) { $commandLinePattern = [string]$patternProp.Value }
    return @([pscustomobject]@{
        ProcessName = [string]$Descriptor.OwnerProcessName
        CommandLinePattern = $commandLinePattern
    })
}

function Test-GatewayProcessOwnership {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Process
    )
    try {
        foreach ($identity in @(Get-GatewayOwnerIdentities -Descriptor $Descriptor)) {
            $pattern = [string]$identity.CommandLinePattern
            if (-not $pattern) { continue }
            if ($Process.Name -eq [string]$identity.ProcessName -and
                    $Process.CommandLine -match $pattern) {
                return $true
            }
        }
        return $false
    } catch {
        return $false
    }
}

function Get-GatewayListenerScopeClass {
    # All managed health/control clients target 127.0.0.1. A Serve listener on
    # another interface can share the port without owning that endpoint. An
    # IPv6 wildcard can be dual-stack, so it cannot be excluded safely.
    param([Parameter(Mandatory = $true)]$Listener)
    try {
        $property = $Listener.PSObject.Properties['LocalAddress']
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            return 'Unconfirmed'
        }
        $address = $null
        if (-not [Net.IPAddress]::TryParse([string]$property.Value, [ref]$address)) {
            return 'Unconfirmed'
        }
        if ($address.IsIPv4MappedToIPv6) { $address = $address.MapToIPv4() }
        if ($address.Equals([Net.IPAddress]::Any) -or
                $address.Equals([Net.IPAddress]::IPv6Any) -or
                $address.Equals([Net.IPAddress]::Loopback)) {
            return 'Overlapping'
        }
        return 'Separate'
    } catch {
        return 'Unconfirmed'
    }
}

function Get-GatewayListenerSnapshot {
    param([Parameter(Mandatory = $true)][int]$Port)
    $listeners = @()
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        # NetTCPIP reports an ordinary empty match as ObjectNotFound. Every
        # other failure is an unreadable snapshot, not an empty listener set.
        if ([string]$_.CategoryInfo.Category -ne 'ObjectNotFound' -or
                [string]$_.FullyQualifiedErrorId -notmatch 'CmdletizationQuery_NotFound') {
            throw
        }
    }
    $overlapping = @()
    $separate = @()
    $unconfirmed = @()
    foreach ($listener in $listeners) {
        $scopeClass = Get-GatewayListenerScopeClass -Listener $listener
        try {
            if (-not $listener.PSObject.Properties['OwningProcess'] -or
                    [int]$listener.OwningProcess -le 0) { $scopeClass = 'Unconfirmed' }
        } catch { $scopeClass = 'Unconfirmed' }
        switch ($scopeClass) {
            'Overlapping' { $overlapping += $listener }
            'Separate' { $separate += $listener }
            default { $unconfirmed += $listener }
        }
    }
    return [pscustomobject]@{
        Overlapping = $overlapping; Separate = $separate; Unconfirmed = $unconfirmed
    }
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

function Get-GatewayCommandLineTokens {
    # #133 A2/P1: split a Windows command line into argument tokens. CIM
    # quotes a path that contains spaces ("C:\Program Files\...\app.exe"), and
    # the old \S* reference regex could not cross that space, so the executable
    # was misread as Foreign. Double quotes group a token; a backslash-escaped
    # quote stays literal. Single quotes are not a Windows quoting form and are
    # kept as ordinary characters (the caller trims a whole-token pair).
    param([AllowNull()][string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return @() }
    $tokens = @()
    $current = New-Object System.Text.StringBuilder
    $inQuotes = $false
    $hasToken = $false
    for ($index = 0; $index -lt $CommandLine.Length; $index++) {
        $ch = $CommandLine[$index]
        if ($ch -eq '\' -and $index + 1 -lt $CommandLine.Length -and
                $CommandLine[$index + 1] -eq '"') {
            [void]$current.Append('"')
            $hasToken = $true
            $index += 1
            continue
        }
        if ($ch -eq '"') {
            $inQuotes = -not $inQuotes
            $hasToken = $true
            continue
        }
        if (-not $inQuotes -and [char]::IsWhiteSpace($ch)) {
            if ($hasToken) {
                $tokens += $current.ToString()
                [void]$current.Clear()
                $hasToken = $false
            }
            continue
        }
        [void]$current.Append($ch)
        $hasToken = $true
    }
    if ($hasToken) { $tokens += $current.ToString() }
    return $tokens
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
        [Parameter(Mandatory = $true)][string]$GatewayRoot,
        $Identity
    )
    if ([string]::IsNullOrWhiteSpace([string]$Process.CommandLine)) { return 'Unconfirmed' }
    $commandLine = [string]$Process.CommandLine

    if ($null -eq $Identity) {
        $Identity = @(Get-GatewayStopIdentities -Descriptor $Descriptor) |
            Select-Object -First 1
    }
    if ($null -eq $Identity) { return 'Unconfirmed' }

    $instanceScript = ''
    $instanceProp = $Identity.PSObject.Properties['InstanceScript']
    if ($null -ne $instanceProp) { $instanceScript = [string]$instanceProp.Value }
    if (-not $instanceScript) { return 'Unconfirmed' }
    $expected = $null
    try {
        $expected = [IO.Path]::GetFullPath(
            (Join-Path $GatewayRoot $instanceScript))
    } catch {
        return 'Unconfirmed'
    }

    # The entrypoint may be given bare (the private launcher's shape) or
    # path-qualified; either way it must reference THIS entrypoint. Tokenizing
    # first is what makes a quoted path with spaces ("C:\Program Files\...
    # um-gateway-candidate.exe") comparable to the expected full path.
    $commandLinePattern = [string]$Identity.CommandLinePattern
    if (-not $commandLinePattern) { return 'Unconfirmed' }
    $bareToken = $commandLine -match $commandLinePattern
    $expectedLeaf = [IO.Path]::GetFileName($expected)
    $references = @()
    foreach ($token in @(Get-GatewayCommandLineTokens -CommandLine $commandLine)) {
        $candidate = $token.Trim('"', "'")
        if (-not $candidate) { continue }
        # A bare script name is the $bareToken case; only a path-qualified
        # token is a resolvable reference.
        if ($candidate -notmatch '[\\/]') { continue }
        $candidateLeaf = ''
        try { $candidateLeaf = [IO.Path]::GetFileName($candidate) } catch { }
        if ($candidateLeaf -and
                $candidateLeaf.Equals($expectedLeaf, [StringComparison]::OrdinalIgnoreCase)) {
            $references += $candidate
        }
    }
    if (-not $bareToken -and $references.Count -eq 0) { return 'Foreign' }

    # #133 A2: the split identities are positively selected by their mode.
    # A missing required marker means this is a different shape (the legacy
    # single-process production instance, a candidate or a cloud instance).
    $requiredPatterns = @()
    $requiredProp = $Identity.PSObject.Properties['RequiredArgumentPatterns']
    if ($null -ne $requiredProp -and $null -ne $requiredProp.Value) {
        $requiredPatterns = @($requiredProp.Value)
    }
    foreach ($pattern in $requiredPatterns) {
        if ($pattern -and $commandLine -notmatch $pattern) { return 'Foreign' }
    }

    # A Portless identity binds --port 0 (the worker's real port is published
    # in WorkerPortFile); comparing 0 with the descriptor's 8000 would classify
    # the private worker as foreign.
    $portless = $false
    $portlessProp = $Identity.PSObject.Properties['Portless']
    if ($null -ne $portlessProp) { $portless = [bool]$portlessProp.Value }
    if (-not $portless) {
        $declaredPort = Get-GatewayDeclaredPort -CommandLine $commandLine
        if ($null -ne $declaredPort -and $declaredPort -ne [int]$Descriptor.Port) {
            return 'Foreign'
        }
    }

    $foreignPatterns = @()
    $foreignProp = $Identity.PSObject.Properties['ForeignArgumentPatterns']
    if ($null -ne $foreignProp -and $null -ne $foreignProp.Value) {
        $foreignPatterns = @($foreignProp.Value)
    }
    foreach ($pattern in $foreignPatterns) {
        if ($pattern -and $commandLine -match $pattern) { return 'Foreign' }
    }

    foreach ($reference in $references) {
        $resolved = $reference
        if (-not [IO.Path]::IsPathRooted($resolved)) {
            $resolved = Join-Path $GatewayRoot $resolved
        }
        try { $resolved = [IO.Path]::GetFullPath($resolved) } catch { return 'Unconfirmed' }
        if (-not $resolved.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
            return 'Foreign'
        }
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
        [Parameter(Mandatory = $true)][string]$GatewayRoot,
        # #133 A2: a caller may narrow the scan to one identity (the watch
        # queries the worker only). An empty list keeps the descriptor's full
        # stop scope.
        [object[]]$Identities = @()
    )
    $private = @()
    $foreign = @()
    $unconfirmed = @()
    # Do not reuse the parameter name for the resolved list: PowerShell
    # variables are case-insensitive, so `$identities = @()` would clear the
    # caller's narrowed identity list and fall back to the full stop scope.
    $scopeIdentities = @()
    if ($null -ne $Identities -and @($Identities).Count -gt 0) {
        $scopeIdentities = @($Identities)
    } else {
        $scopeIdentities = @(Get-GatewayStopIdentities -Descriptor $Descriptor)
    }
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $matching = @($scopeIdentities | Where-Object {
            [string]$process.Name -eq [string]$_.ProcessName
        })
        if ($matching.Count -eq 0) { continue }
        if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
            # A name match with no readable command line could be the private
            # instance; classify it (Unconfirmed) instead of skipping it.
            $unconfirmed += $process
            continue
        }
        # The same process name can carry several shapes (the legacy
        # single-process production instance, the worker, the ingress). The
        # process is private only when at least one identity positively claims
        # it; an unconfirmed result from any identity is never silently
        # downgraded to foreign.
        $scopeClass = 'Foreign'
        $unconfirmedClass = $false
        foreach ($identity in $matching) {
            $candidateClass = Get-GatewayStopScopeClass -Descriptor $Descriptor `
                -Identity $identity -Process $process -GatewayRoot $GatewayRoot
            if ($candidateClass -eq 'Private') {
                $scopeClass = 'Private'
                break
            }
            if ($candidateClass -eq 'Unconfirmed') { $unconfirmedClass = $true }
        }
        if ($scopeClass -ne 'Private' -and $unconfirmedClass) {
            $scopeClass = 'Unconfirmed'
        }
        switch ($scopeClass) {
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
    which selected another chat2api.py instance too. Now
    the scope is classified first, each selected PID is re-verified against its
    startup identity right before the kill, and an unconfirmable scope terminates
    NOTHING and reports StopRefused instead of a broad force-stop or a fake
    "stopped".
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)]$Descriptor,
        [switch]$LoopbackPortOnly,
        # #133 A2: a component cleanup stops only that component's identity.
        [object[]]$Identities = @()
    )
    try {
        $scopeArgs = @{ Descriptor = $Descriptor; GatewayRoot = (Get-GatewayRoot) }
        if ($null -ne $Identities -and @($Identities).Count -gt 0) {
            $scopeArgs.Identities = @($Identities)
        }
        $scope = Select-GatewayStopScope @scopeArgs
        $targets = @($scope.Private)
        if ($LoopbackPortOnly) {
            $listeners = Get-GatewayListenerSnapshot -Port $Descriptor.Port
            if (@($listeners.Unconfirmed).Count -gt 0) {
                return [pscustomobject]@{
                    Key = $Key; Status = 'StopRefused'; Action = 'Stop'
                    TerminatedProcessIds = @()
                    Detail = '监听地址或进程归属无法确认，未终止任何进程'
                }
            }
            $localPids = @($listeners.Overlapping | ForEach-Object { [int]$_.OwningProcess })
            $separatePids = @($listeners.Separate | ForEach-Object { [int]$_.OwningProcess })
            $privatePids = @($targets | ForEach-Object { [int]$_.ProcessId })
            if (@($localPids | Where-Object { $_ -notin $privatePids }).Count -gt 0) {
                return [pscustomobject]@{
                    Key = $Key; Status = 'StopRefused'; Action = 'Stop'
                    TerminatedProcessIds = @()
                    Detail = '本机端口存在未确认的受管进程，未终止任何进程'
                }
            }
            # Preserve cleanup of confirmed residual processes with no socket,
            # but never select a process listening only on another interface.
            $targets = @($targets | Where-Object {
                [int]$_.ProcessId -notin $separatePids -or [int]$_.ProcessId -in $localPids
            })
        }
    } catch {
        return [pscustomobject]@{
            Key = $Key; Status = 'StopRefused'; Action = 'Stop'
            TerminatedProcessIds = @()
            Detail = '进程归属无法确认（进程或监听清单查询失败），未终止任何进程'
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
    foreach ($target in $targets) {
        if ($Key -eq 'chat2api') {
            Write-GatewayStopRequest -Target $target -Method 'force_stop'
        }
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

# ---------------------------------------------------------------------------
# #133 A2: local 8000 components (worker + ingress)
#
# The worker owns model processing and binds --port 0; its real port is the
# decimal value in WorkerPortFile. The ingress owns 127.0.0.1:8000 and proxies
# the worker. A component is Present when its listener (or, for a worker that is
# still starting, its process) is positively identified, Residual when only the
# process is left, Absent when neither exists and Conflict when ownership cannot
# be confirmed.
# ---------------------------------------------------------------------------

function Get-GatewayStartComponents {
    param([Parameter(Mandatory = $true)]$Descriptor)
    $prop = $Descriptor.PSObject.Properties['StartComponents']
    if ($null -eq $prop -or $null -eq $prop.Value) { return @() }
    return @($prop.Value)
}

function Test-GatewayComponentMode {
    # A .bat StartPath is the Python rollback and stays single-process. The
    # component branch only applies to the Rust binary with StartComponents.
    param($Descriptor, [string]$StartPath)
    if (-not $StartPath) { return $false }
    if ($StartPath -notmatch '\.exe$') { return $false }
    return (@(Get-GatewayStartComponents -Descriptor $Descriptor).Count -gt 0)
}

function Get-GatewayWorkerPortFilePath {
    param([Parameter(Mandatory = $true)]$Descriptor)
    $prop = $Descriptor.PSObject.Properties['WorkerPortFile']
    if ($null -eq $prop -or -not $prop.Value) { return $null }
    $value = [string]$prop.Value
    try {
        # Tests inject an absolute temp file; production keeps the descriptor
        # value relative to the repository root. Join-Path would turn an absolute
        # path into a second path below the root.
        if ([IO.Path]::IsPathRooted($value)) {
            return [IO.Path]::GetFullPath($value)
        }
        return [IO.Path]::GetFullPath((Join-Path (Get-GatewayRoot) $value))
    } catch {
        return $null
    }
}

function Get-GatewayWorkerPort {
    # The worker writes "decimal port + newline + PID + newline" atomically
    # after the listener is bound. Both lines are required: the port is the
    # first line, the worker PID the second. Anything missing, unreadable or
    # invalid means "unknown", never a guessed port.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $path = Get-GatewayWorkerPortFilePath -Descriptor $Descriptor
    if (-not $path) { return $null }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $text = [IO.File]::ReadAllText($path)
    } catch {
        return $null
    }
    $lines = @($text -split "`r?`n")
    if ($lines.Count -lt 2) { return $null }
    $port = 0
    if (-not [int]::TryParse($lines[0].Trim(), [ref]$port)) { return $null }
    if ($port -le 0 -or $port -gt 65535) { return $null }
    $workerPid = 0
    if (-not [int]::TryParse($lines[1].Trim(), [ref]$workerPid)) { return $null }
    if ($workerPid -le 0) { return $null }
    return $port
}

function Remove-GatewayWorkerPortFile {
    # #133 A2/H3: a worker that was force-stopped never removes its record, and
    # the next worker publishes a fresh port. Removing the stale file keeps the
    # ingress from forwarding to whatever now owns that port.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $path = Get-GatewayWorkerPortFilePath -Descriptor $Descriptor
    if (-not $path) { return $false }
    try {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force -ErrorAction Stop
        }
        return $true
    } catch {
        return $false
    }
}

function Get-GatewayWorkerProcessIds {
    # #133 A2/H3: verified worker identity PIDs. $null means "cannot be
    # confirmed" (fail closed); an empty array means no worker process exists.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $identity = Get-GatewayComponentIdentity -Descriptor $Descriptor -Name 'worker'
    if ($null -eq $identity) { return $null }
    try {
        $scope = Select-GatewayStopScope -Descriptor $Descriptor `
            -GatewayRoot (Get-GatewayRoot) -Identities @($identity)
    } catch {
        return $null
    }
    if (@($scope.Unconfirmed).Count -gt 0) { return $null }
    return @($scope.Private | ForEach-Object { [int]$_.ProcessId })
}

function Get-GatewayWorkerRuntimePort {
    # #133 A2/H3: the recorded port is usable only while a verified worker
    # identity process owns its loopback listener. A leftover WorkerPortFile
    # (the worker crashed and an unrelated process reused the port) is ignored:
    # health probes and shutdown requests must never target that process.
    param([Parameter(Mandatory = $true)]$Descriptor)
    $port = Get-GatewayWorkerPort -Descriptor $Descriptor
    if ($null -eq $port) { return $null }
    $workerPids = Get-GatewayWorkerProcessIds -Descriptor $Descriptor
    if ($null -eq $workerPids -or @($workerPids).Count -eq 0) { return $null }
    try {
        $snapshot = Get-GatewayListenerSnapshot -Port $port
    } catch {
        return $null
    }
    if (@($snapshot.Unconfirmed).Count -gt 0) { return $null }
    $listenerPids = @($snapshot.Overlapping |
        ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
    if ($listenerPids.Count -eq 0) { return $null }
    foreach ($listenerPid in $listenerPids) {
        if ($listenerPid -notin $workerPids) { return $null }
    }
    return $port
}

function Get-GatewayComponentIdentity {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$Name
    )
    foreach ($identity in @(Get-GatewayStopIdentities -Descriptor $Descriptor)) {
        $componentProp = $identity.PSObject.Properties['Component']
        if ($null -ne $componentProp -and
                [string]$componentProp.Value -eq $Name) {
            return $identity
        }
    }
    return $null
}

function Get-GatewayComponentHealthUrl {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Component
    )
    $healthProp = $Component.PSObject.Properties['HealthUrl']
    if ($null -eq $healthProp -or -not $healthProp.Value) { return $null }
    $template = [string]$healthProp.Value
    if ($template -match '\{worker-port\}') {
        # A stale WorkerPortFile (no worker identity process, or its port now
        # owned by another process) yields no URL: the health check fails
        # instead of probing an unrelated process.
        $port = Get-GatewayWorkerRuntimePort -Descriptor $Descriptor
        if ($null -eq $port) { return $null }
        return $template.Replace('{worker-port}', [string]$port)
    }
    return $template
}

function Test-GatewayComponentHealth {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Component,
        [scriptblock]$HttpChecker,
        [int]$TimeoutSeconds = 2
    )
    $url = Get-GatewayComponentHealthUrl -Descriptor $Descriptor -Component $Component
    if (-not $url) { return $false }
    if ($null -ne $HttpChecker) {
        return [bool](& $HttpChecker $url $TimeoutSeconds)
    }
    $requireOk = $false
    $requireOkProp = $Descriptor.PSObject.Properties['HealthRequiresOk']
    if ($null -ne $requireOkProp) { $requireOk = [bool]$requireOkProp.Value }
    return [bool](Test-GatewayHealthUrl -Url $url -TimeoutSeconds $TimeoutSeconds `
        -RequireOk $requireOk)
}

function Test-GatewayProcessMatchesIdentity {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)]$Process
    )
    try {
        return ((Get-GatewayStopScopeClass -Descriptor $Descriptor `
            -Identity $Identity -Process $Process `
            -GatewayRoot (Get-GatewayRoot)) -eq 'Private')
    } catch {
        return $false
    }
}

function Test-GatewayWorkerStartingExpired {
    # #133 A2: a live worker process that never publishes WorkerPortFile is only
    # "Starting" within the start budget. Past it the worker is stuck (e.g. a UM
    # login browser wait) and must be treated as Residual so the verified scoped
    # stop can restart it. An unreadable creation time fails closed to "not
    # expired" (keep waiting).
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [object[]]$Processes = @()
    )
    if (@($Processes).Count -eq 0) { return $false }
    $timeoutSeconds = [Math]::Max(1, [int]$Descriptor.StartTimeoutSeconds)
    $now = [DateTime]::UtcNow
    foreach ($process in @($Processes)) {
        $creationProp = $process.PSObject.Properties['CreationDate']
        if ($null -eq $creationProp -or $null -eq $creationProp.Value) {
            return $false
        }
        $rawCreated = $creationProp.Value
        if ($rawCreated -isnot [DateTime] -and
                ($rawCreated -isnot [string] -or
                 [string]::IsNullOrWhiteSpace([string]$rawCreated))) {
            return $false
        }
        $created = $null
        try {
            $created = ([DateTime]$rawCreated).ToUniversalTime()
        } catch {
            return $false
        }
        if (($now - $created).TotalSeconds -le $timeoutSeconds) { return $false }
    }
    return $true
}

function Get-GatewayWorkerInspection {
    # #133 A2/H3: the worker identity process is the authority for the
    # recorded port. No worker process means the record is stale, even if an
    # unrelated process now listens on that port: the state is Absent (the next
    # start deletes the file), never Conflict/Present for a foreign listener.
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Identity
    )
    try {
        $scope = Select-GatewayStopScope -Descriptor $Descriptor `
            -GatewayRoot (Get-GatewayRoot) -Identities @($Identity)
    } catch {
        return [pscustomobject]@{
            Name = 'worker'; State = 'Conflict'; ProcessIds = @()
            Detail = '进程信息无法读取：worker'
        }
    }
    if (@($scope.Unconfirmed).Count -gt 0) {
        return [pscustomobject]@{
            Name = 'worker'; State = 'Conflict'
            ProcessIds = @($scope.Unconfirmed | ForEach-Object { [int]$_.ProcessId })
            Detail = '进程归属无法确认：worker'
        }
    }
    $workerPids = @($scope.Private | ForEach-Object { [int]$_.ProcessId })
    if ($workerPids.Count -eq 0) {
        # The worker is gone; WorkerPortFile is a leftover from its previous
        # (non-graceful) exit. Ignore it and let the start replace it.
        return [pscustomobject]@{
            Name = 'worker'; State = 'Absent'; ProcessIds = @(); Detail = ''
        }
    }
    $port = Get-GatewayWorkerPort -Descriptor $Descriptor
    if ($null -eq $port) {
        if (Test-GatewayWorkerStartingExpired -Descriptor $Descriptor `
                -Processes $scope.Private) {
            # The startup window is over and the worker still has no port: it is
            # stuck, not starting. Classify it as residual so the verified
            # scoped stop can restart it.
            return [pscustomobject]@{
                Name = 'worker'; State = 'Residual'; ProcessIds = @($workerPids)
                Detail = '发现组件残留进程：worker（启动超时）'
            }
        }
        # The worker process is alive but has not published a usable port
        # yet. This is the startup window, not a residual instance: the caller
        # must wait for it within the start budget instead of force-stopping it.
        $portFile = Get-GatewayWorkerPortFilePath -Descriptor $Descriptor
        $portFilePresent = $false
        if ($portFile) {
            try {
                $portFilePresent = Test-Path -LiteralPath $portFile -PathType Leaf
            } catch {
                # An unreadable existence check is treated as a present but
                # unusable record, never as "not written yet".
                $portFilePresent = $true
            }
        }
        $startingDetail = if ($portFilePresent) {
            'worker 正在启动（WorkerPortFile 不可读或格式非法）'
        } else {
            'worker 正在启动（尚未写入 WorkerPortFile）'
        }
        return [pscustomobject]@{
            Name = 'worker'; State = 'Starting'; ProcessIds = @($workerPids)
            Detail = $startingDetail
        }
    }
    try {
        $snapshot = Get-GatewayListenerSnapshot -Port $port
    } catch {
        return [pscustomobject]@{
            Name = 'worker'; State = 'Conflict'; ProcessIds = @()
            Detail = '监听信息无法读取：worker'
        }
    }
    if (@($snapshot.Unconfirmed).Count -gt 0) {
        return [pscustomobject]@{
            Name = 'worker'; State = 'Conflict'; ProcessIds = @()
            Detail = '监听地址或进程归属无法确认：worker'
        }
    }
    $listenerPids = @($snapshot.Overlapping |
        ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
    if ($listenerPids.Count -eq 0) {
        # The worker process is alive but has not (re)bound the recorded port.
        return [pscustomobject]@{
            Name = 'worker'; State = 'Residual'; ProcessIds = @($workerPids)
            Detail = '发现组件残留进程：worker'
        }
    }
    $foreignPids = @($listenerPids | Where-Object { $_ -notin $workerPids })
    if ($foreignPids.Count -gt 0) {
        # The recorded port belongs to something else while the worker process
        # is alive but not listening there: the record is stale for this worker.
        return [pscustomobject]@{
            Name = 'worker'; State = 'Residual'; ProcessIds = @($workerPids)
            Detail = '发现组件残留进程：worker'
        }
    }
    return [pscustomobject]@{
        Name = 'worker'; State = 'Present'; ProcessIds = @($listenerPids); Detail = ''
    }
}

function Get-GatewayComponentInspection {
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$ComponentName
    )
    $identity = Get-GatewayComponentIdentity -Descriptor $Descriptor -Name $ComponentName
    if ($null -eq $identity) {
        return [pscustomobject]@{
            Name = $ComponentName; State = 'Conflict'; ProcessIds = @()
            Detail = ('缺少组件身份：' + $ComponentName)
        }
    }
    if ($ComponentName -eq 'worker') {
        return (Get-GatewayWorkerInspection -Descriptor $Descriptor -Identity $identity)
    }
    $port = [int]$Descriptor.Port
    $owners = @()
    if ($null -ne $port) {
        try {
            $snapshot = Get-GatewayListenerSnapshot -Port $port
        } catch {
            return [pscustomobject]@{
                Name = $ComponentName; State = 'Conflict'; ProcessIds = @()
                Detail = ('监听信息无法读取：' + $ComponentName)
            }
        }
        if (@($snapshot.Unconfirmed).Count -gt 0) {
            return [pscustomobject]@{
                Name = $ComponentName; State = 'Conflict'; ProcessIds = @()
                Detail = ('监听地址或进程归属无法确认：' + $ComponentName)
            }
        }
        $unconfirmedPids = @()
        $listenerPids = @($snapshot.Overlapping |
            ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
        foreach ($listenerPid in $listenerPids) {
            try {
                $process = @(Get-CimInstance Win32_Process `
                    -Filter "ProcessId=$listenerPid" -ErrorAction Stop)
                if ($process.Count -ne 1 -or
                        [int]$process[0].ProcessId -ne $listenerPid) {
                    $unconfirmedPids += $listenerPid
                } else { $owners += $process[0] }
            } catch { $unconfirmedPids += $listenerPid }
        }
        if ($unconfirmedPids.Count -gt 0) {
            return [pscustomobject]@{
                Name = $ComponentName; State = 'Conflict'
                ProcessIds = @($unconfirmedPids)
                Detail = ('监听进程归属无法确认：' + $ComponentName)
            }
        }
        $foreign = @($owners | Where-Object {
            -not (Test-GatewayProcessMatchesIdentity -Descriptor $Descriptor `
                -Identity $identity -Process $_)
        })
        if ($foreign.Count -gt 0) {
            return [pscustomobject]@{
                Name = $ComponentName; State = 'Conflict'
                ProcessIds = @($foreign.ProcessId)
                Detail = ('端口被非组件进程占用：' + $ComponentName)
            }
        }
    }
    if ($owners.Count -gt 0) {
        return [pscustomobject]@{
            Name = $ComponentName; State = 'Present'
            ProcessIds = @($owners.ProcessId); Detail = ''
        }
    }
    # No listener: a verified process of this identity is residual (it has not
    # bound its port yet or lost it); anything else is absent.
    try {
        $scope = Select-GatewayStopScope -Descriptor $Descriptor `
            -GatewayRoot (Get-GatewayRoot) -Identities @($identity)
    } catch {
        return [pscustomobject]@{
            Name = $ComponentName; State = 'Conflict'; ProcessIds = @()
            Detail = ('进程信息无法读取：' + $ComponentName)
        }
    }
    if (@($scope.Unconfirmed).Count -gt 0) {
        return [pscustomobject]@{
            Name = $ComponentName; State = 'Conflict'
            ProcessIds = @($scope.Unconfirmed | ForEach-Object { [int]$_.ProcessId })
            Detail = ('进程归属无法确认：' + $ComponentName)
        }
    }
    if (@($scope.Private).Count -gt 0) {
        return [pscustomobject]@{
            Name = $ComponentName; State = 'Residual'
            ProcessIds = @($scope.Private | ForEach-Object { [int]$_.ProcessId })
            Detail = ('发现组件残留进程：' + $ComponentName)
        }
    }
    return [pscustomobject]@{
        Name = $ComponentName; State = 'Absent'; ProcessIds = @(); Detail = ''
    }
}

function Get-GatewayServiceInspection {
    param([Parameter(Mandatory = $true)][string]$Key)

    $desc = $script:GatewayServiceDescriptors[$Key]
    $startPath = $null
    try { $startPath = Resolve-GatewayStartPath -Key $Key } catch { }
    $worker = $null
    $ingress = $null
    if (Test-GatewayComponentMode -Descriptor $desc -StartPath $startPath) {
        $worker = Get-GatewayComponentInspection -Descriptor $desc -ComponentName 'worker'
        $ingress = Get-GatewayComponentInspection -Descriptor $desc -ComponentName 'ingress'
    }
    # Both components absent means this is not a live split pair. A legacy
    # single-process instance (the Python rollback or the transition shape) may
    # still own 8000; the legacy branch below recognizes it instead of reporting
    # Conflict merely because the descriptor now declares components. An ingress
    # Conflict alone (an unverified owner on 8000) also goes through the legacy
    # branch, which reports the same Conflict from its own ownership check. A
    # WORKER Conflict is different: it positively proves the split shape (a worker
    # identity process exists but its listener/ownership is unconfirmed), so it
    # must never fall back to the legacy branch and be mistaken for a residual
    # single-process instance.
    $splitPresent = $false
    if ($null -ne $worker -and $null -ne $ingress) {
        $splitPresent = ($worker.State -in @('Present', 'Residual', 'Starting', 'Conflict')) -or
            ($ingress.State -in @('Present', 'Residual'))
    }
    if ($splitPresent) {
        $conflict = @($worker, $ingress | Where-Object { $_.State -eq 'Conflict' })
        if ($conflict.Count -gt 0) {
            # An unreadable command line can be claimed by both component
            # identities; report each PID once.
            $conflictPids = @()
            foreach ($processId in @($conflict | ForEach-Object { $_.ProcessIds })) {
                if ($null -ne $processId -and [int]$processId -notin $conflictPids) {
                    $conflictPids += [int]$processId
                }
            }
            return [pscustomobject]@{
                Key = $Key; State = 'Conflict'; Healthy = $false
                ProcessIds = $conflictPids
                Detail = (($conflict | ForEach-Object { $_.Detail }) -join '；')
            }
        }
        # #133 A2: judge the two components separately. Both healthy is the
        # only Running state; any component Present/Residual but not all healthy
        # is Degraded. Stopped is reserved for "both components Absent" and is
        # handled by the legacy branch below (where a single-process instance
        # may still own 8000).
        $workerHealthy = $false
        if ($worker.State -in @('Present', 'Residual', 'Starting')) {
            $workerComponent = @(Get-GatewayStartComponents -Descriptor $desc) |
                Where-Object { [string]$_.Name -eq 'worker' } | Select-Object -First 1
            if ($null -ne $workerComponent) {
                $workerHealthy = Test-GatewayComponentHealth -Descriptor $desc `
                    -Component $workerComponent
            }
        }
        $ingressHealthy = $false
        if ($ingress.State -in @('Present', 'Residual')) {
            $ingressComponent = @(Get-GatewayStartComponents -Descriptor $desc) |
                Where-Object { [string]$_.Name -eq 'ingress' } | Select-Object -First 1
            if ($null -ne $ingressComponent) {
                $ingressHealthy = Test-GatewayComponentHealth -Descriptor $desc `
                    -Component $ingressComponent
            }
        }
        $pids = @()
        foreach ($processId in @($worker.ProcessIds) + @($ingress.ProcessIds)) {
            if ($null -ne $processId -and [int]$processId -notin $pids) {
                $pids += [int]$processId
            }
        }
        if ($workerHealthy -and $ingressHealthy) {
            return [pscustomobject]@{
                Key = $Key; State = 'Running'; Healthy = $true
                ProcessIds = $pids; Detail = '运行中'
            }
        }
        $unready = @()
        if (-not $workerHealthy) {
            $unready += if ($worker.State -eq 'Absent') { '缺少 worker' } else { 'worker 未就绪' }
        }
        if (-not $ingressHealthy) {
            $unready += if ($ingress.State -eq 'Absent') { '缺少 ingress' } else { 'ingress 未就绪' }
        }
        return [pscustomobject]@{
            Key = $Key; State = 'Degraded'; Healthy = $false
            ProcessIds = $pids; Detail = ($unready -join '；')
        }
    }

    try {
        $snapshot = Get-GatewayListenerSnapshot -Port $desc.Port
    } catch {
        return [pscustomobject]@{
            Key = $Key; State = 'Conflict'; Healthy = $false; ProcessIds = @()
            Detail = '监听信息无法读取，无法确认端口状态'
        }
    }
    if (@($snapshot.Unconfirmed).Count -gt 0) {
        return [pscustomobject]@{
            Key = $Key; State = 'Conflict'; Healthy = $false; ProcessIds = @()
            Detail = '监听地址或进程归属无法确认'
        }
    }
    $owners = @()
    $unconfirmedPids = @()
    $listenerPids = @($snapshot.Overlapping | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
    foreach ($listenerPid in $listenerPids) {
        try {
            $process = @(Get-CimInstance Win32_Process `
                -Filter "ProcessId=$listenerPid" -ErrorAction Stop)
            if ($process.Count -ne 1 -or [int]$process[0].ProcessId -ne $listenerPid) {
                $unconfirmedPids += $listenerPid
            } else { $owners += $process[0] }
        } catch { $unconfirmedPids += $listenerPid }
    }
    if ($unconfirmedPids.Count -gt 0) {
        return [pscustomobject]@{
            Key = $Key; State = 'Conflict'; Healthy = $false
            ProcessIds = @($unconfirmedPids); Detail = '监听进程归属无法确认'
        }
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
    try {
        $residual = @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { Test-GatewayProcessOwnership $desc $_ })
    } catch {
        return [pscustomobject]@{
            Key = $Key; State = 'Conflict'; Healthy = $false; ProcessIds = @()
            Detail = '进程信息无法读取，无法确认服务状态'
        }
    }
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
    # SELFUSEAI_GATEWAY_ACTION_SOURCE; any other caller is a script ("cli").
    $raw = [string]$env:SELFUSEAI_GATEWAY_ACTION_SOURCE
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
    $root = [string]$env:SELFUSEAI_GATEWAY_WATCH_DIR
    if (-not $root) {
        $root = Join-Path $env:LOCALAPPDATA 'SelfUseAI\ai-gateway\watch'
    }
    return Join-Path $root ('{0}-desired.json' -f $Key)
}

function Get-GatewayExitRecordDirectory {
    Join-Path (Split-Path -Parent (Get-GatewayDesiredStatePath -Key 'chat2api')) 'chat2api-exits'
}

function Write-GatewayStopRequest {
    param(
        [Parameter(Mandatory = $true)]$Target,
        [Parameter(Mandatory = $true)][ValidateSet('graceful_http', 'force_stop')][string]$Method,
        [string]$RecordDirectory = (Get-GatewayExitRecordDirectory)
    )
    $process = $null
    $pending = $null
    try {
        $process = Get-Process -Id ([int]$Target.ProcessId) -ErrorAction Stop
        [void]$process.Handle
        $ticks = $process.StartTime.ToUniversalTime().Ticks
        $cimTicks = ([DateTime]$Target.CreationDate).ToUniversalTime().Ticks
        # CIM creation time has microsecond precision. The filename uses the
        # exact start time from the process handle, shared with the observer.
        if (($ticks - ($ticks % 10)) -ne $cimTicks -or $process.HasExited) { return }
        $path = Join-Path $RecordDirectory ('stop-{0}-{1}.json' -f $ticks, $process.Id)
        $pending = '{0}.{1}.tmp' -f $path, $PID
        [void][IO.Directory]::CreateDirectory($RecordDirectory)
        $record = @{
            source = Get-GatewayActionSource
            method = $Method
            at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        }
        [IO.File]::WriteAllText($pending, ($record | ConvertTo-Json -Compress),
            (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $pending -Destination $path -Force -ErrorAction Stop
        Get-ChildItem -LiteralPath $RecordDirectory -File -Filter 'stop-*.json' |
            Where-Object Name -Match '^stop-[0-9]+-[0-9]+\.json$' |
            Sort-Object LastWriteTimeUtc, Name -Descending |
            Select-Object -Skip 32 |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
    } catch {
        # Missing diagnostic evidence must not block shutdown.
        Write-Verbose 'Unable to record the local gateway stop request.'
    } finally {
        if ($null -ne $process) { $process.Dispose() }
        if ($null -ne $pending) { Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue }
    }
}

function Write-GatewayGracefulStopRequests {
    param($Descriptor)
    try {
        $scope = Select-GatewayStopScope -Descriptor $Descriptor -GatewayRoot (Get-GatewayRoot)
        foreach ($target in @($scope.Private)) {
            Write-GatewayStopRequest -Target $target -Method 'graceful_http'
        }
    } catch {
        Write-Verbose 'Unable to identify the local gateway processes for stop diagnostics.'
    }
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

function Get-GatewayManuallyStoppedKeys {
    # R1/D1（2026-10-11）：哪些键被人为停用（desired state = stopped）。
    # 定时/手动全源同步也要遵守「显式停用取消自动上游查询」：调用方按本函数
    # 剔除人工停用来源（不拉起、不进同步来源集合）。标记缺席/不可读/形状不合法
    # = 「意图未知」，与「已停用」不同，不在这里剔除——未知不能伪装成已停止。
    param([Parameter(Mandatory = $true)][string[]]$Keys)
    $stopped = New-Object System.Collections.Generic.List[string]
    foreach ($key in $Keys) {
        $desired = Read-GatewayDesiredState -Key $key
        if ($null -ne $desired -and [string]$desired.state -eq 'stopped') {
            $stopped.Add($key)
        }
    }
    return $stopped.ToArray()
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
    $root = [string]$env:SELFUSEAI_GATEWAY_WATCH_DIR
    if (-not $root) {
        $root = Join-Path $env:LOCALAPPDATA 'SelfUseAI\ai-gateway\watch'
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

function Get-GatewayChildEnvironment {
    # Environment injected into a launched child. The single-process branch and
    # both #133 components share the hidden-launch marker, the launch source and
    # the optional StartEnvironment entries. The worker alone gets the UM
    # credentials and the optional Playwright driver; both components get the
    # gateway boot identity and desired-state path so the ingress can decide
    # whether to queue a not-yet-forwarded request.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$StartPath,
        [switch]$IncludeGatewayState,
        [switch]$IncludeCredentials
    )
    $childEnv = @{ SELFUSEAI_HIDDEN_LAUNCH = '1' }
    $sourceProp = $Descriptor.PSObject.Properties['LaunchSourceEnv']
    if ($null -ne $sourceProp -and $sourceProp.Value) {
        $childEnv[[string]$sourceProp.Value] = Get-GatewayActionSource
    }
    # #85: the Rust console resolves its local defaults from the role
    # environment. A batch StartPath (the Python rollback) ignores these entries.
    $environmentProp = $Descriptor.PSObject.Properties['StartEnvironment']
    if ($null -ne $environmentProp -and $null -ne $environmentProp.Value) {
        foreach ($entry in @($environmentProp.Value)) {
            $nameProp = $entry.PSObject.Properties['Name']
            $valueProp = $entry.PSObject.Properties['Value']
            if ($null -ne $nameProp -and $null -ne $valueProp -and $nameProp.Value) {
                $childEnv[[string]$nameProp.Value] = [string]$valueProp.Value
            }
        }
    }
    if ($IncludeCredentials -and $Key -eq 'chat2api') {
        # #120: inject the machine-local UM account into the 8000 worker only.
        # An explicit environment pair wins; the credentials file is used only
        # when that pair is incomplete. The verbose status is desensitized
        # (present/missing/invalid/ACL too broad), never the values themselves.
        $credentials = Get-GatewayLocalUmCredentials
        if ($credentials.Status -eq 'credentials_present') {
            $childEnv['CHAT2API_UM_ACCOUNT'] = [string]$credentials.Account
            $childEnv['CHAT2API_UM_PASSWORD'] = [string]$credentials.Password
        }
        Write-Verbose ('chat2api credentials: ' + [string]$credentials.Status)
    }
    if ($IncludeCredentials -and $Key -eq 'chat2api' -and $StartPath -match '\.exe$') {
        # Optional machine-local Playwright driver. Removing this file and
        # restarting restores the native Rust driver.
        $pythonFile = Join-Path (Get-GatewayRoot) 'data\login-playwright-python'
        if (Test-Path -LiteralPath $pythonFile -PathType Leaf) {
            $pythonPath = (Get-Content -LiteralPath $pythonFile -Raw).Trim()
            if (-not [IO.Path]::IsPathRooted($pythonPath)) {
                throw 'data/login-playwright-python must contain an absolute Python executable path'
            }
            $childEnv['CHAT2API_LOGIN_PLAYWRIGHT_PYTHON'] = $pythonPath
        }
    }
    if ($IncludeGatewayState) {
        # #133 A2: both children read the same boot identity and desired-state
        # marker. An empty boot id is injected as empty so a stale parent value
        # cannot leak in; the ingress treats it as "not running" (fail closed).
        $childEnv['SELFUSEAI_GATEWAY_DESIRED_STATE'] =
            Get-GatewayDesiredStatePath -Key $Key
        $bootId = Get-GatewayBootIdentity
        $childEnv['SELFUSEAI_GATEWAY_BOOT_ID'] = if ($bootId) { [string]$bootId } else { '' }
    }
    return $childEnv
}

function Expand-GatewayComponentArguments {
    # Expand the descriptor placeholders and drop the --boot-id pair when the
    # boot identity is unavailable: passing an empty value would make the next
    # argument parse as the boot id. The ingress then fails closed through the
    # missing SELFUSEAI_GATEWAY_BOOT_ID environment value.
    param(
        [Parameter(Mandatory = $true)][object[]]$Arguments,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )
    $expanded = @()
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = [string]$Arguments[$index]
        if ($argument -eq '--boot-id' -and $index + 1 -lt $Arguments.Count -and
                [string]$Arguments[$index + 1] -eq '{boot-id}' -and
                -not [string]$Values['boot-id']) {
            $index += 1
            continue
        }
        $value = $argument
        foreach ($name in $Values.Keys) {
            $value = $value.Replace('{' + $name + '}', [string]$Values[$name])
        }
        $expanded += $value
    }
    return $expanded
}

function Invoke-GatewayChildLaunch {
    # One child launch with its environment applied and restored around the
    # call. The Launcher seam receives the component name as its fourth
    # argument; existing three-parameter blocks ignore it.
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$ComponentName,
        [Parameter(Mandatory = $true)][string]$StartPath,
        [object[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [hashtable]$ChildEnv = @{},
        [scriptblock]$Launcher
    )
    $saved = @{}
    foreach ($name in $ChildEnv.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $ChildEnv[$name], 'Process')
    }
    try {
        if ($null -ne $Launcher) {
            & $Launcher $Key $StartPath $Descriptor $ComponentName
            return
        }
        if ($Key -eq 'chat2api' -and $ComponentName -in @('worker', 'ingress')) {
            Start-GatewayObservedProcess -Executable $StartPath `
                -Arguments @($Arguments) -WorkingDirectory $WorkingDirectory -Component $ComponentName
        } else {
            Start-Process -FilePath $StartPath -ArgumentList @($Arguments) `
                -WorkingDirectory $WorkingDirectory -WindowStyle Hidden
        }
    } finally {
        foreach ($name in $saved.Keys) {
            if ($null -eq $saved[$name]) {
                Remove-Item -LiteralPath ('Env:' + $name) `
                    -ErrorAction SilentlyContinue
            } else {
                [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
            }
        }
    }
}

function Wait-GatewayComponentHealth {
    # Wait up to TimeoutSeconds for one component's /health. Returns the seconds
    # actually consumed and whether it became healthy. #133 A2/J3: both the
    # startup-window wait and the post-launch wait use this helper so they share
    # one budget and one sleeper seam.
    param(
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)]$Component,
        [scriptblock]$HttpChecker,
        [scriptblock]$Sleeper,
        [int]$TimeoutSeconds = 0
    )
    $consumed = 0
    $healthy = $false
    $timeout = [Math]::Max(0, $TimeoutSeconds)
    for ($elapsed = 0; $elapsed -lt $timeout; $elapsed++) {
        if (Test-GatewayComponentHealth -Descriptor $Descriptor `
                -Component $Component -HttpChecker $HttpChecker) {
            $consumed = $elapsed
            $healthy = $true
            break
        }
        if ($null -ne $Sleeper) { & $Sleeper 1 } else { Start-Sleep -Seconds 1 }
        $consumed = $elapsed + 1
    }
    return [pscustomobject]@{ Healthy = $healthy; Seconds = $consumed }
}

function Start-GatewayComponents {
    # Start only the missing components, worker first. A healthy component is
    # never restarted; a residual component is cleaned through its own identity
    # before the fresh launch. With the Launcher seam (tests) no process table
    # is queried: a failed component health check means "missing".
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)]$Descriptor,
        [Parameter(Mandatory = $true)][string]$StartPath,
        [object[]]$Components = @(),
        [scriptblock]$HttpChecker,
        [scriptblock]$Launcher,
        [scriptblock]$Sleeper
    )

    $workerPortFile = Get-GatewayWorkerPortFilePath -Descriptor $Descriptor
    $desiredStatePath = Get-GatewayDesiredStatePath -Key $Key
    $bootId = Get-GatewayBootIdentity
    $values = @{
        'worker-port-file' = [string]$workerPortFile
        'desired-state-file' = [string]$desiredStatePath
        'boot-id' = if ($bootId) { [string]$bootId } else { '' }
    }
    $workingDirectory = Split-Path -Parent $StartPath
    $workingDirectoryProp = $Descriptor.PSObject.Properties['StartWorkingDirectory']
    if ($null -ne $workingDirectoryProp -and $workingDirectoryProp.Value) {
        $workingDirectory = [IO.Path]::GetFullPath(
            (Join-Path (Get-GatewayRoot) ([string]$workingDirectoryProp.Value)))
    }

    $started = @()
    $skipped = @()
    $workerWaitSeconds = 0
    if ($null -eq $Launcher) {
        # #133 A2/J2: collect every component's Conflict BEFORE launching
        # anything. A conflict discovered only in the loop (e.g. the ingress,
        # after the worker was already started) used to leave an orphan worker.
        $preflight = @{}
        foreach ($component in $Components) {
            $name = [string]$component.Name
            $preflightInspection = Get-GatewayComponentInspection `
                -Descriptor $Descriptor -ComponentName $name
            if ($preflightInspection.State -eq 'Conflict') {
                return [pscustomobject]@{
                    Started = @($started); Skipped = @($skipped)
                    Blocked = $true; Detail = [string]$preflightInspection.Detail
                    Reason = 'Conflict'
                    WorkerWaitSeconds = 0
                }
            }
            $preflight[$name] = $preflightInspection
        }
        # A private process of another shape (the Python rollback or the
        # legacy production instance) is residual next to a split start: stop
        # only identities that no component claims. A component that is Present
        # or Residual is handled by its own branch below and is never stopped
        # here twice.
        $presentNames = @()
        foreach ($component in $Components) {
            $name = [string]$component.Name
            if ($preflight[$name].State -in @('Present', 'Residual', 'Starting')) {
                $presentNames += $name
            }
        }
        $residualIdentities = @()
        foreach ($identity in @(Get-GatewayStopIdentities -Descriptor $Descriptor)) {
            $componentProp = $identity.PSObject.Properties['Component']
            $componentName = ''
            if ($null -ne $componentProp) { $componentName = [string]$componentProp.Value }
            if (-not $componentName -or $componentName -notin $presentNames) {
                $residualIdentities += $identity
            }
        }
        if ($residualIdentities.Count -gt 0) {
            $residualCleanup = Stop-GatewayScopedProcesses -Key $Key `
                -Descriptor $Descriptor -Identities $residualIdentities
            if ($residualCleanup.Status -eq 'StopRefused') {
                return [pscustomobject]@{
                    Started = @($started); Skipped = @($skipped)
                    Blocked = $true
                    Detail = [string]$residualCleanup.Detail
                    WorkerWaitSeconds = 0
                    Reason = 'CleanupRefused'
                }
            }
        }
    }
    foreach ($component in $Components) {
        $name = [string]$component.Name
        # The Launcher seam models "a start was requested": it bypasses the
        # process/listener inspection and the per-component health checks so a
        # test can count launches without touching a real port.
        if ($null -eq $Launcher -and
                (Test-GatewayComponentHealth -Descriptor $Descriptor `
                    -Component $component -HttpChecker $HttpChecker)) {
            $skipped += $name
            continue
        }
        if ($null -eq $Launcher) {
            $componentInspection = Get-GatewayComponentInspection `
                -Descriptor $Descriptor -ComponentName $name
            if ($componentInspection.State -eq 'Conflict') {
                return [pscustomobject]@{
                    Started = @($started); Skipped = @($skipped)
                    Blocked = $true; Detail = [string]$componentInspection.Detail
                    WorkerWaitSeconds = $workerWaitSeconds
                    Reason = 'Conflict'
                }
            }
            if ($componentInspection.State -eq 'Present') {
                # Listening but /health is not ready (e.g. the ingress is
                # waiting for its worker): keep it, never restart it.
                $skipped += $name
                continue
            }
            if ($componentInspection.State -eq 'Starting') {
                # #133 A2/P2: the worker process is alive but has not
                # published its port yet. Wait for it within the remaining
                # start budget; a worker still starting after the budget is
                # stuck, not starting, so it falls through to the Residual
                # cleanup and a fresh launch.
                $remaining = [Math]::Max(0,
                    [int]$Descriptor.StartTimeoutSeconds - $workerWaitSeconds)
                $wait = Wait-GatewayComponentHealth -Descriptor $Descriptor `
                    -Component $component -HttpChecker $HttpChecker `
                    -Sleeper $Sleeper -TimeoutSeconds $remaining
                $workerWaitSeconds += [int]$wait.Seconds
                $componentInspection = Get-GatewayComponentInspection `
                    -Descriptor $Descriptor -ComponentName $name
                if ($componentInspection.State -eq 'Starting' -or
                        $componentInspection.State -eq 'Present') {
                    $skipped += $name
                    continue
                }
                if ($componentInspection.State -eq 'Conflict') {
                    return [pscustomobject]@{
                        Started = @($started); Skipped = @($skipped)
                        Blocked = $true; Detail = [string]$componentInspection.Detail
                        WorkerWaitSeconds = $workerWaitSeconds
                        Reason = 'Conflict'
                    }
                }
                # Residual/Absent: fall through to the cleanup/launch below.
            }
            if ($componentInspection.State -eq 'Residual') {
                $identity = Get-GatewayComponentIdentity -Descriptor $Descriptor `
                    -Name $name
                $cleanup = Stop-GatewayScopedProcesses -Key $Key `
                    -Descriptor $Descriptor -Identities @($identity)
                if ($cleanup.Status -eq 'StopRefused') {
                    return [pscustomobject]@{
                        Started = @($started); Skipped = @($skipped)
                        Blocked = $true
                        Detail = [string]$cleanup.Detail
                        WorkerWaitSeconds = $workerWaitSeconds
                        Reason = 'CleanupRefused'
                    }
                }
            }
        }
        if ($name -eq 'worker') {
            # #133 A2/H3: a new worker publishes a fresh port. A record left
            # by the previous (non-graceful) worker is stale and must not
            # survive into the start, or the ingress could forward to whatever
            # now owns that port.
            [void](Remove-GatewayWorkerPortFile -Descriptor $Descriptor)
        }
        $arguments = Expand-GatewayComponentArguments `
            -Arguments @($component.StartArguments) -Values $values
        $childEnv = Get-GatewayChildEnvironment -Key $Key `
            -Descriptor $Descriptor -StartPath $StartPath `
            -IncludeGatewayState -IncludeCredentials:($name -eq 'worker')
        Invoke-GatewayChildLaunch -Key $Key -Descriptor $Descriptor `
            -ComponentName $name -StartPath $StartPath `
            -Arguments $arguments -WorkingDirectory $workingDirectory `
            -ChildEnv $childEnv -Launcher $Launcher
        $started += $name
        if ($name -eq 'worker' -and $null -eq $Launcher) {
            # Wait for the worker before starting the ingress. A failed worker
            # does not prevent the ingress launch below: the ingress can queue
            # while the worker recovers, and the outer poll reports pending.
            # #133 A2/J3: the wait is reported so the caller can subtract it
            # from the same StartTimeoutSeconds budget instead of granting a
            # second full timeout.
            $remaining = [Math]::Max(0,
                [int]$Descriptor.StartTimeoutSeconds - $workerWaitSeconds)
            $wait = Wait-GatewayComponentHealth -Descriptor $Descriptor `
                -Component $component -HttpChecker $HttpChecker `
                -Sleeper $Sleeper -TimeoutSeconds $remaining
            $workerWaitSeconds += [int]$wait.Seconds
        }
    }
    return [pscustomobject]@{
        Started = @($started); Skipped = @($skipped)
        Blocked = $false; Detail = ''
        WorkerWaitSeconds = $workerWaitSeconds
    }
}

function ConvertTo-GatewayArgumentLine {
    # #133 A2/H2: build the Windows command line for the observer's
    # ProcessStartInfo.Arguments. The A2 --worker-port-file and
    # --desired-state-file values are absolute paths and may contain spaces;
    # joining them bare made the child split them into separate arguments.
    # Arguments without whitespace or quotes stay byte-identical (the fixed CLI
    # line is unchanged); the rest are double-quoted and backslashes before a
    # quote (and trailing backslashes) are doubled per the Windows
    # CommandLineToArgvW rules so the receiving parser recovers the value.
    param([object[]]$Arguments = @())
    $parts = @()
    foreach ($argument in $Arguments) {
        $value = [string]$argument
        if ($value -eq '') {
            $parts += '""'
            continue
        }
        if ($value -notmatch '[\s"]') {
            $parts += $value
            continue
        }
        # A caller may already pass one quoted token (the historical observer
        # fixture shape). Wrapping it again would turn the quotes into literal
        # path characters; keep the single quoted token as-is.
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and
                $value.EndsWith('"') -and
                $value.Substring(1, $value.Length - 2) -notmatch '"') {
            $parts += $value
            continue
        }
        $escaped = [regex]::Replace($value, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        $parts += ('"' + $escaped + '"')
    }
    return ($parts -join ' ')
}

function Start-GatewayObservedProcess {
    param(
        [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory,
        [ValidateSet('gateway', 'ingress', 'worker')][string]$Component = 'gateway'
    )

    $observer = Join-Path (Get-GatewayRoot) 'lib\observe-chat2api.ps1'
    if (!(Test-Path -LiteralPath $observer -PathType Leaf)) {
        throw 'local gateway exit observer script is missing'
    }
    $envName = 'SELFUSEAI_CHAT2API_OBSERVER_LAUNCH'
    $previous = [Environment]::GetEnvironmentVariable($envName, 'Process')
    $definition = @{
        executable = $Executable
        arguments = (ConvertTo-GatewayArgumentLine -Arguments @($Arguments))
        working_directory = $WorkingDirectory
        record_directory = Get-GatewayExitRecordDirectory
        component = $Component
    } | ConvertTo-Json -Compress
    try {
        [Environment]::SetEnvironmentVariable($envName, $definition, 'Process')
        Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @(
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', ('"' + $observer + '"')) -WorkingDirectory $WorkingDirectory -WindowStyle Hidden
    } finally {
        [Environment]::SetEnvironmentVariable($envName, $previous, 'Process')
    }
}

function Start-GatewayService {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [scriptblock]$HttpChecker,
        [scriptblock]$Launcher,
        [scriptblock]$Sleeper
    )

    # Archival metadata remains readable; no desired-state or process action.
    if (Test-GatewayServiceArchived -Key $Key) {
        return [pscustomobject]@{
            Key = $Key; Status = 'Archived'; Action = 'None'
            Detail = '服务已归档；仅保留历史身份，不提供启停'
        }
    }

    $desc = $script:GatewayServiceDescriptors[$Key]
    # Rev13-W order: publish the intent, then make the independent watch entry
    # ready, then launch; the start outcome is recorded after the launch is
    # verified (below). A failed intent write skips the scheduling step - the
    # marker stays unknown and the watch refuses to act, so arming it would
    # only invite an enable/disable fight.
    $intentWritten = [bool](Write-GatewayDesiredState -Key $Key -State 'running')
    $watch = $null
    if ($intentWritten) {
        $watch = Set-GatewayWatchScheduling -Key $Key -Enabled $true
    }

    $startPath = Resolve-GatewayStartPath -Key $Key
    $components = @(Get-GatewayStartComponents -Descriptor $desc)
    $componentMode = Test-GatewayComponentMode -Descriptor $desc -StartPath $startPath

    $inspection = $null
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
        # #133 A2: in component mode a residual/degraded state is handled per
        # component below (only the missing component is started). Stopping the
        # whole pair here would also restart a healthy ingress.
        if (-not $componentMode -and
                $inspection.State -in @('Residual', 'Degraded')) {
            # Cleanup on the way to a start: intent stays "running".
            $cleanup = Stop-GatewayService -Key $Key -KeepDesiredState
            if ($cleanup.Status -ne 'Stopped') {
                $cleanupDetail = ''
                if ($cleanup.PSObject.Properties['Detail']) {
                    $cleanupDetail = [string]$cleanup.Detail
                }
                $detail = '受管残留进程清理失败'
                if ($cleanupDetail) { $detail += '：' + $cleanupDetail }
                $watch = Add-GatewayStartFailureRelease -Key $Key -Watch $watch
                return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                    Key = $Key; Status = 'StartBlocked'; Action = 'None'
                    Detail = $detail
                }) -Watch $watch)
            }
        }
    }

    # Idempotent: don't restart if already healthy. In component mode the
    # inspection is the authority: the ingress /health proxies the worker and can
    # answer 200 from a stale WorkerPortFile, so a Degraded inspection must
    # reach the component start (which fills the missing worker) instead of
    # returning AlreadyRunning.
    if (($null -eq $inspection -or -not $componentMode) -and
            (Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker)) {
        [void](Set-GatewayDesiredStartOutcome -Key $Key -Outcome 'succeeded')
        return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
            Key = $Key; Status = 'AlreadyRunning'; Action = 'None'
        }) -Watch $watch)
    }

    $workerWaitSeconds = 0
    if ($componentMode) {
        # #133 A2: start worker then ingress, skipping whatever is already
        # healthy. A blocked residual cleanup surfaces as StartBlocked.
        $componentStart = Start-GatewayComponents -Key $Key -Descriptor $desc `
            -StartPath $startPath -Components $components `
            -HttpChecker $HttpChecker -Launcher $Launcher -Sleeper $Sleeper
        if ($componentStart.Blocked) {
            $reason = ''
            if ($componentStart.PSObject.Properties['Reason']) {
                $reason = [string]$componentStart.Reason
            }
            $blockedDetail = ''
            if ($componentStart.Detail) { $blockedDetail = [string]$componentStart.Detail }
            $watch = Add-GatewayStartFailureRelease -Key $Key -Watch $watch
            if ($reason -eq 'Conflict') {
                # A component port/ownership conflict is not a residual
                # cleanup failure; report the conflict reason unchanged.
                if (-not $blockedDetail) { $blockedDetail = '组件端口或进程归属冲突' }
                return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                    Key = $Key; Status = 'Conflict'; Action = 'None'
                    Detail = $blockedDetail
                }) -Watch $watch)
            }
            $detail = '受管残留进程清理失败'
            if ($blockedDetail) { $detail += '：' + $blockedDetail }
            return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
                Key = $Key; Status = 'StartBlocked'; Action = 'None'
                Detail = $detail
            }) -Watch $watch)
        }
        if ($componentStart.PSObject.Properties['WorkerWaitSeconds']) {
            $workerWaitSeconds = [Math]::Max(0, [int]$componentStart.WorkerWaitSeconds)
        }
    } elseif ($null -ne $Launcher) {
        & $Launcher $Key $startPath $desc
    } else {
        switch ($desc.StartAction) {
            'HiddenBatch' {
                # #80: the chat2api descriptor points StartPath at the Rust
                # executable and carries its arguments + working directory.
                # A batch StartPath (the Python rollback) ignores the arguments
                # and keeps the historical cmd.exe launch.
                $childEnv = Get-GatewayChildEnvironment -Key $Key `
                    -Descriptor $desc -StartPath $startPath
                $startArguments = @()
                $argumentsProp = $desc.PSObject.Properties['StartArguments']
                if ($null -ne $argumentsProp -and $argumentsProp.Value) {
                    $startArguments = @([string]$argumentsProp.Value)
                }
                $workingDirectory = Split-Path -Parent $startPath
                $workingDirectoryProp = $desc.PSObject.Properties['StartWorkingDirectory']
                if ($null -ne $workingDirectoryProp -and $workingDirectoryProp.Value) {
                    $workingDirectory = [IO.Path]::GetFullPath(
                        (Join-Path (Get-GatewayRoot) ([string]$workingDirectoryProp.Value)))
                }
                Invoke-GatewayChildLaunch -Key $Key -Descriptor $desc `
                    -ComponentName 'gateway' -StartPath $startPath `
                    -Arguments $startArguments -WorkingDirectory $workingDirectory `
                    -ChildEnv $childEnv
            }
            'HiddenExecutable' {
                Start-Process -FilePath $startPath `
                    -ArgumentList @($desc.StartArguments) `
                    -WorkingDirectory (Split-Path -Parent $startPath) `
                    -WindowStyle Hidden
            }
        }
    }

    # Poll up to the service-specific timeout. External launchers can need
    # materially longer than two seconds on a cold start. #133 A2/J3: the worker
    # wait already consumed part of the same budget; do not grant a second full
    # timeout on top of it.
    $healthy = $false
    $timeoutSeconds = [Math]::Max(1, [int]$desc.StartTimeoutSeconds)
    if ($componentMode) {
        $timeoutSeconds = [Math]::Max(0, $timeoutSeconds - $workerWaitSeconds)
    }
    if ($componentMode -and $timeoutSeconds -le 0) {
        # The budget is exhausted; one last check still reports a service that
        # became healthy during the final worker wait.
        $healthy = Test-GatewayServiceHealth -Key $Key -HttpChecker $HttpChecker
    }
    for ($elapsed = 0; -not $healthy -and $elapsed -lt $timeoutSeconds; $elapsed++) {
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

    # Archival metadata remains readable; no desired-state or process action.
    if (Test-GatewayServiceArchived -Key $Key) {
        return [pscustomobject]@{
            Key = $Key; Status = 'Archived'; Action = 'None'
            Detail = '服务已归档；仅保留历史身份，不提供启停'
        }
    }

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

    $startPath = $null
    try { $startPath = Resolve-GatewayStartPath -Key $Key } catch { }
    $componentMode = Test-GatewayComponentMode -Descriptor $desc -StartPath $startPath

    $inspection = $null
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
    # #118: a Residual inspection means a managed process is still alive even
    # though the port is not listening. "Not healthy" alone must not turn that
    # into AlreadyStopped: the caller asked to stop, and the scoped stop below is
    # exactly the verified cleanup of that instance. Degraded (listening but
    # unhealthy) keeps the historical StopWhenUnhealthy gate.
    # #133 A2: a component pair may have one listener down and one process
    # alive; the scoped stop is still the verified cleanup, so it never falls
    # back to AlreadyStopped from health alone.
    $residualPending = ($null -ne $inspection -and $inspection.State -eq 'Residual')
    if ($componentMode) {
        # #133 A2: a component pair is Degraded when exactly one component is
        # missing; the scoped stop is still the verified cleanup of whatever is
        # alive, so it must not fall back to AlreadyStopped from health alone.
        $residualPending = ($null -eq $inspection -or
            $inspection.State -in @('Residual', 'Degraded'))
    }
    if (-not $wasHealthy -and -not $stopWhenUnhealthy -and -not $residualPending) {
        return (Add-GatewayWatchToResult -Result ([pscustomobject]@{
            Key = $Key; Status = 'AlreadyStopped'; Action = 'None'
        }) -Watch $watch)
    }

    # Exit observability (round 18): the scoped stop reports the instances it
    # actually terminated; a null value means "this strategy did not report".
    $scoped = $null

    if ($null -ne $Stopper) {
        & $Stopper $Key $desc
    } elseif ($componentMode) {
        # #133 A2: ingress shutdown first (it releases queued requests and
        # forwards the stop to the worker), then the worker's own shutdown
        # endpoint, then the verified scoped stop as the fallback. The ingress
        # is addressed at 8000; the worker port comes from WorkerPortFile.
        Write-GatewayGracefulStopRequests -Descriptor $desc
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
        if ($gracefulRequested) {
            if ($null -ne $Sleeper) { & $Sleeper 3 } else { Start-Sleep -Seconds 3 }
        }
        # #133 A2/H3: never POST a shutdown to a stale port; only the port a
        # verified worker identity process owns is addressed.
        $workerPort = Get-GatewayWorkerRuntimePort -Descriptor $desc
        if ($null -ne $workerPort) {
            try {
                $previousProgressPreference = $ProgressPreference
                try {
                    $ProgressPreference = 'SilentlyContinue'
                    Invoke-WebRequest -Method Post `
                        -Uri ('http://127.0.0.1:{0}/admin/shutdown' -f $workerPort) `
                        -Headers (Get-LocalControlHeader) `
                        -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop | Out-Null
                } finally {
                    $ProgressPreference = $previousProgressPreference
                }
            } catch { }
        }
        # #133 A2/J4: give the worker a bounded chance to exit after its
        # graceful shutdown POST before the verified force-stop fallback runs.
        $stopTimeoutSeconds = 5
        $stopTimeoutProp = $desc.PSObject.Properties['StopTimeoutSeconds']
        if ($null -ne $stopTimeoutProp) {
            $stopTimeoutSeconds = [Math]::Max(1, [int]$stopTimeoutProp.Value)
        }
        for ($elapsed = 0; $elapsed -lt $stopTimeoutSeconds; $elapsed++) {
            $workerPids = Get-GatewayWorkerProcessIds -Descriptor $desc
            if ($null -ne $workerPids -and @($workerPids).Count -eq 0) { break }
            if ($null -ne $Sleeper) { & $Sleeper 1 } else { Start-Sleep -Seconds 1 }
        }
        $scoped = Stop-GatewayScopedProcesses -Key $Key -Descriptor $desc
        if ($scoped.Status -eq 'StopRefused') {
            return (Add-GatewayWatchToResult -Result $scoped -Watch $watch)
        }
        # A force-stopped worker never removes its own record; remove it here
        # so the next start/ingress cannot reuse a dead port.
        [void](Remove-GatewayWorkerPortFile -Descriptor $desc)
    } else {
        switch ($desc.StopStrategy) {
            'GracefulHttp' {
                # The graceful request itself is already instance-scoped: only
                # the private instance's own control token opens its
                # /admin/shutdown (the share backend reads
                # CHAT2API_CONTROL_TOKEN_FILE from its own data dir,
                # console/share_production.py:1236-1246). The FORCE-KILL below
                # is the part that used to select the share backend, so it runs
                # through the verified stop scope.
                if ($Key -eq 'chat2api') { Write-GatewayGracefulStopRequests -Descriptor $desc }
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
            'Port' {
                $scoped = Stop-GatewayScopedProcesses -Key $Key -Descriptor $desc -LoopbackPortOnly
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

    # Archival metadata remains readable; no desired-state or process action.
    if (Test-GatewayServiceArchived -Key $Key) {
        return [pscustomobject]@{
            Key = $Key; Status = 'Archived'; Action = 'None'
            Detail = '服务已归档；仅保留历史身份，不提供启停'
        }
    }

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
