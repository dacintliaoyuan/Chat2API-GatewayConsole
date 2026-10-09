# invoke-console-runtime-json.ps1 - fixed-parameter JSON entry for the console
# runtime bridge. stdout carries exactly one JSON object; stderr carries
# diagnostics. Exit codes: 0 success, 1 execution failure, 2 invalid arguments.
# All service identity/start/stop authority lives in lib/gateway-runtime.ps1.

param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$Key = '',
    # Resource-lock directory override: tests point BOTH ends (Python task
    # layer and this wrapper) at one temporary namespace so a self-lock would
    # actually surface. Production leaves it empty -> data\console-locks,
    # the same directory the task layer uses.
    [string]$LockDir = '',
    # Declares the caller already owns the resource lock for this key (the
    # console task layer acquires it before invoking start/stop); the bridge
    # then must not open the same lock file again. Without this switch the
    # wrapper acquires the lock itself (standalone/manual invocations).
    [switch]$CallerHoldsLock,
    # TEST-ONLY seam (same pattern as watchdog-umshare -TestMode): a ps1 file
    # defining $ConsoleTestStartHook / $ConsoleTestStopHook scriptblocks so
    # offline tests can drive start/stop against an isolated dummy process.
    # Production callers never pass it; when absent the real runtime runs.
    [string]$RuntimeHooksFile = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if ($Action -notin @('status', 'status-all', 'start', 'stop', 'restart')) {
    [Console]::Out.Write((ConvertTo-Json @{
        ok = $false; error = 'invalid_arguments'; detail = 'unknown action'
    } -Compress -Depth 4))
    exit 2
}
$needsKey = $Action -ne 'status-all'
if ($needsKey -and [string]::IsNullOrWhiteSpace($Key)) {
    [Console]::Out.Write((ConvertTo-Json @{
        ok = $false; error = 'invalid_arguments'; detail = 'a service key is required'
    } -Compress -Depth 4))
    exit 2
}

. (Join-Path $PSScriptRoot 'lib\gateway-runtime.ps1')
. (Join-Path $PSScriptRoot 'lib\console-runtime-bridge.ps1')

# Operator intent (round 19): a real console start/stop moves the desired-state
# marker the private-instance watch obeys. Offline tests (hooks file or their
# own lock namespace) must never touch the real marker.
if ($RuntimeHooksFile -eq '' -and $LockDir -eq '') {
    if (-not $env:AIGATEWAY_PUBLIC_ACTION_SOURCE) {
        $env:AIGATEWAY_PUBLIC_ACTION_SOURCE = 'console'
    }
    Enable-GatewayDesiredState
}

$startHook = $null
$stopHook = $null
if ($RuntimeHooksFile -ne '') {
    if (-not (Test-Path -LiteralPath $RuntimeHooksFile)) {
        [Console]::Out.Write((ConvertTo-Json @{
            ok = $false; error = 'invalid_arguments'; detail = 'runtime hooks file not found'
        } -Compress -Depth 4))
        exit 2
    }
    . (Resolve-Path -LiteralPath $RuntimeHooksFile).Path
    $startHook = $ConsoleTestStartHook
    $stopHook = $ConsoleTestStopHook
}

$payload = $null
try {
    switch ($Action) {
        'status'     { $payload = Get-ConsoleRuntimeSnapshot -Key $Key }
        'status-all' { $payload = Get-ConsoleRuntimeSnapshot }
        'start'      { $payload = Invoke-ConsoleRuntimeStart -Key $Key -LockDir $LockDir -CallerHoldsLock:$CallerHoldsLock -Starter $startHook }
        'stop'       { $payload = Invoke-ConsoleRuntimeStop -Key $Key -LockDir $LockDir -CallerHoldsLock:$CallerHoldsLock -Stopper $stopHook }
        'restart'    { $payload = Invoke-ConsoleRuntimeRestart -Key $Key -LockDir $LockDir -CallerHoldsLock:$CallerHoldsLock -Starter $startHook -Stopper $stopHook }
    }
} catch {
    $json = ConvertTo-Json @{
        ok = $false; error = 'runtime_failure'; detail = $_.Exception.Message
    } -Compress -Depth 4
    [Console]::Out.Write($json)
    exit 1
}

$json = ConvertTo-Json $payload -Compress -Depth 6
[Console]::Out.Write($json)
if ($payload.ok) { exit 0 }
exit 1
