# deploy-elevated.ps1 — privileged stages of the UM share deployment.
#
# Runs ONLY elevated (UAC). It is the single sanctioned way to start the share
# process, so the "no share without verified limits" invariant holds:
#   Stage Prep   : refresh run copy, hardlink image, install+verify WFP,
#                  create the um-share writable run/ dir.
#   Stage Canary : run boundary probes AS um_share (files/network) and the Job
#                  escape probe (control + under-job); stops before any share.
#   Stage Start  : verify WFP present -> start share via guard (Job) as um_share
#                  -> verify listener/owner/image -> enter watchdog loop that
#                  stops the share the moment any filter disappears.
#   Stage Stop   : kill the exact share pid (image+owner checked) + task.
#
# Execute with:  .\deploy-elevated.ps1 -Stage Prep
#                .\deploy-elevated.ps1 -Stage Canary
#                .\deploy-elevated.ps1 -Stage Start -Mode probe|real
#                .\deploy-elevated.ps1 -Stage Stop
param(
    [Parameter(Mandatory = $true)][ValidateSet('Prep', 'Canary', 'Start', 'Stop', 'DrainStop', 'PublishProjection', 'Watchdog')][string]$Stage,
    [ValidateSet('probe', 'real')][string]$Mode = 'probe',
    [string]$Root = 'D:\git\github\personal_files\SelfUseAI',
    [string]$RunRoot = 'C:\ProgramData\UMShareRun',
    [string]$DeployDir = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy',
    [string]$WfpHelper = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\wfp_helper.ps1',
    # The running broker task on this host is the SYSTEM `umshare-broker`
    # (installed from the recovery install-state). The operator-interactive
    # `umshare-broker-ops` task was retired after the SYSTEM broker took over;
    # a caller may override the task name for an experiment or recovery.
    [string]$BrokerTask = 'umshare-broker',
    # Dedicated share backend listen port (Revision 6 R6-A1). The share
    # gateway forwards here (never to the production chat2api port); the WFP
    # permit filter is keyed to this loopback port. Default keeps the
    # historical single-backend wiring until the isolation gate passes.
    [int]$ShareBackendPort = 8110
)
$ErrorActionPreference = 'Stop'
# Every deployment child check uses this host, independent of the caller PATH.
$env:PATH = $PSHOME + [IO.Path]::PathSeparator + $env:PATH

function Select-ShareWatchdogProcesses {
    param([object[]]$Processes, [string]$WatchdogPath, [int]$CurrentProcessId = $PID)
    $escapedPath = [regex]::Escape($WatchdogPath)
    $pattern = '(?i)(?:^|\s)-File\s+(?:"' + $escapedPath + '"|' + $escapedPath + ')(?=\s|$)'
    foreach ($process in $Processes) {
        if ($process.Name -in @('powershell.exe', 'pwsh.exe') -and
            $process.ProcessId -ne $CurrentProcessId -and
            [string]$process.CommandLine -match $pattern) { $process }
    }
}

$appExe = Join-Path $RunRoot 'um-share-python.exe'
# The dedicated image is a hardlink of python.exe in the run copy, which has NO
# stdlib: python resolves its home from the exe path at C:\ProgramData\UMShareRun
# and aborts with "Failed to import encodings". Every um_share execution is
# therefore wrapped in a .cmd that sets PYTHONHOME to the real Python314 dir
# (um_share has RX there) before running.
$PythonHome = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314'
# The operation log must be readable by the non-elevated driver, so it lives in
# the repo tmp/ dir (ProgramData\UMShareRun is deliberately unreadable by the
# non-elevated user). Everything observational (log + result copies) is mirrored
# here.
$ReadableDir = $DeployDir
$log = Join-Path $ReadableDir ('logs\deploy-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
# Shared attribution key (C2 verified attribution): BOTH ends must point at ONE
# file. 8100 (share gateway) reads it ONCE at startup through the
# SHARE_ATTRIBUTION_KEY_FILE env var this script injects in the Start stage;
# 8110 (dedicated backend) re-stats it per request through
# CHAT2API_SHARE_ATTRIBUTION_KEY_FILE, whose path
# console_share_production.share_attribution_key_path() resolves to the SAME
# file. The name literal below is the one that Python module keeps in
# SHARE_ATTRIBUTION_KEY_NAME; the test suite parses this file and fails if
# the two ever drift apart. The file must NOT live in ai-gateway/data (explicit
# DENY ACE for um_share: a key there is unreadable by 8100 and silently
# disables attribution) and NOT in run\live (every deploy removes and recreates
# that dir, :744-746). A missing/unreadable/too-short file is NOT a failure:
# it is the explicit "no attribution" state (all requests unattributed), so it is
# reported as a warning and the share still starts.
$AttributionKeyName = 'share-attribution.key'
$script:LogLines = @()
function Write-Log($msg) {
    # NOTE: must NOT emit to the pipeline - several functions that call it also
    # return values (Test-WfpInstalled, New-UmCaptureScript), and leaked output
    # would corrupt their return values into arrays.
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    $script:LogLines += $line
    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
}

function Write-Console($msg) {
    # explicit console line for the CLI; never call this inside a fn returning a value
    Write-Output $msg
}

function Copy-ReadableResult($sourceJson, $name) {
    # admin can read ProgramData\UMShareRun; mirror the result for the driver.
    $dest = Join-Path $ReadableDir ("logs\$name")
    try {
        Copy-Item -LiteralPath $sourceJson -Destination $dest -Force
        Write-Log "result mirrored: $dest"
    } catch { Write-Log ("WARN: could not mirror $name : " + $_.Exception.Message) }
}

function Assert-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'deploy-elevated.ps1 must run elevated (admin token).'
    }
}

$FirewallRuleName = 'um-share-outbound-block'

