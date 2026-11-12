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

# B0（2026-11-01）：默认同步装配只剩 UM。Qoder 国内/国际、CLIProxy 与 M365
# 已随源码退役（服务描述符、端点与 token 文件一并删除），不再作为来源键；
# 显式点名退役来源由 model_sync.py 在边界处拒绝，绝不拉起或读取旧上游。
$sourceKeys = @('chat2api')
$startedHere = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

# R1/D1（2026-10-11）：定时/手动全源路径同样遵守「显式停用取消自动上游查询」。
# 人工停用（desired state = stopped）的来源不拉起、也不进本轮同步来源集合，
# 因此不会向上游读目录。意图未知（标记缺席/不可读）不在这里剔除——未知不能
# 伪装成已停止，异常退出仍按原守护契约由启动逻辑恢复。
$manuallyStopped = Get-GatewayManuallyStoppedKeys -Keys $sourceKeys
$syncSources = New-Object System.Collections.Generic.List[string]
foreach ($key in $sourceKeys) {
    if ($key -in $manuallyStopped) {
        $desc = (Get-GatewayServiceDescriptors)[$key]
        Write-Host "[跳过] $($desc.DisplayName)：人工停用，本轮不拉起、不读上游。"
        continue
    }
    # chat2api 是 UM 会话适配的宿主，模型来源名固定为 um（model_sync 的
    # ACTIVE_SOURCES 也只有 um）。
    $syncSources.Add('um')
}

try {
    foreach ($key in $sourceKeys) {
        if ($key -in $manuallyStopped) { continue }
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

    # R1/D1 空集合分支（2026-10-11 返修）：本轮无可同步来源时明确正常返回，
    # 不调用 Python、不读上一次结果（避免把上一轮读数误报成本轮同步）。
    if ($syncSources.Count -eq 0) {
        Write-Host '[跳过] 本轮无可同步来源（人工停用已剔除），不读取任何上游。'
        exit 0
    }

    $arguments = @((Join-Path $gatewayRoot 'model_sync.py'))
    # 同步来源集合：只含本轮应同步的来源（人工停用的已剔除）。空集合已在上面
    # 正常返回，绝不会传成「全部来源」。
    # PS 5.1 的 native 参数编组会丢弃空字符串参数，所以用单参数 `--sources=<值>`
    # 形式跨边界（`--sources=` 与值同在一个参数里，不会被丢掉）。
    $arguments += ('--sources=' + ($syncSources -join ','))
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
