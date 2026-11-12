[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply')]
    [string]$Mode = 'Apply',
    [string]$StateDir,
    [switch]$SelfTest,
    [switch]$PublicSupervision,
    [int]$TimeoutSeconds = 840
)

# Thin entry for the dedicated share backend (8110) watch and the public Funnel
# supervision - contract share-resilience-contract-4 section 4.1. ALL decision
# logic lives in the two Python modules:
#   share_backend_supervision.py - desired intent, maintenance lease, broker
#                                 lifecycle, confirmations, hourly budget;
#   share_public_supervision.py  - public DNS/TLS rounds, the in-process 10 s
#                                 re-check, the rolling 30 minute budget and
#                                 the bounded mapping/TS recovery.
# SAFETY DEFAULT: the public lane is OFF. The registered task action passes no
# switch, so every run drives ONLY the backend (8110) supervision unless S7
# authorization created share-public-enabled.json in the watch/state directory, or
# an operator passes -PublicSupervision explicitly. When the lane is enabled, a
# missing public module is a fail-closed deployment error (exit 2), never a
# silently skipped half-run.
# This file resolves the gateway root through the shared runtime module, runs the
# selected CLIs with bounded wall clocks and merges their REAL exit codes (0 no
# action, 1 refused, 2 unconfirmed). It registers no task and stores no
# credential.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RuntimePath = Join-Path $PSScriptRoot 'gateway-runtime.ps1'
if (-not (Test-Path -LiteralPath $script:RuntimePath)) {
    throw "runtime module not found: $script:RuntimePath"
}
. $script:RuntimePath

# Frozen budgets (contract sections 2.3 / 4.1): backend 180 s (existing 8110
# supervision worst case ~140-150 s plus margin), public 600 s (module internal
# CYCLE_DEADLINE_S=570 s plus 30 s startup/finish), wrapper 840 s, task PT15M
# (900 s). A caller may override the total but never shrink the backend below
# its real worst case.
$script:BackendBudgetSeconds = 180
$script:PublicBudgetSeconds = 600
$script:OverheadSeconds = 15
$script:MinimumTotalSeconds = 195
$script:PublicEnableMarkerName = 'share-public-enabled.json'

function Get-SupervisionPython {
    # Same interpreter resolution as the other Python entries: an explicit
    # SELFUSEAI_PYTHON wins, otherwise the PATH python. Never a hardcoded
    # versioned path.
    $candidates = @()
    if ($env:SELFUSEAI_PYTHON) {
        $candidates += [string]$env:SELFUSEAI_PYTHON
    }
    $candidates += 'python'
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command -and $command.Source) { return $command.Source }
    }
    throw 'python interpreter not found (set SELFUSEAI_PYTHON or PATH)'
}

function Get-SupervisionScriptPath {
    # Both modules are fixed names under the gateway root; a missing module is a
    # fail-closed deployment error, never a silently skipped half-run.
    param([Parameter(Mandatory = $true)][string]$Name)
    $scriptPath = Join-Path (Get-GatewayRoot) $Name
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "supervision script not found: $scriptPath"
    }
    return $scriptPath
}

function Get-BackendSupervisionScript {
    return Get-SupervisionScriptPath -Name 'share_backend_supervision.py'
}

function Get-PublicSupervisionScript {
    return Get-SupervisionScriptPath -Name 'share_public_supervision.py'
}