function Add-UmFirewallBlockRule {
    # Program-scoped outbound block for the share image. This is what covers
    # UDP (and unreachable-loopback) egress: the custom WFP app-id filters
    # cannot express UDP (OUTBOUND_TRANSPORT has no ALE_APP_ID field), and
    # Windows Firewall does not filter loopback, so loopback UDP stays a
    # documented residual while REMOTE UDP/IPv4+v6 is blocked by program.
    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Write-Log "firewall rule $FirewallRuleName already present"
        return
    }
    New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Outbound -Action Block `
        -Program $appExe -Profile Any -ErrorAction Stop | Out-Null
    Write-Log "created firewall outbound block rule $FirewallRuleName for $appExe"
}

function Test-UmFirewallBlockRule {
    return [bool](Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue)
}

function Remove-UmFirewallBlockRule {
    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if ($rule) { Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue; Write-Log "removed firewall rule $FirewallRuleName" }
}

function Restart-BrokerAndVerify {
    # R6-A6 (maintainer review 2026-09-17, finding 4): Prep copies new
    # modules over RunRoot, but the RUNNING broker process keeps the OLD
    # code until it restarts — disk-hash equality proves nothing about what
    # the process actually loaded. This helper restarts the umshare-broker
    # task and proves the refreshed protocol is live with THREE checks
    # (fail-closed: the share must not open against a stale or absent
    # broker; a plain ok status would prove only that a pipe answers):
    #   1. a FRESH broker process (CommandLine pins share/broker_server.py
    #      under the RunRoot image; CreationDate > restart moment),
    #   2. a signed management `status` exchange through the pipe, AND
    #   3. the R51.4-generation `host-verify` verb — the restricted
    #      decorator (share_broker_server._HostOpsControlCoordinator)
    #      answers {"check":"running"} with ok + result.host_running,
    #      while any pre-extension broker rejects the unknown action. The
    #      wire shapes this probe depends on are pinned by
    #      tests/test_share/broker_server.py (status result = host/task/
    #      state only; host-verify ok carries host_running) — Reviewer
    #      finding R1 (2026-09-17): share_max_concurrency is a
    #      ShareManager.status() field and NEVER crosses the broker pipe.
    # The control key lives in the broker dir and is never printed.
    param([string]$BrokerTask = 'umshare-broker',
          [string]$BrokerPipe = '\\.\pipe\um-share-broker',
          [string]$ControlKeyPath = (Join-Path $RunRoot 'broker\broker-control-key'))
    $task = Get-ScheduledTask -TaskName $BrokerTask -ErrorAction SilentlyContinue
    if (-not $task) {
        throw 'umshare-broker task not installed; refusing to start the share (projection mode requires the broker)'
    }
    $restartAt = Get-Date
    Write-Log "restarting broker task $BrokerTask to load refreshed modules"
    Stop-ScheduledTask -TaskName $BrokerTask -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 1
    Start-ScheduledTask -TaskName $BrokerTask -ErrorAction Stop
    # Check 1: fresh broker process identity (command line + start time),
    # anchored to THIS RunRoot image so a foreign broker process (e.g. the
    # umshare-broker-exp experiment task under another run root) can never
    # satisfy the check (reviewer finding R4).
    $brokerProc = $null
    $procDeadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $procDeadline) {
        $brokerProc = @(Get-CimInstance Win32_Process -Filter "Name='um-share-python.exe'" |
            Where-Object { $_.CommandLine -like ('*' + $RunRoot + '*share/broker_server.py*') -and
                           $_.CreationDate -gt $restartAt })[0]
        if ($brokerProc) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $brokerProc) {
        throw ('broker restart verification failed: no fresh share/broker_server.py ' +
            'process started under ' + $RunRoot + ' after the task restart; ' +
            'NOT starting the share')
    }
    Write-Log ("broker fresh process: pid=" + $brokerProc.ProcessId +
        " started=" + $brokerProc.CreationDate.ToString('s') + " (after restart at " +
        $restartAt.ToString('s') + ")")
    $probePath = Join-Path $RunRoot 'broker-verify-result.json'
    $verifyPy = Join-Path $RunRoot 'broker-verify.py'
    @'
import json
import sys
from share.broker_client import BrokerClient, NamedPipeBrokerTransport
from share.broker_server import read_control_key

secret = read_control_key(sys.argv[1])
client = BrokerClient(lambda: NamedPipeBrokerTransport(sys.argv[2]), secret)

def frame(reply):
    return {"ok": reply.ok, "error_code": reply.error_code,
            "result": reply.result}

# status proves pipe liveness + identity; host-verify is the generation
# discriminator: only the refreshed decorator understands this verb and
# answers {"ok": True, "result": {"host_running": <bool|None>}}.
status = client.status()
hostverify = client.call("host-verify", {"check": "running"})
json.dump({"status": frame(status), "hostverify": frame(hostverify)},
          open(sys.argv[3], "w", encoding="utf-8"))
'@ | Set-Content -LiteralPath $verifyPy -Encoding UTF8
    # Checks 2+3: signed status exchange + generation discriminator.
    try {
        $deadline = (Get-Date).AddSeconds(45)
        while ((Get-Date) -lt $deadline) {
            Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
            # No 2>&1: with $ErrorActionPreference='Stop' a redirection
            # would turn the probe's own stderr into a terminating
            # NativeCommandError (same trap documented at ~:287).
            $out = & (Join-Path $RunRoot 'um-share-python.exe') `
                $verifyPy $ControlKeyPath $BrokerPipe $probePath
            if (Test-Path -LiteralPath $probePath) {
                $payload = Get-Content -LiteralPath $probePath -Raw -ErrorAction SilentlyContinue |
                    ConvertFrom-Json
                if ($payload -and $payload.status.ok) {
                    # Generation discriminator = the VERB, not the verdict
                    # value: only the refreshed decorator understands
                    # host-verify at all (pre-extension brokers reject it).
                    # The verdict itself may honestly be False or None here —
                    # at this stage the umshare-live task is NOT yet
                    # registered, so WindowsHostOps.verify_running answers
                    # None (unresolvable evidence, never a fake success,
                    # share/broker_ops.py:91-97). Requiring non-null here made
                    # the check unpassable and killed Start 3× (21:17 log).
                    if ($payload.hostverify.ok -and
                        $null -ne $payload.hostverify.result) {
                        Write-Log ("broker verify: signed status ok (host=" +
                            $payload.status.result.host + ", task=" +
                            $payload.status.result.task + ", state=" +
                            $payload.status.result.state + "); host-verify ok " +
                            "(host_running=" +
                            $payload.hostverify.result.host_running +
                            "; None=task not yet registered at this stage) " +
                            "- refreshed modules confirmed live")
                        return
                    }
                    Write-Log ("broker verify attempt: status ok but host-verify " +
                        "answered " + $payload.hostverify.error_code +
                        " - stale broker generation")
                } else {
                    Write-Log ("broker verify attempt: " + ($out -join ' ') +
                        " payload=" + $payload)
                }
            } else {
                Write-Log ("broker probe output: " + ($out -join ' '))
            }
            Start-Sleep -Seconds 2
        }
        throw 'broker restart verification failed: management pipe did not answer a signed status plus an ok host-verify (generation verb); NOT starting the share'
    } finally {
        # Probe artifacts are one-shot diagnostics inside RunRoot: remove
        # them on BOTH the success and the failure path (reviewer finding
        # R5). The result file holds host/task/state only - no key material
        # either way, but nothing needs to survive this function.
        Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $verifyPy -Force -ErrorAction SilentlyContinue
    }
}

function Install-Limits {
    # remove-then-add: the app-id filters use fixed GUIDs, so a leftover filter
    # from an earlier experiment (added for the OLD python-dir image path) would
    # be kept via FWP_E_ALREADY_EXISTS and never match the current ProgramData
    # image -> "present" but not enforced. Rebuilding keys them to $appExe.
    Invoke-WfpRemove
    Invoke-WfpAdd
    Add-UmFirewallBlockRule
}

function Test-LimitsInstalled {
    $wfpOk = Test-WfpInstalled
    $fwOk = Test-UmFirewallBlockRule
    Write-Log "limits check: WFP=$wfpOk firewallRule=$fwOk"
    return ($wfpOk -and $fwOk)
}

function Remove-Limits {
    Invoke-WfpRemove
    Remove-UmFirewallBlockRule
}

function Invoke-WfpAdd {
    Write-Log "installing WFP app-id filters for $appExe (permit port $ShareBackendPort)"
    $out = & pwsh -NoProfile -ExecutionPolicy Bypass -File $WfpHelper -Action add -AppExePath $appExe -Port $ShareBackendPort 2>&1
    foreach ($line in $out) { Write-Log "  wfp: $line" }
    if ($LASTEXITCODE -ne 0) { throw "wfp_helper add failed (exit $LASTEXITCODE)" }
}

