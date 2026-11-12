# verify-scenarios-elevated.ps1 — ONE elevated session: arm the hardened SYSTEM
# watchdog, start a probe share, then run the runtime lifecycle scenarios and a
# real-receiver UDP egress comparison. Every scenario writes a log + a result
# marker; the suite fails if any expectation is violated. Never touches DSH /
# chat2api (8000/8096/8095/8097/3080/4141 are left running).
$ErrorActionPreference = 'Continue'
$ResultFile = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\scenarios-result.txt'
$Log = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\scenarios-transcript.log'
$RunRoot = 'C:\ProgramData\UMShareRun'
$DeployDir = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy'
$WfpHelper = Join-Path $RunRoot 'wfp_helper.ps1'
$AppExe = Join-Path $RunRoot 'um-share-python.exe'
$Py = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314\python.exe'
$Deploy = Join-Path $DeployDir 'deploy-elevated.ps1'
$LiveLog = Join-Path $RunRoot 'run\live\watchdog.log'
$script:results = @()

function Log($m) { $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m); Write-Output $line; try { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 } catch { } }
function Report($name, $ok, $detail) {
    $r = 'PASS ' + $name; if (-not $ok) { $r = 'FAIL ' + $name }
    $script:results += $r + ' | ' + $detail
    Log $r
}
function Listen8100 { return [bool](Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) }
function Wait-Stop($sec) {
    $deadline = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $deadline) { if (-not (Listen8100)) { return $true }; Start-Sleep -Milliseconds 500 }
    return -not (Listen8100)
}
function Reset-WdLog { try { if (Test-Path -LiteralPath $LiveLog) { Remove-Item -LiteralPath $LiveLog -Force } } catch { } }

# ---- helpers that reuse deploy-elevated stages (all elevated) ----
function Invoke-Stage($stage, $mode = 'probe') {
    $p = Start-Process pwsh -Wait -PassThru -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File', $Deploy, '-Stage', $stage, '-Mode', $mode)
    return $p.ExitCode
}

function Ensure-Armed {
    # limits + watchdog armed + probe up
    $rc = Invoke-Stage 'Prep'
    if ($rc -ne 0) { Report 'ensure-prep' $false "rc=$rc"; return $false }
    $rc = Invoke-Stage 'Watchdog'
    if ($rc -ne 0) { Report 'ensure-watchdog' $false "rc=$rc"; return $false }
    Start-Sleep -Seconds 3
    $wd = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
    if (-not $wd -or $wd.State -ne 'Running') { Report 'ensure-watchdog-running' $false ('state=' + $wd.State); return $false }
    Report 'ensure-watchdog-running' $true 'Running'
    $rc = Invoke-Stage 'Start'   # requires watchdog Running (deploy gate)
    if ($rc -ne 0) { Report 'ensure-probe-up' $false "start rc=$rc"; return $false }
    if (-not (Listen8100)) { Report 'ensure-8100' $false 'not listening'; return $false }
    Report 'ensure-probe-up' $true '8100 listening, watchdog armed'
    return $true
}

