# Offline regression tests for the gateway runtime module.
# Uses injected hooks to verify start/stop/status calls without touching
# real processes or the task scheduler. Health-only cases use isolated
# ephemeral loopback sockets, never a configured service port.
param([switch]$HealthOnly, [switch]$LauncherOnly)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$runtimeLibrary = Join-Path $root 'lib\gateway-runtime.ps1'
$script:failures = 0

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ([object]::Equals($Actual, $Expected)) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }

    $script:failures++
    Write-Host "FAIL $Name`: expected '$Expected', got '$Actual'" -ForegroundColor Red
}

function Assert-Null {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Actual,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Actual) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }

    $script:failures++
    Write-Host "FAIL $Name`: expected null, got '$Actual'" -ForegroundColor Red
}

. $runtimeLibrary

# #133 A2: every offline scenario that reaches the split 8000 branch reads the
# worker port from WorkerPortFile. Point it at an empty temp file for the whole
# suite so no test can read a real data/chat2api-worker.port; component tests
# write their own port there. The real descriptor value is restored at the end.
$script:workerPortRoot = Join-Path ([IO.Path]::GetTempPath()) ('gateway-worker-port-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
[void][IO.Directory]::CreateDirectory($script:workerPortRoot)
$script:workerPortFile = Join-Path $script:workerPortRoot 'chat2api-worker.port'
$script:originalWorkerPortFile = [string]$script:GatewayServiceDescriptors['chat2api'].WorkerPortFile
$script:GatewayServiceDescriptors['chat2api'].WorkerPortFile = $script:workerPortFile

# ---------------------------------------------------------------------------
# Real HTTP boundary under the launcher's StrictMode 3.0.
# Missing Response must mean unreachable; an HTTP error response still proves
# a listener exists. The child serves one raw response on an ephemeral port.
# ---------------------------------------------------------------------------
& {
    Set-StrictMode -Version 3.0
    $originalPort = $script:GatewayServiceDescriptors['usage'].Port
    $originalChat2ApiPort = $script:GatewayServiceDescriptors['chat2api'].Port
    $originalProgressPreference = $ProgressPreference
    $ProgressPreference = 'Continue'

    function Assert-IsolatedHealth {
        param([bool]$Expected, [string]$Name)
        $healthValue = $null
        $healthError = $null
        try {
            $healthValue = Test-GatewayServiceHealth -Key usage -TimeoutSeconds 2
        } catch {
            $healthError = $_.Exception.GetType().Name
        }
        Assert-Null $healthError "$Name returns without a strict-mode exception"
        Assert-Equal ($healthValue -is [bool] -and $healthValue -eq $Expected) $true "$Name returns the expected reachability"
        Assert-Equal ([string]$ProgressPreference) 'Continue' "$Name restores ProgressPreference"
    }

    function Assert-IsolatedProbe {
        param(
            [Parameter(Mandatory = $true)][string]$Url,
            [Parameter(Mandatory = $true)][bool]$ExpectedReady,
            [Parameter(Mandatory = $true)][bool]$ExpectedResponded,
            [Parameter(Mandatory = $true)][AllowNull()]$ExpectedStatus,
            [Parameter(Mandatory = $true)][AllowNull()]$ExpectedReason,
            [Parameter(Mandatory = $true)][string]$Name
        )
        $probeValue = $null
        $probeError = $null
        try {
            $probeValue = Invoke-GatewayReadyProbe -Url $Url -TimeoutSeconds 5
        } catch {
            $probeError = $_.Exception.GetType().Name + ':' + $_.FullyQualifiedErrorId
        }
        Assert-Null $probeError "$Name returns without a strict-mode exception"
        Assert-Equal ($null -ne $probeValue) $true "$Name returns a probe result"
        if ($null -eq $probeValue) { return }
        Assert-Equal ([bool]$probeValue.Ready) $ExpectedReady "$Name reports the expected readiness"
        Assert-Equal ([bool]$probeValue.Responded) $ExpectedResponded "$Name distinguishes a missing HTTP response"
        if ($null -eq $ExpectedStatus) {
            Assert-Null $probeValue.HttpStatus "$Name reports no HTTP status"
        } else {
            Assert-Equal $probeValue.HttpStatus $ExpectedStatus "$Name reports the expected HTTP status"
        }
        if ($ExpectedReason -is [string]) {
            Assert-Equal ([string]$probeValue.Reason) $ExpectedReason "$Name names the sanitized probe reason"
        } else {
            Assert-Equal (@($ExpectedReason) -contains [string]$probeValue.Reason) $true "$Name names a sanitized no-response reason"
        }
        Assert-Equal ([string]$ProgressPreference) 'Continue' "$Name restores ProgressPreference"
        Assert-Equal (($probeValue | ConvertTo-Json -Compress) -notmatch 'http://') $true "$Name never carries the probe URL"
    }

    try {
        # Bind without Listen: the isolated closed port remains reserved so
        # another process cannot claim it between allocation and the request.
        $closedSocket = [System.Net.Sockets.Socket]::new(
            [System.Net.Sockets.AddressFamily]::InterNetwork,
            [System.Net.Sockets.SocketType]::Stream,
            [System.Net.Sockets.ProtocolType]::Tcp)
        try {
            $closedSocket.Bind([System.Net.IPEndPoint]::new(
                [System.Net.IPAddress]::Loopback, 0))
            $script:GatewayServiceDescriptors['usage'].Port = $closedSocket.LocalEndPoint.Port
            Assert-IsolatedHealth -Expected $false -Name 'StrictMode connection refused'
            Assert-IsolatedProbe -Url ('http://127.0.0.1:{0}/ready' -f $closedSocket.LocalEndPoint.Port) `
                -ExpectedReady $false -ExpectedResponded $false -ExpectedStatus $null `
                -ExpectedReason 'connect_refused' -Name 'StrictMode probe on a closed port'

            $script:checkerCalls = 0
            $checkerPort = $script:GatewayServiceDescriptors['usage'].Port
            $injected = Test-GatewayServiceHealth -Key usage -TimeoutSeconds 7 -HttpChecker {
                param($url, $timeout)
                $script:checkerCalls++
                Assert-Equal $url "http://127.0.0.1:$checkerPort/health" 'injected checker receives the descriptor URL'
                Assert-Equal $timeout 7 'injected checker receives the timeout'
                return $true
            }
            Assert-Equal $injected $true 'injected checker remains authoritative without HTTP'
            Assert-Equal $script:checkerCalls 1 'injected checker executes exactly once'
            Assert-Equal ([string]$ProgressPreference) 'Continue' 'injected checker preserves ProgressPreference'
        } finally {
            $closedSocket.Dispose()
        }

        function Start-IsolatedHttpFixture {
            param([int]$StatusCode)
            $job = Start-Job -ArgumentList $StatusCode -ScriptBlock {
                param([int]$StatusCode)
                $ErrorActionPreference = 'Stop'
                $listener = [System.Net.Sockets.TcpListener]::new(
                    [System.Net.IPAddress]::Loopback, 0)
                $client = $null
                try {
                    $listener.Start(1)
                    Write-Output $listener.LocalEndpoint.Port
                    $accepted = $listener.AcceptTcpClientAsync()
                    if (-not $accepted.Wait(10000)) { throw 'isolated HTTP accept timed out' }
                    $client = $accepted.Result
                    $stream = $client.GetStream()
                    $stream.ReadTimeout = 5000
                    $buffer = [byte[]]::new(4096)
                    [void]$stream.Read($buffer, 0, $buffer.Length)
                    if ($StatusCode -ne 0) {
                        $response = "HTTP/1.1 $StatusCode Fixture`r`nContent-Length: 0`r`nConnection: close`r`n`r`n"
                        $bytes = [System.Text.Encoding]::ASCII.GetBytes($response)
                        $stream.Write($bytes, 0, $bytes.Length)
                        $stream.Flush()
                    }
                } finally {
                    if ($null -ne $client) { $client.Dispose() }
                    $listener.Stop()
                }
            }
            $ready = @()
            $readyDeadline = [DateTime]::UtcNow.AddSeconds(10)
            while ($ready.Count -eq 0 -and [DateTime]::UtcNow -lt $readyDeadline) {
                $ready = @(Receive-Job -Job $job -Keep)
                if ($ready.Count -eq 0) { Start-Sleep -Milliseconds 20 }
            }
            if ($ready.Count -ne 1) {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
                Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
                throw 'isolated HTTP listener did not report its ephemeral port'
            }
            return [pscustomobject]@{ Job = $job; Port = [int]$ready[0] }
        }

        function Stop-IsolatedHttpFixture {
            param($Fixture)
            if ($null -eq $Fixture) { return }
            Stop-Job -Job $Fixture.Job -ErrorAction SilentlyContinue
            Remove-Job -Job $Fixture.Job -Force -ErrorAction SilentlyContinue
        }

        foreach ($statusCode in @(200, 403, 500, 0)) {
            $fixture = $null
            try {
                $fixture = Start-IsolatedHttpFixture -StatusCode $statusCode
                $script:GatewayServiceDescriptors['usage'].Port = $fixture.Port
                if ($statusCode -eq 0) {
                    Assert-IsolatedHealth -Expected $false -Name 'StrictMode connection closed without HTTP response'
                } else {
                    Assert-IsolatedHealth -Expected $true -Name "StrictMode HTTP $statusCode"
                }
                $completed = Wait-Job -Job $fixture.Job -Timeout 5
                Assert-Equal ($null -ne $completed -and $fixture.Job.State -eq 'Completed') $true 'isolated HTTP child finishes after one request'
                Receive-Job -Job $fixture.Job | Out-Null
            } finally {
                Stop-IsolatedHttpFixture -Fixture $fixture
            }
        }

        # #133 A2: chat2api requires /health 200. The ingress proxies the
        # worker and answers 503 while the worker is missing, so any other HTTP
        # response is not "healthy" (the 8001 reachability semantics stays).
        foreach ($statusCode in @(200, 503)) {
            $fixture = $null
            try {
                $fixture = Start-IsolatedHttpFixture -StatusCode $statusCode
                $script:GatewayServiceDescriptors['chat2api'].Port = $fixture.Port
                $chat2apiHealth = Test-GatewayServiceHealth -Key chat2api -TimeoutSeconds 2
                Assert-Equal ($chat2apiHealth -eq ($statusCode -eq 200)) $true "chat2api health requires 200 (HTTP $statusCode)"
                $completed = Wait-Job -Job $fixture.Job -Timeout 5
                Assert-Equal ($null -ne $completed -and $fixture.Job.State -eq 'Completed') $true 'chat2api health child finishes after one request'
                Receive-Job -Job $fixture.Job | Out-Null
            } finally {
                Stop-IsolatedHttpFixture -Fixture $fixture
            }
        }

        # #117: the probe distinguishes "no HTTP response at all" from "an HTTP
        # response that is not 200". Each status needs its own single-response
        # fixture: the health check above already consumed the other listeners.
        foreach ($statusCode in @(200, 503, 0)) {
            $fixture = $null
            try {
                $fixture = Start-IsolatedHttpFixture -StatusCode $statusCode
                $probeUrl = 'http://127.0.0.1:{0}/ready' -f $fixture.Port
                if ($statusCode -eq 0) {
                    Assert-IsolatedProbe -Url $probeUrl -ExpectedReady $false `
                        -ExpectedResponded $false -ExpectedStatus $null `
                        -ExpectedReason @('no_response', 'probe_timeout', 'connect_failed', 'connect_refused', 'dns_failure') `
                        -Name 'StrictMode probe on a closed connection'
                } else {
                    $probeReason = if ($statusCode -eq 200) { 'ready' } else { 'http_not_ready' }
                    Assert-IsolatedProbe -Url $probeUrl -ExpectedReady ($statusCode -eq 200) `
                        -ExpectedResponded $true -ExpectedStatus $statusCode `
                        -ExpectedReason $probeReason -Name "StrictMode probe HTTP $statusCode"
                }
                $completed = Wait-Job -Job $fixture.Job -Timeout 5
                Assert-Equal ($null -ne $completed -and $fixture.Job.State -eq 'Completed') $true 'isolated probe child finishes after one request'
                Receive-Job -Job $fixture.Job | Out-Null
            } finally {
                Stop-IsolatedHttpFixture -Fixture $fixture
            }
        }
    } finally {
        $script:GatewayServiceDescriptors['usage'].Port = $originalPort
        $script:GatewayServiceDescriptors['chat2api'].Port = $originalChat2ApiPort
        $ProgressPreference = $originalProgressPreference
    }
}

# ---------------------------------------------------------------------------
# #117: the probe failure classification is a fixed, sanitized vocabulary.
# Synthetic exceptions cover the timeout and DNS paths (a real probe timeout is
# slow and flaky) and prove that a message carrying a secret never reaches the
# result.
# ---------------------------------------------------------------------------
& {
    Set-StrictMode -Version 3.0
    # PS 5.1 does not load System.Net.Http by default; the production PS7
    # transport exception lives there.
    Add-Type -AssemblyName System.Net.Http
    $secret = 'secret-token-117@example.invalid'
    $refused = [System.Net.Http.HttpRequestException]::new(
        'connect failed ' + $secret,
        [System.Net.Sockets.SocketException]::new(10061))
    Assert-Equal (Get-GatewayProbeFailureReason -Exception $refused) 'connect_refused' `
        'connection refused classifies as connect_refused'
    $dns = [System.Net.Http.HttpRequestException]::new(
        'name resolution failed ' + $secret,
        [System.Net.Sockets.SocketException]::new(11001))
    Assert-Equal (Get-GatewayProbeFailureReason -Exception $dns) 'dns_failure' `
        'name resolution failure classifies as dns_failure'
    $timeout = [System.Threading.Tasks.TaskCanceledException]::new(
        'the request timed out ' + $secret)
    Assert-Equal (Get-GatewayProbeFailureReason -Exception $timeout) 'probe_timeout' `
        'a cancelled request classifies as probe_timeout'
    $webTimeout = [System.Net.WebException]::new(
        'the operation timed out ' + $secret,
        [System.Net.WebExceptionStatus]::Timeout)
    Assert-Equal (Get-GatewayProbeFailureReason -Exception $webTimeout) 'probe_timeout' `
        'a WebException timeout classifies as probe_timeout'
    $unknown = [System.InvalidOperationException]::new('unexpected ' + $secret)
    Assert-Equal (Get-GatewayProbeFailureReason -Exception $unknown) 'no_response' `
        'an unrecognized transport failure classifies as no_response'
    foreach ($exception in @($refused, $dns, $timeout, $webTimeout, $unknown)) {
        $reason = Get-GatewayProbeFailureReason -Exception $exception
        Assert-Equal ($reason -notmatch [regex]::Escape($secret)) $true `
            'the probe reason never carries the exception message'
        Assert-Equal ($reason -match '^[a-z_]+$') $true `
            'the probe reason stays in the sanitized vocabulary'
    }
}

if ($HealthOnly) {
    if ($script:failures -gt 0) {
        Write-Host "Health checks failed: $($script:failures)" -ForegroundColor Red
        exit 1
    }
    Write-Host 'All isolated gateway health tests passed.' -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------
# Descriptor integrity
# ---------------------------------------------------------------------------

$descriptors = Get-GatewayServiceDescriptors
$expectedKeys = @('chat2api', 'usage')
Assert-Equal (@($descriptors.Keys) -join ',') ($expectedKeys -join ',') 'two service descriptors in stable order'

# Unique keys and ports
$ports = @($descriptors.Values | ForEach-Object { $_.Port })
Assert-Equal ($ports | Select-Object -Unique).Count $ports.Count 'all service ports are unique'

$healthEndpoints = @($descriptors.Values | ForEach-Object { "$($_.Port)$($_.HealthPath)" })
Assert-Equal ($healthEndpoints | Select-Object -Unique).Count $healthEndpoints.Count 'all health endpoints (port+path) are unique'

# ---------------------------------------------------------------------------
# Startup order
# ---------------------------------------------------------------------------

$startOrder = Get-GatewayStartOrder
Assert-Equal ($startOrder -join ',') 'chat2api,usage' 'startup order keeps active sources ahead of consumers'

$stopOrder = Get-GatewayStopOrder
Assert-Equal ($stopOrder -join ',') 'usage,chat2api' 'stop order keeps the established consumer/source relative order'

# ---------------------------------------------------------------------------
# Port and health URL mapping
# ---------------------------------------------------------------------------

Assert-Equal $descriptors['chat2api'].Port 8000 'chat2api port is 8000'
Assert-Equal $descriptors['chat2api'].HealthPath '/health' 'chat2api health path is /health'
Assert-Equal $descriptors['chat2api'].AuthStatusUrl 'http://127.0.0.1:8000/admin/auth/status' 'chat2api auth status URL is local'
Assert-Equal $descriptors['chat2api'].ReauthenticationUrl 'http://127.0.0.1:8000/admin/auth/login' 'chat2api reauthentication URL is local'
Assert-Equal $descriptors['chat2api'].ShutdownUrl 'http://127.0.0.1:8000/admin/shutdown' 'chat2api shutdown URL is local'
Assert-Equal $descriptors['chat2api'].StopWhenUnhealthy $true 'chat2api stop cleans up a pre-listen process'
Assert-Equal $descriptors['chat2api'].StopCommandLinePattern '(^|\s)chat2api\.py(\s|$)' 'chat2api stop targets only its entrypoint'
Assert-Equal $descriptors['chat2api'].WatchTaskName 'SelfUseAI AI Gateway Chat2API Watch' 'chat2api carries its independent watch task name (Rev13-W)'
Assert-Equal $script:originalWorkerPortFile 'data/chat2api-worker.port' 'chat2api worker publishes its ephemeral port in the fixed file'
Assert-Equal $descriptors['chat2api'].HealthRequiresOk $true 'chat2api requires /health 200 (the ingress proxies the worker)'
Assert-Equal $descriptors['chat2api'].RecoveryIdentity.Portless $true 'the watch recovery identity ignores the worker --port 0'
Assert-Equal ($descriptors['chat2api'].RecoveryIdentity.RequiredArgumentPatterns -join ',') '--mode worker(\s|$)' 'the watch recovery identity is the worker mode only'
$chat2apiComponents = @($descriptors['chat2api'].StartComponents)
Assert-Equal $chat2apiComponents.Count 2 'chat2api starts two components'
Assert-Equal (($chat2apiComponents | ForEach-Object { [string]$_.Name }) -join ',') 'worker,ingress' 'the worker starts before the ingress'
Assert-Equal (($chat2apiComponents[0].StartArguments -join ' ') -match '^--mode worker --port 0 --role local --worker-port-file \{worker-port-file\} ') $true 'the worker argument line matches the fixed CLI'
Assert-Equal (($chat2apiComponents[1].StartArguments -join ' ') -match '^--mode ingress --port 8000 --worker-port-file \{worker-port-file\} --desired-state-file \{desired-state-file\} --boot-id \{boot-id\} ') $true 'the ingress argument line matches the fixed CLI'
Assert-Equal ([string]$chat2apiComponents[0].HealthUrl) 'http://127.0.0.1:{worker-port}/health' 'the worker health URL is built from WorkerPortFile'
Assert-Equal ([string]$chat2apiComponents[1].HealthUrl) 'http://127.0.0.1:8000/health' 'the ingress health URL is 8000/health'
Assert-Null $descriptors['usage'].PSObject.Properties['StartComponents'] 'usage keeps the single-process descriptor'
Assert-Equal $descriptors['usage'].Port 8001 'usage port is 8001'
Assert-Equal $descriptors['usage'].HealthPath '/health' 'usage health path is /health'

# Open URLs
Assert-Equal $descriptors['usage'].OpenUrl 'http://127.0.0.1:8001/' 'usage opens 8001'
Assert-Null $descriptors['chat2api'].OpenUrl 'chat2api has no open URL'

# ---------------------------------------------------------------------------
# Start: idempotent when already healthy
# ---------------------------------------------------------------------------

$launcherCalls = New-Object System.Collections.ArrayList
$result = Start-GatewayService -Key 'chat2api' `
    -HttpChecker { param($url, $timeout) $true } `
    -Launcher { param($key, $path, $desc) [void]$launcherCalls.Add($key) }

Assert-Equal $result.Status 'AlreadyRunning' 'start is idempotent when service is healthy'
Assert-Equal $result.Action 'None' 'no start action when already healthy'
Assert-Equal $launcherCalls.Count 0 'no launcher call when already healthy'

# ---------------------------------------------------------------------------
# Start: calls launcher when not healthy
# ---------------------------------------------------------------------------

$launcherCalls.Clear()
$sleeperCalls = New-Object System.Collections.ArrayList
$componentEnvironments = New-Object System.Collections.ArrayList
$desiredStatePath = Get-GatewayDesiredStatePath -Key 'chat2api'
$result = Start-GatewayService -Key 'chat2api' `
    -HttpChecker { param($url, $timeout) $false } `
    -Launcher {
        param($key, $path, $desc, $component)
        [void]$launcherCalls.Add(@($key, $path, $component))
        [void]$componentEnvironments.Add([pscustomobject]@{
            Component = $component
            BootId = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_BOOT_ID', 'Process')
            DesiredState = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_DESIRED_STATE', 'Process')
        })
    } `
    -Sleeper { param($seconds) [void]$sleeperCalls.Add($seconds) }

Assert-Equal $result.Status 'StartPending' 'start reports pending when still unhealthy after launch'
Assert-Equal $launcherCalls.Count 2 'component start launches the worker and the ingress'
Assert-Equal (($launcherCalls | ForEach-Object { $_[2] }) -join ',') 'worker,ingress' 'the worker is launched before the ingress'
Assert-Equal $launcherCalls[0][0] 'chat2api' 'the worker launcher received the correct service key'
Assert-Equal $componentEnvironments.Count 2 'both components receive the gateway environment'
Assert-Equal ($componentEnvironments[0].DesiredState -eq $desiredStatePath) $true 'the worker receives SELFUSEAI_GATEWAY_DESIRED_STATE'
Assert-Equal ($componentEnvironments[1].DesiredState -eq $desiredStatePath) $true 'the ingress receives SELFUSEAI_GATEWAY_DESIRED_STATE'
Assert-Equal ($componentEnvironments[0].BootId -eq $componentEnvironments[1].BootId) $true 'both components receive the same SELFUSEAI_GATEWAY_BOOT_ID'
Assert-Equal ($null -ne $componentEnvironments[0].BootId) $true 'the boot identity is injected when readable'
Assert-Equal $sleeperCalls.Count $descriptors['chat2api'].StartTimeoutSeconds 'start polls until its timeout'

# ---------------------------------------------------------------------------
# Start: reports Started when health check passes after launch
# ---------------------------------------------------------------------------

$launcherCalls.Clear()
$healthResults = [System.Collections.Generic.Queue[bool]]::new()
$healthResults.Enqueue($false)  # before launch: not healthy
$healthResults.Enqueue($true)   # after launch: healthy
$result = Start-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $healthResults.Dequeue() } `
    -Launcher { param($key, $path, $desc) [void]$launcherCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'Started' 'start reports Started when health check passes after launch'
Assert-Equal $launcherCalls.Count 1 'launcher called once'

# ---------------------------------------------------------------------------
# Stop: calls stopper hook. Generic stop scenarios run against usage, the
# stable consumer service used by the generic stop scenarios.
# ---------------------------------------------------------------------------

$stopperCalls = New-Object System.Collections.ArrayList
$stopHealthResults = [System.Collections.Generic.Queue[bool]]::new()
$stopHealthResults.Enqueue($true)
$stopHealthResults.Enqueue($false)
$result = Stop-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $stopHealthResults.Dequeue() } `
    -Stopper { param($key, $desc) [void]$stopperCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'Stopped' 'stop reports Stopped'
Assert-Equal $stopperCalls.Count 1 'stopper called once'
Assert-Equal $stopperCalls[0] 'usage' 'stopper received the correct service key'

$stopperCalls.Clear()
$result = Stop-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $false } `
    -Stopper { param($key, $desc) [void]$stopperCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'AlreadyStopped' 'stop is idempotent when service is already stopped'
Assert-Equal $result.Action 'None' 'no stop action when already stopped'
Assert-Equal $stopperCalls.Count 0 'no stopper call when already stopped'

$stopperCalls.Clear()
$result = Stop-GatewayService -Key 'chat2api' `
    -HttpChecker { param($url, $timeout) $false } `
    -Stopper { param($key, $desc) [void]$stopperCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'Stopped' 'chat2api stop handles a process that has not opened its port'
Assert-Equal $stopperCalls.Count 1 'chat2api pre-listen stop still invokes cleanup'

$stopHealthResults = [System.Collections.Generic.Queue[bool]]::new()
$stopHealthResults.Enqueue($true)
$stopHealthResults.Enqueue($true)
$result = Stop-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $stopHealthResults.Dequeue() } `
    -Stopper { param($key, $desc) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'StopPending' 'stop reports pending when service remains healthy'

# ---------------------------------------------------------------------------
# Restart: composes stop + start through the same injected hooks.
# ---------------------------------------------------------------------------

$restartStopperCalls = New-Object System.Collections.ArrayList
$restartLauncherCalls = New-Object System.Collections.ArrayList
$restartHealth = [System.Collections.Generic.Queue[bool]]::new()
$restartHealth.Enqueue($true)   # stop: was healthy
$restartHealth.Enqueue($false)  # stop post-check: now stopped
$restartHealth.Enqueue($false)  # start pre-check: not already running
$restartHealth.Enqueue($true)   # start post-launch: healthy again
$result = Restart-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $restartHealth.Dequeue() } `
    -Launcher { param($key, $path, $desc) [void]$restartLauncherCalls.Add($key) } `
    -Stopper { param($key, $desc) [void]$restartStopperCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'Restarted' 'restart reports Restarted when stop and start both complete'
Assert-Equal $result.Action 'Restart' 'restart action is reported'
Assert-Equal $restartStopperCalls.Count 1 'restart stops through the descriptor stopper'
Assert-Equal $restartLauncherCalls.Count 1 'restart launches through the descriptor launcher'

$restartStopperCalls.Clear()
$restartLauncherCalls.Clear()
$restartHealth = [System.Collections.Generic.Queue[bool]]::new()
$restartHealth.Enqueue($true)   # stop: was healthy
$restartHealth.Enqueue($true)   # stop post-check: still healthy -> StopPending
$result = Restart-GatewayService -Key 'usage' `
    -HttpChecker { param($url, $timeout) $restartHealth.Dequeue() } `
    -Launcher { param($key, $path, $desc) [void]$restartLauncherCalls.Add($key) } `
    -Stopper { param($key, $desc) [void]$restartStopperCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $result.Status 'StopPending' 'failed stop surfaces as StopPending, no silent success'
Assert-Equal $result.Action 'Restart' 'restart action recorded for the failed stop'
Assert-Equal $restartLauncherCalls.Count 0 'no launch attempt when the stop did not complete'
Assert-Equal $restartStopperCalls.Count 1 'the stop attempt itself still ran'

# ---------------------------------------------------------------------------
# Start-All: calls all services in order
# ---------------------------------------------------------------------------

$startAllCalls = New-Object System.Collections.ArrayList
$results = Start-AllGatewayServices `
    -HttpChecker { param($url, $timeout) $false } `
    -Launcher {
        param($key, $path, $desc, $component)
        [void]$startAllCalls.Add([pscustomobject]@{
            Key = $key; Component = [string]$component
        })
    } `
    -Sleeper { param($seconds) }

Assert-Equal $results.Count 2 'start-all launches the two active services'
Assert-Equal (($startAllCalls | ForEach-Object { $_.Key }) -join ',') 'chat2api,chat2api,usage' 'start-all follows the fixed startup order'
Assert-Equal (($startAllCalls | ForEach-Object {
    if ($_.Component) { [string]$_.Component } else { 'single' }
}) -join ',') 'worker,ingress,single' 'start-all launches the worker before the ingress'

# ---------------------------------------------------------------------------
# Stop-All: calls all services in reverse order
# ---------------------------------------------------------------------------

$stopAllCalls = New-Object System.Collections.ArrayList
$stopAllHealthResults = [System.Collections.Generic.Queue[bool]]::new()
1..2 | ForEach-Object {
    $stopAllHealthResults.Enqueue($true)
    $stopAllHealthResults.Enqueue($false)
}
$results = Stop-AllGatewayServices `
    -HttpChecker { param($url, $timeout) $stopAllHealthResults.Dequeue() } `
    -Stopper { param($key, $desc) [void]$stopAllCalls.Add($key) } `
    -Sleeper { param($seconds) }

Assert-Equal $results.Count 2 'stop-all covers the two active descriptors'
Assert-Equal ($stopAllCalls -join ',') 'usage,chat2api' 'stop-all follows the reverse startup order'

# ---------------------------------------------------------------------------
# Proxy environment
# ---------------------------------------------------------------------------

$proxyEnv = Get-GatewayProxyEnvironment
$proxyMap = @{}
foreach ($entry in $proxyEnv) { $proxyMap[$entry.Name] = $entry.Value }
Assert-Equal $proxyMap['HTTP_PROXY'] '' 'proxy HTTP_PROXY is cleared'
Assert-Equal $proxyMap['HTTPS_PROXY'] '' 'proxy HTTPS_PROXY is cleared'
Assert-Equal $proxyMap['ALL_PROXY'] '' 'proxy ALL_PROXY is cleared'
Assert-Equal $proxyMap['http_proxy'] '' 'proxy http_proxy is cleared'
Assert-Equal $proxyMap['NO_PROXY'] '*' 'proxy NO_PROXY is set to *'
Assert-Equal $proxyMap['no_proxy'] '*' 'proxy no_proxy is set to *'
Assert-Equal $proxyEnv.Count 8 'proxy environment has 8 entries (4 cleared + 4 case variants)'

# ---------------------------------------------------------------------------
# Health check URL construction
# ---------------------------------------------------------------------------

$healthCheckCalls = New-Object System.Collections.ArrayList
Test-GatewayServiceHealth -Key 'chat2api' -HttpChecker {
    param($url, $timeout)
    [void]$healthCheckCalls.Add($url)
    return $true
}
Assert-Equal $healthCheckCalls[0] 'http://127.0.0.1:8000/health' 'active gateway health URL is correct'

$runtimeSource = Get-Content -LiteralPath $runtimeLibrary -Raw
Assert-Equal ($runtimeSource -notmatch 'python manage\.py') $true 'retired managed launch and stop implementation is removed'
# HTTP error reachability is exercised above with real 403/500 responses;
# requiring the old Exception.Response spelling would reintroduce the bug.
Assert-Equal ($runtimeSource -match '\$ProgressPreference\s*=\s*''SilentlyContinue''') $true 'health checks suppress the PowerShell progress banner'

foreach ($scriptName in @(
    'invoke-gateway-service.ps1',
    'lib\gateway-runtime.ps1',
    'lib\watch-chat2api.ps1', 'lib\register-chat2api-watch.ps1')) {
    $tokens = $null
    $parseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $root $scriptName), [ref]$tokens, [ref]$parseErrors) | Out-Null
    Assert-Equal $parseErrors.Count 0 "$scriptName parses without errors"
}

# ---------------------------------------------------------------------------
# #80: 本机 8000 启动器切换到 Rust 二进制。
#
# 只验证启动描述符、健康边界与回滚入口，不启动、不查询真实 8000：
# CIM/HTTP/Start-Process 边界都在本块内替换，离开作用域即恢复。
# ---------------------------------------------------------------------------

& {
    $desc = (Get-GatewayServiceDescriptors)['chat2api']
    Assert-Equal ([IO.Path]::GetFileName([string]$desc.StartPath)) 'um-gateway-candidate.exe' 'chat2api StartPath points at the Rust binary'
    Assert-Equal ([string]$desc.StartAction) 'HiddenBatch' 'chat2api keeps the hidden launcher action'
    Assert-Equal ([string]$desc.StartArguments) '--mode production --port 8000 --role local --upstream-base-url https://chat.genai.um.edu.mo --control-token-file data/admin-control-token --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log' 'chat2api starts production local 8000 with the Python control token file'
    Assert-Equal ([string]$desc.StartWorkingDirectory) '.' 'chat2api launches from the repository root (Python default token/profile paths)'
    Assert-Equal ([IO.Path]::GetFileName((Resolve-GatewayStartPath -Key 'chat2api'))) 'um-gateway-candidate.exe' 'resolved StartPath is the Rust binary'
    Assert-Equal $desc.Port 8000 'Rust launcher keeps port 8000'
    Assert-Equal $desc.HealthPath '/health' 'Rust launcher keeps the /health path'
    Assert-Equal $desc.ShutdownUrl 'http://127.0.0.1:8000/admin/shutdown' 'Rust launcher keeps the /admin/shutdown URL'
    Assert-Equal $desc.StopStrategy 'GracefulHttp' 'Rust launcher keeps the graceful stop strategy'

    $healthCalls = New-Object System.Collections.ArrayList
    Test-GatewayServiceHealth -Key 'chat2api' -HttpChecker {
        param($url, $timeout)
        [void]$healthCalls.Add($url)
        return $true
    } | Out-Null
    Assert-Equal $healthCalls[0] 'http://127.0.0.1:8000/health' 'Rust 8000 health check keeps the 8000/health URL'

    # Launch shape: the Rust binary directly, with production/local arguments
    # and the repository root as cwd so the Rust defaults resolve to
    # chat2api/token.json and chat2api/.edge-profile like Python 8000.
    $script:ticket80Launches = New-Object System.Collections.ArrayList
    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        return @()
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @()
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        throw 'offline substitute: health probe refused'
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        throw 'offline substitute: no process may be stopped'
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
        $plan = $null
        if ($env:SELFUSEAI_CHAT2API_OBSERVER_LAUNCH) {
            $plan = $env:SELFUSEAI_CHAT2API_OBSERVER_LAUNCH | ConvertFrom-Json
        }
        $throughObserver = $null -ne $plan
        [void]$script:ticket80Launches.Add([pscustomobject]@{
            ObserverCommand = @($ArgumentList) -join ' '
            FilePath = if ($throughObserver) { $plan.executable } else { [string]$FilePath }
            ArgumentList = if ($throughObserver) { $plan.arguments } else { @($ArgumentList) -join ' ' }
            WorkingDirectory = if ($throughObserver) { $plan.working_directory } else { [string]$WorkingDirectory }
            Component = if ($throughObserver -and $plan.PSObject.Properties['component']) { $plan.component }
        })
    }
    try {
        $started = Start-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $started.Status 'StartPending' 'Rust launch without a listener reports StartPending'
        Assert-Equal $script:ticket80Launches.Count 2 'split launcher starts the worker and the ingress'
        $workerLaunch = $script:ticket80Launches[0]
        Assert-Equal ($workerLaunch.ObserverCommand -match 'observe-chat2api\.ps1') $true 'the worker launches through its exit observer'
        Assert-Equal $workerLaunch.FilePath (Join-Path $root 'rust\target\release\um-gateway-candidate.exe') 'observer launches the Rust binary'
        Assert-Equal ($workerLaunch.ArgumentList -match '^--mode worker --port 0 --role local --worker-port-file ') $true 'the worker argument line matches the fixed CLI'
        Assert-Equal ($workerLaunch.ArgumentList -match '--upstream-base-url https://chat.genai.um.edu.mo') $true 'the worker keeps the production upstream'
        Assert-Equal ([IO.Path]::GetFullPath($workerLaunch.WorkingDirectory)) $root 'the worker runs from the repository root'
        $ingressLaunch = $script:ticket80Launches[1]
        Assert-Equal ($ingressLaunch.ObserverCommand -match 'observe-chat2api\.ps1') $true 'the ingress launches through its own exit observer'
        Assert-Equal $workerLaunch.Component 'worker' 'the worker has its own exit record identity'
        Assert-Equal $ingressLaunch.Component 'ingress' 'the ingress has its own exit record identity'
        Assert-Equal $ingressLaunch.FilePath (Join-Path $root 'rust\target\release\um-gateway-candidate.exe') 'the ingress launches the Rust binary'
        Assert-Equal ($ingressLaunch.ArgumentList -match '^--mode ingress --port 8000 --worker-port-file ') $true 'the ingress argument line matches the fixed CLI'
        Assert-Equal ($ingressLaunch.ArgumentList -match '--desired-state-file ') $true 'the ingress receives the desired-state file'
        Assert-Equal ($ingressLaunch.ArgumentList -match '--boot-id ') $true 'the ingress receives the boot id'
        Assert-Equal ($ingressLaunch.ArgumentList -match '--control-token-file data/admin-control-token$') $true 'the ingress receives the control token file'
        Assert-Equal ([IO.Path]::GetFullPath($ingressLaunch.WorkingDirectory)) $root 'the ingress runs from the repository root'
    } finally {
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# #80 AC2: console start/stop/restart keep working on the Rust 8000 instance.
#
# The stop scope must recognize BOTH the Rust binary (current launcher) and the
# Python entrypoint (rollback). A Rust candidate/cloud instance and another
# checkout's Python instance stay foreign. The bridge wiring is verified with
# substituted runtime functions, so no real service is started or stopped.
# ---------------------------------------------------------------------------

& {
    $desc = (Get-GatewayServiceDescriptors)['chat2api']
    $ticket80PortRoot = Join-Path ([IO.Path]::GetTempPath()) ('ticket80-ports-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($ticket80PortRoot)
    $script:ticket80WorkerPortFile = Join-Path $ticket80PortRoot 'chat2api-worker.port'
    [IO.File]::WriteAllText($script:ticket80WorkerPortFile, "9911`n",
        (New-Object System.Text.UTF8Encoding($false)))
    $savedWorkerPortFile = [string]$desc.WorkerPortFile
    $desc.WorkerPortFile = $script:ticket80WorkerPortFile
    $stopIdentities = @(Get-GatewayStopIdentities -Descriptor $desc)
    Assert-Equal (($stopIdentities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-gateway-candidate.exe,um-gateway-candidate.exe,um-gateway-candidate.exe' 'stop identities cover the Python rollback, the legacy production shape, the worker and the ingress'
    Assert-Equal ((@($stopIdentities | Where-Object { [string]$_.Component }) |
        ForEach-Object { [string]$_.Component }) -join ',') 'worker,ingress' 'stop identities name both split components'
    $ownerIdentities = @(Get-GatewayOwnerIdentities -Descriptor $desc)
    Assert-Equal (($ownerIdentities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-gateway-candidate.exe,um-gateway-candidate.exe,um-gateway-candidate.exe' 'owner identities cover the Python rollback and both Rust split forms'
    $workerCommandLine = ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '" --mode worker --port 0 --role local --worker-port-file data/chat2api-worker.port --upstream-base-url https://chat.example.invalid')
    $ingressCommandLine = ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '" --mode ingress --port 8000 --worker-port-file data/chat2api-worker.port --boot-id fixture-boot')
    $workerProcess = [pscustomobject]@{
        ProcessId = 990206; Name = 'um-gateway-candidate.exe'
        CommandLine = $workerCommandLine; CreationDate = [datetime]'2026-11-21T10:00:00'
    }
    $ingressProcess = [pscustomobject]@{
        ProcessId = 990207; Name = 'um-gateway-candidate.exe'
        CommandLine = $ingressCommandLine; CreationDate = [datetime]'2026-11-21T10:00:00'
    }
    Assert-Equal ([bool](Test-GatewayProcessOwnership $desc $workerProcess)) $true 'the worker command line is recognized as the managed program'
    Assert-Equal ([bool](Test-GatewayProcessOwnership $desc $ingressProcess)) $true 'the ingress command line is recognized as the managed program'

    $script:ticket80Terminated = New-Object System.Collections.ArrayList
    $script:ticket80Requests = New-Object System.Collections.ArrayList
    $script:ticket80Launches = New-Object System.Collections.ArrayList
    $script:ticket80Table = @()
    $script:ticket80Healthy = $false

    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
            $wanted = [int]$Matches[1]
            return @($script:ticket80Table | Where-Object { [int]$_.ProcessId -eq $wanted })
        }
        return @($script:ticket80Table)
    }
    $script:ticket80ListenersByPort = @{}
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        if ($script:ticket80ListenersByPort.ContainsKey([int]$LocalPort)) {
            return @($script:ticket80ListenersByPort[[int]$LocalPort])
        }
        return @()
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        if ($Method -eq 'Post') {
            [void]$script:ticket80Requests.Add([string]$Uri)
            $script:ticket80Healthy = $false
            return [pscustomobject]@{ StatusCode = 200; Content = '{}' }
        }
        if (-not $script:ticket80Healthy) {
            throw 'offline substitute: health probe refused'
        }
        return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        [void]$script:ticket80Terminated.Add([int]$Id)
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
        [void]$script:ticket80Launches.Add([string]$FilePath)
    }

    function New-Ticket80Process {
        param([int]$ProcessId, [string]$CommandLine, [string]$Name = 'python.exe',
              [datetime]$CreationDate = [datetime]'2026-11-17T10:00:00')
        return [pscustomobject]@{
            ProcessId = $ProcessId; Name = $Name
            CommandLine = $CommandLine; CreationDate = $CreationDate
        }
    }

    $rustBinary = '"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '"'
    $worker = New-Ticket80Process -ProcessId 990206 -Name 'um-gateway-candidate.exe' `
        -CommandLine ($rustBinary + ' --mode worker --port 0 --role local --worker-port-file ' + $script:ticket80WorkerPortFile + ' --upstream-base-url https://chat.example.invalid')
    $ingress = New-Ticket80Process -ProcessId 990207 -Name 'um-gateway-candidate.exe' `
        -CommandLine ($rustBinary + ' --mode ingress --port 8000 --worker-port-file ' + $script:ticket80WorkerPortFile + ' --boot-id fixture-boot')
    $rustCandidate = New-Ticket80Process -ProcessId 990202 -Name 'um-gateway-candidate.exe' `
        -CommandLine 'um-gateway-candidate.exe --mode candidate --port 0 --upstream-base-url https://chat.example.invalid'
    $rustCloud = New-Ticket80Process -ProcessId 990203 -Name 'um-gateway-candidate.exe' `
        -CommandLine 'um-gateway-candidate.exe --mode production --port 8003 --role cloud --upstream-base-url https://chat.example.invalid'
    $pythonPrivate = New-Ticket80Process -ProcessId 990204 `
        -CommandLine 'python chat2api.py --base-url https://chat.example.invalid'
    $otherCheckout = New-Ticket80Process -ProcessId 990205 `
        -CommandLine 'python D:\other-checkout\ai-gateway\chat2api\chat2api.py --base-url https://chat.example.invalid'

    try {
        # Owner identity: the worker listener is recognized from WorkerPortFile,
        # the ingress listener from 8000. Both PIDs are reported.
        $script:ticket80Table = @($worker, $ingress)
        $script:ticket80ListenersByPort = @{
            9911 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990206 })
            8000 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990207 })
        }
        $script:ticket80Healthy = $true
        $inspection = Get-GatewayServiceInspection -Key 'chat2api'
        Assert-Equal $inspection.State 'Running' 'worker + ingress listeners are recognized as the split private instance'
        Assert-Equal ($inspection.ProcessIds -join ',') '990206,990207' 'split inspection names both component PIDs'

        # Stop scope: worker + ingress + Python rollback are private;
        # candidate/cloud/other checkout are foreign.
        $script:ticket80Table = @($worker, $ingress, $rustCandidate, $rustCloud, $pythonPrivate, $otherCheckout)
        $script:ticket80Terminated.Clear()
        $script:ticket80Requests.Clear()
        $stopped = Stop-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $stopped.Status 'Stopped' 'split 8000 stop reports Stopped'
        Assert-Equal ($script:ticket80Terminated -join ',') '990206,990207,990204' 'split 8000 stop terminates the worker, the ingress and the Python rollback instance'
        Assert-Equal ($script:ticket80Terminated -contains 990202) $false 'Rust candidate instance is never stopped'
        Assert-Equal ($script:ticket80Terminated -contains 990203) $false 'Rust cloud instance is never stopped'
        Assert-Equal ($script:ticket80Terminated -contains 990205) $false 'another checkout''s Python instance is never stopped'
        Assert-Equal ($script:ticket80Requests -join ',') ('http://127.0.0.1:8000/admin/shutdown,http://127.0.0.1:9911/admin/shutdown') 'split 8000 stop posts the ingress shutdown first, then the worker shutdown'

        # Rollback path: after StartPath reverts, the Python identity alone remains selectable.
        $script:ticket80Table = @($pythonPrivate, $otherCheckout)
        $script:ticket80ListenersByPort = @{
            8000 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990204 })
        }
        $script:ticket80Healthy = $true
        $script:ticket80Terminated.Clear()
        $pythonStopped = Stop-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $pythonStopped.Status 'Stopped' 'Python rollback stop reports Stopped'
        Assert-Equal ($script:ticket80Terminated -join ',') '990204' 'Python rollback stop still selects the Python private instance'
    } finally {
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
        $desc.WorkerPortFile = $savedWorkerPortFile
        Remove-Item -LiteralPath $ticket80PortRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Bridge start/stop/restart wiring: the JSON entry calls the same runtime
    # functions; the start result carries the Rust StartPath in its detail.
    & {
        . (Join-Path $root 'lib\console-runtime-bridge.ps1')
        function Start-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Started'; Action = 'Start'
                                      Detail = (Resolve-GatewayStartPath -Key $Key) }
        }
        function Stop-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Stopped'; Action = 'Stop' }
        }
        function Restart-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Restarted'; Action = 'Restart' }
        }
        $lockDir = Join-Path ([IO.Path]::GetTempPath()) ('ticket80-locks-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        try {
            $start = Invoke-ConsoleRuntimeStart -Key 'chat2api' -LockDir $lockDir
            Assert-Equal $start.ok $true 'bridge start on Rust 8000 reports ok'
            Assert-Equal $start.result.status 'Started' 'bridge start on Rust 8000 reports Started'
            Assert-Equal ([IO.Path]::GetFileName([string]$start.result.detail)) 'um-gateway-candidate.exe' 'bridge start on Rust 8000 resolves the Rust StartPath'
            $stop = Invoke-ConsoleRuntimeStop -Key 'chat2api' -LockDir $lockDir
            Assert-Equal $stop.ok $true 'bridge stop on Rust 8000 reports ok'
            Assert-Equal $stop.result.status 'Stopped' 'bridge stop on Rust 8000 reports Stopped'
            $restart = Invoke-ConsoleRuntimeRestart -Key 'chat2api' -LockDir $lockDir
            Assert-Equal $restart.ok $true 'bridge restart on Rust 8000 reports ok'
            Assert-Equal $restart.result.status 'Restarted' 'bridge restart on Rust 8000 reports Restarted'
        } finally {
            Remove-Item -LiteralPath $lockDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# #133 A2: split 8000 component start/stop.
#
# The runtime starts only the missing component, worker before ingress, and stops
# ingress before worker. Every OS boundary (CIM process table, listener snapshot,
# health HTTP, Start-Process, Stop-Process) is substituted; the worker port file
# points at a temp file so no real data/chat2api-worker.port is read or written.
# ---------------------------------------------------------------------------

& {
    $desc = (Get-GatewayServiceDescriptors)['chat2api']
    $savedWorkerPortFile = [string]$desc.WorkerPortFile
    $componentPortRoot = Join-Path ([IO.Path]::GetTempPath()) ('component-ports-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($componentPortRoot)
    $script:componentWorkerPortFile = Join-Path $componentPortRoot 'chat2api-worker.port'
    [IO.File]::WriteAllText($script:componentWorkerPortFile, "9911`n",
        (New-Object System.Text.UTF8Encoding($false)))
    $desc.WorkerPortFile = $script:componentWorkerPortFile

    $script:componentTable = @()
    $script:componentListeners = @{}
    $script:componentWorkerHealthy = $false
    $script:componentIngressHealthy = $false
    $script:componentLaunches = New-Object System.Collections.ArrayList
    $script:componentRequests = New-Object System.Collections.ArrayList
    $script:componentTerminated = New-Object System.Collections.ArrayList

    $script:componentWorker = [pscustomobject]@{
        ProcessId = 990206; Name = 'um-gateway-candidate.exe'
        CommandLine = ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '" --mode worker --port 0 --role local --worker-port-file ' + $script:componentWorkerPortFile)
        CreationDate = [datetime]'2026-11-21T10:00:00'
    }
    $script:componentIngress = [pscustomobject]@{
        ProcessId = 990207; Name = 'um-gateway-candidate.exe'
        CommandLine = ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '" --mode ingress --port 8000 --worker-port-file ' + $script:componentWorkerPortFile)
        CreationDate = [datetime]'2026-11-21T10:00:00'
    }

    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
            $wanted = [int]$Matches[1]
            return @($script:componentTable | Where-Object { [int]$_.ProcessId -eq $wanted })
        }
        return @($script:componentTable)
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        if ($script:componentListeners.ContainsKey([int]$LocalPort)) {
            return @($script:componentListeners[[int]$LocalPort])
        }
        return @()
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        if ($Method -eq 'Post') {
            [void]$script:componentRequests.Add([string]$Uri)
            if ($Uri -match ':8000/') { $script:componentIngressHealthy = $false }
            return [pscustomobject]@{ StatusCode = 202; Content = '{"status":"stopping"}' }
        }
        if ($Uri -match ':9911/health') {
            if ($script:componentWorkerHealthy) {
                return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
            }
            throw 'offline substitute: worker health refused'
        }
        if ($Uri -match ':8000/health') {
            if ($script:componentIngressHealthy) {
                return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
            }
            throw 'offline substitute: ingress health refused'
        }
        throw 'offline substitute: unexpected health URL'
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        [void]$script:componentTerminated.Add([int]$Id)
        $script:componentWorkerHealthy = $false
        $script:componentIngressHealthy = $false
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
        $plan = $env:SELFUSEAI_CHAT2API_OBSERVER_LAUNCH
        if ($plan) {
            $definition = $plan | ConvertFrom-Json
            $arguments = [string]$definition.arguments
            [void]$script:componentLaunches.Add([pscustomobject]@{
                Component = [string]$definition.component; Arguments = $arguments
                WorkingDirectory = [string]$definition.working_directory
                BootId = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_BOOT_ID', 'Process')
                DesiredState = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_DESIRED_STATE', 'Process')
            })
            return
        }
        $arguments = @($ArgumentList) -join ' '
        $component = if ($arguments -match '--mode ingress') { 'ingress' } else { 'other' }
        [void]$script:componentLaunches.Add([pscustomobject]@{
            Component = $component; Arguments = $arguments
            WorkingDirectory = [string]$WorkingDirectory
            BootId = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_BOOT_ID', 'Process')
            DesiredState = [Environment]::GetEnvironmentVariable('SELFUSEAI_GATEWAY_DESIRED_STATE', 'Process')
        })
    }

    function Set-ComponentFixture {
        param(
            [array]$Processes = @(),
            [hashtable]$Listeners = @{},
            [bool]$WorkerHealthy = $false,
            [bool]$IngressHealthy = $false
        )
        $script:componentTable = $Processes
        $script:componentListeners = $Listeners
        $script:componentWorkerHealthy = $WorkerHealthy
        $script:componentIngressHealthy = $IngressHealthy
        $script:componentLaunches.Clear()
        $script:componentRequests.Clear()
        $script:componentTerminated.Clear()
    }

    try {
        # Both components healthy: the start is idempotent and launches nothing.
        Set-ComponentFixture -Processes @($script:componentWorker, $script:componentIngress) `
            -Listeners @{
                9911 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990206 })
                8000 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990207 })
            } -WorkerHealthy $true -IngressHealthy $true
        $result = Start-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $result.Status 'AlreadyRunning' 'both healthy components are reused'
        Assert-Equal $script:componentLaunches.Count 0 'no component is launched when both are healthy'

        # Ingress process present but /health 503 (worker missing): start the
        # worker only, never the ingress.
        Set-ComponentFixture -Processes @($script:componentIngress) `
            -Listeners @{ 8000 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990207 }) }
        $result = Start-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $result.Status 'StartPending' 'a worker-only start reports StartPending until the worker is healthy'
        Assert-Equal (($script:componentLaunches | ForEach-Object { $_.Component }) -join ',') 'worker' 'an existing ingress is not launched again'
        $workerLaunch = $script:componentLaunches[0]
        Assert-Equal ($workerLaunch.Arguments -match '--mode worker --port 0 --role local --worker-port-file ') $true 'the worker argument line matches the fixed CLI'
        Assert-Equal ($workerLaunch.Arguments -match '--upstream-base-url https://chat.genai.um.edu.mo') $true 'the worker keeps the production upstream'
        Assert-Equal ($workerLaunch.WorkingDirectory -eq (Get-GatewayRoot)) $true 'the worker runs from the repository root'
        Assert-Equal ($null -ne $workerLaunch.BootId) $true 'the worker receives SELFUSEAI_GATEWAY_BOOT_ID'
        Assert-Equal ($workerLaunch.DesiredState -eq (Get-GatewayDesiredStatePath -Key 'chat2api')) $true 'the worker receives SELFUSEAI_GATEWAY_DESIRED_STATE'

        # Both missing: worker first, then ingress.
        Set-ComponentFixture
        $result = Start-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $result.Status 'StartPending' 'a cold split start reports StartPending until the pair is healthy'
        Assert-Equal (($script:componentLaunches | ForEach-Object { $_.Component }) -join ',') 'worker,ingress' 'the cold start launches the worker before the ingress'
        $ingressLaunch = $script:componentLaunches[1]
        Assert-Equal ($ingressLaunch.Arguments -match '--mode ingress --port 8000 --worker-port-file ') $true 'the ingress argument line matches the fixed CLI'
        Assert-Equal ($ingressLaunch.Arguments -match ('--desired-state-file ' + [regex]::Escape((Get-GatewayDesiredStatePath -Key 'chat2api')))) $true 'the ingress receives the desired-state file'
        Assert-Equal ($ingressLaunch.Arguments -match '--boot-id ') $true 'the ingress receives the boot id when readable'
        Assert-Equal ($ingressLaunch.WorkingDirectory -eq (Get-GatewayRoot)) $true 'the ingress runs from the repository root'
        Assert-Equal ($null -ne $ingressLaunch.BootId) $true 'the ingress receives SELFUSEAI_GATEWAY_BOOT_ID'
        Assert-Equal ($ingressLaunch.DesiredState -eq (Get-GatewayDesiredStatePath -Key 'chat2api')) $true 'the ingress receives SELFUSEAI_GATEWAY_DESIRED_STATE'

        # Stop order: ingress shutdown first, then worker shutdown, then the
        # verified scoped cleanup. Both PIDs are reported as terminated.
        Set-ComponentFixture -Processes @($script:componentWorker, $script:componentIngress) `
            -Listeners @{
                9911 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990206 })
                8000 = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990207 })
            } -WorkerHealthy $true -IngressHealthy $true
        $stopped = Stop-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $stopped.Status 'Stopped' 'the split stop reports Stopped'
        Assert-Equal ($script:componentRequests -join ',') 'http://127.0.0.1:8000/admin/shutdown,http://127.0.0.1:9911/admin/shutdown' 'the ingress shutdown is posted before the worker shutdown'
        Assert-Equal ($script:componentTerminated -join ',') '990206,990207' 'the scoped fallback terminates both components'
    } finally {
        $desc.WorkerPortFile = $savedWorkerPortFile
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $componentPortRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# #80 AC3: graceful stop still goes through /admin/shutdown, and the Rust
# process exits through the descriptor's GracefulHttp StopStrategy when the
# endpoint accepts OR refuses the request (Rust currently answers 501 for the
# admin surface; the scoped stop is the strategy's fallback).
# ---------------------------------------------------------------------------

& {
    $script:ticket80Events = New-Object System.Collections.ArrayList
    $script:ticket80Terminated = New-Object System.Collections.ArrayList
    $script:ticket80Table = @()
    $script:ticket80Listeners = @()
    $script:ticket80Healthy = $false
    $script:ticket80GracefulFails = $false
    $desc = (Get-GatewayServiceDescriptors)['chat2api']
    $savedWorkerPortFile = [string]$desc.WorkerPortFile
    $absentPortFile = Join-Path ([IO.Path]::GetTempPath()) ('missing-worker-port-' + [guid]::NewGuid().ToString('N') + '.port')
    $desc.WorkerPortFile = $absentPortFile

    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
            $wanted = [int]$Matches[1]
            return @($script:ticket80Table | Where-Object { [int]$_.ProcessId -eq $wanted })
        }
        return @($script:ticket80Table)
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @($script:ticket80Listeners)
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        if ($Method -eq 'Post') {
            [void]$script:ticket80Events.Add('request')
            if ($script:ticket80GracefulFails) {
                throw 'offline substitute: Rust /admin/shutdown refused'
            }
            $script:ticket80Healthy = $false
            return [pscustomobject]@{ StatusCode = 200; Content = '{}' }
        }
        if (-not $script:ticket80Healthy) { throw 'offline substitute: health probe refused' }
        return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        [void]$script:ticket80Events.Add('stop')
        [void]$script:ticket80Terminated.Add([int]$Id)
        # The terminated process no longer answers /health.
        $script:ticket80Healthy = $false
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
    }

    function New-Ticket80Rust {
        return [pscustomobject]@{
            ProcessId = 990201; Name = 'um-gateway-candidate.exe'
            CommandLine = ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-gateway-candidate.exe') + '" --mode production --port 8000 --role local')
            CreationDate = [datetime]'2026-11-17T10:00:00'
        }
    }

    try {
        $script:ticket80Table = @(New-Ticket80Rust)
        $script:ticket80Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990201 })
        $script:ticket80Healthy = $true
        $script:ticket80Events.Clear()
        $script:ticket80Terminated.Clear()
        $accepted = Stop-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $accepted.Status 'Stopped' 'graceful stop reports Stopped after the Rust process exits'
        Assert-Equal ($script:ticket80Events -join ',') 'request,stop' 'the /admin/shutdown request is sent before the scoped stop'
        Assert-Equal ($script:ticket80Terminated -join ',') '990201' 'the scoped stop terminates the Rust private instance'

        # A refused/501 graceful request still exits through the same strategy.
        $script:ticket80Table = @(New-Ticket80Rust)
        $script:ticket80Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990201 })
        $script:ticket80Healthy = $true
        $script:ticket80GracefulFails = $true
        $script:ticket80Events.Clear()
        $script:ticket80Terminated.Clear()
        $refused = Stop-GatewayService -Key 'chat2api' -Sleeper { param($seconds) }
        Assert-Equal $refused.Status 'Stopped' 'a refused graceful request still reports Stopped through the scoped stop'
        Assert-Equal ($script:ticket80Events -join ',') 'request,stop' 'a refused graceful request still tries /admin/shutdown before the scoped stop'
        Assert-Equal ($script:ticket80Terminated -join ',') '990201' 'a refused graceful request still terminates only the Rust private instance'
    } finally {
        $desc.WorkerPortFile = $savedWorkerPortFile
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# #80 AC4: rollback is one StartPath change back to start_chat2api.bat; the
# Python command, port and stop identity stay in place, and the rollback steps
# are written down for the operator.
# ---------------------------------------------------------------------------

& {
    # The Python 8000 launcher was retired with the rollback closure (#90);
    # StartPath is the Rust binary and the descriptor is its only entrypoint.

    $contract = Get-Content -LiteralPath (Join-Path $root 'docs\rust-contract.md') -Raw -Encoding utf8
    Assert-Equal ($contract -match '本机 8000 启动器与回滚') $true 'rust-contract documents the local 8000 launcher and rollback'
    Assert-Equal ($contract -match '只改 `StartPath`') $true 'rollback is documented as a single StartPath change'
    Assert-Equal ($contract -match 'start_chat2api\.bat') $true 'rollback names the Python launcher'
    Assert-Equal ($contract -match '不需要重装') $true 'rollback documents that no reinstall is needed'
    Assert-Equal ($contract -match '不需要改端口|无需改端口') $true 'rollback documents that no port change is needed'

    # Simulate the rollback: only StartPath changes back; port and stop
    # identities stay valid for the Python runtime.
    $desc = (Get-GatewayServiceDescriptors)['chat2api']
    $savedStartPath = [string]$desc.StartPath
    try {
        $desc.StartPath = 'start_chat2api.bat'
        Assert-Equal ([IO.Path]::GetFileName((Resolve-GatewayStartPath -Key 'chat2api'))) 'start_chat2api.bat' 'changing only StartPath resolves the Python launcher'
        Assert-Equal $desc.Port 8000 'rollback keeps port 8000'
        Assert-Equal $desc.StopStrategy 'GracefulHttp' 'rollback keeps the graceful stop strategy'
        $identities = @(Get-GatewayStopIdentities -Descriptor $desc)
        Assert-Equal (($identities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-gateway-candidate.exe,um-gateway-candidate.exe,um-gateway-candidate.exe' 'rollback still recognizes the Python private instance and both Rust split forms'
    } finally {
        $desc.StartPath = $savedStartPath
    }
}

# ---------------------------------------------------------------------------
# #120: 本机 8000 启动器注入 UM 凭据。
#
# 凭据文件是 ADR 0012 的 %LOCALAPPDATA%\Chat2API\credentials.json。
# 显式环境变量优先；环境变量不完整时才读文件。账号和密码都必须是
# 字符串且 trim 后非空，文件 ACL 还必须只允许当前用户访问。文件缺失、
# 内容损坏、版本不符、只有一半字段或 ACL 过宽时都不注入，8000 仍照常
# 启动。测试把 LOCALAPPDATA 指向临时目录，并在 Start-Process 边界读取
# 子进程环境；真实登录不执行。
# ---------------------------------------------------------------------------

& {
    $savedLocalAppData = $env:LOCALAPPDATA
    $savedAccount = [Environment]::GetEnvironmentVariable('CHAT2API_UM_ACCOUNT', 'Process')
    $savedPassword = [Environment]::GetEnvironmentVariable('CHAT2API_UM_PASSWORD', 'Process')
    $savedHiddenLaunch = $env:SELFUSEAI_HIDDEN_LAUNCH
    $credentialRoot = Join-Path ([IO.Path]::GetTempPath()) ('gateway-credentials-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $credentialDir = Join-Path $credentialRoot 'Chat2API'
    [void][IO.Directory]::CreateDirectory($credentialDir)
    $credentialPath = Join-Path $credentialDir 'credentials.json'
    $env:LOCALAPPDATA = $credentialRoot
    Remove-Item Env:\CHAT2API_UM_ACCOUNT -ErrorAction SilentlyContinue
    Remove-Item Env:\CHAT2API_UM_PASSWORD -ErrorAction SilentlyContinue

    $script:credentialLaunches = New-Object System.Collections.ArrayList
    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        return @()
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @()
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        throw 'offline substitute: health probe refused'
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        throw 'offline substitute: no process may be stopped'
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
        [void]$script:credentialLaunches.Add([pscustomobject]@{
            FilePath = [string]$FilePath
            ArgumentList = @($ArgumentList)
            Account = [Environment]::GetEnvironmentVariable('CHAT2API_UM_ACCOUNT', 'Process')
            Password = [Environment]::GetEnvironmentVariable('CHAT2API_UM_PASSWORD', 'Process')
        })
    }

    function Write-CredentialFixture {
        param([string]$Json)
        [IO.File]::WriteAllText($credentialPath, $Json,
            (New-Object System.Text.UTF8Encoding($false)))
    }

    function Set-CredentialAcl {
        param([switch]$Broaden)
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if ($Broaden) {
            & icacls.exe $credentialPath /grant '*S-1-5-32-545:(R)' | Out-Null
        } else {
            & icacls.exe $credentialPath /reset | Out-Null
            & icacls.exe $credentialPath /inheritance:r /grant:r ('*' + $sid + ':F') | Out-Null
        }
        if ($LASTEXITCODE -ne 0) { throw 'credential ACL fixture failed' }
    }

    function Invoke-CredentialStart {
        param([string]$Key = 'chat2api')
        $script:credentialLaunches.Clear()
        $savedVerbose = $VerbosePreference
        $VerbosePreference = 'Continue'
        try {
            $output = @(Start-GatewayService -Key $Key -Sleeper { param($seconds) } 4>&1)
        } finally {
            $VerbosePreference = $savedVerbose
        }
        $result = @($output | Where-Object {
            $_ -isnot [System.Management.Automation.VerboseRecord]
        }) | Select-Object -First 1
        $verbose = @($output | Where-Object {
            $_ -is [System.Management.Automation.VerboseRecord]
        } | ForEach-Object { [string]$_.Message }) -join "`n"
        return [pscustomobject]@{
            Result = $result
            Verbose = $verbose
            Launches = @($script:credentialLaunches)
        }
    }

    try {
        $secretAccount = 'um-account-120@example.invalid'
        $secretPassword = 'p@ss word 120'
        $envAccount = 'env-account-120@example.invalid'
        $envPassword = 'env-password-120'

        # 显式环境变量优先：文件即使损坏且 ACL 过宽也不会被读取。
        Write-CredentialFixture ('{"version":1,"account":"file-account-120","password":"file-password-120"')
        Set-CredentialAcl -Broaden
        [Environment]::SetEnvironmentVariable('CHAT2API_UM_ACCOUNT', $envAccount, 'Process')
        [Environment]::SetEnvironmentVariable('CHAT2API_UM_PASSWORD', $envPassword, 'Process')
        $fromEnv = Invoke-CredentialStart
        Assert-Equal $fromEnv.Result.Status 'StartPending' 'credentials: env pair still starts 8000'
        Assert-Equal $fromEnv.Launches.Count 2 'credentials: env pair launches twice (worker and ingress)'
        Assert-Equal ($fromEnv.Launches[0].Account -eq $envAccount) $true 'credentials: env account wins over the file'
        Assert-Equal ($fromEnv.Launches[0].Password -eq $envPassword) $true 'credentials: env password wins over the file'
        Assert-Equal ($fromEnv.Verbose -match 'credentials_present') $true 'credentials: env pair records credentials_present'
        Assert-Equal ($fromEnv.Verbose -notmatch [regex]::Escape('file-account-120')) $true 'credentials: file content is not read while env is complete'
        Remove-Item Env:\CHAT2API_UM_ACCOUNT -ErrorAction SilentlyContinue
        Remove-Item Env:\CHAT2API_UM_PASSWORD -ErrorAction SilentlyContinue

        # 文件 ACL 过宽：拒绝注入，只记录 credentials_acl_too_broad。
        Write-CredentialFixture ('{"version":1,"account":"' + $secretAccount + '","password":"' + $secretPassword + '"}')
        Set-CredentialAcl -Broaden
        $broadAcl = Invoke-CredentialStart
        Assert-Equal $broadAcl.Result.Status 'StartPending' 'credentials: broad ACL still starts 8000'
        Assert-Equal $broadAcl.Launches.Count 2 'credentials: broad ACL launches twice (worker and ingress)'
        Assert-Null $broadAcl.Launches[0].Account 'credentials: broad ACL injects no account'
        Assert-Null $broadAcl.Launches[0].Password 'credentials: broad ACL injects no password'
        Assert-Equal ($broadAcl.Verbose -match 'credentials_acl_too_broad') $true 'credentials: broad ACL records credentials_acl_too_broad'
        Assert-Equal ($broadAcl.Verbose -notmatch [regex]::Escape($secretPassword)) $true 'credentials: broad ACL never logs the password'

        # 有效文件：两个变量都进入 8000 子进程，值来自文件；父进程恢复。
        Set-CredentialAcl
        $env:SELFUSEAI_HIDDEN_LAUNCH = 'parent-hidden'
        $valid = Invoke-CredentialStart
        Assert-Equal $valid.Result.Status 'StartPending' 'credentials: valid file still starts 8000'
        Assert-Equal $valid.Launches.Count 2 'credentials: valid file launches twice (worker and ingress)'
        Assert-Equal ($valid.Launches[0].Account -eq $secretAccount) $true 'credentials: child receives the account'
        Assert-Equal ($valid.Launches[0].Password -eq $secretPassword) $true 'credentials: child receives the password'
        Assert-Equal ([Environment]::GetEnvironmentVariable('CHAT2API_UM_ACCOUNT', 'Process') -eq $null) $true 'credentials: parent account is not polluted'
        Assert-Equal ([Environment]::GetEnvironmentVariable('CHAT2API_UM_PASSWORD', 'Process') -eq $null) $true 'credentials: parent password is not polluted'
        Assert-Equal ([Environment]::GetEnvironmentVariable('SELFUSEAI_HIDDEN_LAUNCH', 'Process') -eq 'parent-hidden') $true 'credentials: unrelated child environment is restored'
        Assert-Equal ($valid.Verbose -match 'credentials_present') $true 'credentials: valid file records credentials_present'
        Assert-Equal ($valid.Verbose -notmatch [regex]::Escape($secretAccount)) $true 'credentials: account never enters the log'
        Assert-Equal ($valid.Verbose -notmatch [regex]::Escape($secretPassword)) $true 'credentials: password never enters the log'
        Assert-Equal (($valid.Launches[0].ArgumentList -join ' ') -notmatch [regex]::Escape($secretPassword)) $true 'credentials: password never enters the launch arguments'
        Assert-Equal (($valid.Result | ConvertTo-Json -Compress) -notmatch [regex]::Escape($secretPassword)) $true 'credentials: password never enters the result'
        Assert-Null $valid.Launches[1].Account 'credentials: the ingress receives no account'
        Assert-Null $valid.Launches[1].Password 'credentials: the ingress receives no password'

        # 文件缺失：不注入，8000 仍启动。
        Remove-Item -LiteralPath $credentialPath -Force
        $missing = Invoke-CredentialStart
        Assert-Equal $missing.Result.Status 'StartPending' 'credentials: missing file still starts 8000'
        Assert-Equal $missing.Launches.Count 2 'credentials: missing file launches twice (worker and ingress)'
        Assert-Null $missing.Launches[0].Account 'credentials: missing file injects no account'
        Assert-Null $missing.Launches[0].Password 'credentials: missing file injects no password'
        Assert-Equal ($missing.Verbose -match 'credentials_missing') $true 'credentials: missing file records credentials_missing'

        # JSON 损坏：不注入，8000 仍启动；损坏内容里的秘密不出现在错误/日志。
        $brokenSecret = 'broken-secret-120'
        Write-CredentialFixture ('{"version":1,"account":"' + $brokenSecret + '","password":"' + $brokenSecret + '"')
        $broken = Invoke-CredentialStart
        Assert-Equal $broken.Result.Status 'StartPending' 'credentials: broken JSON still starts 8000'
        Assert-Equal $broken.Launches.Count 2 'credentials: broken JSON launches twice (worker and ingress)'
        Assert-Null $broken.Launches[0].Account 'credentials: broken JSON injects no account'
        Assert-Null $broken.Launches[0].Password 'credentials: broken JSON injects no password'
        Assert-Equal ($broken.Verbose -match 'credentials_invalid') $true 'credentials: broken JSON records credentials_invalid'
        Assert-Equal ($broken.Verbose -notmatch [regex]::Escape($brokenSecret)) $true 'credentials: broken JSON never logs its content'
        Assert-Equal (($Error | ForEach-Object { [string]$_.Exception.Message }) -join "`n" -notmatch [regex]::Escape($brokenSecret)) $true 'credentials: broken JSON never enters the error stream'

        # version 不是 1：不注入。
        Write-CredentialFixture ('{"version":2,"account":"' + $secretAccount + '","password":"' + $secretPassword + '"}')
        $wrongVersion = Invoke-CredentialStart
        Assert-Equal $wrongVersion.Launches.Count 2 'credentials: wrong version still starts 8000 (worker and ingress)'
        Assert-Null $wrongVersion.Launches[0].Account 'credentials: wrong version injects no account'
        Assert-Null $wrongVersion.Launches[0].Password 'credentials: wrong version injects no password'
        Assert-Equal ($wrongVersion.Verbose -match 'credentials_invalid') $true 'credentials: wrong version records credentials_invalid'

        # 只有 account / 只有 password：都不注入。
        foreach ($half in @(
            ('{"version":1,"account":"' + $secretAccount + '"}'),
            ('{"version":1,"password":"' + $secretPassword + '"}')
        )) {
            Write-CredentialFixture $half
            $partial = Invoke-CredentialStart
            Assert-Equal $partial.Launches.Count 2 'credentials: half file still starts 8000 (worker and ingress)'
            Assert-Null $partial.Launches[0].Account 'credentials: half file injects no account'
            Assert-Null $partial.Launches[0].Password 'credentials: half file injects no password'
            Assert-Equal ($partial.Verbose -match 'credentials_invalid') $true 'credentials: half file records credentials_invalid'
        }

        # 空白或非字符串：不注入。
        foreach ($invalidJson in @(
            ('{"version":1,"account":"   ","password":"' + $secretPassword + '"}'),
            ('{"version":1,"account":120,"password":"' + $secretPassword + '"}')
        )) {
            Write-CredentialFixture $invalidJson
            $invalid = Invoke-CredentialStart
            Assert-Equal $invalid.Launches.Count 2 'credentials: invalid field still starts 8000 (worker and ingress)'
            Assert-Null $invalid.Launches[0].Account 'credentials: invalid field injects no account'
            Assert-Null $invalid.Launches[0].Password 'credentials: invalid field injects no password'
            Assert-Equal ($invalid.Verbose -match 'credentials_invalid') $true 'credentials: invalid field records credentials_invalid'
        }

        # usage 8001 不受影响：即使文件有效也不注入。
        Write-CredentialFixture ('{"version":1,"account":"' + $secretAccount + '","password":"' + $secretPassword + '"}')
        Set-CredentialAcl
        $usage = Invoke-CredentialStart -Key 'usage'
        Assert-Equal $usage.Launches.Count 1 'credentials: usage still starts 8001'
        Assert-Null $usage.Launches[0].Account 'credentials: usage never receives the UM account'
        Assert-Null $usage.Launches[0].Password 'credentials: usage never receives the UM password'
        Assert-Equal ($usage.Verbose -notmatch 'credentials_present') $true 'credentials: usage never records chat2api credentials'
    } finally {
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Write-CredentialFixture -ErrorAction SilentlyContinue
        Remove-Item function:\Set-CredentialAcl -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-CredentialStart -ErrorAction SilentlyContinue
        $env:LOCALAPPDATA = $savedLocalAppData
        if ($null -eq $savedAccount) {
            Remove-Item Env:\CHAT2API_UM_ACCOUNT -ErrorAction SilentlyContinue
        } else {
            $env:CHAT2API_UM_ACCOUNT = $savedAccount
        }
        if ($null -eq $savedPassword) {
            Remove-Item Env:\CHAT2API_UM_PASSWORD -ErrorAction SilentlyContinue
        } else {
            $env:CHAT2API_UM_PASSWORD = $savedPassword
        }
        if ($null -eq $savedHiddenLaunch) {
            Remove-Item Env:\SELFUSEAI_HIDDEN_LAUNCH -ErrorAction SilentlyContinue
        } else {
            $env:SELFUSEAI_HIDDEN_LAUNCH = $savedHiddenLaunch
        }
        Remove-Item -LiteralPath $credentialRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# #85: 本机 8001 启动器切换到 Rust 控制台。
#
# 只验证启动描述符、健康边界、停止身份与回滚入口，不启动、不查询真实 8001：
# CIM/HTTP/Start-Process 边界都在本块内替换，离开作用域即恢复。
# ---------------------------------------------------------------------------

& {
    $desc = (Get-GatewayServiceDescriptors)['usage']
    Assert-Equal ([IO.Path]::GetFileName([string]$desc.StartPath)) 'um-console-candidate.exe' 'usage StartPath points at the Rust console binary'
    Assert-Equal ([string]$desc.StartAction) 'HiddenBatch' 'usage keeps the hidden launcher action'
    Assert-Equal ([string]$desc.StartArguments) '--mode production --bind-port 8001 --report-port 8001 --role local --gateway-port 8000 --state-file data/console-state.sqlite3 --dist-dir web/dist --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log' 'usage starts production local 8001 with the console paths'
    Assert-Equal ([string]$desc.StartWorkingDirectory) '.' 'usage launches from the repository root (Python default data paths)'
    Assert-Equal ($null -eq $desc.PSObject.Properties['StartEnvironment']) $true 'usage descriptor needs no environment override; --role local applies the Rust local defaults'
    Assert-Equal ([IO.Path]::GetFileName((Resolve-GatewayStartPath -Key 'usage'))) 'um-console-candidate.exe' 'resolved usage StartPath is the Rust binary'
    Assert-Equal $desc.Port 8001 'Rust usage launcher keeps port 8001'
    Assert-Equal $desc.HealthPath '/health' 'Rust usage launcher keeps the /health path'
    Assert-Equal $desc.StopStrategy 'Port' 'Rust usage launcher keeps the port stop strategy'

    $healthCalls = New-Object System.Collections.ArrayList
    Test-GatewayServiceHealth -Key 'usage' -HttpChecker {
        param($url, $timeout)
        [void]$healthCalls.Add($url)
        return $true
    } | Out-Null
    Assert-Equal $healthCalls[0] 'http://127.0.0.1:8001/health' 'Rust 8001 health check keeps the 8001/health URL'

    # Launch shape: the Rust binary directly, production/local 8001 arguments
    # and the repository root as cwd so data/* and web/dist resolve like Python.
    # `--role local` applies the Rust local defaults, so the launcher must not
    # need an environment override; clear any ambient value to observe that.
    $savedShareRole = [Environment]::GetEnvironmentVariable('UM_SHARE_ROLE', 'Process')
    [Environment]::SetEnvironmentVariable('UM_SHARE_ROLE', $null, 'Process')
    $script:ticket85Launches = New-Object System.Collections.ArrayList
    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        return @()
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @()
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        throw 'offline substitute: health probe refused'
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        throw 'offline substitute: no process may be stopped'
    }
    function Start-Process {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
              [string]$WorkingDirectory, [object]$WindowStyle)
        [void]$script:ticket85Launches.Add([pscustomobject]@{
            FilePath = [string]$FilePath
            ArgumentList = @($ArgumentList)
            WorkingDirectory = [string]$WorkingDirectory
            ShareRole = [string][Environment]::GetEnvironmentVariable('UM_SHARE_ROLE', 'Process')
        })
    }
    try {
        $started = Start-GatewayService -Key 'usage' -Sleeper { param($seconds) }
        Assert-Equal $started.Status 'StartPending' 'Rust usage launch without a listener reports StartPending'
        Assert-Equal $script:ticket85Launches.Count 1 'Rust usage launcher starts exactly once'
        $launch = $script:ticket85Launches[0]
        Assert-Equal $launch.FilePath (Join-Path $root 'rust\target\release\um-console-candidate.exe') 'usage launcher runs the Rust binary directly'
        Assert-Equal ($launch.ArgumentList -join ' ') '--mode production --bind-port 8001 --report-port 8001 --role local --gateway-port 8000 --state-file data/console-state.sqlite3 --dist-dir web/dist --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log' 'usage launcher passes the production/local/8001 arguments'
        Assert-Equal ([IO.Path]::GetFullPath($launch.WorkingDirectory)) $root 'usage launcher runs from the repository root'
        Assert-Equal ([string]::IsNullOrEmpty($launch.ShareRole)) $true 'usage launcher does not set UM_SHARE_ROLE; --role local applies the Rust local defaults'
    } finally {
        [Environment]::SetEnvironmentVariable('UM_SHARE_ROLE', $savedShareRole, 'Process')
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
        Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# #85 AC2: the port stop scope recognizes BOTH the Rust console (current
# launcher) and the Python usagesrv.py entrypoint (rollback). A Rust candidate
# on an ephemeral port stays foreign; the 8001 loopback listener is the authority.
# ---------------------------------------------------------------------------

& {
    $desc = (Get-GatewayServiceDescriptors)['usage']
    $stopIdentities = @(Get-GatewayStopIdentities -Descriptor $desc)
    Assert-Equal (($stopIdentities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-console-candidate.exe' 'usage stop identities cover the Rust runtime and the Python rollback'
    $ownerIdentities = @(Get-GatewayOwnerIdentities -Descriptor $desc)
    Assert-Equal (($ownerIdentities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-console-candidate.exe' 'usage owner identities cover the Rust runtime and the Python rollback'

    $script:ticket85Terminated = New-Object System.Collections.ArrayList
    $script:ticket85Table = @()
    $script:ticket85Listeners = @()
    $script:ticket85Healthy = $false

    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
            $wanted = [int]$Matches[1]
            return @($script:ticket85Table | Where-Object { [int]$_.ProcessId -eq $wanted })
        }
        return @($script:ticket85Table)
    }
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @($script:ticket85Listeners)
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param([string]$Uri, [string]$Method, [object]$Headers,
              [int]$TimeoutSec, [switch]$UseBasicParsing)
        if (-not $script:ticket85Healthy) {
            throw 'offline substitute: health probe refused'
        }
        return [pscustomobject]@{ StatusCode = 200; Content = '{}' }
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        [void]$script:ticket85Terminated.Add([int]$Id)
        # The terminated process no longer answers /health.
        $script:ticket85Healthy = $false
    }

    function New-Ticket85Process {
        param([int]$ProcessId, [string]$CommandLine, [string]$Name = 'python.exe',
              [datetime]$CreationDate = [datetime]'2026-11-17T18:00:00')
        return [pscustomobject]@{
            ProcessId = $ProcessId; Name = $Name
            CommandLine = $CommandLine; CreationDate = $CreationDate
        }
    }

    $rustPrivate = New-Ticket85Process -ProcessId 990301 -Name 'um-console-candidate.exe' `
        -CommandLine ('"' + (Join-Path (Get-GatewayRoot) 'rust\target\release\um-console-candidate.exe') + '" --mode production --bind-port 8001 --report-port 8001 --role local --gateway-port 8000 --state-file data/console-state.sqlite3 --dist-dir web/dist --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log')
    $rustCandidate = New-Ticket85Process -ProcessId 990302 -Name 'um-console-candidate.exe' `
        -CommandLine 'um-console-candidate.exe --mode candidate --role local --bind-port 0 --report-port 8001'
    $pythonPrivate = New-Ticket85Process -ProcessId 990303 `
        -CommandLine 'python usagesrv.py'

    try {
        # Owner identity: the Rust process listening on 8001 is the private instance.
        $script:ticket85Table = @($rustPrivate)
        $script:ticket85Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990301 })
        $script:ticket85Healthy = $true
        $inspection = Get-GatewayServiceInspection -Key 'usage'
        Assert-Equal $inspection.State 'Running' 'Rust 8001 listener is recognized as the private instance'
        Assert-Equal ($inspection.ProcessIds -join ',') '990301' 'Rust 8001 inspection names the Rust PID'

        # Stop scope: Rust private and the Python rollback are private; the
        # ephemeral candidate stays foreign and is never stopped.
        $script:ticket85Table = @($rustPrivate, $rustCandidate, $pythonPrivate)
        $script:ticket85Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990301 })
        $script:ticket85Healthy = $true
        $script:ticket85Terminated.Clear()
        $stopped = Stop-GatewayService -Key 'usage' -Sleeper { param($seconds) }
        Assert-Equal $stopped.Status 'Stopped' 'Rust 8001 stop reports Stopped'
        Assert-Equal ($script:ticket85Terminated -join ',') '990301,990303' 'Rust 8001 stop terminates the Rust private instance and the Python rollback instance'
        Assert-Equal ($script:ticket85Terminated -contains 990302) $false 'Rust candidate instance is never stopped'

        # Rollback path: after StartPath reverts, the Python identity alone remains selectable.
        $script:ticket85Table = @($pythonPrivate)
        $script:ticket85Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990303 })
        $script:ticket85Healthy = $true
        $script:ticket85Terminated.Clear()
        $pythonStopped = Stop-GatewayService -Key 'usage' -Sleeper { param($seconds) }
        Assert-Equal $pythonStopped.Status 'Stopped' 'Python rollback stop reports Stopped'
        Assert-Equal ($script:ticket85Terminated -join ',') '990303' 'Python rollback stop still selects the Python private instance'
    } finally {
        Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
        Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
        Remove-Item function:\Invoke-WebRequest -ErrorAction SilentlyContinue
        Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
    }

    # Bridge start/stop/restart wiring: the JSON entry calls the same runtime
    # functions; the start result carries the Rust StartPath in its detail.
    & {
        . (Join-Path $root 'lib\console-runtime-bridge.ps1')
        function Start-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Started'; Action = 'Start'
                                      Detail = (Resolve-GatewayStartPath -Key $Key) }
        }
        function Stop-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Stopped'; Action = 'Stop' }
        }
        function Restart-GatewayService {
            param($Key)
            return [pscustomobject]@{ Key = $Key; Status = 'Restarted'; Action = 'Restart' }
        }
        $lockDir = Join-Path ([IO.Path]::GetTempPath()) ('ticket85-locks-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        try {
            $start = Invoke-ConsoleRuntimeStart -Key 'usage' -LockDir $lockDir
            Assert-Equal $start.ok $true 'bridge start on Rust 8001 reports ok'
            Assert-Equal $start.result.status 'Started' 'bridge start on Rust 8001 reports Started'
            Assert-Equal ([IO.Path]::GetFileName([string]$start.result.detail)) 'um-console-candidate.exe' 'bridge start on Rust 8001 resolves the Rust StartPath'
            $stop = Invoke-ConsoleRuntimeStop -Key 'usage' -LockDir $lockDir
            Assert-Equal $stop.ok $true 'bridge stop on Rust 8001 reports ok'
            Assert-Equal $stop.result.status 'Stopped' 'bridge stop on Rust 8001 reports Stopped'
            $restart = Invoke-ConsoleRuntimeRestart -Key 'usage' -LockDir $lockDir
            Assert-Equal $restart.ok $true 'bridge restart on Rust 8001 reports ok'
            Assert-Equal $restart.result.status 'Restarted' 'bridge restart on Rust 8001 reports Restarted'
        } finally {
            Remove-Item -LiteralPath $lockDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# #85 AC3: rollback is one StartPath change back to start_usage_service.bat; the
# Python command, port and stop identity stay in place, and the rollback steps are
# written down for the operator.
# ---------------------------------------------------------------------------

& {
    # The Python 8001 launcher was retired with the rollback closure (#90);
    # StartPath is the Rust console binary and the descriptor is its only entrypoint.

    $contract = Get-Content -LiteralPath (Join-Path $root 'docs\rust-contract.md') -Raw -Encoding utf8
    Assert-Equal ($contract -match '本机 8001 启动器与回滚') $true 'rust-contract documents the local 8001 launcher and rollback'
    Assert-Equal ($contract -match '只改 `StartPath`') $true 'rollback is documented as a single StartPath change'
    Assert-Equal ($contract -match 'start_usage_service\.bat') $true 'rollback names the Python launcher'
    Assert-Equal ($contract -match '不需要重装') $true 'rollback documents that no reinstall is needed'
    Assert-Equal ($contract -match '不需要改端口|无需改端口') $true 'rollback documents that no port change is needed'

    # Simulate the rollback: only StartPath changes back; port, health path
    # and stop identities stay valid for the Python runtime.
    $desc = (Get-GatewayServiceDescriptors)['usage']
    $savedStartPath = [string]$desc.StartPath
    try {
        $desc.StartPath = 'start_usage_service.bat'
        Assert-Equal ([IO.Path]::GetFileName((Resolve-GatewayStartPath -Key 'usage'))) 'start_usage_service.bat' 'changing only StartPath resolves the Python launcher'
        Assert-Equal $desc.Port 8001 'rollback keeps port 8001'
        Assert-Equal $desc.HealthPath '/health' 'rollback keeps the /health path'
        Assert-Equal $desc.StopStrategy 'Port' 'rollback keeps the port stop strategy'
        $identities = @(Get-GatewayStopIdentities -Descriptor $desc)
        Assert-Equal (($identities | ForEach-Object { [string]$_.ProcessName }) -join ',') 'python.exe,um-console-candidate.exe' 'rollback still recognizes both private instances'
    } finally {
        $desc.StartPath = $savedStartPath
    }
}

if ($LauncherOnly) {
    if ($script:failures -gt 0) {
        Write-Host "`n$($script:failures) local 8000/8001 launcher test(s) FAILED." -ForegroundColor Red
        exit 1
    }
    Write-Host "`nAll local 8000/8001 launcher tests passed." -ForegroundColor Green
    exit 0
}

# Thin launch adapters must carry only a service identity. Runtime details stay
# in gateway-runtime.ps1.
foreach ($adapter in @(
    @{ File = 'start_chat2api_hidden.vbs'; Key = 'chat2api' },
    @{ File = 'start_usage_service.vbs'; Key = 'usage' }
)) {
    $source = Get-Content -LiteralPath (Join-Path $root $adapter.File) -Raw -Encoding utf8
    Assert-Equal ($source -match 'invoke-gateway-service\.ps1') $true "$($adapter.File) delegates to the runtime adapter"
    Assert-Equal ($source -match ("-Key " + $adapter.Key)) $true "$($adapter.File) passes its service key"
    Assert-Equal ($source -notmatch '127\.0\.0\.1|D:\\git') $true "$($adapter.File) has no port or repository path"
}

# The private chat2api launcher was retired with the rollback closure (#90);
# the Rust gateway reads CHAT2API_ALLOWED_ORIGINS from its own environment.

$healthCheckCalls.Clear()
Test-GatewayServiceHealth -Key 'usage' -HttpChecker {
    param($url, $timeout)
    [void]$healthCheckCalls.Add($url)
    return $true
}
Assert-Equal $healthCheckCalls[0] 'http://127.0.0.1:8001/health' 'usage health URL is correct'

# --- Get-LocalControlHeader (chat2api local management token) ---
$controlDir = Join-Path $env:TEMP ("gwr-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $controlDir | Out-Null
try {
    $missing = Get-LocalControlHeader -TokenFile (Join-Path $controlDir 'absent-token')
    Assert-Equal ($missing.Count) 0 'control header empty when token file absent'
    $tokenFile = Join-Path $controlDir 'admin-control-token'
    Set-Content -Path $tokenFile -Value "  secret-token-value`n" -Encoding ASCII
    $header = Get-LocalControlHeader -TokenFile $tokenFile
    Assert-Equal $header['X-Admin-Control'] 'secret-token-value' 'control header carries trimmed token'
} finally {
    Remove-Item -Recurse -Force $controlDir -ErrorAction SilentlyContinue
}

# --- Test-Chat2ApiControlUri (secret only leaves for 8000 admin/api paths) ---
Assert-Equal (Test-Chat2ApiControlUri 'http://127.0.0.1:8000/admin/auth/status') $true 'control uri: 8000 admin'
Assert-Equal (Test-Chat2ApiControlUri 'http://localhost:8000/api/version') $true 'control uri: localhost api'
Assert-Equal (Test-Chat2ApiControlUri 'http://127.0.0.1:8000/v1/models') $false 'control uri: business path excluded'
Assert-Equal (Test-Chat2ApiControlUri 'http://127.0.0.1:8001/api/summary') $false 'control uri: usage never'
Assert-Equal (Test-Chat2ApiControlUri 'http://127.0.0.1:8000/health') $false 'control uri: health excluded'

# ---------------------------------------------------------------------------
# Round 18 (2026-09-29): private stop scope isolation (D-6/D-7).
#
# StopCommandLinePattern ((^|\s)chat2api\.py(\s|$)) alone also matched the
# a second chat2api.py instance, so Stop-GatewayService's GracefulHttp residual
# branch could force-stop the wrong instance.
#
# The substitutes below land on the LAST boundary — the CIM process query and the
# Stop-Process / Invoke-WebRequest / Start-Process calls — so the real selection
# branch under test actually runs. The top-level -Stopper seam would skip it
# entirely, which is why these cases never pass it. Nothing here can reach a real
# process or port: the four cmdlets are replaced in this scope (asserted below),
# and every fake PID is verified not to be a live process.
# ---------------------------------------------------------------------------

$script:stopScopeTerminated = New-Object System.Collections.ArrayList
$script:stopScopeRequests = New-Object System.Collections.ArrayList
$script:stopScopeLaunches = New-Object System.Collections.ArrayList
$script:stopScopeTable = @()
$script:stopScopeRequery = $null
$script:stopScopeQueryFails = $false
$script:stopScopeGracefulOk = $true
$script:stopScopeHealthy = $false
$script:stopScopeListeners = @()
$script:stopScopeListenerQueryFails = $false

function Get-CimInstance {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$ClassName,
        [string]$Filter,
        [string]$Namespace
    )
    # The substitute owns ONLY the Win32_Process boundary; any other class
    # (e.g. Win32_OperatingSystem for the boot identity) delegates to the real
    # cmdlet so offline results stay consistent with production identities.
    if ($ClassName -and $ClassName -ne 'Win32_Process') {
        return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
    }
    if ($script:stopScopeQueryFails) {
        throw 'offline substitute: Win32_Process query unavailable'
    }
    if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
        $wanted = [int]$Matches[1]
        if ($null -ne $script:stopScopeRequery) {
            return @($script:stopScopeRequery)
        }
        return @($script:stopScopeTable | Where-Object { [int]$_.ProcessId -eq $wanted })
    }
    return @($script:stopScopeTable)
}