function Test-WfpInstalled {
    # Strong check: presence + action/layer + app-id condition == current image
    # path (same standard the SYSTEM watchdog uses). Detection failure (helper
    # error/non-zero/no STRONG=OK) is treated as NOT installed.
    $output = & pwsh -NoProfile -ExecutionPolicy Bypass -File $WfpHelper -Action strong -AppExePath $appExe -Port $ShareBackendPort 2>&1
    $text = ($output -join ' ')
    $output | ForEach-Object { Write-Log "  wfp: $_" }
    if ($LASTEXITCODE -ne 0 -or $text -notlike '*STRONG=OK*') { return $false }
    return $true
}

function Invoke-WfpRemove {
    Write-Log "removing WFP app-id filters"
    $out = & pwsh -NoProfile -ExecutionPolicy Bypass -File $WfpHelper -Action remove -AppExePath $appExe 2>&1
    foreach ($line in $out) { Write-Log "  wfp: $line" }
}

# --- deployment mode gate (D3/R29-5) ----------------------------------------
# The deployment mode is an EXPLICIT, protected state — never inferred from
# the presence or absence of any authorization file:
#   {"mode":"projection"} : SQLite is the single authority; Prep must NOT
#                           copy the legacy digest file over the derived
#                           projection.
#   {"mode":"legacy"}     : an explicitly not-yet-migrated deployment; the
#                           legacy digest copy is allowed.
# A missing or corrupt mode file REFUSES the copy: after a migration a lost
# projection file must never silently re-introduce the old authorizations.
function Get-DeployMode {
    param([string]$ModeFile)
    if (-not (Test-Path -LiteralPath $ModeFile)) {
        throw ("deployment mode file missing: " + $ModeFile +
               ". Create it explicitly with mode=legacy for a not-yet-migrated deployment - mode is never inferred (R29-5).")
    }
    try {
        $mode = (Get-Content -LiteralPath $ModeFile -Raw | ConvertFrom-Json).mode
    } catch {
        throw ("deployment mode file is corrupt: " + $_.Exception.Message)
    }
    if ($mode -ne 'projection' -and $mode -ne 'legacy') {
        throw ("deployment mode must be 'projection' or 'legacy', got '" + $mode + "'")
    }
    return $mode
}

# --- run copy staging -------------------------------------------------------
function Assert-ProjectionPayloadValid {
    # R35-2: FULL validation of the derived projection reuses the ONE shared
    # pure parser (share_broker_protocol.parse_projection_payload) through
    # the gateway runtime - a hand-written PowerShell re-implementation would
    # drift from the real authentication loader. Runs BEFORE any copy/start
    # side effect: a legacy clients file, a bool schema, an invalid entry, a
    # missing generation or a corrupt object refuses the deployment instead
    # of being consumed as authorization.
    param([string]$ProjectionFile, [string]$GatewaySrcDir, [string]$PythonExe)
    $code = ("import json,sys; sys.path.insert(0, sys.argv[1]); " +
             "from share_broker_protocol import parse_projection_payload; " +
             "parse_projection_payload(json.load(open(sys.argv[2], " +
             "encoding='utf-8'))); print('projection-valid')")
    # No 2>&1: with $ErrorActionPreference='Stop' a redirection would turn the
    # parser's own stderr into a terminating NativeCommandError; stderr passes
    # to the console instead and the exit code decides (deterministic refusal).
    & $PythonExe -c $code $GatewaySrcDir $ProjectionFile
    if ($LASTEXITCODE -ne 0) {
        throw ("projection-mode derived file is not a valid projection " +
               "(shared parser exit " + $LASTEXITCODE + "; see parser output)")
    }
    Write-Log ("derived projection full-validated: " +
               (Split-Path $ProjectionFile -Leaf))
}