try {
    # ---------- Scenario B: rule disable stops the share ----------
    if (Ensure-Armed) {
        Reset-WdLog
        Disable-NetFirewallRule -DisplayName 'um-share-outbound-block' -ErrorAction Stop
        Start-Sleep -Seconds 2
        $ok = Wait-Stop 20
        Enable-NetFirewallRule -DisplayName 'um-share-outbound-block' -ErrorAction SilentlyContinue
        Report 'scn-rule-disable-stop' $ok 'watchdog must stop share within bounded delay after firewall rule disabled'
        # cleanup
        Invoke-Stage 'Stop' | Out-Null
    }

    # ---------- Scenario A: WFP filter loss stops the share ----------
    if (Ensure-Armed) {
        Reset-WdLog
        & pwsh -NoProfile -ExecutionPolicy Bypass -File $WfpHelper -Action remove -AppExePath $AppExe | Out-Null
        Start-Sleep -Seconds 2
        $ok = Wait-Stop 20
        Report 'scn-filter-loss-stop' $ok 'watchdog must stop share after WFP app-id filters lost'
        Start-Sleep -Seconds 3   # let the watchdog process finish
        $wdProc = Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'watchdog-umshare' }
        Report 'scn-monitor-exit' (-not $wdProc) ('watchdog process exits after acting; running=' + $(if ($wdProc) { $wdProc.ProcessId } else { 'none' }))
        # restore limits for the next scenario
        Invoke-Stage 'Stop' | Out-Null
        Invoke-Stage 'Prep' | Out-Null
    }

    # ---------- Scenario C: detection failure (helper missing) stops ----------
    if (Ensure-Armed) {
        Reset-WdLog
        $backup = Join-Path $RunRoot 'wfp_helper.ps1.bak'
        Move-Item -LiteralPath $WfpHelper -Destination $backup -Force
        Start-Sleep -Seconds 2
        $ok = Wait-Stop 20
        Move-Item -LiteralPath $backup -Destination $WfpHelper -Force
        Report 'scn-detection-failure-stop' $ok 'watchdog must treat helper failure as limit loss and stop'
        Invoke-Stage 'Stop' | Out-Null
    }

    # ---------- Scenario D: state tamper must NOT fool the watchdog ----------
    if (Ensure-Armed) {
        Reset-WdLog
        $statusFile = Join-Path $RunRoot 'run\live\guard-status.json'
        try {
            if (Test-Path -LiteralPath $statusFile) {
                $fake = '{"guard":"ok","pid":424242,"port":8100,"mode":"probe"}'
                [IO.File]::WriteAllText($statusFile, $fake, (New-Object Text.UTF8Encoding($false)))
            }
        } catch { }
        Start-Sleep -Seconds 14   # allow 1+ polls; watchdog must keep the real share
        $still = Listen8100
        Report 'scn-state-tamper-ignored' $still 'watchdog must ignore a tampered pid file and keep the LIVE share (identity from listener)'
        Invoke-Stage 'Stop' | Out-Null
    }

    # ---------- Scenario F: monitor restarts and becomes armed again ----------
    Invoke-Stage 'Prep' | Out-Null   # the previous Stop removed limits; restore first
    $rc = Invoke-Stage 'Watchdog'
    Start-Sleep -Seconds 3
    $wd2 = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
    Report 'scn-monitor-restart' ($rc -eq 0 -and $wd2 -and $wd2.State -eq 'Running') ('restart rc=' + $rc + ' state=' + $wd2.State)

    # ---------- UDP real-receiver comparison (non-loopback) ----------
    # A real receiver on the machine's LAN IP proves two things: (1) the
    # unrestricted dacint control path delivers (receiver works + control open),
    # and (2) the restricted share image (um_share, under the firewall program
    # outbound block) is NOT received. If the machine has no non-loopback IPv4
    # the loopback residual is reported honestly as NOT representative.
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.|100\.119\.)' -and $_.IPAddress -notlike '0.*' } |
        Select-Object -First 1).IPAddress
    $receiver = Join-Path $DeployDir 'udp_receiver.py'
    $sender = Join-Path $DeployDir 'udp_send.py'

    function Run-UdpPhase($phase, $asShare) {
        $hostIp = if ($lanIp) { $lanIp } else { '127.0.0.1' }
        $port = 14556 + (Get-Random -Maximum 200)
        $rlog = Join-Path $DeployDir ("logs\udp-{0}.log" -f $phase)
        if (Test-Path -LiteralPath $rlog) { Remove-Item -LiteralPath $rlog -Force }
        $recv = Start-Process -FilePath $Py -ArgumentList @($receiver, $hostIp, $port, '10', $rlog) -WindowStyle Hidden -PassThru
        Start-Sleep -Milliseconds 800
        if ($asShare) {
            $cmd = Join-Path $RunRoot 'run-udpsend.cmd'
            $body = "@echo off`r`nset `"PYTHONHOME=C:\Users\dacint\AppData\Local\Programs\Python\Python314`"`r`n`"$AppExe`" `"$sender`" $hostIp $port 8 > nul 2>&1`r`nexit /b %errorlevel%`r`n"
            [IO.File]::WriteAllText($cmd, $body, (New-Object Text.UTF8Encoding($false)))
            icacls $cmd /grant:r 'DAICNT\um_share:(RX)' | Out-Null
            $act = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/d /c ' + $cmd) -WorkingDirectory $RunRoot
            $rand = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 28 | ForEach-Object {[char]$_})
            net user um_share $rand | Out-Null
            Register-ScheduledTask -TaskName 'umshare-udpsend' -Action $act -User 'DAICNT\um_share' -Password $rand -Force | Out-Null
            Start-ScheduledTask -TaskName 'umshare-udpsend'
        } else {
            & $Py $sender $hostIp $port 8 2>&1 | Out-Null
        }
        Start-Sleep -Seconds 4
        $recv.WaitForExit(9000) | Out-Null
        if ($asShare) { try { Unregister-ScheduledTask -TaskName 'umshare-udpsend' -Confirm:$false -ErrorAction SilentlyContinue } catch { } }
        $got = 0
        if (Test-Path -LiteralPath $rlog) { try { $got = [int](Get-Content -LiteralPath $rlog -Raw -ErrorAction Stop) } catch { $got = -1 } }
        return ,@($hostIp, $got)
    }

    $ctl = Run-UdpPhase 'control' $false
    $shr = Run-UdpPhase 'share' $true
    $hostIp = $ctl[0]
    $ctlGot = [int]$ctl[1]
    $shrGot = [int]$shr[1]
    if ($lanIp) {
        Report 'udp-control-receiver' ($ctlGot -eq 8) ("control (dacint) delivered $ctlGot/8 on $hostIp (receiver works)")
        Report 'udp-share-blocked' ($shrGot -eq 0) ("share (um_share) delivered $shrGot/8 on $hostIp (expect 0; firewall program block with real receiver)")
    } else {
        Report 'udp-loopback-residual' ($shrGot -ne 0) ("no non-loopback IPv4; loopback residual observed=$shrGot (dacint control=$ctlGot) - NOT a pass for egress blocking")
    }

    if ($script:results | Where-Object { $_ -like 'FAIL *' }) {
        Set-Content -LiteralPath $ResultFile -Value (($script:results -join "`n") + "`nSUITE=FAIL") -Encoding UTF8
        Log 'SUITE=FAIL'
        exit 1
    }
    Set-Content -LiteralPath $ResultFile -Value (($script:results -join "`n") + "`nSUITE=PASS") -Encoding UTF8
    Log 'SUITE=PASS'
    exit 0
} catch {
    $msg = $_.Exception.Message
    $script:results += 'ERROR | ' + $msg
    Log ('SUITE-ERROR: ' + $msg)
    Set-Content -LiteralPath $ResultFile -Value ($script:results -join "`n") -Encoding UTF8
    exit 1
}
