# revoke2-elevated.ps1 — remove friend2 from the RUN COPY only; online effect.
# The repo config (authoritative) is the source of truth for surviving digests.
# Keeps friend1/friend3 digests identical, preserves the run-copy ACL (File.Replace
# keeps the destination file's security/ACL), and never prints any key.
param(
    [string]$RepoConfig = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share.json',
    [string]$RunConfig = 'C:\ProgramData\UMShareRun\um-share.json'
)
$ErrorActionPreference = 'Stop'
$ResultFile = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\revoke2-result.txt'
$TranscriptPath = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\logs\revoke2-transcript.log'

function Put-Result($kind, $detail) {
    try { Set-Content -LiteralPath $ResultFile -Value ($kind + ' | ' + $detail) -Encoding UTF8 } catch { }
}

function Get-Clients($path) {
    $j = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $m = @{}
    foreach ($p in $j.clients.PSObject.Properties) { $m[$p.Name] = $p.Value }
    return $m
}

try {
    Start-Transcript -Path $TranscriptPath -Append -Force | Out-Null
    $rep = Get-Clients $RepoConfig
    $run = Get-Clients $RunConfig
    # safety: never write plaintext, only names + digest prefixes
    $names = ($rep.Keys | Sort-Object) -join ','
    Write-Output ("REPO names: $names")
    Write-Output ("RUN  names: " + (($run.Keys | Sort-Object) -join ','))

    if ($rep.ContainsKey('friend2')) { throw 'repo config still contains friend2; abort (repo must be the cleaner)' }
    foreach ($n in @('friend1', 'friend3')) {
        if ($rep.ContainsKey($n) -and $run.ContainsKey($n) -and $rep[$n] -ne $run[$n]) { throw "digest mismatch $n" }
        if ($rep.ContainsKey($n) -and -not $run.ContainsKey($n)) { throw "repo has $n but run copy lacks it" }
    }

    if (-not $run.ContainsKey('friend2')) {
        Write-Output 'run copy already has no friend2 (online already revoked)'
    } else {
        $new = @{ clients = @{} }
        foreach ($n in $rep.Keys) { $new.clients[$n] = $rep[$n] }
        $text = (ConvertTo-Json $new -Depth 4)
        $tmp = $RunConfig + '.revoke2.tmp'
        [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding($false)))
        $bak = $RunConfig + '.revoke2.bak'
        [IO.File]::Replace($tmp, $RunConfig, $bak)   # atomic, preserves dest ACL
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        Write-Output 'run copy updated: friend2 removed, others preserved (atomic File.Replace)'
    }

    $v = Get-Clients $RunConfig
    Write-Output ("AFTER names: " + (($v.Keys | Sort-Object) -join ','))
    if ($v.ContainsKey('friend2')) { throw 'friend2 STILL present in run copy after update' }
    foreach ($n in $rep.Keys) {
        if (-not $v.ContainsKey($n)) { throw "missing $n after update" }
        if ($v[$n] -ne $rep[$n]) { throw "digest mismatch for $n after update" }
    }
    Put-Result 'REVOKE2-OK' ("run-copy clients now: " + (($v.Keys | Sort-Object) -join ','))
    Write-Output 'REVOKE2-OK'
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
} catch {
    $msg = $_.Exception.Message
    Write-Output ("REVOKE2-FAIL: " + $msg)
    Put-Result 'REVOKE2-FAIL' $msg
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