function Stop-Process {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
    [void]$script:stopScopeTerminated.Add([int]$Id)
}

function Invoke-WebRequest {
    [CmdletBinding()]
    param(
        [string]$Uri,
        [string]$Method,
        [object]$Headers,
        [int]$TimeoutSec,
        [switch]$UseBasicParsing
    )
    # The header value (the local control token) is deliberately never recorded.
    if ($Method -eq 'Post') {
        [void]$script:stopScopeRequests.Add([string]$Uri)
        if (-not $script:stopScopeGracefulOk) {
            throw 'offline substitute: shutdown request refused'
        }
        return [pscustomobject]@{ StatusCode = 200; Content = '{}' }
    }
    if (-not $script:stopScopeHealthy) {
        throw 'offline substitute: health probe refused'
    }
    return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
}

function Start-Process {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$FilePath,
        [object]$ArgumentList,
        [string]$WorkingDirectory,
        [object]$WindowStyle
    )
    if ($env:SELFUSEAI_CHAT2API_OBSERVER_LAUNCH) {
        $plan = $env:SELFUSEAI_CHAT2API_OBSERVER_LAUNCH | ConvertFrom-Json
        $FilePath = $plan.executable
    }
    [void]$script:stopScopeLaunches.Add([string]$FilePath)
}

