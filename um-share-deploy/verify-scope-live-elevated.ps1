# verify-scope-live-elevated.ps1 — LIVE check that the watchdog's hardened
# scope/attribute checks actually ACT: start a probe share under the NEW
# watchdog, narrow the firewall rule's scope (Protocol->UDP) -> the watchdog
# must treat it as limit loss and stop the share within the bounded delay, then
# restore the rule and hand back an armed watchdog + running share (probe is
# enough for this verification; real chat is verified separately).
$ErrorActionPreference = 'Continue'
$Log = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\scope-transcript.log'
$ResultFile = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\scope-result.txt'
$Deploy = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\deploy-elevated.ps1'
$FirewallRule = 'um-share-outbound-block'

function Log($m) { $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m); Write-Output $line; try { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 } catch { } }
function Stage($s, $m = 'probe') {
    $p = Start-Process pwsh -Wait -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Deploy,'-Stage',$s,'-Mode',$m)
    return $p.ExitCode
}
function Listen8100 { return [bool](Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) }
function Wait-Stop($sec) { $d=(Get-Date).AddSeconds($sec); while((Get-Date)-lt $d){ if(-not (Listen8100)){return $true}; Start-Sleep -Milliseconds 500 }; return -not (Listen8100) }
function Wait-Listen($sec) { $d=(Get-Date).AddSeconds($sec); while((Get-Date)-lt $d){ if(Listen8100){return $true}; Start-Sleep -Milliseconds 500 }; return Listen8100 }

try {
    if ((Stage 'Prep') -ne 0) { throw 'Prep failed' }
    if ((Stage 'Watchdog') -ne 0) { throw 'Watchdog stage failed' }
    Start-Sleep -Seconds 3
    $wd = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
    if (-not $wd -or $wd.State -ne 'Running') { throw ('watchdog not armed (' + $wd.State + ')') }
    Log 'new watchdog armed (Running)'

    if ((Stage 'Start' 'probe') -ne 0) { throw 'Start probe failed' }
    if (-not (Wait-Listen 30)) { throw 'probe not listening' }
    Log 'probe share up'

    # scope tamper: narrow the firewall rule to UDP-only (Same action/program,
    # but restricted scope) -> the new attribute check must treat it as loss
    Set-NetFirewallRule -DisplayName $FirewallRule -Protocol UDP -ErrorAction Stop | Out-Null
    Log 'rule narrowed (Protocol=UDP) -> watchdog must stop the share (bounded delay)'
    $ok = Wait-Stop 25
    Set-NetFirewallRule -DisplayName $FirewallRule -Protocol Any -ErrorAction SilentlyContinue | Out-Null
    Log ('   rule restored: ' + (Get-NetFirewallRule -DisplayName $FirewallRule).Enabled)

    $line = 'PASS scope-narrow-stops-share' -f $null
    if (-not $ok) { $line = 'FAIL scope-narrow-stops-share (share still running after rule narrowed)' }
    if ($ok) { $line = 'PASS scope-narrow-stops-share | watchdog acted on narrowed firewall scope (bounded delay)' }
    else { $line = 'FAIL scope-narrow-stops-share | share not stopped after narrowing' }
    Write-Output $line
    Add-Content -LiteralPath $Log -Value $line -Encoding UTF8

    # hand back an armed watchdog + running probe (controlled state)
    if ((Stage 'Prep') -ne 0) { throw 'Prep(revive) failed' }
    if ((Stage 'Watchdog') -ne 0) { throw 'Watchdog(revive) failed' }
    if ((Stage 'Start' 'probe') -ne 0) { throw 'Start(revive) failed' }
    if (-not (Wait-Listen 30)) { throw 'revive not listening' }
    Log 'revived: probe share live under new armed watchdog'

    Set-Content -LiteralPath $ResultFile -Value ($line + "`nREVIVED=armed+probe") -Encoding UTF8
    if (-not $ok) { exit 1 }
    exit 0
} catch {
    $m = $_.Exception.Message
    Log ('SCOPE-ERROR: ' + $m)
    Set-Content -LiteralPath $ResultFile -Value ('ERROR | ' + $m) -Encoding UTF8
    exit 1
}