function Get-PublicEnableMarkerPath {
    # The marker lives next to the public supervision state: an explicit
    # -StateDir (tests/diagnostics) or the module's own default watch dir.
    # With no LOCALAPPDATA there is no marker to find, so the lane stays off.
    if ($StateDir) {
        return (Join-Path ([System.IO.Path]::GetFullPath($StateDir)) `
            $script:PublicEnableMarkerName)
    }
    if (-not $env:LOCALAPPDATA) { return $null }
    return (Join-Path (Join-Path $env:LOCALAPPDATA 'SelfUseAI\ai-gateway\watch') `
        $script:PublicEnableMarkerName)
}

function Test-PublicSupervisionEnabled {
    # Default OFF. Only an explicit -PublicSupervision or an S7-authorized
    # share-public-enabled.json marker enables the public lane. Presence alone is
    # the authorization record; a malformed marker still counts as present, so a
    # corrupt marker can never silently enable or disable another lane.
    if ($PublicSupervision) { return $true }
    $marker = Get-PublicEnableMarkerPath
    if (-not $marker) { return $false }
    return (Test-Path -LiteralPath $marker -PathType Leaf)
}

function Format-ProcessArgument {
    # Quote one argument for ProcessStartInfo.Arguments. Paths may contain
    # spaces, Chinese characters or quotes; the child must receive the exact
    # value, never a split path.
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-SupervisionCli {
    # Run ONE CLI with a bounded wall clock and return its REAL exit code. A run
    # that exceeds the bound is killed and reported as unconfirmed (2); the
    # caller still runs the other module, so one hung child cannot hide the
    # other result. Start-Process -NoNewWindow -PassThru leaves ExitCode $null
    # after WaitForExit(ms) on this host ($null -> 0, observed 2026-10-04), so
    # the process is started through System.Diagnostics.Process directly.
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $python = Get-SupervisionPython
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    # StartInfo takes one argument string; quote every element individually.
    $parts = @('-B', (Format-ProcessArgument $ScriptPath))
    foreach ($argument in $Arguments) {
        $parts += (Format-ProcessArgument ([string]$argument))
    }
    $startInfo.Arguments = $parts -join ' '
    $startInfo.UseShellExecute = $false
    # CreateNoWindow stays $false: the child inherits the current (possibly
    # hidden) console exactly like the previous -NoNewWindow did, so its JSON
    # output is still captured by the caller and no window flashes.
    $startInfo.CreateNoWindow = $false
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $boundedMs = [Math]::Max(1, $TimeoutSeconds) * 1000
    try {
        if (-not $process.WaitForExit($boundedMs)) {
            try { $process.Kill() } catch { }
            Write-Host ('supervision run exceeded {0}s: child killed ({1})' -f
                $TimeoutSeconds, (Split-Path -Leaf $ScriptPath))
            return 2
        }
        return [int]$process.ExitCode
    }
    finally {
        try { $process.Dispose() } catch { }
    }
}

function Get-MergedExitCode {
    # Contract section 4.1 item 4: any 2 wins, then any 1, otherwise 0.
    # An unexpected non-zero code (a crashed interpreter) is a refusal (1),
    # never a silent success.
    param([int[]]$Codes)
    if ($Codes -contains 2) { return 2 }
    if ($Codes -contains 1) { return 1 }
    foreach ($code in $Codes) {
        if ($code -ne 0) { return 1 }
    }
    return 0
}

function Invoke-WatchRun {
    # Run the backend supervision first and, only when the public lane is
    # enabled, the public supervision, each within its own bounded wall clock.
    # Contract section 4.1 keeps the registered task backend-only by default;
    # the public lane needs share-public-enabled.json or -PublicSupervision and
    # there is no second scheduler.
    # A total budget that cannot cover the backend's real 180 s worst case plus
    # overhead is refused before ANY child starts, so the backend is never
    # silently shortened.
    if ($TimeoutSeconds -le $script:MinimumTotalSeconds) {
        $message = 'watch budget {0}s is insufficient: at least {1}s is required ' +
            '(180s backend + 15s overhead); no child was started'
        Write-Host ($message -f $TimeoutSeconds, $script:MinimumTotalSeconds)
        return 2
    }

    $backendScript = Get-BackendSupervisionScript
    $modeArgument = if ($Mode -eq 'Check') { '--check' } else { '--apply' }
    $backendArguments = @($modeArgument)
    $publicArguments = @($modeArgument)
    if ($StateDir) {
        $fullStateDir = [System.IO.Path]::GetFullPath($StateDir)
        $lockDir = Join-Path $fullStateDir 'locks'
        $backendArguments += @('--state-dir', $fullStateDir)
        $backendArguments += @('--lock-dir', $lockDir)
        $publicArguments += @('--state-dir', $fullStateDir)
        # A TEMP state dir also isolates the action lock: a test or diagnostic
        # run must never contend with the production share:orchestration lock.
        $publicArguments += @('--lock-dir', $lockDir)
    }

    $backendBudget = [Math]::Min($script:BackendBudgetSeconds, $TimeoutSeconds)
    $publicEnabled = Test-PublicSupervisionEnabled
    $publicScript = $null
    $publicBudget = 0
    if ($publicEnabled) {
        # Resolve the module before starting ANY child: an enabled lane with a
        # missing module is a fail-closed deployment error, never a half-run.
        # The public child keeps the module's internal CYCLE_DEADLINE_S=570 s
        # untouched: a caller override that leaves less than 600 s only bounds
        # the child earlier, it never shortens the module's own deadline.
        $publicScript = Get-PublicSupervisionScript
        $publicBudget = [Math]::Min($script:PublicBudgetSeconds,
            [Math]::Max(0, $TimeoutSeconds - $script:BackendBudgetSeconds -
                $script:OverheadSeconds))
        if ($publicBudget -lt 1) {
            $message = 'watch budget {0}s leaves no public supervision budget ' +
                '(backend {1}s + overhead {2}s); no child was started'
            Write-Host ($message -f $TimeoutSeconds,
                $script:BackendBudgetSeconds, $script:OverheadSeconds)
            return 2
        }
    }

    $backendCode = Invoke-SupervisionCli -ScriptPath $backendScript `
        -Arguments $backendArguments -TimeoutSeconds $backendBudget
    if (-not $publicEnabled) {
        # Even with the public lane off, an unexpected non-zero backend code
        # (a crashed interpreter) is a refusal (1), never a silent success.
        $mergedCode = Get-MergedExitCode -Codes @($backendCode)
        Write-Host ('watch-share-backend exit: backend={0} public=off merged={1}' -f
            $backendCode, $mergedCode)
        return $mergedCode
    }
    $publicCode = Invoke-SupervisionCli -ScriptPath $publicScript `
        -Arguments $publicArguments -TimeoutSeconds $publicBudget
    $mergedCode = Get-MergedExitCode -Codes @($backendCode, $publicCode)
    Write-Host ('watch-share-backend exit: backend={0} public={1} merged={2}' -f
        $backendCode, $publicCode, $mergedCode)
    return $mergedCode
}

function Invoke-WatchSelfTest {
    # Offline self-test: a TEMP LOCALAPPDATA sandbox plus each module's own
    # --selftest (TEMP dirs and injected stubs only). It never reads or writes
    # the production watch directory and never starts a service.
    $savedLocalAppData = $env:LOCALAPPDATA
    $sandbox = Join-Path $env:TEMP (
        'SelfUseAI-watch-share-backend-selftest-{0}' -f
        [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
    try {
        $env:LOCALAPPDATA = $sandbox
        $python = Get-SupervisionPython
        foreach ($scriptPath in @(
                (Get-BackendSupervisionScript),
                (Get-PublicSupervisionScript))) {
            $output = & $python -B $scriptPath --selftest 2>&1
            $code = $LASTEXITCODE
            foreach ($line in @($output)) { Write-Host $line }
            if ($code -ne 0) {
                throw ('watch-share-backend self-test failed (exit {0}): {1}' -f
                    $code, (Split-Path -Leaf $scriptPath))
            }
        }
    }
    finally {
        $env:LOCALAPPDATA = $savedLocalAppData
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host ('watch-share-backend public lane default: off (enable marker ' +
        '{0} or -PublicSupervision)' -f $script:PublicEnableMarkerName)
    $message = 'watch-share-backend self-test: PASS (default budget {0}s: ' +
        'backend {1}s, public {2}s when enabled)'
    Write-Host ($message -f $TimeoutSeconds,
        $script:BackendBudgetSeconds, $script:PublicBudgetSeconds)
    return 0
}

if ($SelfTest) {
    exit (Invoke-WatchSelfTest)
}

try {
    exit (Invoke-WatchRun)
}
catch {
    # Fail closed: a missing module or interpreter is never silently skipped.
    Write-Host $_.Exception.Message
    exit 2
}