function Copy-ShareSource {
    $pythonExe = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314\python.exe'

    # 0. deployment mode gate FIRST (R35-2): read the explicit mode and, in
    # projection mode, FULL-validate the derived projection BEFORE any
    # copy/start side effect - a legacy/bool/corrupt derived file refuses
    # deployment instead of being consumed as authorization afterwards.
    $deployMode = Get-DeployMode -ModeFile (Join-Path $RunRoot 'deploy-mode.json')
    Write-Log "deployment mode: $deployMode"
    if ($deployMode -eq 'projection') {
        $derived = Join-Path $RunRoot 'um-share-projection.json'
        if (-not (Test-Path -LiteralPath $derived)) {
            throw ("projection-mode deployment but the derived projection is " +
                   "missing at " + $derived +
                   "; refusing to start (a lost projection must never " +
                   "silently re-introduce the old authorizations)")
        }
        Assert-ProjectionPayloadValid -ProjectionFile $derived `
            -GatewaySrcDir (Join-Path $Root 'ai-gateway') -PythonExe $pythonExe
        Write-Log 'projection mode: skipping legacy um-share.json copy (SQLite is the single authority)'
        # The gateway's instance-control channel needs the FULL share-side
        # module closure in the run copy (share_entry imports
        # share_instance_pipe -> share_broker_server -> share_broker_host ->
        # share_runtime -> share_management -> share_store; the protocol
        # module alone leaves the instance channel ImportError-dead, which
        # R41-1 would downgrade to a never-confirming publication). The
        # list mirrors install-share-broker-lib.ps1's copy closure.
        $closureSrc = @(
            'share/broker_protocol.py', 'share/broker_host.py',
            'share/broker_client.py', 'share/broker_ops.py',
            'share/broker_server.py', 'share/instance_pipe.py',
            'share/runtime.py', 'share/management.py', 'share/store.py'
        )
        foreach ($modName in $closureSrc) {
            $srcMod = Join-Path $Root ('ai-gateway\' + $modName)
            $dstMod = Join-Path $RunRoot $modName
            if (-not (Test-Path -LiteralPath $srcMod)) {
                throw ("projection-mode closure source missing: " + $srcMod)
            }
            Copy-Item -LiteralPath $srcMod -Destination $dstMod -Force
            $srcHash = (Get-FileHash -LiteralPath $srcMod -Algorithm SHA256).Hash
            $dstHash = (Get-FileHash -LiteralPath $dstMod -Algorithm SHA256).Hash
            if ($srcHash -ne $dstHash) { throw ("closure copy hash mismatch: " + $modName) }
            Write-Log ("copied+verified " + $modName + " (" + $srcHash.Substring(0, 12) + "...)")
        }
    } else {
        # Legacy mode (explicit): the digest-only client config. Reject any
        # plaintext or non-64-hex value and any known test identity so a test
        # key can never be shipped in the copy.
        Write-Log 'legacy mode (explicit): copying digest config'
        $srcDigests = Join-Path $Root 'ai-gateway\data\um-share.json'
        $raw = Get-Content -LiteralPath $srcDigests -Raw
        if ($raw -match 'um-share-') { throw 'refusing to copy: um-share.json contains a plaintext key' }
        $clients = (($raw | ConvertFrom-Json).clients)
        $bad = @()
        $testNames = @('p0-canary', 'keytest1', 'canary', 'test')
        foreach ($prop in $clients.PSObject.Properties) {
            if ($prop.Value -notmatch '^[0-9a-fA-F]{64}$') { $bad += $prop.Name }
            if ($testNames -contains $prop.Name.ToLowerInvariant()) { $bad += ('TEST-KEY:' + $prop.Name) }
        }
        if ($bad.Count -gt 0) { throw 'refusing to copy: um-share.json has invalid/test entries: ' + ($bad -join ',') }
        Copy-Item -LiteralPath $srcDigests -Destination (Join-Path $RunRoot 'um-share.json') -Force
        $srcHash = (Get-FileHash -LiteralPath $srcDigests -Algorithm SHA256).Hash
        $dstHash = (Get-FileHash -LiteralPath (Join-Path $RunRoot 'um-share.json') -Algorithm SHA256).Hash
        if ($srcHash -ne $dstHash) { throw 'um-share.json copy hash mismatch' }
        Write-Log "copied+verified um-share.json (client names: $($clients.PSObject.Properties.Name -join ','))"
    }

    # 1. refresh the run-copy scripts from the (already reviewed) source.
    $srcShare = Join-Path $Root 'ai-gateway\chat2api\share_entry.py'
    $pairs = New-Object System.Collections.ArrayList
    [void]$pairs.Add(@((Join-Path $DeployDir 'share_guard.py'), (Join-Path $RunRoot 'share_guard.py')))
    [void]$pairs.Add(@((Join-Path $DeployDir 'canary_um.py'), (Join-Path $RunRoot 'canary_um.py')))
    [void]$pairs.Add(@((Join-Path $DeployDir 'escape_probe.py'), (Join-Path $RunRoot 'escape_probe.py')))
    [void]$pairs.Add(@($srcShare, (Join-Path $RunRoot 'share_entry.py')))
    foreach ($pair in $pairs) {
        Copy-Item -LiteralPath $pair[0] -Destination $pair[1] -Force
        $srcHash = (Get-FileHash -LiteralPath $pair[0] -Algorithm SHA256).Hash
        $dstHash = (Get-FileHash -LiteralPath $pair[1] -Algorithm SHA256).Hash
        if ($srcHash -ne $dstHash) { throw "run copy hash mismatch for $($pair[1])" }
        Write-Log "copied+verified $(Split-Path $pair[1] -Leaf) ($($srcHash.Substring(0,12))...)"
    }

    # 3. dedicated image: hardlink umbrella so the WFP app-id is unique. Use
    # fsutil (elevated) which fails loudly if a link already exists there.
    if (-not (Test-Path -LiteralPath $appExe)) {
        $out = & fsutil hardlink create $appExe $pythonExe 2>&1
        Write-Log "hardlink: $out"
        if ($LASTEXITCODE -ne 0) { throw 'fsutil hardlink create failed' }
    } else {
        Write-Log "um-share-python.exe already present"
    }

    # 3b. Python runtime DLLs. um-share-python.exe is a hardlink of python.exe,
    # so the loader must find python314.dll/vcruntime*.dll next to it (the run
    # copy is self-contained; um_share's PATH has no Python314). Missing DLL =
    # the scheduled task exits 0xC0000135 STATUS_DLL_NOT_FOUND before any probe
    # runs. Copy + hash-verify every DLL (idempotent).
    $pythonDir = 'C:\Users\dacint\AppData\Local\Programs\Python\Python314'
    foreach ($dllName in @('python3.dll', 'python314.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
        $srcDll = Join-Path $pythonDir $dllName
        $dstDll = Join-Path $RunRoot $dllName
        if (-not (Test-Path -LiteralPath $srcDll)) { throw "python runtime DLL missing: $srcDll" }
        $srcHash = (Get-FileHash -LiteralPath $srcDll -Algorithm SHA256).Hash
        try {
            Copy-Item -LiteralPath $srcDll -Destination $dstDll -Force
        } catch {
            # A running share has the DLL mapped -> sharing violation. This is
            # only acceptable if the destination is already byte-identical to
            # the source (running copy stays correct); otherwise fail.
            if (Test-Path -LiteralPath $dstDll) {
                $dstHash = (Get-FileHash -LiteralPath $dstDll -Algorithm SHA256).Hash
                if ($dstHash -eq $srcHash) { Write-Log "$dllName in use but already up-to-date (hash matches)"; continue }
            }
            throw "cannot refresh $dllName : $($_.Exception.Message)"
        }
        $dstHash = (Get-FileHash -LiteralPath $dstDll -Algorithm SHA256).Hash
        if ($srcHash -ne $dstHash) { throw "DLL copy hash mismatch: $dllName" }
        Write-Log "copied+verified $dllName ($($srcHash.Substring(0,12))...)"
    }

    # 4. writable run/ dir owned by um_share (guard status/canary results).
    $runDir = Join-Path $RunRoot 'run'
    if (-not (Test-Path -LiteralPath $runDir)) { New-Item -ItemType Directory -Path $runDir | Out-Null }
    icacls $runDir /inheritance:r /grant:r 'DAICNT\um_share:(OI)(CI)RW' /grant:r 'SYSTEM:(OI)(CI)F' /grant:r 'BUILTIN\Administrators:(OI)(CI)F' | Out-Null
    Write-Log "run/ dir ready (um_share RW, admin/SYSTEM full)"
    # 4b. deployment-side attribution visibility: report whether the shared
    # attribution key FILE IS READY (warning only - the share starts either way;
    [void](Write-AttributionKeyState -Where 'Prep')

    # 5. make sure the share can EXECUTE the image (Python RX inherited already)
    return $true
}

# --- task scheduler (password-in-LSA recipe, proven by P0 setup-elevated) ----
function Start-UmTask {
    param([Parameter(Mandatory = $true)][string]$Name,
          [Parameter(Mandatory = $true)][string]$AsCmd,
          [string]$WorkDir = $RunRoot)
    # Reset the account to a fresh random password, then register with
    # -User/-Password so the secret goes straight into the Task Scheduler LSA
    # store (nothing written to disk/logs). Same recipe setup-elevated.ps1 used.
    $randBytes = [byte[]]::new(28)
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randBytes)
        $rand = -join ($randBytes | ForEach-Object { "{0:x2}" -f $_ })
    $netOut = net user um_share $rand 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("net user um_share reset failed: " + ($netOut -join ' ')) }
    # run the wrapper batch through %ComSpec%; batch path has no spaces so
    # /d /c <path> is safe
    $action = New-ScheduledTaskAction -Execute "$env:ComSpec" -Argument ("/d /c " + $AsCmd) -WorkingDirectory $WorkDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    try {
        Register-ScheduledTask -TaskName $Name -Action $action -User 'DAICNT\um_share' -Password $rand `
            -Settings $settings -Description 'UM share one-shot (LSA password)' -Force -ErrorAction Stop | Out-Null
        Write-Log "registered task $Name (um_share, LSA password recipe)"
    } catch {
        throw ("Register-ScheduledTask($Name) failed: " + $_.Exception.Message)
    }
    try {
        Start-ScheduledTask -TaskName $Name -ErrorAction Stop | Out-Null
        Write-Log "started task $Name"
    } catch {
        throw ("Start-ScheduledTask($Name) failed: " + $_.Exception.Message)
    }
}