function Get-NetTCPConnection {
    [CmdletBinding()]
    param([int]$LocalPort, [object]$State)
    if ($script:stopScopeListenerQueryFails) {
        throw 'offline substitute: listener query unavailable'
    }
    return @($script:stopScopeListeners)
}

function New-StopScopeProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory = $true)][datetime]$CreationDate,
        [string]$Name = 'python.exe'
    )
    return [pscustomobject]@{
        ProcessId = $ProcessId; Name = $Name
        CommandLine = $CommandLine; CreationDate = $CreationDate
    }
}

# Command-line fixtures mirror the shapes read from the live machine in round 18
# (8000 PID 58336 = private); paths are synthetic here so the fixture never
# depends on this checkout's location.
$script:stopScopePrivateCmdLine = 'python  chat2api.py --base-url https://chat.example.invalid'
$script:stopScopeOtherCheckoutCmdLine = 'python D:\other-checkout\ai-gateway\chat2api\chat2api.py --base-url https://chat.example.invalid'
$script:stopScopePrivateQualifiedCmdLine = 'python ' + (Join-Path (Get-GatewayRoot) 'chat2api\chat2api.py') + ' --base-url https://chat.example.invalid'
$script:stopScopePrivateStart = [datetime]'2026-09-29T20:11:25'
$script:stopScopeShareCmdLine = 'python chat2api.py --profile D:\share\edge-profile'
$script:stopScopeShareStart = [datetime]'2026-09-29T20:12:25'

