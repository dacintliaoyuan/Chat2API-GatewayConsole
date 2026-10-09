# watchdog-umshare.ps1 — persistent SYSTEM monitor for the live UM share.
#
# Hardened per review:
#  * limit check = wfp_helper `strong` (presence + action + layer + app-id blob
#    equals current image) PLUS the permit filter's destination address/port AND
#    the block filters having ONLY the app-id condition (any narrowing is a
#    tamper); firewall check = rule exists + Enabled + Action=Block +
#    Program==exact image + Direction=Outbound + no protocol/port/address-range
#    narrowing (any narrowed scope => limit loss).
#  * DETECTION FAILURE (helper cannot run / non-zero / any mismatch) is treated
#    exactly like limit loss.
#  * the dedicated-backend permit port is read from backend-port.txt; a missing or
#    invalid file is a DETECTION FAILURE (LIMIT-LOSS stop), never a fallback to
#    the private 8000.
#  * share discovery distinguishes THREE states: verified (live listener whose
#    full image path, owner, and creation time are confirmed), absent (no
#    listener, query succeeded), unconfirmed (query failed OR a listener exists
#    whose identity is NOT verifiably ours). 'unconfirmed' on the share port is
#    an anomaly -> close public + exit; it must NOT be treated as "no share".
#  * stop re-verifies the full identity (image + owner + CREATION TIME) right
#    before killing, so a recycled PID of a different process is never killed;
#    stop failure / unconfidence -> verified public-close fallback.
#  * public close checks the Tailscale command exit code and re-verifies the
#    mapping is actually gone (a failed verification is NOT success).
#  * all watchdog code + wfp_helper dependency live on the protected run-copy
#    path (never user-writable tmp).
#  * polling = BOUNDED-DELAY stop, not instant. Only LIMIT-LOSS acts on the
#    next poll; share-death needs sustained absence; anomaly acts immediately.
#
# TestMode is TEST-ONLY: it logs would-be actions and may run a fake close
# command via -CloseCmd when -TestCloseReal is set; never used in deployment.
param(
    [string]$RunRoot = 'C:\ProgramData\AIGatewayPublicShareRun',
    [string]$WfpHelper = '',
    [string]$AppExe = '',
    [string]$FirewallRule = 'um-share-public-outbound-block',
    [string]$RequireOwner = 'um_share_public',
    [int]$LivePort = 8100,
    [string]$Tailscale = 'C:\Program Files\Tailscale\tailscale.exe',
    [string]$CloseCmd = '',
    [string]$StateDir = '',
    [string]$Log = '',
    [string]$BrokerStatusPath = '',
    [int]$PollSeconds = 10,
    [switch]$TestMode,
    [switch]$TestCloseReal,
    [switch]$OneShot,
    [switch]$AssumeLimitsOk = $false,   # TEST-ONLY seam, never set in deployment
    [switch]$FunctionsOnly,             # TEST-ONLY seam: define functions, no loop
    [string]$WatchdogStateDir = ''      # TEST-ONLY override of the SYSTEM-only
                                        # decision-file directory (never set in deployment)
)
$ErrorActionPreference = 'Continue'

