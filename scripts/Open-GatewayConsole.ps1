# Desktop entry for the unified web console (deployed 2026-09-16).
#
# Reuse the installed services, start missing services under their runtime
# locks and exchange a fresh one-time bootstrap code before opening the console.

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

# This entry lives in scripts/; the repository root still owns lib/, data/ and
# the web console.
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $here 'lib\gateway-runtime.ps1')
. (Join-Path $here 'lib\console-runtime-bridge.ps1')

$base = 'http://127.0.0.1:8001'

function Start-MissingConsoleService {
    param([string]$Key, [string]$Label)
    if (Test-GatewayServiceHealth -Key $Key) { return }

    $lock = Enter-ConsoleResourceLock -Resource ("service:$Key") -Owner 'cli-console-entry'
    try {
        # Recheck under the lock in case another entry just started it.
        $inspection = Get-GatewayServiceInspection -Key $Key
        if ($inspection.State -eq 'Running') { return }
        if ($inspection.State -ne 'Stopped') { throw $inspection.Detail }

        Write-Host "[入口] 正在启动 $Label…"
        $result = Start-GatewayService -Key $Key
        if ($result.Status -in @('Started', 'AlreadyRunning')) { return }
        if ($result.Status -ne 'StartPending') {
            $detail = $result.PSObject.Properties['Detail']
            if ($detail -and $detail.Value) { throw $detail.Value }
            throw "启动返回 $($result.Status)"
        }

        # The runtime start can return before the service finishes starting.
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            if (Test-GatewayServiceHealth -Key $Key) { return }
            Start-Sleep -Seconds 2
        }
        throw '服务未在等待上限内就绪'
    } finally {
        Exit-ConsoleResourceLock -Lock $lock
    }
}

try {
    Start-MissingConsoleService -Key 'chat2api' -Label 'UM 网关（8000）'
} catch {
    Write-Warning "UM 网关（8000）启动失败：$($_.Exception.Message)。仍会尝试打开控制台。"
}
try {
    Start-MissingConsoleService -Key 'usage' -Label '控制台（8001）'
} catch {
    Write-Warning "控制台（8001）未就绪：$($_.Exception.Message)"
    exit 1
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
Write-Host ('已打开统一控制台：{0}' -f $base)
exit 0