function Invoke-StopScopeScenario {
    # Runs one private stop against the real runtime with the boundary
    # substitutes above and reports the SELECTED termination set.
    param(
        [Parameter(Mandatory = $true)][array]$Processes,
        [switch]$GracefulFails,
        [switch]$QueryFails,
        [AllowNull()][array]$RequeryProcesses,
        [switch]$Healthy,
        [AllowNull()][array]$Listeners
    )
    $script:stopScopeTable = $Processes
    $script:stopScopeRequery = $RequeryProcesses
    $script:stopScopeQueryFails = [bool]$QueryFails
    $script:stopScopeGracefulOk = -not [bool]$GracefulFails
    $script:stopScopeHealthy = [bool]$Healthy
    $script:stopScopeListeners = @()
    if ($null -ne $Listeners) { $script:stopScopeListeners = @($Listeners) }
    $script:stopScopeTerminated.Clear()
    $script:stopScopeRequests.Clear()
    $script:stopScopeLaunches.Clear()
    $result = $null
    try {
        $result = Stop-GatewayService -Key 'chat2api' `
            -HttpChecker { param($url, $timeout) $script:stopScopeHealthy } `
            -Sleeper { param($seconds) }
    } catch {
        $result = [pscustomobject]@{ Key = 'chat2api'; Status = 'THREW'
                                   Action = 'Stop'; Detail = $_.Exception.GetType().Name }
    }
    return [pscustomobject]@{
        Result     = $result
        Terminated = @($script:stopScopeTerminated)
        Requests   = @($script:stopScopeRequests)
    }
}

