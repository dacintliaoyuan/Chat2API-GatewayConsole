# Desktop entry for the unified web console (deployed 2026-09-16).
#
# Usage-entry semantics carried over from the retired CLI menu adapter
# (AI网关控制台.ps1, removed 2026-09-23): ensure the management host is up,
# exchange a fresh one-time bootstrap code, then open the web console.
#
# Single lock-holder rule: this CLI path self-locks and calls
# gateway-runtime directly (Start-GatewayService); it never goes through
# the console wrapper, so the same resource lock is acquired exactly once.
#
# Failure handling: a bounded ready-wait follows the start attempt; a
# failure exits non-zero with an actionable message that names the CLI
# start entry (invoke-gateway-service.ps1) as the recovery path. A running
# host is never restarted, so repeated double-clicks are idempotent.

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'lib\gateway-runtime.ps1')
. (Join-Path $here 'lib\console-runtime-bridge.ps1')

$base = 'http://127.0.0.1:8096'

function Wait-UsageReady {
    # Bounded recheck after a start attempt: the runtime's own health
    # poll may still be settling when Start-GatewayService returns.
    param([int]$Attempts = 5, [int]$IntervalSeconds = 2)
    for ($i = 0; $i -lt $Attempts; $i++) {
        $state = Get-GatewayServiceStatus -Key 'usage'
        if ($state -eq 'Running') { return $true }
        Start-Sleep -Seconds $IntervalSeconds
    }
    return $false
}

# 1) Ensure the management host is up. Start only when it is not already
#    running; a running host is never restarted. This CLI path self-holds
#    the service:usage resource lock around the direct runtime start (same
#    single-acquirer rule as invoke-gateway-service.ps1 — never the
#    wrapper).
$state = Get-GatewayServiceStatus -Key 'usage'
if ($state -eq 'Conflict') {
    throw '8096 belongs to another process or checkout. Stop that instance explicitly or change the public configuration before opening.'
}
if ($state -ne 'Running') {
    Write-Host '[入口] 管理宿主未运行，正在启动（约需数秒）…'
    $lock = Enter-ConsoleResourceLock -Resource 'service:usage' -Owner 'cli-console-entry'
    try {
        $null = Start-GatewayService -Key 'usage'
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
    if (-not (Wait-UsageReady)) {
        Write-Warning '管理宿主（8096）未能就绪，暂不打开控制台。'
        Write-Warning '可稍后重试；应急入口：同目录 invoke-gateway-service.ps1 -Key usage -Action Start。'
        exit 1
    }
}

# 2) One-time bootstrap code: prefer a fresh reissue under the admin
#    control token (the code file is unreadable to the share identity);
#    fall back to the local code file for older service versions.
$code = $null
$tokenFile = Join-Path $here 'data\admin-control-token'
if (Test-Path $tokenFile) {
    try {
        $token = (Get-Content $tokenFile -Raw).Trim()
        $response = Invoke-RestMethod -Method Post `
            -Uri ('{0}/api/session/reissue' -f $base) `
            -Headers @{ 'X-Admin-Control' = $token } `
            -TimeoutSec 5
        if ($response.code) { $code = $response.code }
    } catch {
        Write-Warning '引导码重新申请失败，退回本地码文件。'
    }
}
if (-not $code) {
    $codeFile = Join-Path $here 'data\usage-session-code'
    if (Test-Path $codeFile) {
        $code = Get-Content $codeFile -Raw -ErrorAction SilentlyContinue
    }
}

# 3) Open the web console root. A fresh one-time bootstrap code rides the
#    "#s=" fragment when one was obtained; the code itself never persists
#    anywhere in the browser.
$url = '{0}' -f $base
if ($code) {
    $url = '{0}#s={1}' -f $url, $code.Trim()
}
Start-Process $url
Write-Host ('已打开统一控制台：{0}' -f $url)
exit 0