if ($WfpHelper -eq '') { $WfpHelper = Join-Path $RunRoot 'wfp_helper.ps1' }
if ($AppExe -eq '') { $AppExe = Join-Path $RunRoot 'um-share-python.exe' }
if ($StateDir -eq '') { $StateDir = Join-Path $RunRoot 'run\live' }
# (watchdog.log moved to the SYSTEM-only watchdog-system dir below,
# S3-R6A3 R-S3-2: decision evidence must not sit in the um_share RW dir)
if ($BrokerStatusPath -eq '') { $BrokerStatusPath = Join-Path $RunRoot 'broker\broker-status.json' }
# The broker's PROTECTED maintenance record (contract §11.1/§11.2) lives
# NEXT TO broker-status.json in the broker dir; it is derived from the
# status path so a test override keeps both records in the same directory.
$BrokerMaintenancePath = ''
$brokerDir = Split-Path -Parent $BrokerStatusPath
if ($brokerDir) {
    $BrokerMaintenancePath = Join-Path $brokerDir 'maintenance.json'
} else {
    $BrokerMaintenancePath = Join-Path $RunRoot 'broker\maintenance.json'
}
# Dedicated-backend permit port: written by the deployment (backend-port.txt
# in the protected run root). There is NO fallback port: a missing/invalid file
# makes the limits check a DETECTION FAILURE (the existing LIMIT-LOSS path stops
# the share and closes public) instead of silently verifying against 8000, which
# would name the wrong failure and could match a permit nobody installed.
$script:BackendPort = 0
$script:BackendPortError = ''
try {
    $portFile = Join-Path $RunRoot 'backend-port.txt'
    if (-not (Test-Path -LiteralPath $portFile)) {
        $script:BackendPortError = 'backend-port-unavailable(no backend-port.txt)'
    } else {
        $rawPort = (Get-Content -LiteralPath $portFile -TotalCount 1 -ErrorAction Stop).Trim()
        $parsed = 0
        if ([int]::TryParse($rawPort, [ref]$parsed) -and $parsed -ge 1 -and $parsed -le 65535) {
            $script:BackendPort = $parsed
        } else {
            $script:BackendPortError = ('backend-port-unavailable(bad value: ' + $rawPort + ')')
        }
    }
} catch {
    $script:BackendPortError = ('backend-port-unavailable(read failed: ' + $_.Exception.Message + ')')
}
# S3-R6A3 R-S3-2: the watchdog's decision files (recovery budget + seen
# marker + decision log) live in a SYSTEM-only directory, NOT the um_share
# writable run dir — a compromised share process can no longer reset the
# recovery budget (bounded recovery stays bounded) or pollute the evidence.
# The broker lifecycle record remains the authority for managed stops.
# -WatchdogStateDir overrides the location (TEST-ONLY seam; the production
# deployment never passes it).
if ($WatchdogStateDir -eq '') {
    $WatchdogStateDir = Join-Path $RunRoot 'watchdog-system'
}
if (-not $FunctionsOnly) {
    try {
        if (-not (Test-Path -LiteralPath $WatchdogStateDir)) {
            New-Item -ItemType Directory -Path $WatchdogStateDir -Force |
                Out-Null
        }
        icacls $WatchdogStateDir /inheritance:r `
            /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' |
            Out-Null
    } catch {
        Write-Output ('[watchdog] system-state dir setup failed: ' +
            $_.Exception.Message + ' (ACL item open; files still written)')
    }
}
$SeenFile = Join-Path $WatchdogStateDir 'watchdog-seen.json'
$RecoveryFile = Join-Path $WatchdogStateDir 'watchdog-recovery.json'
if ($Log -eq '') { $Log = Join-Path $WatchdogStateDir 'watchdog.log' }
$ExpectedExe = $AppExe   # full exact path; identity must match this, not just a suffix

function Write-WdLog($msg) {
    $line = ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    try { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 } catch { }
    try { Write-Host $line } catch { }   # host only - never the pipeline (would corrupt return values)
}

function Test-FirewallScopeOk {
    # A valid share outbound block must NOT be narrowed to a subset of
    # protocols / ports / remote addresses - otherwise egress by other means is
    # left open while the rule still "exists with Action=Block".
    param($rule)
    try {
        if ($rule.Direction -ne 'Outbound') { return $false, ('direction=' + $rule.Direction) }
        $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $addr = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $proto = $port.Protocol
        $remote = $addr.RemoteAddress
        if ($proto -and $proto -ne 'Any' -and $proto -ne '') { return $false, ('protocol=' + $proto) }
        if ($remote -and $remote -ne 'Any' -and $remote -ne '' -and $remote -ne '*') { return $false, ('remote=' + ($remote -join ',')) }
        return $true, 'scope-ok'
    } catch {
        return $false, ('scope-query-error: ' + $_.Exception.Message)
    }
}

function Get-FirewallState {
    try {
        $rule = Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction Stop
        if (-not $rule) { return $false, 'rule-missing' }
        if ($rule.Enabled -ne 'True' -and $rule.Enabled -ne $true) { return $false, 'rule-disabled' }
        if ($rule.Action -ne 'Block') { return $false, ('action=' + $rule.Action) }
        $app = (Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop).Program
        if ($app -ne $AppExe) { return $false, ('program=' + $app) }
        $scopeOk, $scopeWhy = Test-FirewallScopeOk $rule
        if (-not $scopeOk) { return $false, $scopeWhy }
        return $true, 'ok'
    } catch {
        return $false, ('firewall-error: ' + $_.Exception.Message)
    }
}

function Get-LimitsState {
    if ($AssumeLimitsOk) { return $true, 'assumed-ok(test-only)' }
    if ($script:BackendPort -lt 1) {
        # No permit port could be read: the strong check cannot name the
        # permit it is verifying, so this is a DETECTION FAILURE (the caller's
        # LIMIT-LOSS path stops the share and closes public) - never a silent
        # fallback port and never a "limit lost" claim about a port we did not
        # read.
        return $false, ('detection-failure(' + $(if ($script:BackendPortError) { $script:BackendPortError } else { 'backend-port-unavailable' }) + ')')
    }
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $WfpHelper -Action strong -AppExePath $AppExe -Port $script:BackendPort 2>&1
    $rc = $LASTEXITCODE
    $text = ($output -join ' ')
    if (($null -eq $output -or $output.Count -eq 0)) { return $false, "detection-failure(no-output, rc=$rc)" }
    if ($rc -ne 0) { return $false, "detection-failure(rc=$rc)" }
    if ($text -notlike '*STRONG=OK*') { return $false, ('strong-not-ok: ' + ($text.Substring(0, [Math]::Min(120, $text.Length)))) }
    return $true, 'ok'
}

# Returns a hashtable: state = verified | absent | unconfirmed ; share / err.
function Get-ShareState {
    try {
        $conn = Get-NetTCPConnection -LocalPort $LivePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $conn) { return @{ state = 'absent'; share = $null; err = 'no-listener' } }
        $pid_ = [int]$conn.OwningProcess
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pid_" -ErrorAction Stop
            $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
            $exe = [string]$proc.ExecutablePath
            $crt = $proc.CreationDate
        } catch {
            return @{ state = 'unconfirmed'; share = $null; err = 'procquery: ' + $_.Exception.Message }
        }
        $imgOk = ($exe -and $exe -eq $ExpectedExe)
        # RequireOwner=='' (TEST-ONLY) skips the owner assertion; otherwise the
        # real um_share owner must match. TestMode no longer blanket-skips it,
        # so identity-mismatch behaviour is testable.
        $ownerOk = if ($RequireOwner -eq '') { $true } else { ($owner -and $owner.User -eq $RequireOwner) }
        if (-not ($imgOk -and $ownerOk)) {
            return @{ state = 'unconfirmed'; share = $null;
                      err = ('identity-mismatch exe=' + $exe + ' owner=' + $(if ($owner) { $owner.User } else { '?' })) }
        }
        return @{ state = 'verified'; share = [pscustomobject]@{ pid = $pid_; creation = $crt } ; err = $null }
    } catch {
        return @{ state = 'unconfirmed'; share = $null; err = 'listener-query-error: ' + $_.Exception.Message }
    }
}

# Reads the canonical creation_epoch (Unix seconds) from a watchdog-seen
# record; falls back to parsing the raw creation field in either the
# /Date(ms)/ form or the culture-sensitive DateTime string form, so the
# decision never depends on a serializer's output format. Returns $null on
# an unparsable/absent timestamp.
function Get-SeenCreationEpoch {
    param($seen)
    if ($null -ne $seen.creation_epoch) {
        $v = $null
        if ([double]::TryParse([string]$seen.creation_epoch, [ref]$v) -and $v -gt 0) {
            return $v
        }
        return $null
    }
    $creation = [string]$seen.creation
    if (-not $creation) { return $null }
    if ($creation -match '/Date\((?<ms>\d+)\)/') {
        return [double]$Matches['ms'] / 1000.0
    }
    $dto = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse(
            $creation, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$dto)) {
        return [double]$dto.ToUnixTimeSeconds()
    }
    return $null
}

# Managed-stop coordination: DISTINGUISH a deliberate management stop from an
# anomalous disappearance using the SYSTEM broker's PROTECTED lifecycle
# record (broker dir: SYSTEM/admin write, operator read-only - never an
# arbitrarily writable marker). Returns True ONLY when the broker's verified
# record says a drain/emergency-stop was confirmed AFTER the last share
# instance this watchdog verified; a stale record (a share was observed after
# the stop) or a malformed/missing record keeps the anomaly path active.
function Test-ManagedStopInForce {
    if (-not (Test-Path -LiteralPath $BrokerStatusPath)) { return $false }
    try {
        $status = Get-Content -LiteralPath $BrokerStatusPath -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch { return $false }
    $lifecycle = $status.lifecycle
    if (-not $lifecycle -or $lifecycle.state -ne 'stopped') { return $false }
    try { $markerAt = [double]$lifecycle.at } catch { return $false }
    if ($markerAt -le 0) { return $false }
    if (-not (Test-Path -LiteralPath $SeenFile)) { return $false }
    try {
        $seen = Get-Content -LiteralPath $SeenFile -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch { return $false }
    $seenEpoch = Get-SeenCreationEpoch $seen
    if ($null -eq $seenEpoch) { return $false }
    return $markerAt -gt $seenEpoch
}

# Maintenance-window coordination (contract §9.3/§11.2): the broker's
# PROTECTED maintenance record (maintenance.json) says a temporary lease
# (leased) or an 8100 gateway-restart window (restarting) is in force. The
# watchdog must NOT race that window with a bounded recovery Start; the
# record's own expires_at is the ONLY expiry authority (never a hardcoded
# TTL). Missing/malformed records and a missing/illegal/expired expires_at
# are fail-safe False. This check gates ONLY the sustained-absence recovery
# branch - the LIMIT-LOSS / ANOMALY / identity hard gates are never masked.
function Test-MaintenanceInForce {
    if (-not (Test-Path -LiteralPath $BrokerMaintenancePath)) { return $false }
    try {
        $record = Get-Content -LiteralPath $BrokerMaintenancePath -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch { return $false }
    if (-not $record) { return $false }
    if ([string]$record.state -ne 'leased' -and [string]$record.state -ne 'restarting') { return $false }
    $expires = 0
    try { $expires = [double]$record.expires_at } catch { return $false }
    if ($expires -le 0) { return $false }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return ($now -lt $expires)
}

function Stop-ShareProcess {
    param([object]$share)
    if (-not $share) { return $false, 'no-share-object' }   # not confirmed => must NOT count as success
    if ($TestMode) { return $true, ("TEST-STOP(pid=" + $share.pid + ")") }
    try {
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $share.pid) -ErrorAction Stop
        $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
        $exe = [string]$proc.ExecutablePath
        $crt = $proc.CreationDate
        # re-verify FULL identity (image + owner + creation time) so a recycled
        # PID of an unrelated process is never killed
        if ($exe -ne $ExpectedExe) { return $false, ('image-mismatch ' + $exe) }
        if (-not ($owner -and $owner.User -eq $RequireOwner)) { return $false, ('owner-mismatch ' + $owner.User) }
        if (($share.creation -is [datetime]) -and ($crt -is [datetime])) {
            if ([Math]::Abs(($share.creation - $crt).TotalSeconds) -gt 2) { return $false, 'creation-mismatch' }
        }
        Stop-Process -Id $share.pid -Force -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds(5)
        while ((Get-Date) -lt $deadline) {
            if (-not (Get-Process -Id $share.pid -ErrorAction SilentlyContinue)) { return $true, 'stopped' }
            Start-Sleep -Milliseconds 300
        }
        return $false, 'still-alive-after-5s'
    } catch {
        return $false, ('stop-error: ' + $_.Exception.Message)
    }
}

function Close-Public {
    # Verified public-close fallback: run the close command, then verify the
    # mapping is actually gone. The Tailscale command exit code is checked and a
    # failed / unverifiable status query is NOT counted as success.
    if ($TestMode) {
        if ($TestCloseReal -and $CloseCmd) {
            & $CloseCmd *> $null
            if ($LASTEXITCODE -ne 0) { Write-WdLog 'CLOSE-PUBLIC(TEST): cmd failed -> NOT verified'; return $false }
            Write-WdLog 'CLOSE-PUBLIC(TEST): cmd exited 0 (verification stubbed)'
            return $true
        }
        Write-WdLog 'CLOSE-PUBLIC: TEST-ONLY (funnel unaffected)'
        return $true
    }
    try {
        & $Tailscale funnel --https=443 off *> $null
        $rcOff = $LASTEXITCODE
        if ($rcOff -ne 0) { Write-WdLog ('CLOSE-PUBLIC-CMD-FAILED rc=' + $rcOff); return $false }
        Start-Sleep -Seconds 2
        $svc = & $Tailscale serve status 2>&1
        $rcSvc = $LASTEXITCODE
        if ($rcSvc -ne 0) { Write-WdLog ('CLOSE-PUBLIC-VERIFY-FAILED rc=' + $rcSvc); return $false }
        $out = ($svc -join ' ').Trim()
        if (-not $out) { Write-WdLog 'CLOSE-PUBLIC-VERIFY-EMPTY (cannot confirm mapping state)'; return $false }
        $still = $out -match 'https://[^\s]+'
        if ($still) { Write-WdLog ('CLOSE-PUBLIC-STILL-PRESENT: ' + $out); return $false }
        Write-WdLog 'CLOSE-PUBLIC: verified mapping removed'
        return $true
    } catch {
        Write-WdLog ('CLOSE-PUBLIC-ERROR: ' + $_.Exception.Message)
        return $false
    }
}

function Cleanup-Task {
    if ($TestMode) { Write-WdLog 'CLEANUP: TEST-ONLY (task kept)'; return }
    try { Unregister-ScheduledTask -TaskName 'umshare-public-live' -Confirm:$false -ErrorAction SilentlyContinue } catch { }
}

# ------------------ bounded crash recovery (R8-A7) ------------------
# The watchdog NEVER auto-reopens a deliberately closed share (the broker's
# protected lifecycle record decides that, see Test-ManagedStopInForce) and
# never acts across boots: the recovery budget is keyed to THIS boot's
# identity, so a reboot always starts with a fresh budget and a rebooted
# machine never restores the public entry by itself. Recovery restarts the
# SAME registered share task in place; after the attempt budget is spent the
# watchdog falls back to the safe close and names the manual entry.
$RecoveryLimit = 3

function Get-BootId {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        return ($os.LastBootUpTime.ToUniversalTime().Ticks).ToString()
    } catch { return '' }
}

function Get-RecoveryState {
    # {boot, attempts} keyed by boot identity; a boot change (or any read
    # failure) resets the budget to zero.
    $bootId = Get-BootId
    $attempts = 0
    try {
        if ($bootId -and (Test-Path -LiteralPath $RecoveryFile)) {
            $rec = Get-Content -LiteralPath $RecoveryFile -Raw -ErrorAction Stop |
                ConvertFrom-Json
            $n = 0
            if ([string]$rec.boot -eq [string]$bootId -and
                    [int]::TryParse([string]$rec.attempts, [ref]$n)) {
                $attempts = [Math]::Max(0, $n)
            }
        }
    } catch { $attempts = 0 }
    return @{ boot = $bootId; attempts = $attempts }
}

function Save-RecoveryState {
    param([hashtable]$state)
    try {
        Set-Content -LiteralPath $RecoveryFile `
            -Value (@{ boot = [string]$state.boot; attempts = [int]$state.attempts } |
                    ConvertTo-Json -Compress) -Encoding UTF8
    } catch { }
}

