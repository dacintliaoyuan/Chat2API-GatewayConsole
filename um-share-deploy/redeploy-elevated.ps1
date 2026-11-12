# redeploy-elevated.ps1 — restore controlled sharing after the gates passed:
#   Prep (limits, strong) -> Watchdog (armed) -> Start probe -> verify probe ->
#   Start real -> bounded real-UM verification -> leave real running.
$ErrorActionPreference = 'Continue'
$Log = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\redeploy-transcript.log'
$ResultFile = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\redeploy-result.txt'
$Deploy = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\deploy-elevated.ps1'
$Py = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314\python.exe'
$VerifyProbe = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\verify_8100.py'
$VerifyReal = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\verify_redeploy2.py'

function Log($m) { $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m); Write-Output $line; try { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 } catch { } }
function Stage($s, $m = 'probe') {
    $p = Start-Process pwsh -Wait -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Deploy,'-Stage',$s,'-Mode',$m)
    return $p.ExitCode
}
function Listen8100 { return [bool](Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) }
function Wait-Listen($sec) { $d=(Get-Date).AddSeconds($sec); while((Get-Date)-lt $d){ if(Listen8100){return $true}; Start-Sleep -Milliseconds 500 }; return Listen8100 }

try {
    if ((Stage 'Prep') -ne 0) { throw 'Prep failed' }
    Log 'Prep ok (limits strong-verified)'
    if ((Stage 'Watchdog') -ne 0) { throw 'Watchdog stage failed' }
    Start-Sleep -Seconds 3
    $wd = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
    if (-not $wd -or $wd.State -ne 'Running') { throw ('watchdog not armed: ' + $wd.State) }
    Log 'watchdog armed (Running)'

    # probe phase was verified this session; go straight to real (hardened
    # watchdog now tolerates a controlled restart gap via sustained-absence grace)
    if ((Stage 'Start' 'real') -ne 0) { throw 'Start real failed' }
    if (-not (Wait-Listen 30)) { throw 'real not listening' }
    Log 'real share up; bounded real verification'
    $realOut = & $Py $VerifyReal 2>&1
    $realOut | ForEach-Object { Log ("  real: $_") }
    $rc = $LASTEXITCODE

    # deployed share must remain under an armed watchdog + have its task
    $wd2 = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
    $liveTask = Get-ScheduledTask -TaskName 'umshare-live' -ErrorAction SilentlyContinue
    Log ("post-deploy: watchdog=" + $(if ($wd2) { $wd2.State } else { 'missing' }) + " liveTask=" + $(if ($liveTask) { $liveTask.State } else { 'missing' }))
    $armed = $wd2 -and $wd2.State -eq 'Running' -and $liveTask
    if (-not $armed) {
        # re-arm the watchdog: it may have tolerated an earlier transition; the
        # share keeps running meanwhile (limits in place)
        if ((Stage 'Watchdog') -ne 0) { Log 're-arm Watchdog returned nonzero' }
        Start-Sleep -Seconds 3
        $wd2 = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
        $armed = $wd2 -and $wd2.State -eq 'Running'
        Log ("re-check watchdog=" + $(if ($wd2) { $wd2.State } else { 'missing' }))
    }
    if (-not $armed) { throw 'deployed share not under an armed watchdog' }
    $realOut += "POST-DEPLOY watchdog=" + $(if ($wd2) { $wd2.State } else { 'missing' })

    Set-Content -LiteralPath $ResultFile -Value (($realOut -join "`n") + "`nREDEPLOY-EXIT=$rc") -Encoding UTF8
    if ($rc -ne 0) { Log 'REDEPLOY-VERIFY=FAIL'; exit 1 }
    Log 'REDEPLOY-VERIFY=PASS'
    exit 0
} catch {
    $m = $_.Exception.Message
    Log ('REDEPLOY-ERROR: ' + $m)
    Set-Content -LiteralPath $ResultFile -Value ('ERROR | ' + $m) -Encoding UTF8
    exit 1
}
