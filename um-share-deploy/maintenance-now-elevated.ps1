# maintenance-now-elevated.ps1 — ONE elevated session for the immediate actions:
#   1) revoke friend2 from the RUN COPY only (online effect, keeps friend1/friend3)
#   2) live-verify: friend2 key -> 401, friend3 key -> 200 (keys never printed)
#   3) verify Funnel ownership 443/->8100, then PAUSE the share (precise stop of
#      the verified um_share process + unregister live task), KEEP all limits
#   4) write a result marker to a readable path
$ErrorActionPreference = 'Continue'
$ResultFile = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\maintenance-now-result.txt'
$Log = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\maintenance-now-transcript.log'
$RepoConfig = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share.json'
$RunConfig = 'C:\ProgramData\UMShareRun\um-share.json'
$AppExe = 'C:\ProgramData\UMShareRun\um-share-python.exe'
$Tailscale = 'C:\Program Files\Tailscale\tailscale.exe'

function Put-Result($kind, $detail) { try { Set-Content -LiteralPath $ResultFile -Value ($kind + ' | ' + $detail) -Encoding UTF8 } catch { } }
function Log($m) { $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m); Write-Output $line; try { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 } catch { } }

function Get-Clients($path) {
    $j = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $m = @{}
    foreach ($p in $j.clients.PSObject.Properties) { $m[$p.Name] = $p.Value }
    return $m
}

try {
    # ---- 1) revoke friend2 from the run copy ----
    $rep = Get-Clients $RepoConfig
    $run = Get-Clients $RunConfig
    Log ("REPO: " + (($rep.Keys | Sort-Object) -join ','))
    Log ("RUN : " + (($run.Keys | Sort-Object) -join ','))
    if ($rep.ContainsKey('friend2')) { throw 'repo still contains friend2' }
    foreach ($n in @('friend1','friend3')) {
        if ($rep.ContainsKey($n) -and $run.ContainsKey($n) -and $rep[$n] -ne $run[$n]) { throw "digest mismatch $n" }
    }
    if ($run.ContainsKey('friend2')) {
        $new = @{ clients = @{} }
        foreach ($n in $rep.Keys) { $new.clients[$n] = $rep[$n] }
        $text = (ConvertTo-Json $new -Depth 4)
        $tmp = $RunConfig + '.now.tmp'
        [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding($false)))
        $bak = $RunConfig + '.now.bak'
        [IO.File]::Replace($tmp, $RunConfig, $bak)   # atomic; preserves dest ACL
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        Log 'friend2 removed from RUN COPY (atomic File.Replace, ACL preserved)'
    } else {
        Log 'run copy already has no friend2'
    }
    $v = Get-Clients $RunConfig
    if ($v.ContainsKey('friend2')) { throw 'friend2 still in run copy' }
    foreach ($n in $rep.Keys) { if ($v[$n] -ne $rep[$n]) { throw "digest mismatch after write $n" } }
    Log ("RUN after: " + (($v.Keys | Sort-Object) -join ','))

    # ---- 2) live 401/200 check against the running share ----
    if (Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) {
        $py = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314\python.exe'
        $pyOut = & $py 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\verify_revoke2.py' 2>&1
        $pyOut | ForEach-Object { Log ("  revoke-check: $_") }
        if ($LASTEXITCODE -ne 0) { throw 'live friend2/friend3 check FAILED' }
        Log 'LIVE CHECK OK: friend2=401, friend3=200'
    } else {
        Log 'no 8100 listener (share already down); skipping live 401/200 check'
    }

    # ---- 3) verify Funnel ownership then pause the share ----
    $serveOut = (& $Tailscale serve status 2>&1) -join ' '
    # `serve status` reports the funnelled target, not the literal port number;
    # ownership = the Funnel-public mapping resolves to OUR 127.0.0.1:8100.
    $ours = ($serveOut -match 'Funnel on') -and ($serveOut -match '127\.0\.0\.1:8100')
    if (-not $ours) {
        Log ('Funnel ownership NOT confirmed (' + $serveOut.Trim() + ')')
        throw 'cannot confirm this task owns the public mapping; not pausing blind (share left as-is until cleared)'
    }
    Log 'Funnel ownership confirmed: Funnel / -> 127.0.0.1:8100 (this task)'
    $conn = Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    $paused = $false
    if ($conn) {
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue
        $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction SilentlyContinue
        $imgOk = $proc.ExecutablePath -and $proc.ExecutablePath.EndsWith('um-share-python.exe')
        $ownerOk = $owner -and $owner.User -eq 'um_share'
        Log ("share pid=" + $conn.OwningProcess + " image=" + $proc.ExecutablePath + " owner=" + $owner.User + " imgOk=" + $imgOk + " ownerOk=" + $ownerOk)
        if ($imgOk -and $ownerOk) {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            $still = Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue
            if ($still) { Log 'PAUSE-STOP-FAILED: 8100 still listening' }
            else { $paused = $true; Log 'share process stopped (verified identity); 8100 no longer listening' }
        } else {
            Log ('PAUSE-REFUSED: 8100 listener identity not verified as um_share (imgOk=' + $imgOk + ' ownerOk=' + $ownerOk + '); not stopping')
        }
    } else {
        $paused = $true
        Log 'no 8100 listener; share already paused'
    }
    try { Unregister-ScheduledTask -TaskName 'umshare-live' -Confirm:$false -ErrorAction SilentlyContinue } catch { }
    Log 'umshare-live task unregistered (limits left in place for re-deploy)'
    if (-not $paused) { throw 'share not confirmed paused' }

    Put-Result 'MAINTENANCE-OK' ('friend2 revoked; run-copy=' + (($v.Keys | Sort-Object) -join ',') + '; share paused; limits kept')
    Log 'MAINTENANCE-OK'
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log ('MAINTENANCE-FAIL: ' + $msg)
    Put-Result 'MAINTENANCE-FAIL' $msg
    exit 1
}
