# run-d4-experiment.ps1 - AUTHORIZED-PREVIEW-ONLY parameter guard for the
# future L2/D4 isolation experiment (R46.9/R49.6). This file IS NOT an
# experiment orchestrator: it implements NO E1-E4 stages, runs NO host
# action, and only validates that a future experiment invocation targets a
# strictly isolated namespace. The actual per-stage action/check/failure/
# recovery checklists live in handoff R50 (E1-E4 object table); the real
# orchestration entry will be delivered WITH the explicit L2 authorization
# and reviewed before any execution.
#
# Delivered status: written-but-not-executed. Guards encoded here:
#   1. Authorization switches are checked FIRST (-Acknowledged, -L2Authorized).
#   2. Every value must be EXPLICIT - no defaults, no production fallback.
#   3. Production namespace collisions (root/task/pipes/port) refuse; the
#      experiment root is RESOLVED and reparse points are refused.
#   4. -SourceRoot must exist and contain the broker server entry (the
#      future install's assembly is checked, not guessed).

param(
    [string]$ExperimentRoot = '',
    [string]$SourceRoot = '',
    [string]$TaskName = '',
    [string]$BrokerPipeName = '',
    [string]$InstancePipePrefix = '',
    [int]$SharePort = 0,
    [ValidateSet('E1', 'E2', 'E3', 'E4')]
    [string]$Stage = '',
    [switch]$Acknowledged,
    [switch]$L2Authorized,
    # R51.6: injectable ancestor-probe (offline tests substitute a fake;
    # production walks the REAL parent chain with Get-Item).
    [scriptblock]$ItemProbe = {
        param([string]$Path)
        return (Get-Item -LiteralPath $Path -Force)
    }
)

$ErrorActionPreference = 'Stop'

function Test-AncestorChainReparseFree {
    # R51.6: refuse when ANY ancestor (up to and including the drive root)
    # is a reparse point - a junction ABOVE the experiment root can
    # redirect every later action outside the namespace. $ItemProbe is
    # injectable for offline fixtures.
    param([string]$Path, [scriptblock]$ItemProbe)
    $current = $Path
    while ($true) {
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
        $probe = & $ItemProbe $current
        if ($null -ne $probe -and
                ($probe.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw ("run-d4-experiment: ancestor reparse point refused: " + $current)
        }
    }
}

# Guard 0: authorization FIRST - no parameter set can bypass consent.
if (-not $Acknowledged) {
    throw 'run-d4-experiment: -Acknowledged is REQUIRED (the operator must consciously target the experiment namespace; acknowledgement switch missing)'
}
if (-not $L2Authorized) {
    throw 'run-d4-experiment: L2 authorization NOT granted - this preview refuses to execute. Deliverable status: preview-only, no orchestration (R49.6).'
}

# Guard 1: explicit values - no defaults, no silent production fallback.
if (-not $ExperimentRoot) {
    throw 'run-d4-experiment: -ExperimentRoot is REQUIRED (refusing to guess any path)'
}
if (-not $SourceRoot) {
    throw 'run-d4-experiment: -SourceRoot is REQUIRED (the install source is part of the experiment object table)'
}
if (-not $TaskName) { throw 'run-d4-experiment: -TaskName is REQUIRED' }
if (-not $BrokerPipeName) { throw 'run-d4-experiment: -BrokerPipeName is REQUIRED' }
if (-not $InstancePipePrefix) { throw 'run-d4-experiment: -InstancePipePrefix is REQUIRED' }
if (-not $Stage) { throw 'run-d4-experiment: -Stage is REQUIRED (E1..E4)' }
# R51.6: the experiment port is a FIXED isolated configuration (not a
# free parameter that merely avoids three known ports).
if ($SharePort -ne 9100) {
    throw 'run-d4-experiment: -SharePort must be the fixed experiment port 9100'
}

# Guard 2: path normalization FIRST - the ROOT is resolved (no .. tricks),
# reparse points are refused on the root AND on every ANCESTOR (a junction
# above the root could redirect every later action outside it). Production
# collision checks run against the RESOLVED path.
if (-not (Test-Path -LiteralPath $ExperimentRoot)) {
    throw ("run-d4-experiment: experiment root must exist (isolated namespace): " + $ExperimentRoot)
}
$resolvedRoot = (Resolve-Path -LiteralPath $ExperimentRoot).Path
Test-AncestorChainReparseFree -Path $resolvedRoot -ItemProbe $ItemProbe
$rootItem = Get-Item -LiteralPath $resolvedRoot -Force
if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw ("run-d4-experiment: experiment root is a reparse point - refusing: " + $resolvedRoot)
}

# Guard 3: production namespace collisions on the RESOLVED values -
# explicit refusal, no fallback.
$forbidden = @(
    'C:\ProgramData\UMShareRun',
    'umshare-live', 'umshare-broker',
    '\\.\pipe\um-share-broker', '\\.\pipe\um-share-instance-'
)
foreach ($f in $forbidden) {
    if ($TaskName -eq $f -or $BrokerPipeName -eq $f -or $InstancePipePrefix -eq $f) {
        throw ("run-d4-experiment: experiment value collides with PRODUCTION namespace: " + $f)
    }
}
if ($resolvedRoot.StartsWith('C:\ProgramData\UMShareRun', [StringComparison]::OrdinalIgnoreCase)) {
    throw ("run-d4-experiment: experiment root collides with PRODUCTION namespace: " + $resolvedRoot)
}

# Guard 4: the source root must exist and contain the broker server entry.
if (-not (Test-Path -LiteralPath $SourceRoot)) {
    throw ("run-d4-experiment: source root must exist: " + $SourceRoot)
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'share/broker_server.py') -PathType Leaf)) {
    throw ("run-d4-experiment: source root does not contain the broker server entry: " + $SourceRoot)
}

Write-Output ("AUTHORIZED PREVIEW (no host action): experimentRoot=" + $resolvedRoot +
              " task=" + $TaskName +
              " brokerPipe=" + $BrokerPipeName +
              " instancePrefix=" + $InstancePipePrefix +
              " sharePort=" + $SharePort +
              " stage=" + $Stage +
              " sourceRoot=" + $SourceRoot)
Write-Output 'This entry is a PARAMETER GUARD ONLY: E1-E4 stage orchestration, failure exits and recovery checklists are specified in handoff R50 and remain WRITTEN-BUT-NOT-EXECUTED until L2 authorization.'
exit 0
