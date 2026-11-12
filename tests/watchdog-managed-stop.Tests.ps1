# Offline tests for the watchdog managed-stop coordination
# (Test-ManagedStopInForce in um-share-deploy/watchdog-umshare.ps1) and for the
# R13-3 permit-port contract (the watchdog's backend-port.txt source and
# wfp_helper's explicit -Port requirement).
# The decision function runs against fixture broker-status.json /
# watchdog-seen.json in a temp dir - no real scheduled task, service, pipe
# or public mapping is touched. Positive and negative examples: a confirmed
# broker 'stopped' record AFTER the last verified share is an EXPECTED stop;
# missing / running / stale / malformed records keep the anomaly path.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$tmp = Join-Path $env:TEMP ("wd-mgmt-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $tmp 'run\live') -Force | Out-Null
$liveDir = Join-Path $tmp 'run\live'
$statusPath = Join-Path $tmp 'broker-status.json'
$seenPath = Join-Path $liveDir 'watchdog-seen.json'

. (Join-Path $root 'um-share-deploy\watchdog-umshare.ps1') `
    -FunctionsOnly -RunRoot $tmp -StateDir $liveDir -BrokerStatusPath $statusPath `
    -WatchdogStateDir $liveDir

$script:failures = 0

function Assert-True {
    param([object]$Condition, [string]$Name)
    if ($Condition) { Write-Host "PASS $Name" -ForegroundColor Green; return }
    $script:failures++
    Write-Host "FAIL $Name" -ForegroundColor Red
}

function Assert-False {
    param([object]$Condition, [string]$Name)
    if (-not $Condition) { Write-Host "PASS $Name" -ForegroundColor Green; return }
    $script:failures++
    Write-Host ("FAIL {0}: expected $false, got {1}" -f $Name, $Condition) -ForegroundColor Red
}

