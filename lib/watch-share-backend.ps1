[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply')]
    [string]$Mode = 'Apply',
    [string]$StateDir,
    [switch]$SelfTest,
    [int]$TimeoutSeconds = 180
)

# Thin entry for the dedicated share backend (8110) watch - contract Rev9
# section 7.5. ALL decision logic (desired intent, maintenance lease, broker
# lifecycle, exit evidence, two confirmations, the hourly budget, the
# share:orchestration lock and the shared _ProductionBackendOps.start handler)
# lives in share_backend_supervision.py. This file resolves the gateway root
# through the shared runtime module, runs the CLI with the REAL exit code and
# never decides anything itself; it registers no task and stores no credential.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RuntimePath = Join-Path $PSScriptRoot 'gateway-runtime.ps1'
if (-not (Test-Path -LiteralPath $script:RuntimePath)) {
    throw "runtime module not found: $script:RuntimePath"
}
. $script:RuntimePath

function Get-SupervisionPython {
    # Same interpreter resolution as the other Python entries: an explicit
    # AIGATEWAY_PUBLIC_PYTHON wins, otherwise the PATH python. Never a hardcoded
    # versioned path.
    $candidates = @()
    if ($env:AIGATEWAY_PUBLIC_PYTHON) {
        $candidates += [string]$env:AIGATEWAY_PUBLIC_PYTHON
    }
    $candidates += 'python'
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command -and $command.Source) { return $command.Source }
    }
    throw 'python interpreter not found (set AIGATEWAY_PUBLIC_PYTHON or PATH)'
}

function Get-SupervisionScript {
    $scriptPath = Join-Path (Get-GatewayRoot) 'share_backend_supervision.py'
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "supervision script not found: $scriptPath"
    }
    return $scriptPath
}

function Invoke-SupervisionCli {
    # Run the CLI with a bounded wall clock and propagate its REAL exit code.
    # Start-Process -NoNewWindow -PassThru leaves ExitCode $null after
    # WaitForExit(ms) on this host ($null -> 0, observed 2026-10-04), which
    # would report every refusal as success; the process is therefore started
    # through System.Diagnostics.Process directly (the same bounded wait, with
    # the real exit code available). A run that exceeds the bound is killed and
    # reported as unconfirmed (2): the supervisor must never hang a
    # once-a-minute task.
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $python = Get-SupervisionPython
    $scriptPath = Get-SupervisionScript
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    # StartInfo takes one argument string; join exactly like Start-Process did.
    $startInfo.Arguments = (@('-B', ('"{0}"' -f $scriptPath)) + $Arguments) -join ' '
    $startInfo.UseShellExecute = $false
    # CreateNoWindow stays $false: the child inherits the current (possibly
    # hidden) console exactly like the previous -NoNewWindow did, so its JSON
    # output is still captured by the caller and no window flashes.
    $startInfo.CreateNoWindow = $false
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $boundedMs = [Math]::Max(1, $TimeoutSeconds) * 1000
    if (-not $process.WaitForExit($boundedMs)) {
        try { $process.Kill() } catch { }
        Write-Host ('supervision run exceeded {0}s: child killed, no service started' -f $TimeoutSeconds)
        exit 2
    }
    return [int]$process.ExitCode
}

function Invoke-WatchSelfTest {
    # Offline self-test: a TEMP LOCALAPPDATA sandbox plus the module's own
    # --selftest (TEMP dirs and injected stubs only). It never reads or
    # writes the production watch directory and never starts a process.
    $savedLocalAppData = $env:LOCALAPPDATA
    $sandbox = Join-Path $env:TEMP (
        'AIGatewayPublic-watch-share-backend-selftest-{0}' -f
        [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
    try {
        $env:LOCALAPPDATA = $sandbox
        $python = Get-SupervisionPython
        $scriptPath = Get-SupervisionScript
        $output = & $python -B $scriptPath --selftest 2>&1
        $code = $LASTEXITCODE
        foreach ($line in @($output)) { Write-Host $line }
        if ($code -ne 0) {
            throw ('watch-share-backend self-test failed (exit {0})' -f $code)
        }
    }
    finally {
        $env:LOCALAPPDATA = $savedLocalAppData
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host 'watch-share-backend self-test: PASS'
    return 0
}

if ($SelfTest) {
    exit (Invoke-WatchSelfTest)
}

$arguments = @()
if ($Mode -eq 'Check') { $arguments += '--check' } else { $arguments += '--apply' }
if ($StateDir) { $arguments += @('--state-dir', $StateDir) }
exit (Invoke-SupervisionCli -Arguments $arguments)
