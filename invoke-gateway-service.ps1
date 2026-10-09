[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop')][string]$Action
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

# Shared mutual-exclusion boundary - one acquirer per path: this CLI
# self-holds the resource lock and calls the runtime directly (never the JSON
# wrapper). The console task layer holds the lock itself and passes
# -CallerHoldsLock to the wrapper; a standalone wrapper call without the
# switch takes the lock itself. No path opens the lock file twice.
. (Join-Path $gatewayRoot 'lib\console-runtime-bridge.ps1')

if ($Action -eq 'Start') {
    $lock = Enter-ConsoleResourceLock -Resource ("service:" + $Key) -Owner 'cli-invoker'
    try {
        Start-GatewayService -Key $Key
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
} else {
    $lock = Enter-ConsoleResourceLock -Resource ("service:" + $Key) -Owner 'cli-invoker'
    try {
        $result = Stop-GatewayService -Key $Key
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
    # Round 18 (2026-09-29): a refused stop terminated nothing, so this entry
    # point must not exit 0 as if the service had stopped. Surface the reason
    # and fail instead of a silent no-op.
    if ([string]$result.Status -eq 'StopRefused') {
        [Console]::Error.WriteLine([string]$result.Detail)
        exit 1
    }
}
