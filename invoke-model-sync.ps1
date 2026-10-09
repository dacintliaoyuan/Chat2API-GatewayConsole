[CmdletBinding()]
param(
    [ValidateSet('Preview', 'Manual', 'Scheduled', 'Probe')]
    [string]$Mode = 'Manual'
)

$ErrorActionPreference = 'Stop'
$gatewayRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $gatewayRoot 'lib\gateway-runtime.ps1')

foreach ($entry in Get-GatewayProxyEnvironment) {
    Set-Item -LiteralPath "Env:$($entry.Name)" -Value $entry.Value
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

if ($Mode -eq 'Scheduled') {
    $now = Get-Date
    if ($now.Hour -ne 6 -or $now.Minute -gt 10) {
        Write-Host '[跳过] 已错过 06:00 执行窗口。'
        exit 0
    }
}

$sourceKeys = @('chat2api')
$startedHere = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

try {
    foreach ($key in $sourceKeys) {
        $desc = (Get-GatewayServiceDescriptors)[$key]
        $inspection = Get-GatewayServiceInspection -Key $key
        if ($inspection.State -eq 'Conflict') {
            $warnings.Add("$($desc.DisplayName)：端口冲突")
            continue
        }
        if ($inspection.State -ne 'Running') {
            Write-Host "[准备] 启动 $($desc.DisplayName)..."
            try {
                $start = Start-GatewayService -Key $key
                if ($start.Status -eq 'Started') {
                    $startedHere.Add($key)
                } elseif ($start.Status -notin @('AlreadyRunning', 'Started')) {
                    $warnings.Add("$($desc.DisplayName)：启动未就绪")
                }
            } catch {
                $warnings.Add("$($desc.DisplayName)：启动失败")
            }
        }
    }

    $arguments = @((Join-Path $gatewayRoot 'model_sync.py'))
    # Scheduled 的明确内部语义（Rev8 契约 3）：启用面全量同步、零模型请求
    # （不传 --probe；探测等待集中批准，见下方 pendingProbeCount 提示）。
    if ($Mode -ne 'Preview') { $arguments += '--apply' }
    if ($Mode -eq 'Probe') { $arguments += '--probe' }
    if ($Mode -eq 'Scheduled') { $arguments += '--scheduled' }
    & python @arguments
    $syncExit = $LASTEXITCODE

    $resultName = if ($Mode -eq 'Preview') { 'model-sync-last-preview.json' } else { 'model-sync-last-result.json' }
    $resultPath = Join-Path $gatewayRoot "data\$resultName"
    $result = if (Test-Path -LiteralPath $resultPath) {
        Get-Content -LiteralPath $resultPath -Raw -Encoding utf8 | ConvertFrom-Json
    } else { $null }

    # 2026-09-22 contract: apply runs never auto-probe. New models and changed
    # declarations land in the pending-probe queue and wait for centralized
    # approval (console lane or an explicit -Mode Probe invocation).
    $pendingProbeCount = 0
    $statePath = Join-Path $gatewayRoot 'data\model-sync-state.json'
    if (Test-Path -LiteralPath $statePath) {
        try {
            $probeState = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
            $pendingProbeCount = @($probeState.probes.PSObject.Properties |
                Where-Object {
                    $_.Value.status -eq 'pending' -and $_.Value.newModel -eq $true
                }).Count
        } catch {
            $warnings.Add('能力探测状态无法读取')
        }
    }
    if ($pendingProbeCount -gt 0) {
        Write-Host "[待探测] $pendingProbeCount 项新模型等待能力/上下文探测——不自动发请求，需在控制台集中批准或显式运行 -Mode Probe。"
    }

    if ($Mode -eq 'Scheduled' -and $null -ne $result -and
            ($result.status -ne 'normal' -or $warnings.Count -gt 0)) {
        $summary = switch ($result.status) {
            'changed' { "已应用 $(@($result.applied).Count) 项模型变化" }
            'pending' { "ZCode 运行中，$(@($result.pending).Count) 项等待应用" }
            'failed' { '同步失败，请打开控制台查看' }
            default { "同步状态：$($result.status)" }
        }
        if ($warnings.Count) { $summary += "；" + ($warnings -join '；') }
        & (Join-Path $gatewayRoot 'show-model-sync-notification.ps1') `
            -Title 'AI 网关模型同步' -Message $summary
    }
    exit $syncExit
} catch {
    if ($Mode -eq 'Scheduled') {
        & (Join-Path $gatewayRoot 'show-model-sync-notification.ps1') `
            -Title 'AI 网关模型同步失败' -Message $_.Exception.Message
    }
    Write-Error "模型同步执行失败：$($_.Exception.Message)"
    exit 1
} finally {
    $restoreOrder = @($startedHere)
    [array]::Reverse($restoreOrder)
    foreach ($key in $restoreOrder) {
        $desc = (Get-GatewayServiceDescriptors)[$key]
        Write-Host "[恢复] 停止临时启动的 $($desc.DisplayName)..."
        Stop-GatewayService -Key $key | Out-Null
    }
}