function Invoke-ShareRecovery {
    # One bounded recovery attempt: restart the registered share task and
    # wait for a verified listener. Returns $true only on a re-verified
    # share; every step is logged for the manual-entry fallback.
    Write-WdLog 'RECOVERY: bounded restart of the registered share task begins'
    if ($TestMode) {
        Write-WdLog 'RECOVERY: TEST-ONLY (no task start)'
        return $true
    }
    try {
        Start-ScheduledTask -TaskName 'umshare-public-live' -ErrorAction Stop
    } catch {
        Write-WdLog ('RECOVERY-START-FAILED: ' + $_.Exception.Message)
        return $false
    }
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline) {
        $st = Get-ShareState
        if ($st.state -eq 'verified') {
            Write-WdLog ('RECOVERY-OK: share re-verified pid=' + $st.share.pid)
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-WdLog 'RECOVERY-UNCONFIRMED: listener did not return within 40s'
    return $false
}

# ------------------ main loop ------------------
# -FunctionsOnly (TEST-ONLY seam) provides the decision functions without
# running the polling loop; the loop body below is never reached in tests.
if (-not $FunctionsOnly) {
Write-WdLog ("watchdog starting (poll=$PollSeconds s; BOUNDED-DELAY stop, not instant) mode=" + $(if ($TestMode) { 'test' } else { 'live' }))
$seen = $false
$misses = 0
$MissLimit = 3   # sustained-absence grace (probe/real restart must not be "death")

while ($true) {
    Start-Sleep -Seconds $PollSeconds

    $limitsOk = $true; $limitReason = 'ok'
    if (-not $AssumeLimitsOk) {
        $limitsOk, $limitReason = Get-LimitsState
        if ($limitsOk) { $fwOk, $fwReason = Get-FirewallState; if (-not $fwOk) { $limitsOk = $false; $limitReason = 'firewall:' + $fwReason } }
    }
    $st = Get-ShareState

    # ANOMALY: a listener exists on the share port whose identity is NOT
    # verifiably ours (query failure / identity mismatch). Never treat that as
    # "no share" - close public so the entry cannot serve an unknown process.
    if ($st.state -eq 'unconfirmed') {
        Write-WdLog ('ANOMALY: share port identity not confirmed -> close public. ' + $st.err)
        $okClose = Close-Public
        Write-WdLog ('ANOMALY-CLOSE: ' + $(if ($okClose) { 'OK' } else { 'FAILED (manual action needed)' }))
        Cleanup-Task
        Write-WdLog 'watchdog exiting after anomaly'
        exit 1
    }

    if ($st.state -eq 'verified') {
        # Watchdog-seen carries a CANONICAL creation_epoch (Unix seconds) in
        # addition to the raw DateTime string: ConvertTo-Json serializes a
        # DateTime either as /Date(ms)/ (Windows PowerShell 5.1 captures) or
        # as a culture-sensitive plain string - the managed-stop reader must
        # never depend on the serializer's form, only on the explicit number.
        try {
            $crtEpoch = 0
            try { $crtEpoch = [int][DateTimeOffset]::new(
                    $st.share.creation.ToUniversalTime()).ToUnixTimeSeconds() } catch { }
            $seenObj = @{ pid = $st.share.pid; creation = $st.share.creation;
                          creation_epoch = $crtEpoch }
            Set-Content -LiteralPath $SeenFile -Value ($seenObj | ConvertTo-Json -Compress) -Encoding UTF8
        } catch { }
        $seen = $true
        $misses = 0
    }

    if (-not $limitsOk) {
        Write-WdLog ("LIMIT-LOSS: " + $limitReason + " (bounded stop begins now)")
        if ($st.state -eq 'absent') {
            Write-WdLog 'share confirmed absent on the port; nothing to kill (public served by nothing)'
        } else {
            $okStop, $whyStop = Stop-ShareProcess $st.share
            Write-WdLog ("STOP: " + $whyStop)
            if (-not $okStop) {
                Write-WdLog ('STOP-UNCONFIRMED: ' + $whyStop + ' -> public-close fallback')
                $okClose = Close-Public
                Write-WdLog ('FALLBACK-CLOSE: ' + $(if ($okClose) { 'OK' } else { 'FAILED (manual action needed)' }))
            }
        }
        Cleanup-Task
        Write-WdLog 'watchdog exiting after limit loss'
        exit 1
    }

    if ($seen) {
        if ($st.state -eq 'verified') {
            Write-WdLog ("check-ok limits=" + $limitReason + " share=" + $st.share.pid + " (bounded poll)")
            if ($OneShot) { Write-WdLog 'one-shot done (no action)'; exit 0 }
            continue
        }
        # was seen, now absent -> sustained-absence grace, EXCEPT when the
        # SYSTEM broker's protected lifecycle record says a management stop
        # is in force (a deliberate operator stop is NOT share death): the
        # Password task and the recovery path stay intact. The record never
        # bypasses the ANOMALY / LIMIT-LOSS paths above or below - a rogue
        # listener or a lost limit still closes unconditionally.
        if (Test-ManagedStopInForce) {
            Write-WdLog 'expected-stop: share absent and a confirmed management stop is in force -> task kept, absence not treated as death'
            $misses = 0
            continue
        }
        $misses += 1
        Write-WdLog ("share-absent poll " + $misses + "/" + $MissLimit + " (sustained-absence grace; bounded-delay)")
        if ($misses -ge $MissLimit) {
            # Contract §9.3/§11.2: a maintenance window in force (broker
            # lease / gateway-restart record, not yet expired) means the
            # sustained absence is the RESTART WINDOW, not share death -
            # the watchdog must not race the broker's stop/start and must
            # not consume its own recovery budget.
            if (Test-MaintenanceInForce) {
                Write-WdLog 'maintenance-in-force: broker maintenance window active -> recovery skipped this round'
                $misses = 0
                if ($OneShot) { Write-WdLog 'one-shot done (maintenance in force)'; exit 0 }
                continue
            }
            # Share dead after sustained absence and NO management stop is
            # in force: bounded recovery (R8-A7) before the safe close —
            # same boot only, capped attempts, then the honest fallback.
            $rec = Get-RecoveryState
            if ($rec.boot -and $rec.attempts -lt $RecoveryLimit) {
                Write-WdLog ('SHARE-DEAD -> bounded recovery attempt ' +
                    ($rec.attempts + 1) + '/' + $RecoveryLimit + ' (same boot, no confirmed stop in force)')
                $rec.attempts += 1
                Save-RecoveryState $rec
                if (Invoke-ShareRecovery) {
                    $misses = 0
                    Write-WdLog ('RECOVERY resume polling (attempt ' + $rec.attempts + '/' + $RecoveryLimit + ' used this boot)')
                    if ($OneShot) { Write-WdLog 'one-shot done (recovery attempted)'; exit 0 }
                    continue
                }
                Write-WdLog 'RECOVERY failed -> fall back to safe close'
            } else {
                Write-WdLog ('RECOVERY-BUDGET-EXHAUSTED: attempts=' + $rec.attempts +
                    '/' + $RecoveryLimit + ' this boot; no further automatic attempts')
            }
            Write-WdLog 'SHARE-DEAD (absent for sustained polls) -> cleanup + close public'
            $okClose = Close-Public
            Write-WdLog ('CLEANUP-CLOSE: ' + $(if ($okClose) { 'OK' } else { 'manual action needed' }))
            Cleanup-Task
            Write-WdLog 'watchdog exiting after sustained share absence'
            exit 1
        }
        if ($OneShot) { Write-WdLog 'one-shot done (no action; grace window open)'; exit 0 }
        continue
    }

    Write-WdLog ("check-ok limits=" + $limitReason + " share=none seen=false (startup grace)")
    if ($OneShot) { Write-WdLog 'one-shot done (no action)'; exit 0 }
}
}
