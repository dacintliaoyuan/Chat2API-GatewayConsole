[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('share_public_failed', 'share_public_recovered',
                 'share_public_manual_required',
                 'share_public_budget_exhausted',
                 'share_public_degraded')]
    [string]$Code
)

# Local user-context balloon notification for the public share supervision
# (contract share-resilience-contract-4 section 4.3). The title and body are a
# FIXED mapping from the code; Python never passes arbitrary text, so no host,
# IP, path or credential can reach the notification. It installs no service,
# elevates nothing and writes no file. A failure exits 1 and never throws to the
# caller; the caller records notification_failed and keeps the round conclusion.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$messages = @{
    'share_public_failed' = @{
        Title = '分享公网巡检失败'
        Body = '公网 DNS/TLS 连续失败，正在按 30 分钟预算尝试恢复。'
    }
    'share_public_recovered' = @{
        Title = '分享公网已恢复'
        Body = '公网 DNS/TLS 检查已恢复正常。'
    }
    'share_public_manual_required' = @{
        Title = '分享公网需人工处理'
        Body = '自动恢复已停止，请打开控制台查看原因。'
    }
    'share_public_budget_exhausted' = @{
        Title = '分享公网恢复预算耗尽'
        Body = '30 分钟恢复预算已用完，请稍后复查或人工处理。'
    }
    'share_public_degraded' = @{
        Title = '分享公网部分可用'
        Body = '部分公网入口失败，已保留可用路径，未做破坏性恢复。'
    }
}

try {
    $message = $messages[$Code]
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $notification = New-Object System.Windows.Forms.NotifyIcon
    try {
        $notification.Icon = [System.Drawing.SystemIcons]::Information
        $notification.BalloonTipTitle = $message.Title
        $notification.BalloonTipText = $message.Body
        $notification.Visible = $true
        $notification.ShowBalloonTip(8000)
        # Keep the process alive long enough for the balloon to be shown;
        # no API confirms that the user actually saw it (known limitation).
        Start-Sleep -Seconds 8
    }
    finally {
        $notification.Dispose()
    }
}
catch {
    exit 1
}
exit 0