# Write a wrapper batch that sets PYTHONHOME for um_share and runs <Inner>, with
# ALL stdout/stderr captured into <RunDir>\raw.txt when RunDir is provided
# (captures even a Python bootstrap crash like "Failed to import encodings").
function New-UmCaptureScript {
    param([Parameter(Mandatory = $true)][string]$Label,
          [Parameter(Mandatory = $true)][string]$Inner,
          [string]$RunDir = '',
          [string]$ExtraEnv = '')
    $cmdPath = Join-Path $RunRoot ("run-{0}.cmd" -f $Label)
    $body = "@echo off`r`n" +
            "set `"PYTHONHOME=$PythonHome`"`r`n" +
            "set `"PYTHONDONTWRITEBYTECODE=1`"`r`n"
    if ($ExtraEnv) {
        $body += "$ExtraEnv`r`n"
    }
    if ($RunDir) {
        $raw = Join-Path $RunDir 'raw.txt'
        $body += "`"$appExe`" $Inner > `"$raw`" 2>&1`r`n"
    } else {
        $body += "`"$appExe`" $Inner`r`n"
    }
    $body += "exit /b %errorlevel%`r`n"
    [IO.File]::WriteAllText($cmdPath, $body, (New-Object Text.UTF8Encoding($false)))
    icacls $cmdPath /grant:r 'DAICNT\um_share:(RX)' | Out-Null
    Write-Log "wrote capture script $cmdPath"
    return $cmdPath
}

function Remove-UmTask {
    param([string]$Name)
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Log "removed task $Name"
    }
}

function Wait-UmResult {
    param([Parameter(Mandatory = $true)][string]$JsonPath, [int]$TimeoutSeconds = 60, [string]$GuardPidPath = $null)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $JsonPath) { return }
        Start-Sleep -Milliseconds 400
    }
    throw "timed out waiting for $JsonPath (task may have failed)"
}

function Get-AttributionKeyPath {
    # The ONE key file both ends point at: <RunRoot>\run\share-attribution.key.
    # run/ is the um_share-writable dir this deployment prepares; run\live is
    # NOT usable (removed and recreated by every deploy).
    return (Join-Path (Join-Path $RunRoot 'run') $AttributionKeyName)
}

function Get-AttributionKeyState {
    # Read-only presence/length check. NEVER a failure: a missing, unreadable or
    # too-short file is the explicit "no attribution" state (the share keeps
    # serving and every request is recorded unattributed), so this only reports.
    # The secret content is never logged - path, reason and stripped length only.
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ file_ready = $false; reason = 'missing'; chars = 0 }
    }
    try {
        # Read the bytes and decode WITHOUT preamble stripping: the runtime reads
        # this file with open(encoding="utf-8") (share_entry.load_attribution_key
        # and chat2api's verifier), where a BOM stays part of the secret and
        # counts toward the minimum length. The text cmdlet that decodes UTF8 would
        # drop the BOM under PowerShell 5.1, so the same file would read
        # file_ready/32 in the runtime and too_short/31 here (2026-09-26 review F10).
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        $raw = (New-Object System.Text.UTF8Encoding($false)).GetString($bytes)
    } catch {
        return @{ file_ready = $false; reason = 'unreadable'; chars = 0 }
    }
    $secret = if ($null -eq $raw) { '' } else { $raw.Trim() }
    if ($secret.Length -lt 32) {
        return @{ file_ready = $false; reason = 'too_short'; chars = $secret.Length }
    }
    return @{ file_ready = $true; reason = 'ok'; chars = $secret.Length }
}

function Write-AttributionKeyState {
    # Deployment-side visibility (2026-09-26): the deployment log alone must
    # answer "is attribution enabled", so the operator does not have to read the
    # 8100 startup banner out of run\live\raw.txt to tell "file not ready"
    # from "file ready but nothing attributed yet". Warning, never a failure.
    param([string]$Where = 'deploy')
    $path = Get-AttributionKeyPath
    $state = Get-AttributionKeyState -Path $path
    if ($state.file_ready) {
        Write-Log ("attribution key: FILE READY ($Where) - " + $path + " (" +
            $state.chars + " chars stripped; 8100 reads it once at startup, " +
            "8110 re-stats it per request)")
    } else {
        Write-Log ("WARN attribution key: NOT FILE READY ($Where) - reason=" +
            $state.reason + " file=" + $path +
            "; the share still starts and every request is recorded " +
            "unattributed; generation/rotation: SHARING.md")
    }
    return $state
}

# --- precise kill (image + owner must match the share) ----------------------
function Stop-ShareProcess {
    $status = Join-Path $RunRoot 'run\guard-status.json'
    if (Test-Path -LiteralPath $status) {
        $pidToKill = (Get-Content -LiteralPath $status -Raw | ConvertFrom-Json).pid
        if ($pidToKill) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidToKill" -ErrorAction SilentlyContinue
            if ($null -ne $proc) {
                $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction SilentlyContinue
                if (-not ($proc.ExecutablePath -and $proc.ExecutablePath.EndsWith('um-share-python.exe'))) {
                    Write-Log "WARN: pid $pidToKill image not um-share-python.exe ($($proc.ExecutablePath)); refusing to kill"
                } elseif (-not $owner -or $owner.User -ne 'um_share') {
                    Write-Log "WARN: pid $pidToKill owner not confirmed as um_share; refusing to kill"
                } else {
                    Stop-Process -Id $pidToKill -Force
                    Write-Log "stopped share pid $pidToKill (image+owner verified)"
                }
            } else {
                Write-Log "share pid $pidToKill already gone"
            }
        }
    }
}

# ===========================================================================
# Ensure the readable logs dir exists before anything else can write there.
New-Item -ItemType Directory -Path (Join-Path $ReadableDir 'logs') -Force | Out-Null
$ErrorFile = Join-Path $ReadableDir 'logs\last-error.txt'
try {
    Assert-Admin
    switch ($Stage) {
        'Prep' {
            $recoveries = @(Get-ChildItem -LiteralPath $RunRoot -Directory -Filter 'recovery-*' -ErrorAction SilentlyContinue)
            if (-not $recoveries) { throw 'no recovery backup under RunRoot; refusing to deploy' }
            Write-Log ('recovery backups: ' + (($recoveries | ForEach-Object Name) -join ', '))
            Copy-ShareSource
            $repairSource = Join-Path $DeployDir 'repair-limits.ps1'
            $repairTarget = Join-Path $RunRoot 'repair-limits.ps1'
            Copy-Item -LiteralPath $repairSource -Destination $repairTarget -Force
            if ((Get-FileHash -LiteralPath $repairSource).Hash -ne (Get-FileHash -LiteralPath $repairTarget).Hash) { throw 'repair helper copy hash mismatch' }
            Install-Limits
            if (-not (Test-LimitsInstalled)) { throw 'limit install verification FAILED after add' }
            Write-Log 'Prep OK: run copy + WFP filters + firewall rule installed and verified'
        }
        'Canary' {
            Copy-ShareSource
            if (-not (Test-LimitsInstalled)) {
                Write-Log 'limits missing; installing before canary'
                Install-Limits
                if (-not (Test-LimitsInstalled)) { throw 'limit verification failed before canary' }
            }
        # seed a user-profile canary (created then removed after the probe)
        $docs = Join-Path $env:USERPROFILE 'Documents\_um_share_canary.txt'
        Set-Content -LiteralPath $docs -Value 'um_share must not read this' -Encoding UTF8

        # 0) Python bootstrap self-test as um_share: isolates whether the
        #    dedicated image can even START Python (the 0x80070001 mystery).
        $selftestRun = Join-Path $RunRoot 'run\pyselftest'
        if (Test-Path -LiteralPath $selftestRun) { Remove-Item -LiteralPath $selftestRun -Recurse -Force }
        New-Item -ItemType Directory -Path $selftestRun -Force | Out-Null
        icacls $selftestRun /grant:r 'DAICNT\um_share:(OI)(CI)RW' | Out-Null
        $stCode = "import sys,os;open(r'$selftestRun\pyselftest.txt','w').write(sys.version+'|'+os.getcwd());print('PYOK',sys.version)"
        $stCmd = New-UmCaptureScript -Label 'pyselftest' -Inner ('-c "' + $stCode + '"') -RunDir $selftestRun
        Start-UmTask -Name 'umshare-pyselftest' -AsCmd $stCmd -WorkDir $RunRoot
        Wait-UmResult -JsonPath (Join-Path $selftestRun 'raw.txt') -TimeoutSeconds 30
        Remove-UmTask 'umshare-pyselftest'
        Copy-ReadableResult (Join-Path $selftestRun 'raw.txt') 'pyselftest-raw.txt'
        $stRaw = Get-Content -LiteralPath (Join-Path $selftestRun 'raw.txt') -Raw -ErrorAction SilentlyContinue
        Write-Log "== PYTHON SELFTEST RAW: $($stRaw -replace '\s+',' ')"
        if (-not (Test-Path -LiteralPath (Join-Path $selftestRun 'pyselftest.txt'))) {
            Write-Log 'py selftest wrote no marker -> python itself failed to bootstrap as um_share'
        }

        # 1) file/network boundary as um_share (no job needed), with cmd capture
        $canaryRun = Join-Path $RunRoot 'run\canary'
        if (Test-Path -LiteralPath $canaryRun) { Remove-Item -LiteralPath $canaryRun -Recurse -Force }
        New-Item -ItemType Directory -Path $canaryRun -Force | Out-Null
        icacls $canaryRun /grant:r 'DAICNT\um_share:(OI)(CI)RW' | Out-Null
        $canaryCmd = New-UmCaptureScript -Label 'canary' -Inner ('canary_um.py "' + $canaryRun + '"') -RunDir $canaryRun
        Start-UmTask -Name 'umshare-canary' -AsCmd $canaryCmd -WorkDir $RunRoot
        try {
            Wait-UmResult -JsonPath (Join-Path $canaryRun 'canary-results.json')
        } finally {
            # always mirror whatever the canary produced for diagnosis
            Get-ChildItem -LiteralPath $canaryRun -File -ErrorAction SilentlyContinue | ForEach-Object {
                Copy-ReadableResult $_.FullName ('canary-' + $_.Name)
            }
        }
        Remove-UmTask 'umshare-canary'
        # raw.txt carries the task's full stdout/stderr (even a boot crash)
        $canaryRaw = Join-Path $canaryRun 'raw.txt'
        if (Test-Path -LiteralPath $canaryRaw) {
            Write-Log '== CANARY RAW (stdout+stderr) =='
            Get-Content -LiteralPath $canaryRaw -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  c| $_" }
        }
        $canaryStdout = Join-Path $canaryRun 'canary-stdout.txt'
        if (Test-Path -LiteralPath $canaryStdout) {
            Write-Log '== CANARY STDOUT =='
            Get-Content -LiteralPath $canaryStdout -Encoding UTF8 | ForEach-Object { Write-Log "  c| $_" }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $canaryRun 'canary-results.json'))) {
            throw 'canary produced no results file (see mirrored canary-* logs)'
        }
        Copy-ReadableResult (Join-Path $canaryRun 'canary-results.json') 'canary-results.json'
        $canaryJson = Get-Content -LiteralPath (Join-Path $canaryRun 'canary-results.json') -Raw
        Write-Log "== CONCISE CANARY: $($canaryJson -replace '\s+',' ')"

        # 2) escape probe CONTROL (bare, as um_share): must be able to spawn
        $ectlRun = Join-Path $RunRoot 'run\escape-control'
        if (Test-Path -LiteralPath $ectlRun) { Remove-Item -LiteralPath $ectlRun -Recurse -Force }
        New-Item -ItemType Directory -Path $ectlRun -Force | Out-Null
        icacls $ectlRun /grant:r 'DAICNT\um_share:(OI)(CI)RW' | Out-Null
        $ectlCmd = New-UmCaptureScript -Label 'escctl' -Inner ('escape_probe.py "' + $ectlRun + '"') -RunDir $ectlRun
        Start-UmTask -Name 'umshare-escctl' -AsCmd $ectlCmd -WorkDir $RunRoot
        Wait-UmResult -JsonPath (Join-Path $ectlRun 'escape-results.json')
        Remove-UmTask 'umshare-escctl'
        Copy-ReadableResult (Join-Path $ectlRun 'escape-results.json') 'escape-control.json'
        Write-Log ("== CONCISE ESCAPE CONTROL: " +
            (((Get-Content -LiteralPath (Join-Path $ectlRun 'escape-results.json') -Raw) -replace '\s+', ' ')))

        # 3) escape probe UNDER JOB (guard): every spawn must fail
        $ejobRun = Join-Path $RunRoot 'run\escape-job'
        if (Test-Path -LiteralPath $ejobRun) { Remove-Item -LiteralPath $ejobRun -Recurse -Force }
        New-Item -ItemType Directory -Path $ejobRun -Force | Out-Null
        icacls $ejobRun /grant:r 'DAICNT\um_share:(OI)(CI)RW' | Out-Null
        $ejobCmd = New-UmCaptureScript -Label 'escjob' `
            -Inner ('share_guard.py --run-dir "' + $ejobRun + '" --child escape_probe.py --child-args "' + $ejobRun + '"') `
            -RunDir $ejobRun
        Start-UmTask -Name 'umshare-escjob' -AsCmd $ejobCmd -WorkDir $RunRoot
        try {
            Wait-UmResult -JsonPath (Join-Path $ejobRun 'escape-results.json') -TimeoutSeconds 90
        } catch {
            Write-Log 'escape-job produced NO results (wait timed out)'
        }
        Get-ChildItem -LiteralPath $ejobRun -File -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-ReadableResult $_.FullName ('escjob-' + $_.Name)
        }
        Remove-UmTask 'umshare-escjob'
        $ejRaw = Join-Path $ejobRun 'raw.txt'
        if (Test-Path -LiteralPath $ejRaw) {
            Write-Log '== ESCJOB RAW (guard+probe stdout/stderr) =='
            Get-Content -LiteralPath $ejRaw -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  j| $_" }
        }
        $ejStatus = Join-Path $ejobRun 'guard-status.json'
        if (Test-Path -LiteralPath $ejStatus) {
            Write-Log ("== ESCJOB GUARD-STATUS: " + ((Get-Content -LiteralPath $ejStatus -Raw -ErrorAction SilentlyContinue) -replace '\s+', ' '))
        }
        if (Test-Path -LiteralPath (Join-Path $ejobRun 'escape-results.json')) {
            Copy-ReadableResult (Join-Path $ejobRun 'escape-results.json') 'escape-job.json'
            Write-Log ("== CONCISE ESCAPE UNDER JOB: " +
                (((Get-Content -LiteralPath (Join-Path $ejobRun 'escape-results.json') -Raw) -replace '\s+', ' ')))
        } else {
            Write-Log 'CONCISE ESCAPE UNDER JOB: NO RESULTS (see escjob-* mirrors)'
        }

        Remove-Item -LiteralPath $docs -Force -ErrorAction SilentlyContinue
        Write-Log 'Canary stage complete'
    }
    'Start' {
        if (-not (Test-LimitsInstalled)) {
            Write-Log 'limits missing; refusing to start the share (fail closed)'
            throw 'limit verification failed before share start; NOT starting'
        }
        # Monitoring (SYSTEM watchdog) must be READY before the share is opened:
        # registering + starting the watchdog, then confirming it is Running,
        # then starting the share. No share without an armed watcher.
        $wdTask = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction SilentlyContinue
        if (-not $wdTask -or $wdTask.State -ne 'Running') {
            if ($wdTask) { Write-Log ('watchdog state=' + $wdTask.State) }
            throw 'umshare-watchdog not running; refusing to open the share (monitoring must be armed first)'
        }
        Write-Log 'watchdog armed (Running); proceeding to start share'
        # Broker expected-owner-SID refresh (ROOT CAUSE 2026-09-17, evidence:
        # broker diag stage identity-refused "peer owner mismatch"):
        # um_share was deleted and recreated at some point after install, so
        # its RID changed (1001 -> 1006) while the NAME stayed — the
        # install-time-baked --expect-owner-sid in run-broker.cmd is stale
        # and the identity verifier refuses EVERY peer forever. The account
        # NAME is the deployment fact; the SID must track the CURRENT
        # account, so the deploy refreshes the wrapper before restarting the
        # broker (admin F on RunRoot; the task re-runs this wrapper on every
        # start, so the fresh broker loads the refreshed expectation).
        $shareSidNow = (New-Object Security.Principal.NTAccount('DAICNT', 'um_share')).Translate([Security.Principal.SecurityIdentifier]).Value
        $rbPath = Join-Path $RunRoot 'run-broker.cmd'
        $rb = Get-Content -LiteralPath $rbPath -Raw
        $rbNew = [regex]::Replace($rb, '--expect-owner-sid\s+S-1-5-21-\S+',
                                  ('--expect-owner-sid ' + $shareSidNow))
        if ($rbNew -ne $rb) {
            Set-Content -LiteralPath $rbPath -Value $rbNew -Encoding ASCII
            Write-Log ("broker expect-owner-sid refreshed: " + $shareSidNow)
        } else {
            Write-Log 'broker expect-owner-sid already current'
        }
        # Broker restart + live-protocol verification (maintainer finding 4):
        # the running broker must have loaded the refreshed modules BEFORE
        # the share registers with it; a stale pipe is a fail-closed stop.
        Restart-BrokerAndVerify -BrokerTask $BrokerTask
        # Record the dedicated-backend permit port for the SYSTEM watchdog
        # (it re-reads this file each round to verify the WFP strong check).
        Set-Content -LiteralPath (Join-Path $RunRoot 'backend-port.txt') `
            -Value ([string]$ShareBackendPort) -Encoding ascii
        # one live share at a time
        Remove-UmTask 'umshare-live'
        Stop-ShareProcess
        $liveRun = Join-Path $RunRoot 'run\live'
        if (Test-Path -LiteralPath $liveRun) { Remove-Item -LiteralPath $liveRun -Recurse -Force }
        New-Item -ItemType Directory -Path $liveRun -Force | Out-Null
        # Modify (M = RWXD + delete), NOT RW: the gateway self-publishes its
        # registration via tmp+os.replace, and a rename needs DELETE on the
        # source file (or FILE_DELETE_CHILD on this directory). The inherited
        # RW ACE carries neither, so the rename died with Access Denied and
        # left the .tmp orphan (2026-09-17 21:52 production evidence).
        icacls $liveRun /grant:r 'DAICNT\um_share:(OI)(CI)M' | Out-Null
        # Instance channel wiring (2026-09-17 production gap): the gateway
        # self-publishes its registration ONLY when launched with the
        # instance-channel args (chat2api/share_entry.py:838 — key without
        # registration is a parser error). Without them the broker sees an
        # absent registration forever and EVERY publish answers unconfirmed,
        # so share.start's keys precondition can never pass. Same reviewed
        # DACL discipline as the D4 calibration (experiment-config-notes):
        # instance pipe = share GA + SYSTEM GRGW — the pipe is hosted by the
        # share child (um_share); the broker (SYSTEM) is its only client.
        $shareSid = (New-Object Security.Principal.NTAccount('DAICNT', 'um_share')).Translate([Security.Principal.SecurityIdentifier]).Value
        $instancePipeSddl = ('D:P(A;;GA;;;' + $shareSid + ')(A;;GRGW;;;SY)')
        $umShareArgs = ('--instance-key-file "' + (Join-Path $RunRoot 'instance-channel-key') +
            '" --instance-pipe-prefix "\\.\pipe\um-share-instance-" ' +
            '--registration-file "' + (Join-Path $liveRun 'instance-registration.json') +
            '" --instance-pipe-sddl "' + $instancePipeSddl + '"')
        $liveEnv = 'set "UM_SHARE_ARGS=' + $umShareArgs + '"' + "`r`n" +
            'set "SHARE_ATTRIBUTION_KEY_FILE=' + (Get-AttributionKeyPath) + '"'
        # Attribution key (2026-09-26): ONE independent `set` line, deliberately
        # NOT part of UM_SHARE_ARGS - the D4 experiment path compares that
        # string character-by-character against the config-derived one
        # (share_guard.py :427-431, tests/test_d4_assembly.py :227), so an extra
        # argument there would make D4 refuse to start. The env var is the
        # documented channel (share_entry.py :115, :1208-1212).
        # The line above is injected on EVERY deploy regardless of the key state;
        # the state line below is what makes "file not ready" visible here.
        [void](Write-AttributionKeyState -Where 'Start')
        Write-Log ("live wrapper: instance channel wired (prefix=" +
            '\\.\pipe\um-share-instance-' + ", sddl=" + $instancePipeSddl + ")")
        $liveCmd = New-UmCaptureScript -Label 'live' `
            -Inner ("share_guard.py --run-dir `"$liveRun`" --port 8100 " +
                    "--upstream-port $ShareBackendPort " +
                    $(if ($Mode -eq 'probe') { '--probe' }) ) `
            -RunDir $liveRun `
            -ExtraEnv $liveEnv
        Start-UmTask -Name 'umshare-live' -AsCmd $liveCmd -WorkDir $RunRoot

        # wait for listener on 8100
        $deadline = (Get-Date).AddSeconds(30)
        $sharePid = $null
        while ((Get-Date) -lt $deadline) {
            $conn = Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($conn) { $sharePid = $conn.OwningProcess; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $sharePid) { throw 'share did not start listening on 8100 within 30s' }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$sharePid" -ErrorAction Stop
        $imageOk = $proc.ExecutablePath -and $proc.ExecutablePath.EndsWith('um-share-python.exe')
        $ownerOk = $false
        $ownerLabel = 'unknown'
        try {
            $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
            $ownerLabel = "$($owner.Domain)\$($owner.User)"
            $ownerOk = $owner.User -eq 'um_share'
        } catch {
            Write-Log ('owner lookup failed: ' + $_.Exception.Message)
        }
        Write-Log "share listening: pid=$sharePid image=$($proc.ExecutablePath) owner=$ownerLabel"
        if (-not ($imageOk -and $ownerOk)) { throw "share identity check failed (image=$imageOk owner=$ownerLabel)" }

        # Limit-loss protection is now owned by the SYSTEM watchdog task
        # (already verified Running above); the share must NOT open if it is not
        # armed. This Start stage returns once the share is up and verified.
        Write-Log "share started in $Mode mode; SYSTEM watchdog (umshare-watchdog) is armed and monitoring; stop with Stage Stop"
        exit 0
    }
    'Watchdog' {
        # persistent SYSTEM watcher; its code + wfp_helper dependency live on the
        # protected run-copy path (um_share read/exec only; non-elevated users
        # cannot write there), never trusting user-writable tmp.
        $wdSrc = Join-Path $DeployDir 'watchdog-umshare.ps1'
        $wdPath = Join-Path $RunRoot 'watchdog-umshare.ps1'
        Copy-Item -LiteralPath $wdSrc -Destination $wdPath -Force
        Copy-Item -LiteralPath $WfpHelper -Destination (Join-Path $RunRoot 'wfp_helper.ps1') -Force
        $wdHash = (Get-FileHash -LiteralPath $wdPath -Algorithm SHA256).Hash
        $wdSrcHash = (Get-FileHash -LiteralPath $wdSrc -Algorithm SHA256).Hash
        if ($wdHash -ne $wdSrcHash) { throw 'watchdog copy hash mismatch' }
        Write-Log "watchdog script mirrored to protected path ($($wdHash.Substring(0,12))...)"
        # stop any OLD watchdog process first (precise command-line match) so a
        # stale watcher can never double-act or act with outdated logic
        try {
            $watchdogProcesses = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue)
            Select-ShareWatchdogProcesses -Processes $watchdogProcesses -WatchdogPath $wdPath -CurrentProcessId $PID |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        } catch { }
        try { Unregister-ScheduledTask -TaskName 'umshare-watchdog' -Confirm:$false -ErrorAction SilentlyContinue } catch { }
        # Write the dedicated-backend permit port BEFORE arming: the watcher's
        # FIRST limits round reads this file. An absent/stale file is now a
        # detection failure (backend-port-unavailable) -> the design-correct
        # limit-loss stop, which is exactly what killed the very first armed
        # attempt in ~11 s on 2026-09-17 20:54 - but the diagnosis names the
        # missing port file instead of pretending the limits were lost. There is no
        # fallback port (the private 8000 is never assumed).
        Set-Content -LiteralPath (Join-Path $RunRoot 'backend-port.txt') `
            -Value $ShareBackendPort -Encoding ASCII
        Write-Log "backend-port.txt written before arming (port $ShareBackendPort)"
        # Task Scheduler can retain a PATH from before PowerShell 7 was installed.
        # Register the actual host path instead of relying on that environment.
        $watchdogPowerShell = Join-Path $PSHOME 'pwsh.exe'
        if (-not (Test-Path -LiteralPath $watchdogPowerShell -PathType Leaf)) { throw 'PowerShell 7 host unavailable for watchdog' }
        $action = New-ScheduledTaskAction -Execute $watchdogPowerShell -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$wdPath`"") -WorkingDirectory $RunRoot
        $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        $bootTrigger = New-ScheduledTaskTrigger -AtStartup
        Register-ScheduledTask -TaskName 'umshare-watchdog' -Action $action -Principal $principal -Settings $settings -Trigger $bootTrigger -Description 'UM share limit watchdog (SYSTEM)' -Force | Out-Null
        Start-ScheduledTask -TaskName 'umshare-watchdog'
        $armedDeadline = (Get-Date).AddSeconds(10)
        do {
            $armed = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop
            if ($armed.State -eq 'Running') { break }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $armedDeadline)
        if ($armed.State -ne 'Running') {
            $taskInfo = Get-ScheduledTaskInfo -TaskName 'umshare-watchdog' -ErrorAction Stop
            throw ('watchdog failed to run (last result 0x{0:X8}); cannot proceed to Start' -f $taskInfo.LastTaskResult)
        }
        Write-Log ('watchdog SYSTEM task confirmed Running (host=' + $watchdogPowerShell + ')')
    }
    'DrainStop' {
        # Normal graceful stop = refuse new requests, wait for in-flight to
        # finish within a bounded deadline, THEN stop the process. That needs
        # the authorized runtime drain hook; killing the process directly
        # would fake a "drain complete" while in-flight requests die (R5-c).
        # The hook path is a server-side constant and lives OUTSIDE the
        # um_share-writable run\ dir (it must never be writable by the share
        # identity). Without the hook this stage REFUSES to execute: the
        # share is NOT stopped and nothing is claimed as drained.
        Write-Log 'DrainStop requested (graceful stop path)'
        $drainHook = Join-Path $RunRoot 'drain-control.ps1'
        if (-not (Test-Path -LiteralPath $drainHook)) {
            Write-Log 'drain hook not installed: refusing to kill the share; graceful drain NOT performed'
            throw 'DrainStop requires the authorized drain control hook (not installed in this offline phase). The share was NOT stopped; use Stage Stop for emergency/teardown.'
        }
        $out = & pwsh -NoProfile -ExecutionPolicy Bypass -File $drainHook 2>&1
        foreach ($line in $out) { Write-Log "  drain: $line" }
        if ($LASTEXITCODE -ne 0) {
            throw "drain hook failed (exit $LASTEXITCODE); share NOT stopped"
        }
        Stop-ShareProcess
        Remove-UmTask 'umshare-live'
        Write-Log 'DrainStop complete: bounded drain finished, share stopped, watchdog/limits retained'
    }
    'PublishProjection' {
        # Publishing a specific generation to the protected run copy and
        # receiving the share's confirmed applied generation requires the
        # authorized elevated projection writer. It is NOT part of the offline
        # phase and must fail closed rather than pretend success (R5).
        throw 'PublishProjection requires the authorized elevated projection writer (not installed in this offline phase)'
    }
    'Stop' {
        Stop-ShareProcess
        Remove-UmTask 'umshare-live'
        Remove-UmTask 'umshare-canary'
        Remove-UmTask 'umshare-escctl'
        Remove-UmTask 'umshare-escjob'
        try { Unregister-ScheduledTask -TaskName 'umshare-watchdog' -Confirm:$false -ErrorAction SilentlyContinue } catch { }
        Remove-Limits
        Write-Log 'Stop complete: share stopped, tasks removed, WFP filters + firewall rule removed'
    }
    }
    Write-Log "Stage $Stage done"
} catch {
    $detail = $_.Exception.ToString()
    Write-Log "FATAL: $detail"
    try { Set-Content -LiteralPath $ErrorFile -Value $detail -Encoding UTF8 } catch { }
    # surface to stderr so a UAC console/user can see it
    Write-Error $detail
    exit 1
}