function Invoke-StartScopeScenario {
    # Runs a real start (no HttpChecker/Launcher seam) so the descriptor's
    # inspection + residual-cleanup branch actually runs, with the same boundary
    # substitutes: the fake health probe stays unhealthy and the launch call is
    # recorded instead of spawning a real private instance.
    param(
        [Parameter(Mandatory = $true)][array]$Processes,
        [AllowNull()][array]$Listeners,
        [string]$Key = 'chat2api'
    )
    $script:stopScopeTable = $Processes
    $script:stopScopeListeners = @()
    if ($null -ne $Listeners) { $script:stopScopeListeners = @($Listeners) }
    $script:stopScopeRequery = $null
    $script:stopScopeQueryFails = $false
    $script:stopScopeHealthy = $false
    $script:stopScopeGracefulOk = $true
    $script:stopScopeTerminated.Clear()
    $script:stopScopeLaunches.Clear()
    $result = $null
    try {
        $result = Start-GatewayService -Key $Key -Sleeper { param($seconds) }
    } catch {
        $result = [pscustomobject]@{ Key = $Key; Status = 'THREW'
                                   Action = 'Start'; Detail = $_.Exception.GetType().Name }
    }
    return [pscustomobject]@{
        Result     = $result
        Terminated = @($script:stopScopeTerminated)
        Launches   = @($script:stopScopeLaunches)
    }
}

# Safety gate: if any boundary substitute is missing, the runtime below could
# reach a REAL process or port, so the section reports FAIL and stops instead of
# running. The fake PIDs must not be live processes either.
$boundaryArmed = ((Get-Command Stop-Process).CommandType -eq 'Function') -and
    ((Get-Command Get-CimInstance).CommandType -eq 'Function') -and
    ((Get-Command Invoke-WebRequest).CommandType -eq 'Function') -and
    ((Get-Command Start-Process).CommandType -eq 'Function') -and
    ((Get-Command Get-NetTCPConnection).CommandType -eq 'Function')
