#Requires -Version 7.0
# Local, operator-invoked recovery only. The default action is read-only.
[CmdletBinding()]
param([ValidateSet('Check', 'Recover')][string]$Action = 'Check', [switch]$ConsoleReloaded)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'lib\share-control-recovery.ps1')

if ($Action -eq 'Recover') {
    Write-Host '恢复分享管理后台：必要时请求 Windows 管理员授权，核对已有任务后恢复后台，并重载 8001 控制台。'
} else {
    Write-Host '只读检查分享管理后台；权限不足会显示未确认。'
}
$result = Invoke-ShareControlEntry -Action $Action -EntryPath $PSCommandPath -ConsoleReloaded:$ConsoleReloaded
$labels = @{ inventory = '任务身份'; lock = '恢复互斥'; watchdog = '保护任务';
    broker_stop = '后台停止'; broker_start = '后台启动'; console_reload = '控制台重载'; broker_verify = '后台响应' }
foreach ($event in $result.events) {
    $label = if ($labels.ContainsKey($event.stage)) { $labels[$event.stage] } else { $event.stage }
    $state = if ($event.state -eq 'confirmed') { '已确认' } else { '未确认' }
    Write-Host ("{0}：{1}（{2}）" -f $label, $state, $event.code)
}
if ($result.ok) {
    Write-Host '分享管理后台已确认可响应。请刷新网页，再读取分层状态并按需开启分享。'
    exit 0
}
Write-Host ("尚未确认恢复（{0}）。" -f $result.code) -ForegroundColor Yellow
if ($result.code -eq 'operator_context_required' -or $result.code -eq 'console_reload_requires_operator') {
    Write-Host '请关闭此管理员窗口，普通双击 Recover-ShareControl.bat；8001 必须由原用户权限重载。'
}
if ($result.code -eq 'task_access_denied' -or $result.code -eq 'control_needs_recovery') {
    Write-Host '可运行同目录 Recover-ShareControl.bat，通过 Windows 管理员授权进行固定范围恢复。'
}
$exitCodes = Get-ShareControlExitCodes
if ($exitCodes.ContainsKey($result.code)) { exit $exitCodes[$result.code] }
exit 1
