# Fully isolated protected-helper contract; all Windows command boundaries fake.
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$scratch=Join-Path $env:TEMP ('share-repair-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $scratch 'broker') -Force | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $root 'um-share-deploy\repair-limits.ps1') -Destination (Join-Path $scratch 'repair-limits.ps1')
    Set-Content -LiteralPath (Join-Path $scratch 'wfp_helper.ps1') -Value '# fixture only' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $scratch 'watchdog-umshare.ps1') -Value 'function Get-FirewallState { return $global:shareRepairFixtureFirewall, "fixture" }' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $scratch 'backend-port.txt') -Value '8110' -Encoding ASCII
    $statusPath=Join-Path $scratch 'broker\lifecycle.json'
    Set-Content -LiteralPath $statusPath -Value '{"state":"stopped"}' -Encoding ASCII
    . (Join-Path $scratch 'repair-limits.ps1') -RunRoot $scratch -FunctionsOnly
    # Exercise the real PS 5.1 child boundary with a harmless fixture, including
    # nonzero exit propagation (Start-Process otherwise loses ExitCode).
    $nativeFixture=Join-Path $scratch 'native child.ps1'
    Set-Content -LiteralPath $nativeFixture -Value 'Write-Output native-fixture; exit 7' -Encoding ASCII
    $native=Invoke-LimitsCommand -Exe (Join-Path $PSHOME 'pwsh.exe') -Arguments @('-NoProfile','-NonInteractive','-File',$nativeFixture) -Clock ([Diagnostics.Stopwatch]::StartNew())
    if($native.Code -ne 7 -or $native.Text.Trim() -ne 'native-fixture') { throw 'native child exit/output must be preserved' }
    $global:shareRepairFixtureFirewall=$true
    $script:fixturePublic='{}'; $script:fixtureListeners=@(); $script:fixtureBoot=$true; $script:fixtureTaskState='Running'
    $script:watchEvents=[Collections.Generic.List[string]]::new()
    $script:commandCalls=[Collections.Generic.List[string]]::new()
    function Invoke-LimitsCommand {
        param($Exe,$Arguments,$Clock)
        $script:commandCalls.Add(($Arguments -join '|'))
        if ($Arguments[0] -eq 'funnel') { return @{Code=0;Text=$script:fixturePublic} }
        if($script:fixtureTaskState -ne 'Ready'){throw 'Rule changes require paused watcher'}
        return @{Code=0;Text='STRONG=OK'}
    }
    function Get-NetTCPConnection { return $script:fixtureListeners }
    function Get-NetFirewallRule { return @{DisplayName='um-share-outbound-block'} }
    function New-NetFirewallRule { throw 'unexpected live mutation' }
    function Start-ScheduledTask { $script:watchEvents.Add('start'); $script:fixtureTaskState='Running' }
    function Stop-ScheduledTask { $script:watchEvents.Add('stop'); $script:fixtureTaskState='Ready' }
    function Get-ScheduledTask {
        $triggers=if($script:fixtureBoot){@(@{CimClass=@{CimClassName='MSFT_TaskBootTrigger'}})}else{@()}
        return @{State=$script:fixtureTaskState;Principal=@{UserId='SYSTEM'};Triggers=$triggers;
            Actions=@(@{Execute='pwsh.exe';Arguments=('-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $scratch 'watchdog-umshare.ps1')+'"')})}
    }
    if ((Invoke-ShareLimitsRepair).repaired -ne $true) { throw 'valid repair must pass' }
    if(($script:watchEvents -join ',') -ne 'stop,start'){throw 'Verified watcher must pause only during rule rebuild'}
    if ($script:commandCalls.Count -ne 4 -or $script:commandCalls[1] -notmatch '\|remove\|' -or
        $script:commandCalls[2] -notmatch '\|add\|' -or $script:commandCalls[3] -notmatch '\|strong\|') { throw 'expected bounded remove/add/strong sequence' }
    function Assert-Refused($Expected) {
        try { $null=Invoke-ShareLimitsRepair; throw 'unexpected success' }
        catch { if ($_.Exception.Message -ne $Expected) { throw } }
    }
    $script:fixturePublic='{"TCP":{"443":{"HTTPS":true}}}'
    Assert-Refused 'public_not_closed'
    $script:fixturePublic='{}'
    $script:fixtureListeners=@(@{LocalPort=8100})
    Assert-Refused 'gateway_not_closed'
    $script:fixtureListeners=@()
    Set-Content -LiteralPath $statusPath -Value '{"state":"running"}' -Encoding ASCII
    Assert-Refused 'stop_intent_unconfirmed'
    Set-Content -LiteralPath $statusPath -Value '{"state":"stopped"}' -Encoding ASCII
    $global:shareRepairFixtureFirewall=$false
    Assert-Refused 'firewall_not_satisfied'
    if($script:fixtureTaskState -ne 'Running'){throw 'Failure must also re-arm watcher'}
    $global:shareRepairFixtureFirewall=$true
    $script:fixtureBoot=$false
    Assert-Refused 'watchdog_not_boot_ready'
    Write-Output 'PASS: repair sequence, public/identity/intent gates, firewall and boot-monitor refusal'
} finally {
    Remove-Variable -Name shareRepairFixtureFirewall -Scope Global -ErrorAction SilentlyContinue
    # Only this uniquely created fixture tree is removed.
    $resolved=[IO.Path]::GetFullPath($scratch)
    if(-not $resolved.StartsWith([IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')+'\') -or
        (Split-Path $resolved -Leaf) -notmatch '^share-repair-[a-f0-9]{32}$') { throw 'Unsafe fixture cleanup target' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
