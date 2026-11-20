# Desktop entry for the unified web console (deployed 2026-09-16).
#
# Reuse the installed services, recover a verified residual 8001 instance through
# the same readiness boundary as the CLI, and exchange a fresh one-time bootstrap
# code before opening the console.

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

# This entry lives in scripts/; the repository root still owns lib/, data/ and
# the web console.
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $here 'lib\gateway-runtime.ps1')
. (Join-Path $here 'lib\console-runtime-bridge.ps1')

$base = 'http://127.0.0.1:8001'

try {
    $gateway = Invoke-ConsoleServiceReady -Key 'chat2api'
    if ($gateway.Status -notin @('AlreadyRunning', 'Started')) {
        Write-Warning "UM 网关（8000）启动失败：$($gateway.Detail)。仍会尝试打开控制台。"
    }
} catch {
    Write-Warning "UM 网关（8000）启动失败：$($_.Exception.Message)。仍会尝试打开控制台。"
}

try {
    $console = Invoke-ConsoleServiceReady -Key 'usage'
    if ($console.Status -notin @('AlreadyRunning', 'Started')) {
        throw $console.Detail
    }
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

# 3) Open the web console root through the same readiness helper. A fresh
#    one-time bootstrap code rides the "#s=" fragment when one was obtained; the
#    code itself never persists anywhere in the browser.
try {
    [void](Invoke-GatewayConsoleOpen -Key 'usage' -Label '控制台（8001）' `
        -BaseUrl $base -BootstrapCode $code)
    Write-Host ('已打开统一控制台：{0}' -f $base)
    exit 0
} catch {
    Write-Warning "控制台（8001）未就绪：$($_.Exception.Message)"
    exit 1
}
