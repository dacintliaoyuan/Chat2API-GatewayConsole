[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Disable', 'Repair')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Stop'
$taskName = 'AI Gateway Public Model Sync'
$gatewayRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $gatewayRoot 'invoke-model-sync.ps1'

if ($Action -eq 'Disable') {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) { Write-Host '计划任务尚未安装。'; exit 0 }
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host '已禁用每天 06:00 的模型同步。'
    exit 0
}

if ($Action -in @('Install', 'Repair')) {
    $pwsh = (Get-Command powershell.exe).Source
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$entry`" -Mode Scheduled"
    $taskAction = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments `
        -WorkingDirectory $gatewayRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At '06:00'
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable:$false `
        -WakeToRun:$false -RunOnlyIfNetworkAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $taskAction `
        -Trigger $trigger -Settings $settings -Principal $principal `
        -Description '每天 06:00 镜像 AI 网关模型目录；错过不补跑、不唤醒电脑。' `
        -Force | Out-Null
    Write-Host '已安装/修复模型同步计划任务：每天 06:00，错过不补跑，不唤醒电脑。'
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $task) { Write-Host '状态：未安装'; exit 0 }
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "状态：$($task.State)"
Write-Host "下次运行：$($info.NextRunTime)"
Write-Host "上次结果：$($info.LastTaskResult)"
Write-Host "唤醒电脑：$($task.Settings.WakeToRun)"
Write-Host "错过后补跑：$($task.Settings.StartWhenAvailable)"