function Now-Epoch { return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Write-Json {
    param([string]$Path, [object]$Obj)
    $text = $Obj | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# ============ positive: broker stopped AFTER the last verified share ============
# format A: canonical creation_epoch (production reliable field)
$now = Now-Epoch
$status = @{ pipe_name = '\\.\pipe\um-share-broker'
             lifecycle = @{ state = 'stopped'; at = $now; by = 'drain' } }
Write-Json $statusPath $status
Write-Json $seenPath (@{ pid = 4242; creation = $null; creation_epoch = ($now - 120) })
Assert-True (Test-ManagedStopInForce) 'canonical creation_epoch, stop newer than share -> expected (True)'

# format B: /Date(ms)/ legacy form
Write-Json $seenPath (@{ pid = 4242; creation = ('/Date(' + [long](($now - 120) * 1000) + ')/') })
Assert-True (Test-ManagedStopInForce) '/Date(ms)/ form, stop newer than share -> expected (True)'

# format C: culture-sensitive plain string (real serializer output)
$seenDt = [DateTimeOffset]::FromUnixTimeSeconds($now - 120).UtcDateTime
Write-Json $seenPath (@{ pid = 4242; creation = $seenDt.ToString('MM/dd/yyyy HH:mm:ss') })
Assert-True (Test-ManagedStopInForce) 'plain DateTime string form -> expected (True)'

# format C2: plain string but with garbage-broken culture digits still parses
Write-Json $seenPath (@{ pid = 4242; creation = ('09/01/2026 00:00:00') })
$statusPast = @{ lifecycle = @{ state = 'stopped'; at = 9999999999; by = 'drain' } }
Write-Json $statusPath $statusPast
Assert-True (Test-ManagedStopInForce) 'parses arbitrary valid DateTime string (marker far future)'

# ============ negative cases ============
# --- no broker-status file ---
Remove-Item -LiteralPath $statusPath -Force -ErrorAction SilentlyContinue
Write-Json $seenPath (@{ pid = 4242; creation_epoch = ($now - 120) })
Assert-False (Test-ManagedStopInForce) 'missing broker status -> not expected (False)'

# --- lifecycle state running ---
$status2 = @{ lifecycle = @{ state = 'running'; at = $now; by = 'start' } }
Write-Json $statusPath $status2
Assert-False (Test-ManagedStopInForce) 'lifecycle running -> not expected (False)'

# --- stale: a share was verified AFTER the stop (canonical epoch) ---
Write-Json $statusPath (@{ lifecycle = @{ state = 'stopped'; at = ($now - 3600); by = 'drain' } })
Write-Json $seenPath (@{ pid = 777; creation_epoch = ($now - 60) })
Assert-False (Test-ManagedStopInForce) 'stale stop (canonical, share born after it) -> not expected (False)'

# --- stale: same via plain string ---
$seenAfter = [DateTimeOffset]::FromUnixTimeSeconds($now - 60).UtcDateTime
Write-Json $seenPath (@{ pid = 777; creation = $seenAfter.ToString('MM/dd/yyyy HH:mm:ss') })
Assert-False (Test-ManagedStopInForce) 'stale stop (plain string form) -> not expected (False)'

# --- malformed broker-status ---
[System.IO.File]::WriteAllText($statusPath, 'not-json{', (New-Object System.Text.UTF8Encoding($false)))
Assert-False (Test-ManagedStopInForce) 'malformed broker status -> not expected (False)'

# --- lifecycle missing in otherwise valid status ---
Write-Json $statusPath (@{ pipe_name = '\\.\pipe\um-share-broker'; cycles = 3 })
Assert-False (Test-ManagedStopInForce) 'status without lifecycle -> not expected (False)'

# --- seen file missing ---
Remove-Item -LiteralPath $seenPath -Force -ErrorAction SilentlyContinue
$status5 = @{ lifecycle = @{ state = 'stopped'; at = $now; by = 'emergency-stop' } }
Write-Json $statusPath $status5
Assert-False (Test-ManagedStopInForce) 'no seen record -> fail-safe not expected (False)'

# --- malformed seen creation (both forms absent/invalid) ---
Write-Json $seenPath (@{ pid = 9; creation = 'garbage'; creation_epoch = -5 })
Assert-False (Test-ManagedStopInForce) 'negative epoch / unparsable creation -> not expected (False)'

Write-Json $seenPath (@{ pid = 9; creation = 'garbage' })
Assert-False (Test-ManagedStopInForce) 'garbage creation without epoch -> not expected (False)'

# ============ maintenance window: Test-MaintenanceInForce (contract 11.2) ============
# The broker maintenance record lives NEXT TO broker-status.json
# ($tmp\maintenance.json). It gates ONLY the sustained-absence bounded-recovery
# branch: while a maintenance window is in force (leased / restarting and not
# expired) the watchdog must NOT race the broker's stop/start. A missing/malformed
# record, an illegal state, or a missing/illegal/expired expires_at is fail-safe
# False (the record's OWN bounded expiry is used, never a hardcoded 300).
$maintPath = Join-Path $tmp 'maintenance.json'
Remove-Item -LiteralPath $maintPath -Force -ErrorAction SilentlyContinue
Assert-False (Test-MaintenanceInForce) 'missing maintenance.json -> not in force (False)'

Write-Json $maintPath (@{ schema_version = 1; state = 'leased';
    component = 'share_entry'; lease_id = ('a' * 32); instance_id = 'i-1';
    round = 1; created_at = ($now - 10); expires_at = ($now + 300);
    by = 'maintenance-begin'; last_outcome = $null })
Assert-True (Test-MaintenanceInForce) 'leased, not expired -> in force (True)'

Write-Json $maintPath (@{ state = 'restarting'; expires_at = ($now + 150) })
Assert-True (Test-MaintenanceInForce) 'restarting, not expired -> in force (True)'

Write-Json $maintPath (@{ state = 'idle'; expires_at = ($now + 300) })
Assert-False (Test-MaintenanceInForce) 'idle -> not in force (False)'

Write-Json $maintPath (@{ state = 'stopped'; expires_at = ($now + 300) })
Assert-False (Test-MaintenanceInForce) 'stopped state -> not in force (False)'

Write-Json $maintPath (@{ state = 'leased'; expires_at = ($now - 1) })
Assert-False (Test-MaintenanceInForce) 'expired expires_at -> not in force (False)'

Write-Json $maintPath (@{ state = 'leased' })
Assert-False (Test-MaintenanceInForce) 'missing expires_at -> fail-safe (False)'

Write-Json $maintPath (@{ state = 'leased'; expires_at = 'soon' })
Assert-False (Test-MaintenanceInForce) 'illegal expires_at -> fail-safe (False)'

Write-Json $maintPath (@{ state = 'leased'; expires_at = -5 })
Assert-False (Test-MaintenanceInForce) 'negative expires_at -> fail-safe (False)'

[System.IO.File]::WriteAllText($maintPath, 'not-json{', (New-Object System.Text.UTF8Encoding($false)))
Assert-False (Test-MaintenanceInForce) 'malformed maintenance.json -> fail-safe (False)'

Remove-Item -LiteralPath $maintPath -Force -ErrorAction SilentlyContinue

# Wiring assertion: the recovery branch really consults
# Test-MaintenanceInForce BEFORE Invoke-ShareRecovery (static check,
# complementary to the behavioral cases above).
$wdText = [IO.File]::ReadAllText((Join-Path $root 'um-share-deploy\watchdog-umshare.ps1'))
$recoveryIdx = $wdText.IndexOf('$misses -ge $MissLimit')
$maintIdx = $wdText.IndexOf('Test-MaintenanceInForce', $recoveryIdx)
$recoverIdx = $wdText.IndexOf('Invoke-ShareRecovery', $recoveryIdx)
Assert-True ($recoveryIdx -ge 0) 'recovery branch exists in the watchdog loop'
Assert-True ($maintIdx -ge 0 -and $maintIdx -lt $recoverIdx) 'recovery branch consults Test-MaintenanceInForce BEFORE Invoke-ShareRecovery'

Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue

# ============ R13-3: the permit port never falls back to 8000 ============
# The dot-source above ran WITHOUT a backend-port.txt in $tmp: that must be a
# DETECTION FAILURE naming the missing file (the existing LIMIT-LOSS path stops
# the share), never a silent fallback port.
$limitsOk, $limitsWhy = Get-LimitsState
Assert-False $limitsOk 'missing backend-port.txt -> limits detection failure (no fallback port)'
Assert-True ($limitsWhy -match 'backend-port') ('the reason names the missing port file: ' + $limitsWhy)

# A valid backend-port.txt is read as the permit port. The strong check itself
# needs admin, so only the READ value is asserted here.
$portRoot = Join-Path $env:TEMP ("wd-port-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $portRoot -Force | Out-Null
Set-Content -LiteralPath (Join-Path $portRoot 'backend-port.txt') -Value '8110' -Encoding ASCII
. (Join-Path $root 'um-share-deploy\watchdog-umshare.ps1') `
    -FunctionsOnly -RunRoot $portRoot -StateDir $liveDir -BrokerStatusPath $statusPath `
    -WatchdogStateDir $liveDir
Assert-True ($BackendPort -eq 8110) ('backend-port.txt is read as the permit port (got: ' + $BackendPort + ')')

# A corrupt value is a detection failure too (never 8000).
Set-Content -LiteralPath (Join-Path $portRoot 'backend-port.txt') -Value 'not-a-port' -Encoding ASCII
. (Join-Path $root 'um-share-deploy\watchdog-umshare.ps1') `
    -FunctionsOnly -RunRoot $portRoot -StateDir $liveDir -BrokerStatusPath $statusPath `
    -WatchdogStateDir $liveDir
$limitsOk, $limitsWhy = Get-LimitsState
Assert-False $limitsOk 'corrupt backend-port.txt -> limits detection failure (no fallback port)'
Remove-Item -LiteralPath $portRoot -Recurse -Force -ErrorAction SilentlyContinue

# ============ R13-3: the WFP helper needs an explicit permit port ============
# `add`/`strong` REFUSE without -Port (the historical default 8000 was the
# PRIVATE chat2api instance); `verify` addresses the filters by fixed GUID and
# must keep working without it. The helper is invoked exactly like the deployment
# does (powershell -File). The negative cases fail closed during parameter
# validation, BEFORE any real filter operation: `add` is pointed at a nonexistent
# image path so the app-id lookup cannot install anything even when elevated.
# `remove` is NOT executed here on purpose - it performs a REAL delete of the
# production filters when the caller is elevated.
$wfp = Join-Path $root 'um-share-deploy\wfp_helper.ps1'
$wfpExe = 'C:\nonexistent\um-share-python.exe'
$addNoPort = & pwsh -NoProfile -ExecutionPolicy Bypass -File $wfp -Action add -AppExePath $wfpExe 2>&1 | Out-String
Assert-True ($addNoPort -match '-Port is required') 'wfp_helper add without -Port refuses (no default port)'
$strongNoPort = & pwsh -NoProfile -ExecutionPolicy Bypass -File $wfp -Action strong -AppExePath $wfpExe 2>&1 | Out-String
Assert-True ($strongNoPort -match '-Port is required') 'wfp_helper strong without -Port refuses (no default port)'
$addWithPort = & pwsh -NoProfile -ExecutionPolicy Bypass -File $wfp -Action add -AppExePath $wfpExe -Port 8110 2>&1 | Out-String
Assert-True ($addWithPort -notmatch '-Port is required') 'wfp_helper add with -Port passes parameter validation'
$verifyNoPort = & pwsh -NoProfile -ExecutionPolicy Bypass -File $wfp -Action verify -AppExePath $wfpExe 2>&1 | Out-String
Assert-True ($verifyNoPort -notmatch '-Port is required') 'wfp_helper verify without -Port keeps working (fixed GUIDs)'

& pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'watchdog-listener-race.Tests.ps1')
if ($LASTEXITCODE -ne 0) { $script:failures++ }
& pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'watchdog-limits.Tests.ps1')
if ($LASTEXITCODE -ne 0) { $script:failures++ }
& pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'repair-limits.Tests.ps1')
if ($LASTEXITCODE -ne 0) { $script:failures++ }

if ($script:failures -gt 0) {
    Write-Host ("watchdog managed-stop tests: {0} FAILURES" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'watchdog managed-stop tests: ALL PASS' -ForegroundColor Green
exit 0
