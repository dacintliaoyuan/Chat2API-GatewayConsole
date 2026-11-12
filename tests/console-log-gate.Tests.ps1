# Offline regression for the console LOG page (C4/C5): builds the candidate
# from the FINAL source (release/ is gitignored - the production dist/ is never
# written) and runs the console gate's log-page checks (copy precision during a
# background refresh, filter switching, duplicate events, clipboard refusal, raw
# folding, both themes, narrow viewport, classification colour bar, detail count).
# It also rebuilds both candidate roles around sentinels, so a local rebuild cannot
# silently delete the cloud candidate and a cloud rebuild cannot delete the local one.
# The fixture server binds a random loopback port only; no production port is
# touched and no model request is sent.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$console = Join-Path $root 'web'
$gate = Join-Path $console 'scripts\console-gate\run.mjs'
$check = Join-Path $console 'scripts\check-log-presentation.mjs'
$out = Join-Path (Split-Path -Parent $root) 'output\console-gate\hook-log-gate'

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw 'node was not found on PATH; the offline console log checks need it'
}
if (-not (Test-Path -LiteralPath $gate -PathType Leaf)) {
    throw "console gate entry is missing: $gate"
}

$script:failures = 0

function Assert-True {
    param([object]$Condition, [string]$Name)
    if ($Condition) { Write-Host "PASS $Name" -ForegroundColor Green; return }
    $script:failures++
    Write-Host "FAIL $Name" -ForegroundColor Red
}

function Invoke-ChildProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory)
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        Push-Location $WorkingDirectory
        try {
            $output = & $Executable @Arguments 2>&1 | Out-String
            $code = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Output = $output; ExitCode = [int]$code }
}

# 1. Pure-function checks (classification / real 0 vs unknown / cross-day ticks /
#    time-axis width / the C4 render-identity helpers). Seconds, no browser.
$pure = Invoke-ChildProcess 'node' @(
    '--experimental-strip-types', '--test', $check) $console
if ($pure.ExitCode -ne 0) { Write-Host $pure.Output }
Assert-True ($pure.ExitCode -eq 0) 'check-log-presentation (classification / 0-unknown / cross-day ticks)'

# 2. Type check, then the candidate build from the FINAL source.
$types = Invoke-ChildProcess 'npx' @(
    '--no-install', 'tsc', '--noEmit') $console
if ($types.ExitCode -ne 0) { Write-Host $types.Output }
Assert-True ($types.ExitCode -eq 0) 'console type check (tsc --noEmit)'

# 3. Cross-role rebuild isolation. The legacy default candidate is a local
#    console but writes the shared release/candidate root, so its rebuild must
#    keep the cloud role candidate in release/candidate/cloud. A candidate-cloud
#    rebuild must likewise keep the local role candidate in
#    release/candidate/local. Sentinels are the only files this test adds.
$candidateRoot = Join-Path $console 'release\candidate'
$cloudSentinel = Join-Path $candidateRoot 'cloud\console-log-gate-sentinel.txt'
$localSentinel = Join-Path $candidateRoot 'local\console-log-gate-sentinel.txt'
$sentinelText = "console-log-gate sentinel $([guid]::NewGuid().ToString('N'))"

function New-Sentinel {
    param([string]$Path)
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    [IO.File]::WriteAllText($Path, $sentinelText, [Text.UTF8Encoding]::new($false))
}

New-Sentinel $cloudSentinel

$build = Invoke-ChildProcess 'npx' @(
    '--no-install', 'vite', 'build', '--mode', 'candidate') $console
if ($build.ExitCode -ne 0) { Write-Host $build.Output }
Assert-True ($build.ExitCode -eq 0) 'candidate build (final source -> release/candidate)'
$cloudSentinelSurvived = (
    (Test-Path -LiteralPath $cloudSentinel -PathType Leaf) -and
    ((Get-Content -LiteralPath $cloudSentinel -Raw -Encoding utf8).Trim() -eq $sentinelText)
)
Assert-True $cloudSentinelSurvived 'default candidate rebuild keeps the cloud candidate (sentinel survived)'

if ($build.ExitCode -eq 0) {
    $gateRun = Invoke-ChildProcess 'node' @(
        'scripts/console-gate/run.mjs', '--only', 'logs',
        '--out', $out) $console
    $resultPath = Join-Path $out 'result.json'
    $failureCount = -1
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        try {
            $payload = Get-Content -LiteralPath $resultPath -Raw -Encoding utf8 | ConvertFrom-Json
            if ($null -ne $payload.failureCount) { $failureCount = [int]$payload.failureCount }
        } catch {
            $failureCount = -1
        }
    }
    if ($gateRun.ExitCode -ne 0 -or $failureCount -ne 0) {
        Write-Host $gateRun.Output
        if ($failureCount -gt 0) {
            $failureMessages = @($payload.failures | Select-Object -First 6)
            foreach ($failure in $failureMessages) { Write-Host ("  - " + $failure) -ForegroundColor Red }
        }
    }
    Assert-True ($gateRun.ExitCode -eq 0) 'console log page gate (copy / filter / raw fold / themes / narrow)'
    Assert-True ($failureCount -eq 0) 'console log page gate reported zero failures'
} else {
    Assert-True $false 'console log page gate (candidate build failed)'
}

New-Sentinel $localSentinel
$cloudBuild = Invoke-ChildProcess 'npx' @(
    '--no-install', 'vite', 'build', '--mode', 'candidate-cloud') $console
if ($cloudBuild.ExitCode -ne 0) { Write-Host $cloudBuild.Output }
Assert-True ($cloudBuild.ExitCode -eq 0) 'cloud candidate build (final source -> release/candidate/cloud)'
$localSentinelSurvived = (
    (Test-Path -LiteralPath $localSentinel -PathType Leaf) -and
    ((Get-Content -LiteralPath $localSentinel -Raw -Encoding utf8).Trim() -eq $sentinelText)
)
Assert-True $localSentinelSurvived 'cloud candidate rebuild keeps the local candidate (sentinel survived)'

Remove-Item -LiteralPath $cloudSentinel, $localSentinel -Force -ErrorAction SilentlyContinue

if ($script:failures -gt 0) {
    Write-Host ("console log gate: {0} failure(s)" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'console log gate: all checks passed' -ForegroundColor Green
exit 0