Assert-Equal $boundaryArmed $true 'stop-scope substitutes are in scope (no real process can be queried or terminated)'
foreach ($fakePid in @(990101, 990102, 990103, 990104, 990105, 990106, 990107, 990108)) {
    Assert-Equal ([bool](Get-Process -Id $fakePid -ErrorAction SilentlyContinue)) $false "fake pid $fakePid is not a live process"
}
if (-not $boundaryArmed) {
    # Fail-closed: without the substitutes a stop scenario would query the REAL
    # process table and could terminate a live gateway instance.
    Write-Host 'FAIL stop-scope scenarios skipped: the boundary substitutes are missing.' -ForegroundColor Red
    exit 1
}

# --- Case 1 (load-bearing): two instances coexist + one stop request ---
$twoInstances = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($twoInstances.Terminated -join ',') '990101' 'minimal case: private stop selects only the private 8000 instance'
Assert-Equal $twoInstances.Result.Status 'Stopped' 'minimal case: stop reports Stopped after terminating the private instance'

# --- Case 2 (two instances): + another checkout's same-named script ---
$fullScope = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990104 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopePrivateStart)
)
Assert-Equal ($fullScope.Terminated -join ',') '990101' 'private stop selects exactly the verified private instance when another checkout coexists'
Assert-Equal ($fullScope.Terminated -contains 990104) $false 'another checkout''s same-named script is never selected'
Assert-Equal $fullScope.Result.Status 'Stopped' 'private stop still reports Stopped when only the private instance is terminated'
Assert-Equal ($twoInstances.Requests -join ',') 'http://127.0.0.1:8000/admin/shutdown' 'private stop sends the graceful request to its own 8000 shutdown endpoint'

# --- Case 3: graceful shutdown request fails (pre-listen / refused POST) ---
$gracefulFailed = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
) -GracefulFails
Assert-Equal ($gracefulFailed.Terminated -join ',') '990101' 'failed graceful shutdown still terminates only the verified private instance'
Assert-Equal $gracefulFailed.Result.Status 'Stopped' 'failed graceful shutdown still reports Stopped after the scoped cleanup'

# --- Case 4: residual cleanup (a stale private-class process next to the share backend) ---
$residualScope = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990106 -CommandLine 'python chat2api.py' -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeShareCmdLine -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($residualScope.Terminated -join ',') '990101,990106' 'residual cleanup terminates the verified private-class processes (live and stale)'
Assert-Equal ($residualScope.Terminated -contains 990102) $false 'residual cleanup never terminates the share backend'

# --- Case 5: cold start, private process has not opened its port yet ---
$coldStart = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart)
)
Assert-Equal ($coldStart.Terminated -join ',') '990101' 'cold-start private process (not listening, no --port) is verified private and terminated'
Assert-Equal $coldStart.Result.Status 'Stopped' 'cold-start stop reports Stopped'

# --- Case 6: stale PID (the selected process is gone at kill time) ---
$stalePid = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
) -RequeryProcesses @()
Assert-Equal ($stalePid.Terminated -join ',') '' 'a vanished PID is never re-used blindly: nothing is terminated'
Assert-Equal $stalePid.Result.Status 'StopRefused' 'a vanished PID surfaces as StopRefused, never as a claimed Stopped'

# --- Case 7: reused PID (the same PID now belongs to another process) ---
$reusedPid = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
) -RequeryProcesses @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine 'python some-other-tool.py --serve' -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($reusedPid.Terminated -join ',') '' 'a reused PID is never terminated'
Assert-Equal $reusedPid.Result.Status 'StopRefused' 'a reused PID surfaces as StopRefused, never as a claimed Stopped'

# --- Case 8: ownership unknown (the process table query itself fails) ---
$unknownOwner = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart)
) -QueryFails
Assert-Equal ($unknownOwner.Terminated -join ',') '' 'an unreadable process table terminates nothing'
Assert-Equal $unknownOwner.Result.Status 'StopRefused' 'an unreadable process table refuses instead of broadly force-stopping'

# --- Case 9: ownership unknown (a name-matching process with an unreadable command line) ---
$unreadableOwner = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990107 -CommandLine $null -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($unreadableOwner.Terminated -join ',') '' 'a name-matching process with an unreadable command line is never terminated'
Assert-Equal $unreadableOwner.Result.Status 'StopRefused' 'an unconfirmable identity refuses the stop rather than guessing'

# --- Case 10: same entrypoint referenced by path (this checkout vs another) ---
$qualifiedPath = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990105 -CommandLine $script:stopScopePrivateQualifiedCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990104 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($qualifiedPath.Terminated -join ',') '990105' 'a path-qualified reference to this checkout''s own entrypoint is verified private'
Assert-Equal ($qualifiedPath.Terminated -contains 990104) $false 'a path-qualified same-named script from another checkout stays untouched'

# --- Case 11: the share backend alone (private instance already gone) ---
$shareOnly = Invoke-StopScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeShareCmdLine -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($shareOnly.Terminated -join ',') '' 'a private stop leaves the share backend running when the private instance is already gone'
Assert-Equal $shareOnly.Result.Status 'Stopped' 'the private instance is reported stopped without touching the share backend'

# --- Case 12: restart runs the same scoped stop and launches once ---
$script:restartHealth = [System.Collections.Generic.Queue[bool]]::new()
$script:restartHealth.Enqueue($true)   # stop: was healthy
$script:restartHealth.Enqueue($false)  # stop post-check: stopped
$script:restartHealth.Enqueue($false)  # start pre-check
$script:restartHealth.Enqueue($true)   # start post-launch
$script:stopScopeTerminated.Clear()
$script:stopScopeLaunches.Clear()
$script:stopScopeTable = @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
)
$script:stopScopeRequery = $null
$script:stopScopeQueryFails = $false
$script:restartLaunches = New-Object System.Collections.ArrayList
$restartScoped = Restart-GatewayService -Key 'chat2api' `
    -HttpChecker { param($url, $timeout) $script:restartHealth.Dequeue() } `
    -Launcher {
        param($key, $path, $desc, $component)
        [void]$script:restartLaunches.Add([pscustomobject]@{
            Key = $key; Component = [string]$component
        })
    } `
    -Sleeper { param($seconds) }
Assert-Equal ($script:stopScopeTerminated -join ',') '990101' 'restart stops only the verified private instance'
Assert-Equal (($script:restartLaunches | ForEach-Object { $_.Key }) -join ',') 'chat2api,chat2api' 'restart launches the worker and the ingress after the scoped stop'
Assert-Equal (($script:restartLaunches | ForEach-Object { $_.Component }) -join ',') 'worker,ingress' 'restart launches the worker before the ingress'
Assert-Equal $restartScoped.Status 'Restarted' 'restart reports Restarted'

# --- Case 13: restart refuses to launch when the stop scope is unconfirmed ---
$script:stopScopeTerminated.Clear()
$script:stopScopeLaunches.Clear()
$script:stopScopeQueryFails = $true
$refusedRestart = Restart-GatewayService -Key 'chat2api' `
    -HttpChecker { param($url, $timeout) $false } `
    -Launcher { param($key, $path, $desc) [void]$script:stopScopeLaunches.Add([string]$key) } `
    -Sleeper { param($seconds) }
Assert-Equal $refusedRestart.Status 'StopRefused' 'restart surfaces the stop refusal instead of a silent success'
Assert-Equal ($script:stopScopeLaunches.Count) 0 'restart does not launch after a refused stop'
Assert-Equal ($script:stopScopeTerminated -join ',') '' 'restart terminates nothing when the stop scope is unconfirmed'
$script:stopScopeQueryFails = $false

# --- Case 14: start's residual cleanup uses the same scoped stop ---
$startCleanup = Invoke-StartScopeScenario -Processes @(
    (New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart),
    (New-StopScopeProcess -ProcessId 990102 -CommandLine $script:stopScopeOtherCheckoutCmdLine -CreationDate $script:stopScopeShareStart)
)
Assert-Equal ($startCleanup.Terminated -join ',') '990101' 'start residual cleanup terminates only the verified private instance'
Assert-Equal ($startCleanup.Terminated -contains 990102) $false 'start residual cleanup never terminates the share backend'
Assert-Equal ([IO.Path]::GetFileName([string]$startCleanup.Launches[0])) 'um-gateway-candidate.exe' 'start launches the Rust binary once after the scoped residual cleanup'
Assert-Equal $startCleanup.Result.Status 'StartPending' 'start reports StartPending when the health check stays false'

# --- Case 15 (#118): the usage residual cleanup uses the same scoped stop ---
# 2026-11-20: 8001 had no listener but an old managed console process was
# still alive. Stop-GatewayService returned AlreadyStopped for it because the
# descriptor has no StopWhenUnhealthy, so Start reported "受管残留进程清理失败".
# A Residual inspection must now run the verified scoped stop instead.
$usageStartCleanup = Invoke-StartScopeScenario -Key 'usage' -Processes @(
    (New-StopScopeProcess -ProcessId 990103 -CommandLine 'python usagesrv.py' -CreationDate $script:stopScopePrivateStart)
)
Assert-Equal ($usageStartCleanup.Terminated -join ',') '990103' 'usage start residual cleanup terminates only the verified private instance'
Assert-Equal ([IO.Path]::GetFileName([string]$usageStartCleanup.Launches[0])) 'um-console-candidate.exe' 'usage start launches the Rust console once after the residual cleanup'
Assert-Equal $usageStartCleanup.Result.Status 'StartPending' 'usage start reports StartPending when the health check stays false'

# Loopback endpoint scope: a tailnet Serve listener may legally share the port.
# These cases drive the real inspection/start/stop chain with only OS/HTTP
# boundaries substituted; no configured service port or real PID is touched.
function Invoke-EndpointScopeScenario {
    param(
        [ValidateSet('Inspect', 'Start', 'Stop')][string]$Action = 'Inspect',
        [string]$Key = 'chat2api',
        [array]$Processes = @(),
        [array]$Listeners = @(),
        [AllowNull()][array]$RequeryProcesses,
        [switch]$QueryFails,
        [switch]$ListenerQueryFails,
        [switch]$Healthy
    )
    $script:stopScopeTable = $Processes
    $script:stopScopeListeners = $Listeners
    $script:stopScopeRequery = $RequeryProcesses
    $script:stopScopeQueryFails = [bool]$QueryFails
    $script:stopScopeListenerQueryFails = [bool]$ListenerQueryFails
    $script:stopScopeHealthy = [bool]$Healthy
    $script:stopScopeTerminated.Clear()
    $script:stopScopeLaunches.Clear()
    $result = $null
    try {
        switch ($Action) {
            'Inspect' { $result = Get-GatewayServiceInspection -Key $Key }
            'Start' { $result = Start-GatewayService -Key $Key -Sleeper { param($seconds) } }
            # The health seam bypasses only the pre-inspection here, allowing
            # the kill-time scope and PID recheck to be exercised independently.
            'Stop' {
                $result = Stop-GatewayService -Key $Key `
                    -HttpChecker { param($url, $timeout) $script:stopScopeHealthy } `
                    -Sleeper { param($seconds) }
            }
        }
    } catch {
        $result = [pscustomobject]@{ State = 'THREW'; Status = 'THREW' }
    }
    return [pscustomobject]@{
        Result = $result
        Terminated = @($script:stopScopeTerminated)
        Launches = @($script:stopScopeLaunches)
    }
}
$endpointPrivate = New-StopScopeProcess -ProcessId 990101 -CommandLine $script:stopScopePrivateCmdLine -CreationDate $script:stopScopePrivateStart
$endpointServe = New-StopScopeProcess -ProcessId 990102 -Name 'tailscaled.exe' -CommandLine 'tailscaled.exe' -CreationDate $script:stopScopeShareStart
$endpointUnknown = New-StopScopeProcess -ProcessId 990103 -Name 'unknown.exe' -CommandLine 'unknown.exe' -CreationDate $script:stopScopeShareStart
$loopbackListener = [pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990101 }
$serveListeners = @(
    [pscustomobject]@{ LocalAddress = '100.119.187.73'; OwningProcess = 990102 },
    [pscustomobject]@{ LocalAddress = 'fd7a:115c:a1e0::9401:bbdd'; OwningProcess = 990102 }
)
$coexisting = Invoke-EndpointScopeScenario -Processes @($endpointPrivate, $endpointServe) `
    -Listeners (@($loopbackListener) + $serveListeners) -Healthy
Assert-Equal $coexisting.Result.State 'Running' 'loopback UM remains Running when tailnet IPv4/IPv6 share port 8000'
Assert-Equal ($coexisting.Result.ProcessIds -join ',') '990101' 'inspection names only the loopback owner, never tailscaled'
$coexistingStart = Invoke-EndpointScopeScenario -Action Start -Processes @($endpointPrivate, $endpointServe) `
    -Listeners (@($loopbackListener) + $serveListeners) -Healthy
Assert-Equal $coexistingStart.Result.Status 'AlreadyRunning' 'start is idempotent with a separate tailnet listener'
Assert-Equal ($coexistingStart.Launches -join ',') '' 'start does not launch a duplicate UM instance'
$onlySeparate = Invoke-EndpointScopeScenario -Processes @($endpointServe) -Listeners $serveListeners
Assert-Equal $onlySeparate.Result.State 'Stopped' 'tailnet-only listeners do not occupy the loopback endpoint'
$separateLoopbacks = Invoke-EndpointScopeScenario -Processes @($endpointServe) -Listeners @(
    [pscustomobject]@{ LocalAddress = '::1'; OwningProcess = 990102 },
    [pscustomobject]@{ LocalAddress = '127.0.0.2'; OwningProcess = 990102 }
)
Assert-Equal $separateLoopbacks.Result.State 'Stopped' 'other loopback addresses do not overlap the exact IPv4 endpoint'
foreach ($address in @('127.0.0.1', '0.0.0.0', '::', '::ffff:127.0.0.1')) {
    $conflict = Invoke-EndpointScopeScenario -Processes @($endpointPrivate, $endpointUnknown) -Healthy `
        -Listeners @($loopbackListener, [pscustomobject]@{ LocalAddress = $address; OwningProcess = 990103 })
    Assert-Equal $conflict.Result.State 'Conflict' "an unknown owner on overlapping address $address remains a conflict"
}
foreach ($address in @($null, '', 'unreadable-address')) {
    $unreadableAddress = Invoke-EndpointScopeScenario -Processes @($endpointPrivate) -Healthy `
        -Listeners @([pscustomobject]@{ LocalAddress = $address; OwningProcess = 990101 })
    Assert-Equal $unreadableAddress.Result.State 'Conflict' 'an unconfirmed listener address never reports Running'
}
$missingAddress = Invoke-EndpointScopeScenario -Processes @($endpointPrivate) -Healthy `
    -Listeners @([pscustomobject]@{ OwningProcess = 990101 })
Assert-Equal $missingAddress.Result.State 'Conflict' 'a missing listener address fails closed'
$unreadableListener = [pscustomobject]@{ OwningProcess = 990101 }
$unreadableListener | Add-Member -MemberType ScriptProperty -Name LocalAddress -Value { throw 'offline address property unreadable' }
$unreadableProperty = Invoke-EndpointScopeScenario -Processes @($endpointPrivate) -Healthy -Listeners @($unreadableListener)
Assert-Equal $unreadableProperty.Result.State 'Conflict' 'an unreadable address property fails closed'
$unconfirmedStart = Invoke-EndpointScopeScenario -Action Start -Processes @($endpointPrivate) `
    -Listeners @($unreadableListener)
Assert-Equal $unconfirmedStart.Result.Status 'Conflict' 'start refuses unconfirmed listener scope'
Assert-Equal ($unconfirmedStart.Launches -join ',') '' 'unconfirmed listener scope starts no process'
$missingOwner = Invoke-EndpointScopeScenario -Listeners @($loopbackListener) -Healthy
Assert-Equal $missingOwner.Result.State 'Conflict' 'an unreadable or vanished listener owner never reports Stopped'
$failedOwnerQuery = Invoke-EndpointScopeScenario -Listeners @($loopbackListener) -QueryFails -Healthy
Assert-Equal $failedOwnerQuery.Result.State 'Conflict' 'a failed process identity query fails closed'
$failedListenerQuery = Invoke-EndpointScopeScenario -ListenerQueryFails -Healthy
Assert-Equal $failedListenerQuery.Result.State 'Conflict' 'a failed listener query never reports Stopped'

$usagePrivate = New-StopScopeProcess -ProcessId 990101 -CommandLine 'python usagesrv.py' -CreationDate $script:stopScopePrivateStart
$usageOtherInterface = New-StopScopeProcess -ProcessId 990104 -CommandLine 'python usagesrv.py --host 100.119.187.73' -CreationDate $script:stopScopePrivateStart
$usageListeners = @($loopbackListener) + $serveListeners + @(
    [pscustomobject]@{ LocalAddress = '100.119.187.74'; OwningProcess = 990104 }
)
$usageStop = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy `
    -Processes @($usagePrivate, $endpointServe, $usageOtherInterface) -Listeners $usageListeners
Assert-Equal ($usageStop.Terminated -join ',') '990101' 'stopping 8001 terminates only its verified loopback process'
Assert-Equal ($usageStop.Result.TerminatedProcessIds -join ',') '990101' 'Port stop reports the verified terminated PID'
Assert-Equal ($usageStop.Terminated -contains 990102) $false 'Port stop never terminates tailscaled on separate interfaces'
Assert-Equal ($usageStop.Terminated -contains 990104) $false 'Port stop preserves a same-named process bound only to another interface'
$reusedUsage = New-StopScopeProcess -ProcessId 990101 -CommandLine 'python usagesrv.py' -CreationDate $script:stopScopePrivateStart.AddMinutes(1)
$usageReused = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy -Processes @($usagePrivate) `
    -Listeners @($loopbackListener) -RequeryProcesses @($reusedUsage)
Assert-Equal $usageReused.Result.Status 'StopRefused' 'Port stop refuses a PID whose startup identity changed'
Assert-Equal ($usageReused.Terminated -join ',') '' 'Port stop does not terminate a reused PID'
$usageMissingIdentity = [pscustomobject]@{
    ProcessId = 990101; Name = 'python.exe'; CommandLine = 'python usagesrv.py'; CreationDate = $null
}
$usageUnreadableIdentity = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy -Processes @($usagePrivate) `
    -Listeners @($loopbackListener) -RequeryProcesses @($usageMissingIdentity)
Assert-Equal $usageUnreadableIdentity.Result.Status 'StopRefused' 'Port stop refuses an unreadable startup identity'
Assert-Equal ($usageUnreadableIdentity.Terminated -join ',') '' 'an unreadable startup identity terminates nothing'
$usageUnknownAddress = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy -Processes @($usagePrivate) `
    -Listeners @([pscustomobject]@{ OwningProcess = 990101 })
Assert-Equal $usageUnknownAddress.Result.Status 'StopRefused' 'Port stop refuses an unconfirmed listener address'
Assert-Equal ($usageUnknownAddress.Terminated -join ',') '' 'unconfirmed address terminates nothing'
$usageMissingOwner = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy -Processes @($usagePrivate) `
    -Listeners @($loopbackListener, [pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990103 })
Assert-Equal $usageMissingOwner.Result.Status 'StopRefused' 'Port stop refuses an unknown overlapping owner'
Assert-Equal ($usageMissingOwner.Terminated -join ',') '' 'unknown overlapping owner prevents every termination'
$usageQueryFailure = Invoke-EndpointScopeScenario -Action Stop -Key usage -Healthy -Processes @($usagePrivate) `
    -Listeners @($loopbackListener) -ListenerQueryFails
Assert-Equal $usageQueryFailure.Result.Status 'StopRefused' 'Port stop refuses an unavailable listener snapshot'
Assert-Equal ($usageQueryFailure.Terminated -join ',') '' 'an unavailable listener snapshot terminates nothing'
$script:stopScopeHealthy = $false
$script:stopScopeRequery = $null
$script:stopScopeListenerQueryFails = $false

