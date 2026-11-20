[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop')][string]$Action,
    # Resource-lock directory override: tests point the lock at an isolated
    # namespace. Production leaves it empty -> data\console-locks.
    [string]$LockDir = '',
    # Short readiness recheck before deciding a residual instance is stale.
    [int]$ReadyWaitSeconds = 5,
    # TEST-ONLY hooks file (same contract as invoke-console-runtime-json.ps1):
    # a ps1 defining $ConsoleTestHealthChecker / $ConsoleTestSleeper and,
    # when needed, boundary substitutes. Production never passes it.
    [string]$RuntimeHooksFile = ''
)

$ErrorActionPreference = 'Stop'
$gatewayRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $gatewayRoot 'lib\gateway-runtime.ps1')

if (-not (Get-GatewayServiceDescriptors).Contains($Key)) {
    throw "Unknown gateway service: $Key"
}

if (Test-GatewayServiceArchived -Key $Key) {
    throw "Gateway service is archived: $Key"
}

# Operator intent (round 19): a CLI start/stop moves the desired-state marker
# that lib\watch-chat2api.ps1 obeys, so a CLI stop is never undone by the watch.
Enable-GatewayDesiredState

foreach ($entry in Get-GatewayProxyEnvironment) {
    Set-Item -LiteralPath "Env:$($entry.Name)" -Value $entry.Value
}

# Shared mutual-exclusion boundary - one acquirer per path. The Start path
# delegates to Invoke-ConsoleServiceReady, which takes the service lock itself;
# the Stop path acquires it here and calls the runtime directly.
. (Join-Path $gatewayRoot 'lib\console-runtime-bridge.ps1')

$httpChecker = $null
$sleeper = $null
if ($RuntimeHooksFile -ne '') {
    if (-not (Test-Path -LiteralPath $RuntimeHooksFile)) {
        [Console]::Error.WriteLine('runtime hooks file not found')
        exit 2
    }
    . (Resolve-Path -LiteralPath $RuntimeHooksFile).Path
    $httpChecker = $ConsoleTestHealthChecker
    $sleeper = $ConsoleTestSleeper
}

$result = Invoke-GatewayServiceCommand -Action $Action -Key $Key `
    -LockDir $LockDir -ReadyWaitSeconds $ReadyWaitSeconds `
    -HttpChecker $httpChecker -Sleeper $sleeper
if (-not $result.ok) {
    [Console]::Error.WriteLine([string]$result.detail)
    exit 1
}