# --- Round 19 (2026-09-30): operator intent marker for the private watch ---
# Every case points the marker at a temp directory; the real marker under
# %LOCALAPPDATA% must never be touched by a test.
$markerRoot = Join-Path ([IO.Path]::GetTempPath()) ('gateway-desired-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$savedWatchDir = $env:SELFUSEAI_GATEWAY_WATCH_DIR
$savedSource = $env:SELFUSEAI_GATEWAY_ACTION_SOURCE
$env:SELFUSEAI_GATEWAY_WATCH_DIR = $markerRoot
Remove-Item Env:\SELFUSEAI_GATEWAY_ACTION_SOURCE -ErrorAction SilentlyContinue
$markerPath = Get-GatewayDesiredStatePath -Key 'chat2api'
$noopStop = { param($key, $desc) }
$noopLaunch = { param($key, $path, $desc) }
try {
    Assert-Equal ($markerPath.StartsWith($markerRoot)) $true 'desired-state marker honours the test directory override'

    # Off unless an operator-facing entry point opts in: dot-sourcing the
    # runtime (tests, model sync) never writes the marker.
    $script:GatewayDesiredStateEnabled = $false
    [void](Stop-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } -Stopper $noopStop -Sleeper { param($s) })
    Assert-Equal (Test-Path -LiteralPath $markerPath) $false 'marker is not written while desired-state tracking is off'

    Enable-GatewayDesiredState
    [void](Stop-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } -Stopper $noopStop -Sleeper { param($s) })
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.state) 'stopped' 'a stop records the stopped intent'
    Assert-Equal ([string]$marker.source) 'cli' 'an unmarked caller is recorded as cli'
    Assert-Equal ([int]$marker.version) 2 'the marker is version 2 (Rev13-W start_outcome)'
    Assert-Equal ([string]$marker.start_outcome) 'never' 'a stop before any confirmed start records start_outcome=never'

    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = 'watch'
    [void](Start-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $true } -Launcher $noopLaunch -Sleeper { param($s) })
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.state) 'running' 'a start records the running intent'
    Assert-Equal ([string]$marker.source) 'watch' 'the action source is carried into the marker'
    Assert-Equal ([string]$marker.start_outcome) 'succeeded' 'a health-confirmed start records start_outcome=succeeded'

    # Restart: the stop half keeps the intent, the start half writes running
    # afterwards (the watch relies on "intent newer than the shutdown").
    [void](Write-GatewayDesiredState -Key 'chat2api' -State 'stopped')
    $script:markerDuringStop = $null
    $restartStop = { param($key, $desc) $script:markerDuringStop = [string](Read-GatewayDesiredState -Key 'chat2api').state }
    [void](Restart-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } `
        -Stopper $restartStop -Launcher $noopLaunch -Sleeper { param($s) })
    Assert-Equal $script:markerDuringStop 'stopped' 'restart does not rewrite the intent in its stop half'
    $restartMarker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$restartMarker.state) 'running' 'restart ends with the running intent'
    Assert-Equal ([string]$restartMarker.start_outcome) 'succeeded' 'a failed restart start preserves this boot''s confirmed start (the budget governs recovery)'

    # Old v1 marker (pre-Rev13-W): the reader still SURFACES it - the watch's
    # round-19 compatibility path reads the intent through this reader, and a
    # "null" here would silently strip the guard (R27-LOAD). What must never
    # happen is treating the marker as an authorization by itself: the missing
    # start_outcome fails closed once the lifecycle is opted in, and the watch's
    # own boot check rejects a previous-boot marker (its self-test covers the
    # decision). A damaged v1 shape (state=maybe, below) still reads as unknown.
    Set-Content -LiteralPath $markerPath -Value ('{{"version":1,"key":"chat2api","state":"running","at_utc":"2026-09-20T10:00:00Z","boot_id":"{0}","source":"console"}}' -f 'legacy-boot') -Encoding UTF8
    $legacyMarker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([int]$legacyMarker.version) 1 'an old v1 marker is surfaced, not dropped as unknown'
    Assert-Equal ([string]$legacyMarker.state) 'running' 'the v1 intent reaches the compatibility gate'
    Assert-Equal ([string]$legacyMarker.boot_id) 'legacy-boot' 'the v1 boot identity is preserved for the boot check'
    $legacyOutcome = $null
    if ($null -ne $legacyMarker) { $legacyOutcome = $legacyMarker.PSObject.Properties['start_outcome'] }
    Assert-Null $legacyOutcome 'a v1 marker carries no start_outcome (fail closed once opted in)'

    # A marker from an unknown FUTURE version is never guessed at: the compat
    # gate covers exactly the shapes production has written (v1, v2).
    Set-Content -LiteralPath $markerPath -Value '{"version":3,"key":"chat2api","state":"running","at_utc":"2026-09-20T10:00:00Z","boot_id":"legacy-boot"}' -Encoding UTF8
    Assert-Null (Read-GatewayDesiredState -Key 'chat2api') 'an unknown future marker version reads as unknown'

    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = 'Not A Token'
    Assert-Equal (Get-GatewayActionSource) 'cli' 'an invalid action source falls back to cli'

    # R1/D1 返修（2026-10-11）：人工停止必须写下可持久读取的意图，否则定时/
    # 手动全源路径会在 06:00 把它重新拉起并读上游。chat2api 是唯一带
    # DesiredStateTracked 的服务：停止写下 stopped 意图，守护任务名来自描述符。
    [void](Stop-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } -Stopper $noopStop -Sleeper { param($s) })
    $chat2apiMarker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$chat2apiMarker.state) 'stopped' 'a deliberate stop of chat2api writes the stopped intent'
    Assert-Equal ([string](Get-GatewayWatchTaskName -Key 'chat2api')) 'SelfUseAI AI Gateway Chat2API Watch' 'chat2api names its watch task (Rev13-W)'
    Assert-Equal (@(Get-GatewayManuallyStoppedKeys -Keys @('chat2api')) -join ',') 'chat2api' 'the stopped intents are exactly the manually stopped keys'
    Remove-Item -LiteralPath (Get-GatewayDesiredStatePath -Key 'chat2api') -Force -ErrorAction SilentlyContinue

    # A malformed marker reads as "intent unknown", never as running.
    Set-Content -LiteralPath $markerPath -Value '{"version":1,"key":"chat2api","state":"maybe"}' -Encoding UTF8
    Assert-Null (Read-GatewayDesiredState -Key 'chat2api') 'a malformed marker reads as unknown'
} finally {
    $script:GatewayDesiredStateEnabled = $false
    $env:SELFUSEAI_GATEWAY_WATCH_DIR = $savedWatchDir
    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = $savedSource
    Remove-Item -LiteralPath $markerRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# R1/D1 (2026-10-11): the scheduled/manual full-source sync must obey the
# same "an explicit stop cancels automatic upstream reads" contract as the
# console lane. Get-GatewayManuallyStoppedKeys is the single decision point
# the sync script uses to drop manually stopped sources (never started, never
# read upstream). Intent UNKNOWN (marker absent/malformed/foreign) is NOT an
# explicit stop: unknown must never masquerade as stopped, so an abnormally
# exited source keeps its original start/recover contract.
# ---------------------------------------------------------------------------
$syncMarkerRoot = Join-Path ([IO.Path]::GetTempPath()) ('gateway-sync-stopped-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$savedSyncWatchDir = $env:SELFUSEAI_GATEWAY_WATCH_DIR
$env:SELFUSEAI_GATEWAY_WATCH_DIR = $syncMarkerRoot
try {
    [IO.Directory]::CreateDirectory($syncMarkerRoot) | Out-Null
    $syncBootId = Get-GatewayBootIdentity
    Set-Content -LiteralPath (Join-Path $syncMarkerRoot 'chat2api-desired.json') -Value ('{{"version":2,"key":"chat2api","state":"stopped","at_utc":"2026-10-11T00:00:00Z","boot_id":"{0}","source":"console","start_outcome":"never"}}' -f $syncBootId) -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $syncMarkerRoot 'usage-desired.json') -Value ('{{"version":2,"key":"usage","state":"running","at_utc":"2026-10-11T00:00:00Z","boot_id":"{0}","source":"console","start_outcome":"succeeded"}}' -f $syncBootId) -Encoding UTF8
    $syncStopped = @(Get-GatewayManuallyStoppedKeys -Keys @('chat2api', 'usage'))
    Assert-Equal ($syncStopped -join ',') 'chat2api' 'only an explicit stopped intent is dropped from the scheduled sync sources'
    Assert-Equal (@(Get-GatewayManuallyStoppedKeys -Keys @('usage')) -join ',') '' 'a running intent is never dropped'
    Assert-Equal (@(Get-GatewayManuallyStoppedKeys -Keys @('usage-absent')) -join ',') '' 'an absent marker is unknown, not an explicit stop'
} finally {
    $env:SELFUSEAI_GATEWAY_WATCH_DIR = $savedSyncWatchDir
    Remove-Item -LiteralPath $syncMarkerRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Rev13-W (2026-10-04): opening intent + recovery lifecycle for the private
# 8000 instance (maintainer-approved option A).
#
# The lifecycle is INERT until the deployment window writes the control
# marker: before it, start/stop behave exactly as round 19 and never touch
# the scheduled task. After it: a start publishes the intent, arms the entry
# and records the outcome; a deliberate stop commits the intent then releases
# the entry on the operator path; a controlled restart keeps both; a failed
# start releases only when this boot has no preserved confirmed start; an
# abnormal exit changes nothing. The ScheduledTasks boundary is replaced by
# the recorded hook below - no real task is ever queried or toggled here.
# ---------------------------------------------------------------------------

$script:lifecycleTaskCalls = New-Object System.Collections.ArrayList
$script:lifecycleTaskFails = $false
$script:GatewayWatchSchedulerHook = {
    param([string]$TaskName, [bool]$Enabled)
    if ($script:lifecycleTaskFails) { throw 'offline substitute: scheduler unavailable' }
    [void]$script:lifecycleTaskCalls.Add(@{ TaskName = $TaskName; Enabled = $Enabled })
    return [pscustomobject]@{
        Key = 'chat2api'; TaskName = $TaskName
        Registered = $true; SchedulingEnabled = $Enabled; ControlEnabled = $true
        Action = if ($Enabled) { 'Enable' } else { 'Disable' }; Detail = ''
    }
}

$lifecycleRoot = Join-Path ([IO.Path]::GetTempPath()) ('gateway-lifecycle-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$savedLifecycleWatchDir = $env:SELFUSEAI_GATEWAY_WATCH_DIR
$savedLifecycleSource = $env:SELFUSEAI_GATEWAY_ACTION_SOURCE
$env:SELFUSEAI_GATEWAY_WATCH_DIR = $lifecycleRoot
Remove-Item Env:\SELFUSEAI_GATEWAY_ACTION_SOURCE -ErrorAction SilentlyContinue
$lifecycleMarkerPath = Get-GatewayDesiredStatePath -Key 'chat2api'
try {
    Enable-GatewayDesiredState

    # --- Gate OFF (shipped but not yet deployed): zero scheduled-task calls. ---
    $script:lifecycleTaskCalls.Clear()
    $result = Start-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $true } -Launcher $noopLaunch -Sleeper { param($s) }
    Assert-Equal $result.Status 'AlreadyRunning' 'lifecycle: start still succeeds with the gate off'
    Assert-Equal $script:lifecycleTaskCalls.Count 0 'gate off: start never touches the scheduled task'
    Assert-Equal ([bool]$result.Watch.ControlEnabled) $false 'gate off: the watch report names the control as disabled'
    Assert-Null $result.Watch.SchedulingEnabled 'gate off: scheduling state is unknown, never claimed enabled'
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.start_outcome) 'succeeded' 'gate off: a confirmed healthy start still records start_outcome'

    $script:lifecycleTaskCalls.Clear()
    [void](Stop-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } -Stopper $noopStop -Sleeper { param($s) })
    Assert-Equal $script:lifecycleTaskCalls.Count 0 'gate off: stop never touches the scheduled task'
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.state) 'stopped' 'gate off: stop still records the intent'

    # --- Gate ON: arm on start, release on a deliberate stop. ---
    [void](Enable-GatewayWatchLifecycleControl)
    Assert-Equal (Test-GatewayWatchLifecycleControl) $true 'the control marker enables the lifecycle gate'

    # A never-confirmed start: outcome=failed AND the entry released - an
    # unconfirmed start must not keep a once-a-minute probing task alive.
    Remove-Item -LiteralPath $lifecycleMarkerPath -Force -ErrorAction SilentlyContinue
    $script:lifecycleTaskCalls.Clear()
    $result = Start-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $false } -Launcher $noopLaunch -Sleeper { param($s) }
    Assert-Equal $result.Status 'StartPending' 'lifecycle: an unconfirmed start reports StartPending'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { $_.Enabled }).Count) 1 'gate on: start arms the watch entry once'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { -not $_.Enabled }).Count) 1 'gate on: an unconfirmed first start releases the entry (nothing to recover)'
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.start_outcome) 'failed' 'an unconfirmed first start is recorded as failed, never as succeeded'

    # A launched, health-confirmed start: armed, outcome succeeded.
    $script:lifecycleTaskCalls.Clear()
    $lifecycleHealth = [System.Collections.Generic.Queue[bool]]::new()
    $lifecycleHealth.Enqueue($false)  # pre-launch: not healthy
    $lifecycleHealth.Enqueue($true)   # post-launch: healthy
    $result = Start-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $lifecycleHealth.Dequeue() } `
        -Launcher $noopLaunch -Sleeper { param($s) }
    Assert-Equal $result.Status 'Started' 'lifecycle: a launched start reports Started'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { $_.Enabled }).Count) 1 'gate on: a real start arms the entry exactly once'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { -not $_.Enabled }).Count) 0 'gate on: a confirmed start never releases the entry'
    Assert-Equal ([string](Read-GatewayDesiredState -Key 'chat2api').start_outcome) 'succeeded' 'a launched confirmed start records succeeded'

    # Deliberate stop: the intent is committed first, the entry released on
    # the operator path (never inside the watch's own processing).
    $script:lifecycleTaskCalls.Clear()
    $result = Stop-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $false } -Stopper $noopStop -Sleeper { param($s) }
    Assert-Equal $result.Status 'Stopped' 'lifecycle: deliberate stop reports Stopped'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { -not $_.Enabled }).Count) 1 'gate on: a deliberate stop releases the entry'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { $_.Enabled }).Count) 0 'gate on: a stop never arms the entry'
    $marker = Read-GatewayDesiredState -Key 'chat2api'
    Assert-Equal ([string]$marker.state) 'stopped' 'deliberate stop keeps the stopped intent'
    Assert-Equal ([string]$marker.start_outcome) 'succeeded' 'a same-boot confirmed start survives the stop record'

    # A cleanup/restart stop touches neither the intent nor the scheduling.
    $script:lifecycleTaskCalls.Clear()
    [void](Stop-GatewayService -Key 'chat2api' -HttpChecker { param($u, $t) $false } `
        -Stopper $noopStop -Sleeper { param($s) } -KeepDesiredState)
    Assert-Equal $script:lifecycleTaskCalls.Count 0 'a KeepDesiredState stop never touches the scheduling'

    # Controlled restart: the stop half releases nothing; the start half arms.
    $script:lifecycleTaskCalls.Clear()
    $restartHealth = [System.Collections.Generic.Queue[bool]]::new()
    $restartHealth.Enqueue($false)  # stop: was unhealthy (StopWhenUnhealthy proceeds)
    $restartHealth.Enqueue($false)  # stop post-check: stopped
    $restartHealth.Enqueue($false)  # start pre-check: not already running
    $restartHealth.Enqueue($true)   # start post-launch: healthy
    $result = Restart-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $restartHealth.Dequeue() } `
        -Launcher $noopLaunch -Stopper $noopStop -Sleeper { param($s) }
    Assert-Equal $result.Status 'Restarted' 'lifecycle: a controlled restart reports Restarted'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { $_.Enabled }).Count) 1 'restart: the start half arms the entry once'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { -not $_.Enabled }).Count) 0 'restart: the stop half never releases the entry (not mistaken for a deliberate stop)'
    Assert-Equal ([string](Read-GatewayDesiredState -Key 'chat2api').start_outcome) 'succeeded' 'restart confirms the start outcome'

    # A failed LATER start keeps the entry: this boot's confirmed start still
    # authorizes recovery; the hourly budget governs, never the outcome flag.
    $script:lifecycleTaskCalls.Clear()
    $result = Start-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $false } -Launcher $noopLaunch -Sleeper { param($s) }
    Assert-Equal $result.Status 'StartPending' 'lifecycle: a failed later start reports StartPending'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { $_.Enabled }).Count) 1 'a failed later start re-arms the entry'
    Assert-Equal (@($script:lifecycleTaskCalls | Where-Object { -not $_.Enabled }).Count) 0 'a failed later start keeps the entry (recovery stays authorized)'
    Assert-Equal ([string](Read-GatewayDesiredState -Key 'chat2api').start_outcome) 'succeeded' 'a failed later start preserves the confirmed start'

    # Scheduler failure: the start itself still succeeds and the failure is
    # named - the console can show "running but unwatched" honestly.
    $script:lifecycleTaskCalls.Clear()
    $script:lifecycleTaskFails = $true
    $result = Start-GatewayService -Key 'chat2api' `
        -HttpChecker { param($u, $t) $true } -Launcher $noopLaunch -Sleeper { param($s) }
    $script:lifecycleTaskFails = $false
    Assert-Equal $result.Status 'AlreadyRunning' 'a scheduler failure does not fail the start'
    Assert-Null $result.Watch.SchedulingEnabled 'a scheduler failure reports unknown scheduling, never enabled'
    Assert-Equal ($result.Watch.Detail -match '守护调度切换失败') $true 'a scheduler failure is named in the watch report'
    Assert-Equal ([string](Read-GatewayDesiredState -Key 'chat2api').start_outcome) 'succeeded' 'the start outcome is recorded despite the scheduler failure'

    # --- Registration, scheduling, execution, service: separate facts. ---
    $status = Get-GatewayWatchLifecycleStatus -Key 'chat2api' `
        -TaskStateProvider { param($taskName) [pscustomobject]@{ Registered = $true; State = 'Ready' } } `
        -HttpChecker { param($u, $t) $true }
    Assert-Equal ([bool]$status.Registered) $true 'status: registration is its own fact'
    Assert-Equal ([bool]$status.SchedulingEnabled) $true 'status: scheduling is its own fact'
    Assert-Equal ([bool]$status.Executing) $false 'status: current execution is its own fact (Ready is not running)'
    Assert-Equal ([bool]$status.ServiceRunning) $true 'status: the service state is its own fact'
    Assert-Equal ([bool]$status.ControlEnabled) $true 'status: the control flag is its own fact'
    Assert-Equal ([string]$status.TaskName) 'SelfUseAI AI Gateway Chat2API Watch' 'status names the watch task'

    $status = Get-GatewayWatchLifecycleStatus -Key 'chat2api' `
        -TaskStateProvider { param($taskName) [pscustomobject]@{ Registered = $true; State = 'Disabled' } } `
        -HttpChecker { param($u, $t) $true }
    Assert-Equal ($status.Detail -match '运行但无守护') $true 'status names running-but-unwatched explicitly'

    $status = Get-GatewayWatchLifecycleStatus -Key 'chat2api' `
        -TaskStateProvider { param($taskName) [pscustomobject]@{ Registered = $true; State = 'Running' } } `
        -HttpChecker { param($u, $t) $false }
    Assert-Equal ([bool]$status.Executing) $true 'status: a running task execution is visible separately'

    $status = Get-GatewayWatchLifecycleStatus -Key 'chat2api' `
        -TaskStateProvider { param($taskName) [pscustomobject]@{ Registered = $false; State = $null } } `
        -HttpChecker { param($u, $t) $true }
    Assert-Equal ([bool]$status.Registered) $false 'status: absent registration is its own fact'
    Assert-Equal ($status.Detail -match '未注册') $true 'status names a missing registration explicitly'

    # --- The register script: substituted cmdlets only, real exit codes. ---
    #
    # The child harness imports the real ScheduledTasks module FIRST and only
    # then defines a recording global substitute for EVERY scheduler modification
    # boundary. A substitute defined before the import (or only in this script's
    # scope) is displaced by the module's own functions: the run then really
    # re-registered the PRODUCTION task and moved its trigger. Reproduced
    # 2026-10-05 (R27-LOAD): every suite run moved the real StartBoundary
    # and logged a TaskScheduler event 140, while the local substitute was
    # never called.
    # The harness verifies the binding itself BEFORE the entry point runs
    # (Get-Command identity through the record marker) and refuses a missing or
    # displaced substitute, and the fixture injects refusals at the recording
    # substitutes - which have no real scheduling write capability. The parent
    # asserts the record file, the exit codes, and that the production task's
    # FULL definition did not move (the negative control).
    $registerScriptPath = Join-Path $root 'lib\register-chat2api-watch.ps1'
    $harnessPath = Join-Path $env:TEMP ('register-harness-' + [guid]::NewGuid().ToString('N') + '.ps1')
    @'
param(
    [string]$Action,
    [string]$RegisterScript,
    [string]$WatchDir,
    [string]$TaskName,
    [string]$RecordPath,
    [string]$RefuseBoundary,
    [switch]$DisplaceSubstitutes
)
$ErrorActionPreference = 'Stop'
$env:SELFUSEAI_GATEWAY_WATCH_DIR = $WatchDir
# The real module is imported FIRST: once ScheduledTasks is loaded, a global
# function defined below shadows its same-named function for every caller
# (verified 2026-10-05). With the reverse order the module's own functions win
# and the run really re-registers the PRODUCTION task (R27-LOAD, reproduced
# 2026-10-05: every suite run moved the real StartBoundary and logged a
# TaskScheduler event 140 while the local substitute was never called).
Import-Module ScheduledTasks -ErrorAction Stop
function Record-SubstituteCall {
    param([string]$Name)
    # Appends to the record file: no scope-crossing variable is involved
    # (a `$script:` list defined here is not visible from the dot-sourced
    # register script on PowerShell 5.1). The directory is created here because
    # the register script creates it only later, with the control marker.
    $dir = Split-Path -Parent $RecordPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Add-Content -LiteralPath $RecordPath -Value $Name -Encoding UTF8
    if ($RefuseBoundary -and $Name -eq $RefuseBoundary) {
        # Fault injection: record the refusal at THIS substitute, then refuse.
        # The substitute has no real scheduling write capability, so a refusal
        # here can never reach the production task.
        Add-Content -LiteralPath $RecordPath -Value ('REFUSE:' + $Name) -Encoding UTF8
        throw ('offline substitute refuses the scheduler boundary: ' + $Name)
    }
}
function global:New-ScheduledTaskAction {
    [CmdletBinding()]
    param([string]$Execute, [string]$Argument)
    Record-SubstituteCall 'New-ScheduledTaskAction'
    return [pscustomobject]@{ Kind = 'Action'; Execute = $Execute; Argument = $Argument }
}
function global:New-ScheduledTaskTrigger {
    [CmdletBinding()]
    param([switch]$Once, $At, $RepetitionInterval)
    Record-SubstituteCall 'New-ScheduledTaskTrigger'
    return [pscustomobject]@{ Kind = 'Trigger' }
}
function global:New-ScheduledTaskPrincipal {
    [CmdletBinding()]
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    Record-SubstituteCall 'New-ScheduledTaskPrincipal'
    return [pscustomobject]@{ Kind = 'Principal' }
}
function global:New-ScheduledTaskSettingsSet {
    [CmdletBinding()]
    param([string]$MultipleInstances, $ExecutionTimeLimit,
          [switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries,
          [switch]$DontStopIfGoingOnBatteries)
    Record-SubstituteCall 'New-ScheduledTaskSettingsSet'
    return [pscustomobject]@{ Kind = 'Settings' }
}
function global:New-ScheduledTask {
    [CmdletBinding()]
    param($Action, $Trigger, $Principal, $Settings, [string]$Description)
    Record-SubstituteCall 'New-ScheduledTask'
    return [pscustomobject]@{ Kind = 'Task' }
}
function global:Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName)
    Record-SubstituteCall 'Get-ScheduledTask'
    # A placeholder "existing task" so the Unregister path reaches its
    # substituted removal (the register path never queries at all).
    return [pscustomobject]@{ TaskName = $TaskName }
}
function global:Register-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, $InputObject, [switch]$Force)
    Record-SubstituteCall 'Register-ScheduledTask'
}
function global:Unregister-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [switch]$Confirm)
    Record-SubstituteCall 'Unregister-ScheduledTask'
}
function global:Set-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, $InputObject)
    Record-SubstituteCall 'Set-ScheduledTask'
}
function global:Enable-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName)
    Record-SubstituteCall 'Enable-ScheduledTask'
    return [pscustomobject]@{ Kind = 'Enable' }
}
function global:Disable-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName)
    Record-SubstituteCall 'Disable-ScheduledTask'
    return [pscustomobject]@{ Kind = 'Disable' }
}

# Guard regression switch: re-import the module so its own functions displace
# the global substitutes - the exact R27-LOAD binding mistake. The pre-flight
# check below must refuse this run BEFORE the entry point.
if ($DisplaceSubstitutes) {
    Remove-Module ScheduledTasks -Force -ErrorAction SilentlyContinue
    Import-Module ScheduledTasks -ErrorAction Stop
}

# Pre-flight, BEFORE the entry point: every scheduler modification boundary
# must be a substitute in THIS process. Get-Command is the identity proof - a
# ScheduledTasks module function also reports CommandType=Function, but its
# definition does not carry the record marker. A missing or displaced substitute
# stops the run here, before the register script can reach a real boundary.
$schedulerBoundaries = @(
    'Get-ScheduledTask', 'Register-ScheduledTask', 'Unregister-ScheduledTask',
    'New-ScheduledTaskAction', 'New-ScheduledTaskTrigger', 'New-ScheduledTaskPrincipal',
    'New-ScheduledTaskSettingsSet', 'New-ScheduledTask', 'Set-ScheduledTask',
    'Enable-ScheduledTask', 'Disable-ScheduledTask')
foreach ($boundaryName in $schedulerBoundaries) {
    $command = Get-Command -Name $boundaryName -ErrorAction SilentlyContinue
    if ($null -eq $command -or $command.CommandType -ne 'Function' -or
            [string]$command.Definition -notmatch 'Record-SubstituteCall') {
        $observed = 'not found'
        if ($null -ne $command) {
            $observed = '{0} (module {1})' -f $command.CommandType, $command.ModuleName
        }
        throw ('scheduler boundary is not substituted before the entry point: {0} -> {1}' -f
            $boundaryName, $observed)
    }
}

& $RegisterScript -Action $Action -TaskName $TaskName
'@ | Set-Content -LiteralPath $harnessPath -Encoding UTF8
    $registerRoot = Join-Path $lifecycleRoot 'register-check'
    function Get-RegisterRecordPath {
        param([Parameter(Mandatory = $true)][string]$CaseName)
        return (Join-Path $registerRoot ('substitute-calls-' + $CaseName + '.txt'))
    }
    function Get-WatchTaskDefinition {
        # Full exported XML of the production task - the negative control reads
        # the WHOLE definition, not only the trigger: an Action or enabled-state
        # change would otherwise go unnoticed. A failed collection is NEVER
        # recorded as absence (a SilentlyContinue miss proves nothing): the task
        # is '<absent>' only after a positively identified ObjectNotFound, and
        # '<unreadable:...>' otherwise, which fails the comparison below.
        param([Parameter(Mandatory = $true)][string]$TaskName)
        try {
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        } catch {
            $category = [string]$_.CategoryInfo.Category
            $errorId = [string]$_.FullyQualifiedErrorId
            if ($category -eq 'ObjectNotFound' -and $errorId -match 'NotFound') {
                return '<absent>'
            }
            return ('<unreadable:{0}>' -f $_.Exception.GetType().Name)
        }
        if ($null -eq $task) {
            # -ErrorAction Stop never returns null for a missing task; a null
            # answer here is unreadable, not evidence of absence.
            return '<unreadable:null-task>'
        }
        try {
            $definition = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        } catch {
            return ('<unreadable:{0}>' -f $_.Exception.GetType().Name)
        }
        if ($null -eq $definition -or -not ([string]$definition).Trim()) {
            return '<unreadable:empty-definition>'
        }
        return [string]$definition
    }
    function Invoke-RegisterHarness {
        # Runs one child harness with the given arguments and returns its exit
        # code plus merged output for the refusal diagnostics.
        param([Parameter(Mandatory = $true)][string[]]$Arguments)
        $output = & pwsh.exe -NoProfile -ExecutionPolicy Bypass -File $harnessPath @Arguments 2>&1
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output | Out-String) }
    }
    # The production task definition must not move while the substitutes run.
    $watchTaskName = 'SelfUseAI AI Gateway Chat2API Watch'
    $taskDefinitionBefore = Get-WatchTaskDefinition -TaskName $watchTaskName
    # A failing child's stderr becomes a terminating NativeCommandError under
    # the file's Stop preference; the mismatch case below EXPECTS failure, so
    # relax the preference around these native invocations only.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $registerRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Register', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', $watchTaskName,
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'register'))
        Assert-Equal $registerRun.ExitCode 0 'register substitute run propagates exit code 0'
        Assert-Equal (Test-Path -LiteralPath (Join-Path $registerRoot 'chat2api-watch-lifecycle-control.enabled')) $true 'register writes the lifecycle control marker'
        $registerRecord = Get-Content -LiteralPath (Get-RegisterRecordPath -CaseName 'register') -Raw -Encoding utf8
        Assert-Equal ($registerRecord -match 'New-ScheduledTaskAction') $true 'register substitutes the ScheduledTasks boundary'
        Assert-Equal ($registerRecord -match 'Register-ScheduledTask') $true 'register reaches the substituted registration'
        Assert-Equal ($registerRecord -match 'Get-ScheduledTask') $false 'the register path never queries the real scheduler'

        $unregisterRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Unregister', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', $watchTaskName,
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'unregister'))
        Assert-Equal $unregisterRun.ExitCode 0 'unregister substitute run propagates exit code 0'
        Assert-Equal (Test-Path -LiteralPath (Join-Path $registerRoot 'chat2api-watch-lifecycle-control.enabled')) $false 'unregister removes the lifecycle control marker'
        $unregisterRecord = Get-Content -LiteralPath (Get-RegisterRecordPath -CaseName 'unregister') -Raw -Encoding utf8
        Assert-Equal ($unregisterRecord -match 'Unregister-ScheduledTask') $true 'unregister reaches the substituted removal'

        $mismatchRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Register', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', 'Wrong Watch Name',
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'mismatch'))
        Assert-Equal ($mismatchRun.ExitCode -ne 0) $true 'a task-name mismatch refuses registration with a non-zero exit'
        Assert-Equal (Test-Path -LiteralPath (Get-RegisterRecordPath -CaseName 'mismatch')) $false 'a task-name mismatch refuses before any scheduler boundary call'

        # Fault injection on the WRITE boundary: the substitute records the
        # call and then refuses it. The refusal is proven to happen AT the
        # substitute (no real scheduling write capability exists there).
        $refuseRegisterRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Register', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', $watchTaskName,
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'refuse-register'),
            '-RefuseBoundary', 'Register-ScheduledTask')
        Assert-Equal ($refuseRegisterRun.ExitCode -ne 0) $true 'a refused registration boundary aborts the child with a non-zero exit'
        $refuseRegisterRecord = Get-Content -LiteralPath (Get-RegisterRecordPath -CaseName 'refuse-register') -Raw -Encoding utf8
        Assert-Equal ($refuseRegisterRecord -match 'REFUSE:Register-ScheduledTask') $true 'the refusal is recorded at the substitute boundary (no real scheduling write)'
        Assert-Equal ($refuseRegisterRecord -match 'New-ScheduledTaskAction') $true 'the run reached the substitute chain before the write boundary'

        # Fault injection on the FIRST boundary: the refusal happens before
        # anything can reach the write boundary.
        $refuseActionRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Register', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', $watchTaskName,
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'refuse-action'),
            '-RefuseBoundary', 'New-ScheduledTaskAction')
        Assert-Equal ($refuseActionRun.ExitCode -ne 0) $true 'a refused first boundary aborts the child with a non-zero exit'
        $refuseActionRecord = Get-Content -LiteralPath (Get-RegisterRecordPath -CaseName 'refuse-action') -Raw -Encoding utf8
        Assert-Equal ($refuseActionRecord -match 'REFUSE:New-ScheduledTaskAction') $true 'the early refusal is recorded at the substitute boundary'
        Assert-Equal ($refuseActionRecord -notmatch 'Register-ScheduledTask') $true 'the early refusal happens before the substituted write boundary'

        # Guard regression (R27-LOAD): when the module displaces the global
        # substitutes, the child must refuse BEFORE the entry point instead of
        # running against the real scheduler.
        $displacedRun = Invoke-RegisterHarness -Arguments @(
            '-Action', 'Register', '-RegisterScript', $registerScriptPath,
            '-WatchDir', $registerRoot, '-TaskName', $watchTaskName,
            '-RecordPath', (Get-RegisterRecordPath -CaseName 'displaced'),
            '-DisplaceSubstitutes')
        Assert-Equal ($displacedRun.ExitCode -ne 0) $true 'displaced substitutes refuse the run with a non-zero exit (R27-LOAD guard)'
        Assert-Equal ($displacedRun.Output -match 'scheduler boundary is not substituted') $true 'the guard names the unverified boundary before the entry point'
        Assert-Equal (Test-Path -LiteralPath (Get-RegisterRecordPath -CaseName 'displaced')) $false 'a refused run never reaches the register script: no boundary call was recorded'

        # Negative control for the substitutes themselves: the production task's
        # FULL definition must be identical across all substitute runs above. An
        # unreadable definition fails this comparison instead of passing as
        # "unchanged" or "absent".
        $taskDefinitionAfter = Get-WatchTaskDefinition -TaskName $watchTaskName
        $definitionsReadable = ($taskDefinitionBefore -notlike '<unreadable:*>') -and
            ($taskDefinitionAfter -notlike '<unreadable:*>')
        Assert-Equal $definitionsReadable $true '任务定义不可读（未知），不能证明未变：生产任务定义前后均可完整采集'
        Assert-Equal ($definitionsReadable -and ($taskDefinitionAfter -ceq $taskDefinitionBefore)) $true 'the substitute runs never change the real production task definition (full XML, not only the trigger)'
    } finally {
        $ErrorActionPreference = $savedPreference
        Remove-Item -LiteralPath $harnessPath -Force -ErrorAction SilentlyContinue
    }

    # Source contract: the registration entry enables/withdraws the lifecycle
    # opt-in and validates the task name against the descriptor.
    $registerSource = Get-Content -LiteralPath $registerScriptPath -Raw -Encoding utf8
    Assert-Equal ($registerSource -match 'Enable-GatewayWatchLifecycleControl') $true 'register enables the lifecycle control marker'
    Assert-Equal ($registerSource -match 'Disable-GatewayWatchLifecycleControl') $true 'unregister withdraws the lifecycle control marker'
    Assert-Equal ($registerSource -match 'Get-GatewayWatchTaskName') $true 'register validates its task name against the descriptor'
} finally {
    $script:GatewayWatchSchedulerHook = $null
    $script:GatewayDesiredStateEnabled = $false
    $env:SELFUSEAI_GATEWAY_WATCH_DIR = $savedLifecycleWatchDir
    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = $savedLifecycleSource
    Remove-Item -LiteralPath $lifecycleRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# The watch's own decision table (desired state, identity, graceful-exit
# guard, hourly budget) runs as its built-in self-test, fully stubbed.
$watchScript = Join-Path $root 'lib\watch-chat2api.ps1'
# Rev13-W compatibility: the start-outcome gate is opt-in too - it must consult
# the lifecycle control marker, otherwise an old v1 marker would silently strip
# the round-19 recovery (isolated fixtures in -SelfTest cover the behaviour).
# The regex is scoped to the function body: an unscoped (?s) match would also hit
# the same call in Invoke-WatchSchedulingRelease and pass with the gate deleted.
$watchSource = Get-Content -LiteralPath $watchScript -Raw -Encoding utf8
$startOutcomeBody = [regex]::Match($watchSource,
    '(?s)function Test-StartOutcomeRecoverable\b(.*?)\r?\n\}').Groups[1].Value
Assert-Equal ($startOutcomeBody.Length -gt 0) $true 'the start-outcome gate body was located'
Assert-Equal ($startOutcomeBody -match 'Test-GatewayWatchLifecycleControl') $true 'the start-outcome gate consults the lifecycle control marker (round-19 compatibility before the opt-in)'
$watchOutput = & pwsh.exe -NoProfile -ExecutionPolicy Bypass -File $watchScript -SelfTest 2>&1
Assert-Equal $LASTEXITCODE 0 ('watch-chat2api self-test passes: ' + (($watchOutput | Select-Object -Last 1) -join ''))
$observerOutput = & pwsh.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tests\chat2api-exit-observer.Tests.ps1') 2>&1
Assert-Equal $LASTEXITCODE 0 ('chat2api exit observer self-test passes: ' + (($observerOutput | Select-Object -Last 1) -join ''))

# ---------------------------------------------------------------------------
# P3 返修（2026-10-11）：空来源集合必须经真实 pwsh.exe -> Python
# native 参数边界正常返回。`--sources=` 单参数写法在 5.1 与 7 上都成立，
# 所以这个回归在两种宿主下都证明同一条边界契约（见 gateway-runtime.ps1）。
# 这里用真实 pwsh.exe + 真实 python 打印 sys.argv 证明。
# ---------------------------------------------------------------------------
$boundaryDir = Join-Path ([IO.Path]::GetTempPath()) ('gateway-argv-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
[IO.Directory]::CreateDirectory($boundaryDir) | Out-Null
try {
    $probe = Join-Path $boundaryDir 'argv-probe.py'
    Set-Content -LiteralPath $probe -Encoding UTF8 -Value @(
        'import json, sys',
        'print(json.dumps(sys.argv[1:]))'
    )
    $emptyOut = & pwsh.exe -NoProfile -Command ("& python -X utf8 '" + $probe + "' --sources= --apply") 2>&1
    $emptyArgs = ($emptyOut | Select-Object -Last 1) | ConvertFrom-Json
    Assert-Equal (($emptyArgs | Where-Object { $_ -like '--sources=*' }).Count) 1 'an empty --sources= survives the PS 5.1 native boundary as one argument'
    Assert-Equal (($emptyArgs | Where-Object { $_ -eq '--apply' }).Count) 1 'the trailing switch survives the boundary as well'
    $subsetOut = & pwsh.exe -NoProfile -Command ("& python -X utf8 '" + $probe + "' --sources=um --apply") 2>&1
    $subsetArgs = ($subsetOut | Select-Object -Last 1) | ConvertFrom-Json
    Assert-Equal (($subsetArgs | Where-Object { $_ -eq '--sources=um' }).Count) 1 'a non-empty subset is passed through unchanged'
} finally {
    Remove-Item -LiteralPath $boundaryDir -Recurse -Force -ErrorAction SilentlyContinue
}

$script:GatewayServiceDescriptors['chat2api'].WorkerPortFile = $script:originalWorkerPortFile
Remove-Item -LiteralPath $script:workerPortRoot -Recurse -Force -ErrorAction SilentlyContinue

if ($script:failures -gt 0) {
    Write-Host "`n$($script:failures) test(s) FAILED." -ForegroundColor Red
    exit 1
}

Write-Host "`nAll gateway-runtime tests passed." -ForegroundColor Green
