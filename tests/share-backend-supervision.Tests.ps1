# Offline regression tests for the dedicated share-backend (8110) watch and the
# public Funnel supervision entries (contract share-resilience-contract-4
# sections 4.1 / 4.2 / 6.4):
#   lib/watch-share-backend.ps1           - gated dual-module watch entry
#   lib/register-share-backend-watch.ps1   - register entry, -SelfTest
#   lib/watch-share-backend-hidden.vbs     - hidden wscript launcher
#   lib/show-share-public-notification.ps1  - fixed-code balloon notification
# Everything runs in TEMP: the register entry is exercised only against copies
# with -WhatIf (no scheduled task is ever registered), the watch entry is run
# against TEMP fake modules, and the maintenance-gate/budget semantics run the
# backend module's own stubs in a TEMP sandbox. No production watch directory is
# read or written, no service is started or stopped and no notification is shown.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$watchScript = Join-Path $root 'lib\watch-share-backend.ps1'
$registerScript = Join-Path $root 'lib\register-share-backend-watch.ps1'
$hiddenLauncher = Join-Path $root 'lib\watch-share-backend-hidden.vbs'
$notificationScript = Join-Path $root 'lib\show-share-public-notification.ps1'
$supervisionModule = Join-Path $root 'share_backend_supervision.py'
$publicSupervisionModule = Join-Path $root 'share_public_supervision.py'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'python was not found on PATH; the offline supervision tests need it'
}

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
    Write-Host ("FAIL {0}: expected `$false, got {1}" -f $Name, $Condition) -ForegroundColor Red
}

function Assert-Contains {
    param([string]$Text, [string]$Needle, [string]$Name)
    if ($Text.Contains($Needle)) { Write-Host "PASS $Name" -ForegroundColor Green; return }
    $script:failures++
    Write-Host "FAIL $Name`: missing [$Needle]" -ForegroundColor Red
}

function Invoke-ChildProcess {
    # Run a child process with stderr captured and its REAL exit code kept.
    param([string]$Executable, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Output = $output; ExitCode = [int]$code }
}

# ===========================================================================
# 1. The fixed files exist; Register's fail-closed check requires the launcher.
# ===========================================================================
Assert-True (Test-Path -LiteralPath $watchScript -PathType Leaf) 'watch-share-backend.ps1 exists'
Assert-True (Test-Path -LiteralPath $registerScript -PathType Leaf) 'register-share-backend-watch.ps1 exists'
Assert-True (Test-Path -LiteralPath $hiddenLauncher -PathType Leaf) 'watch-share-backend-hidden.vbs exists'
Assert-True (Test-Path -LiteralPath $notificationScript -PathType Leaf) 'show-share-public-notification.ps1 exists'
Assert-True (Test-Path -LiteralPath $supervisionModule -PathType Leaf) 'share_backend_supervision.py exists'
Assert-True (Test-Path -LiteralPath $publicSupervisionModule -PathType Leaf) 'share_public_supervision.py exists'

# ===========================================================================
# 2. The hidden launcher keeps the watch-chat2api-hidden.vbs shape.
# ===========================================================================
$launcherText = [IO.File]::ReadAllText($hiddenLauncher)
Assert-Contains $launcherText 'watch-share-backend.ps1' 'the launcher runs watch-share-backend.ps1'
Assert-Contains $launcherText 'WScript.ScriptFullName' 'the launcher resolves its own folder (location-relative)'
Assert-Contains $launcherText 'Run(command, 0, True)' 'the launcher runs hidden (0) and waits (True) for the exit code'
Assert-Contains $launcherText 'WScript.Quit' 'the launcher returns the watch exit code to Task Scheduler'
Assert-False ($launcherText.Contains('watch-chat2api')) 'the launcher does not reference the chat2api watch'

# ===========================================================================
# 3. The full launcher chain with TEMP fake modules: the public lane is OFF by
# default (backend-only) and enabled only by share-public-enabled.json or
# -PublicSupervision. When enabled, both children run, their real exit codes are
# merged (any 2 wins, then any 1, otherwise 0), a TEMP state dir reaches both
# modules intact, repeated runs are stable and a hung public child is killed
# within its bounded budget. No production path is read or written.
# ===========================================================================
$chainRoot = Join-Path $env:TEMP ("share-backend-chain-" + [guid]::NewGuid().ToString('N'))
$chainLib = Join-Path $chainRoot 'lib'
New-Item -ItemType Directory -Path $chainLib -Force | Out-Null
try {
    foreach ($name in @('watch-share-backend-hidden.vbs', 'watch-share-backend.ps1', 'gateway-runtime.ps1')) {
        Copy-Item -LiteralPath (Join-Path $root ("lib\" + $name)) -Destination (Join-Path $chainLib $name)
    }
    Set-Content -LiteralPath (Join-Path $chainRoot 'share_backend_supervision.py') -Encoding ASCII -Value @'
import os, sys, time
print('fake backend argv=' + ' '.join(sys.argv[1:]))
time.sleep(float(os.environ.get('FAKE_BACKEND_SLEEP', '0')))
sys.exit(int(os.environ.get('FAKE_BACKEND_EXIT', '0')))
'@
    Set-Content -LiteralPath (Join-Path $chainRoot 'share_public_supervision.py') -Encoding ASCII -Value @'
import os, sys, time
print('fake public argv=' + ' '.join(sys.argv[1:]))
time.sleep(float(os.environ.get('FAKE_PUBLIC_SLEEP', '0')))
sys.exit(int(os.environ.get('FAKE_PUBLIC_EXIT', '0')))
'@
    $chainVbs = Join-Path $chainLib 'watch-share-backend-hidden.vbs'
    $chainWatch = Join-Path $chainLib 'watch-share-backend.ps1'
    $env:FAKE_BACKEND_EXIT = '0'
    $env:FAKE_PUBLIC_EXIT = '0'
    $env:FAKE_BACKEND_SLEEP = '0'
    $env:FAKE_PUBLIC_SLEEP = '0'
    # The default marker path is derived from LOCALAPPDATA; isolate it so a real
    # production marker can never turn the lane on inside this test.
    $savedChainLocalAppData = $env:LOCALAPPDATA
    $env:LOCALAPPDATA = $chainRoot

    $chainZero = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply')
    Assert-True ($chainZero.ExitCode -eq 0) ("the default watch runs backend-only and exits 0 (got {0})" -f $chainZero.ExitCode)
    Assert-Contains $chainZero.Output 'backend=0 public=off merged=0' 'the default watch reports the public lane as off'
    Assert-Contains $chainZero.Output 'fake backend argv=--apply' 'the default run runs the backend child'
    Assert-False ($chainZero.Output.Contains('fake public argv')) 'the default run never starts the public child'

    # The registered task action passes no switch: the public lane stays OFF
    # even when its fake module would fail. This is the safety default, not a
    # deployment error.
    $env:FAKE_PUBLIC_EXIT = '2'
    $defaultStateDir = Join-Path $chainRoot 'default state'
    New-Item -ItemType Directory -Path $defaultStateDir -Force | Out-Null
    $chainDefault = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply', '-StateDir', $defaultStateDir)
    Assert-True ($chainDefault.ExitCode -eq 0) ("the default run ignores a failing public child (got {0})" -f $chainDefault.ExitCode)
    Assert-Contains $chainDefault.Output 'backend=0 public=off merged=0' 'the default run reports the public lane as off'
    Assert-Contains $chainDefault.Output 'fake backend argv=--apply' 'the default run runs the backend child'
    Assert-False ($chainDefault.Output.Contains('fake public argv')) 'the default run never runs the public child'
    Assert-False ($chainDefault.Output.Contains('fake public argv=--apply')) 'a failing public child is never started by default'

    # Marker ON: share-public-enabled.json in the state dir enables the lane;
    # both children run and their real exit codes are merged.
    $publicMarker = Join-Path $defaultStateDir 'share-public-enabled.json'
    Set-Content -LiteralPath $publicMarker -Encoding ASCII -Value '{"authorized":"S7"}'
    $chainMarker = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply', '-StateDir', $defaultStateDir)
    Assert-True ($chainMarker.ExitCode -eq 2) ("the marker enables the public child and exits 2 (got {0})" -f $chainMarker.ExitCode)
    Assert-Contains $chainMarker.Output 'backend=0 public=2 merged=2' 'the marker run reports both child codes'
    Assert-Contains $chainMarker.Output 'fake backend argv=--apply' 'the marker run runs the backend child'
    Assert-Contains $chainMarker.Output 'fake public argv=--apply' 'the marker run runs the public child'
    Remove-Item -LiteralPath $publicMarker -Force

    # Explicit -PublicSupervision enables the lane without a marker.
    $chainSwitch = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply', '-StateDir', $defaultStateDir, '-PublicSupervision')
    Assert-True ($chainSwitch.ExitCode -eq 2) ("the switch enables the public child and exits 2 (got {0})" -f $chainSwitch.ExitCode)
    Assert-Contains $chainSwitch.Output 'backend=0 public=2 merged=2' 'the switch run reports both child codes'
    Assert-Contains $chainSwitch.Output 'fake public argv=--apply' 'the switch run runs the public child'
    $env:FAKE_PUBLIC_EXIT = '0'
    $vbsZero = Invoke-ChildProcess 'cscript' @('//nologo', $chainVbs)
    Assert-True ($vbsZero.ExitCode -eq 0) ("the hidden launcher propagates exit 0 (got {0})" -f $vbsZero.ExitCode)

    # A crashed child is never folded into success: an unexpected non-zero
    # code is a refusal (1) and the enabled public module still runs.
    $env:FAKE_BACKEND_EXIT = '7'
    $chainSeven = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply', '-PublicSupervision')
    Assert-True ($chainSeven.ExitCode -eq 1) ("an unexpected backend exit 7 becomes refusal 1 (got {0})" -f $chainSeven.ExitCode)
    Assert-Contains $chainSeven.Output 'fake public argv=--apply' 'the enabled public child still runs after a backend failure'
    $vbsSeven = Invoke-ChildProcess 'cscript' @('//nologo', $chainVbs)
    Assert-True ($vbsSeven.ExitCode -eq 1) ("the hidden launcher propagates the merged refusal (got {0})" -f $vbsSeven.ExitCode)

    $env:FAKE_BACKEND_EXIT = '1'
    $chainOne = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply')
    Assert-True ($chainOne.ExitCode -eq 1) ("a refused backend keeps exit 1 (got {0})" -f $chainOne.ExitCode)

    $env:FAKE_BACKEND_EXIT = '0'
    $env:FAKE_PUBLIC_EXIT = '2'
    $chainTwo = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Apply', '-PublicSupervision')
    Assert-True ($chainTwo.ExitCode -eq 2) ("an unconfirmed public child wins exit 2 (got {0})" -f $chainTwo.ExitCode)

    $env:FAKE_PUBLIC_EXIT = '0'
    $stateDir = Join-Path $chainRoot 'state with space'
    $chainState = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Check', '-StateDir', $stateDir, '-TimeoutSeconds', '300',
        '-PublicSupervision')
    Assert-True ($chainState.ExitCode -eq 0) ("a TEMP StateDir is passed through (got {0})" -f $chainState.ExitCode)
    # Per-child argv: BOTH modules must receive the isolated --lock-dir (the
    # backend also uses share:orchestration); a merged-output match alone
    # cannot prove that.
    Assert-Contains $chainState.Output ('fake backend argv=--check --state-dir {0} --lock-dir {0}\locks' -f $stateDir) 'the backend child receives --state-dir and --lock-dir'
    Assert-Contains $chainState.Output ('fake public argv=--check --state-dir {0} --lock-dir {0}\locks' -f $stateDir) 'the public child receives --state-dir and --lock-dir'
    Assert-Contains $chainState.Output 'state with space' 'the state dir with a space reaches the child intact'

    $chainRepeat = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Check', '-StateDir', $stateDir, '-TimeoutSeconds', '300',
        '-PublicSupervision')
    Assert-True ($chainRepeat.ExitCode -eq 0) ("a repeated run stays idempotent (got {0})" -f $chainRepeat.ExitCode)

    # 196 s total leaves 1 s for the public child: the module's own 570 s
    # deadline is never shortened, the child is killed at its wrapper bound.
    $env:FAKE_PUBLIC_SLEEP = '5'
    $chainTimeout = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Check', '-TimeoutSeconds', '196', '-PublicSupervision')
    Assert-True ($chainTimeout.ExitCode -eq 2) ("a hung public child exits 2, never 0 (got {0})" -f $chainTimeout.ExitCode)
    Assert-Contains $chainTimeout.Output 'child killed (share_public_supervision.py)' 'the timeout names the killed public child'

    # 195 s cannot cover 180 s backend + 15 s overhead: no child starts.
    $chainBudget = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Check', '-TimeoutSeconds', '195')
    Assert-True ($chainBudget.ExitCode -eq 2) ("a 195 s total budget is refused (got {0})" -f $chainBudget.ExitCode)
    Assert-Contains $chainBudget.Output 'insufficient' 'the refusal explains the insufficient budget'
    Assert-False ($chainBudget.Output.Contains('fake backend')) 'no child starts with an insufficient total budget'

    # The floor is checked BEFORE any child starts even on the default task
    # path: 100 s must never silently truncate the backend's 180 s worst case.
    $chainLowBudget = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $chainWatch,
        '-Mode', 'Check', '-TimeoutSeconds', '100')
    Assert-True ($chainLowBudget.ExitCode -eq 2) ("a 100 s default budget is refused (got {0})" -f $chainLowBudget.ExitCode)
    Assert-Contains $chainLowBudget.Output 'insufficient' 'the default low-budget refusal explains the budget'
    Assert-False ($chainLowBudget.Output.Contains('fake backend')) 'no child starts with a low default budget'

    # A missing public module is a fail-closed deployment error when the lane
    # is enabled, never a silently skipped half-run; the same missing module is
    # irrelevant while the default is off.
    $missingRoot = Join-Path $env:TEMP ("share-backend-missing-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path (Join-Path $missingRoot 'lib') -Force | Out-Null
    try {
        foreach ($name in @('watch-share-backend.ps1', 'gateway-runtime.ps1')) {
            Copy-Item -LiteralPath (Join-Path $root ("lib\" + $name)) -Destination (Join-Path $missingRoot ("lib\" + $name))
        }
        Copy-Item -LiteralPath (Join-Path $chainRoot 'share_backend_supervision.py') -Destination (Join-Path $missingRoot 'share_backend_supervision.py')
        $missingDefault = Invoke-ChildProcess 'pwsh.exe' @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $missingRoot 'lib\watch-share-backend.ps1'),
            '-Mode', 'Check', '-TimeoutSeconds', '300')
        Assert-True ($missingDefault.ExitCode -eq 0) ("a missing public module is ignored while the lane is off (got {0})" -f $missingDefault.ExitCode)
        Assert-Contains $missingDefault.Output 'public=off' 'the default run reports the public lane as off'
        $missing = Invoke-ChildProcess 'pwsh.exe' @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $missingRoot 'lib\watch-share-backend.ps1'),
            '-Mode', 'Check', '-TimeoutSeconds', '300', '-PublicSupervision')
        Assert-True ($missing.ExitCode -eq 2) ("an enabled lane with a missing public module exits 2 (got {0})" -f $missing.ExitCode)
        Assert-Contains $missing.Output 'share_public_supervision.py' 'the refusal names the fixed public module path'
    } finally {
        Remove-Item -LiteralPath $missingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $chainRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:FAKE_BACKEND_EXIT -ErrorAction SilentlyContinue
    Remove-Item Env:FAKE_PUBLIC_EXIT -ErrorAction SilentlyContinue
    Remove-Item Env:FAKE_BACKEND_SLEEP -ErrorAction SilentlyContinue
    Remove-Item Env:FAKE_PUBLIC_SLEEP -ErrorAction SilentlyContinue
    $env:LOCALAPPDATA = $savedChainLocalAppData
}

# ===========================================================================
# 4. Both entries' -SelfTest pass (child processes, TEMP isolation). The watch
# self-test runs BOTH modules' frozen self-tests; the register self-test asserts
# the PT15M task limit.
# ===========================================================================
$watchSelfTest = Invoke-ChildProcess 'pwsh.exe' @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watchScript, '-SelfTest')
Assert-True ($watchSelfTest.ExitCode -eq 0) ("watch-share-backend -SelfTest exits 0 (got {0})" -f $watchSelfTest.ExitCode)
Assert-Contains $watchSelfTest.Output 'watch-share-backend self-test: PASS' 'watch -SelfTest prints PASS'
Assert-Contains $watchSelfTest.Output 'share-public-supervision self-test: PASS' 'watch -SelfTest runs the public module self-test'
Assert-Contains $watchSelfTest.Output 'default budget 840s' 'watch -SelfTest reports the 840 s default budget'
Assert-Contains $watchSelfTest.Output 'public lane default: off' 'watch -SelfTest reports the public lane default as off'

$registerSelfTest = Invoke-ChildProcess 'pwsh.exe' @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $registerScript, '-SelfTest')
Assert-True ($registerSelfTest.ExitCode -eq 0) ("register-share-backend-watch -SelfTest exits 0 (got {0})" -f $registerSelfTest.ExitCode)
Assert-Contains $registerSelfTest.Output 'register-share-backend-watch self-test: PASS' 'register -SelfTest prints PASS'
Assert-Contains $registerSelfTest.Output 'the principal is never SYSTEM' 'register -SelfTest asserts the non-SYSTEM principal'
Assert-Contains $registerSelfTest.Output 'PT15M' 'register -SelfTest asserts the PT15M task limit'
Assert-Contains $registerSelfTest.Output 'the task description states public supervision is off by default' 'register -SelfTest asserts the default-off description'
Assert-Contains $registerSelfTest.Output 'the task description names the S7 enable marker' 'register -SelfTest asserts the S7 marker description'
Assert-Contains $registerSelfTest.Output 'the task description requires re-registration' 'register -SelfTest asserts the re-registration description'

# ===========================================================================
# 5. Register fails closed when the hidden launcher is missing.
# The fake-path injection copies the entry next to a TEMP PSScriptRoot so the
# fixed launcher path resolves inside the TEMP copy; -WhatIf guarantees that a
# broken fail-closed branch could never register a real scheduled task.
# ===========================================================================
$injectRoot = Join-Path $env:TEMP ("share-backend-register-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $injectRoot -Force | Out-Null
try {
    $injectedRegister = Join-Path $injectRoot 'register-share-backend-watch.ps1'
    Copy-Item -LiteralPath $registerScript -Destination $injectedRegister
    Copy-Item -LiteralPath $watchScript -Destination (Join-Path $injectRoot 'watch-share-backend.ps1')

    $missing = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $injectedRegister,
        '-Action', 'Register', '-WhatIf')
    Assert-False ($missing.ExitCode -eq 0) 'register fails closed when the hidden launcher is missing (non-zero exit)'
    Assert-Contains $missing.Output 'hidden launcher not found' 'the refusal names the missing hidden launcher'
    Assert-Contains $missing.Output 'watch-share-backend-hidden.vbs' 'the refusal names the fixed launcher path'

    # Positive control: the same injected copy with a launcher present gets past
    # the check and stops at -WhatIf (nothing is registered either way).
    Set-Content -LiteralPath (Join-Path $injectRoot 'watch-share-backend-hidden.vbs') -Value "' stub launcher" -Encoding ASCII
    $present = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $injectedRegister,
        '-Action', 'Register', '-WhatIf')
    Assert-True ($present.ExitCode -eq 0) ("register passes the launcher check when it exists (got {0})" -f $present.ExitCode)
    Assert-False ($present.Output.Contains('hidden launcher not found')) 'no missing-launcher refusal with the launcher present'
    Assert-True ($present.Output -match 'WhatIf|What if') '-WhatIf keeps the positive control from registering anything'
} finally {
    Remove-Item -LiteralPath $injectRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# ===========================================================================
# 6. Watch entry wiring: a safety-default gate (backend-only unless the S7
# marker or -PublicSupervision enables the lane), no decision logic of its own,
# real exit codes, and the maintenance gate + budgets live in the Python modules.
# ===========================================================================
$watchText = [IO.File]::ReadAllText($watchScript)
Assert-Contains $watchText 'share_backend_supervision.py' 'the watch entry runs share_backend_supervision.py'
Assert-Contains $watchText 'share_public_supervision.py' 'the watch entry runs share_public_supervision.py'
Assert-Contains $watchText 'function Get-BackendSupervisionScript' 'the watch resolves the backend module by fixed name'
Assert-Contains $watchText 'function Get-PublicSupervisionScript' 'the watch resolves the public module by fixed name'
Assert-Contains $watchText '$TimeoutSeconds = 840' 'the watch default budget is 840 seconds'
Assert-Contains $watchText '$script:BackendBudgetSeconds = 180' 'the backend child budget is 180 seconds'
Assert-Contains $watchText '$script:PublicBudgetSeconds = 600' 'the public child budget is 600 seconds'
Assert-Contains $watchText '$script:MinimumTotalSeconds = 195' 'the minimum total budget is 195 seconds'
Assert-Contains $watchText 'Get-MergedExitCode' 'the watch merges both child exit codes'
Assert-Contains $watchText 'exit (Invoke-WatchRun)' 'the watch entry exits with the merged exit code'
Assert-Contains $watchText '--selftest' 'the watch -SelfTest runs both modules frozen self-tests'
Assert-Contains $watchText "'--check'" 'Check mode maps to --check'
Assert-Contains $watchText "'--apply'" 'Apply mode maps to --apply'
Assert-Contains $watchText "'--state-dir'" 'StateDir is passed through as --state-dir'
Assert-Contains $watchText "'--lock-dir'" 'a TEMP StateDir also isolates the action lock'
Assert-Contains $watchText 'function Test-PublicSupervisionEnabled' 'the watch entry has a public-lane opt-in gate'
Assert-Contains $watchText 'share-public-enabled.json' 'the watch entry depends on the S7 enable marker'
Assert-Contains $watchText '[switch]$PublicSupervision' 'the watch entry accepts the explicit -PublicSupervision switch'
Assert-Contains $watchText 'public=off' 'the watch entry names the backend-only default in its output'
Assert-Contains $watchText 'if (-not $publicEnabled)' 'the watch entry skips the public child by default'
Assert-False ($watchText.Contains('Register-ScheduledTask')) 'the watch entry never registers a scheduled task'
Assert-False ($watchText.Contains('Unregister-ScheduledTask')) 'the watch entry never unregisters a scheduled task'
Assert-False ($watchText.Contains('Start-ScheduledTask')) 'the watch entry never starts a scheduled task'
Assert-False ($watchText.Contains('taskkill')) 'the watch entry never kills by PID'
Assert-False ($watchText.Contains('MAX_STARTS_PER_HOUR')) 'the watch entry does not re-implement the backend budget'
Assert-False ($watchText.Contains('maintenance_in_force')) 'the watch entry does not re-implement the maintenance gate'
Assert-False ($watchText.Contains('MAPPING_LIMIT')) 'the watch entry does not re-implement the public budget'

$moduleText = [IO.File]::ReadAllText($supervisionModule)
Assert-Contains $moduleText 'maintenance_in_force' 'the backend module owns the maintenance gate (maintenance_in_force)'
Assert-Contains $moduleText 'maintenance_lease_expired' 'the read-only check reports an expired lease (maintenance_lease_expired)'
Assert-Contains $moduleText '_end_maintenance' 'the backend module owns the single maintenance-end'
Assert-Contains $moduleText 'budget_exhausted' 'the backend module owns the hourly budget (budget_exhausted)'
Assert-Contains $moduleText 'MAX_STARTS_PER_HOUR' 'the backend module owns the hourly budget constant'
Assert-Contains $moduleText 'awaiting_confirmation' 'the backend module owns the two-confirmation rule'

$publicModuleText = [IO.File]::ReadAllText($publicSupervisionModule)
Assert-Contains $publicModuleText 'CONFIRM_DELAY_S = 10.0' 'the public module owns the in-process 10 second re-check'
Assert-Contains $publicModuleText 'BUDGET_WINDOW_S = 1800.0' 'the public module owns the rolling 30 minute budget'
Assert-Contains $publicModuleText 'MAPPING_LIMIT = 2' 'the public module owns the two-mapping limit'
Assert-Contains $publicModuleText 'TS_RESTART_LIMIT = 1' 'the public module owns the one-restart limit'
Assert-Contains $publicModuleText 'show-share-public-notification.ps1' 'the public module calls the fixed notification script'
Assert-False ($publicModuleText.Contains('Register-ScheduledTask')) 'the public module registers no scheduled task'

# ===========================================================================
# 7. Check mode against a TEMP state dir: no intent refuses read-only, and the
# enabled public lane runs with its own bounded budget.
# ===========================================================================
$sandbox = Join-Path $env:TEMP ("share-backend-check-" + [guid]::NewGuid().ToString('N'))
$stateDir = Join-Path $sandbox 'state'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$savedLocalAppData = $env:LOCALAPPDATA
try {
    # LOCALAPPDATA is redirected so a resolver slip can never reach the
    # production watch directory.
    $env:LOCALAPPDATA = $sandbox
    $check = Invoke-ChildProcess 'pwsh.exe' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watchScript,
        '-Mode', 'Check', '-StateDir', $stateDir, '-TimeoutSeconds', '300',
        '-PublicSupervision')
} finally {
    $env:LOCALAPPDATA = $savedLocalAppData
}
Assert-True ($check.ExitCode -eq 1) ("Check with no intent refuses with exit 1 (got {0})" -f $check.ExitCode)
Assert-Contains $check.Output '"code": "desired_state_unknown"' 'the backend refusal is desired_state_unknown'
Assert-Contains $check.Output '"code": "intent_unknown"' 'the public check refuses an unknown intent'
Assert-False (Test-Path -LiteralPath (Join-Path $stateDir 'share-backend-watch-state.json')) 'Check writes no watch state (read-only)'
Assert-False (Test-Path -LiteralPath (Join-Path $stateDir 'share-backend-watch.log')) 'Check writes no decision log (read-only)'
Assert-False (Test-Path -LiteralPath (Join-Path $stateDir 'share-public-state.json')) 'public Check writes no public state (read-only)'
Assert-False (Test-Path -LiteralPath (Join-Path $stateDir 'share-public-budget.json')) 'public Check writes no budget (read-only)'

# A stopped intent for the current boot is refused the same way.
$writeDesired = Join-Path $sandbox 'write-desired.py'
Set-Content -LiteralPath $writeDesired -Encoding ASCII -Value @'
import sys
sys.path.insert(0, sys.argv[1])
import share_backend_supervision as m
sys.exit(0 if m.write_desired_state("stopped", source="ps-suite",
                                     path=sys.argv[2]) else 3)
'@
$writer = Invoke-ChildProcess 'python' @('-B', $writeDesired, $root, (Join-Path $stateDir 'share-backend-desired.json'))
Assert-True ($writer.ExitCode -eq 0) ("the stopped intent marker was written for the sandbox (got {0})" -f $writer.ExitCode)
$checkStopped = Invoke-ChildProcess 'pwsh.exe' @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watchScript,
    '-Mode', 'Check', '-StateDir', $stateDir, '-TimeoutSeconds', '300')
Assert-True ($checkStopped.ExitCode -eq 1) ("a stopped intent refuses with exit 1 (got {0})" -f $checkStopped.ExitCode)
Assert-Contains $checkStopped.Output '"code": "desired_stopped"' 'a stopped intent is never auto-started'
Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

# ===========================================================================
# 8. Maintenance gate + budget semantics (module stubs, TEMP sandbox only).
# ===========================================================================
$harnessDir = Join-Path $env:TEMP ("share-backend-gates-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $harnessDir -Force | Out-Null
$harnessScript = Join-Path $harnessDir 'gates.py'
Set-Content -LiteralPath $harnessScript -Encoding ASCII -Value @'
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import share_backend_supervision as m

failures = []


def expect(condition, why):
    if condition:
        print("PASS " + why)
    else:
        failures.append(why)
        print("FAIL " + why)


boot = "ps-suite-boot"
m._BOOT_ID_READER = lambda: boot
m._DESIRED_STOP_READER = lambda: None

root = tempfile.mkdtemp(prefix="SelfUseAI-ps-supervision-")
try:
    state_dir = os.path.join(root, "state")
    os.makedirs(state_dir, exist_ok=True)
    desired = os.path.join(state_dir, m.DESIRED_FILE_NAME)
    state = os.path.join(state_dir, m.STATE_FILE_NAME)
    service_log = os.path.join(root, m.SERVICE_LOG_NAME)
    lifecycle_log = os.path.join(root, m.LIFECYCLE_LOG_NAME)

    m.write_desired_state("running", source="ps-suite", path=desired,
                          boot_id=boot)

    def check(now, ops, broker):
        return m.check_preconditions(now=now, ops=ops, broker=broker,
                                     state_dir=state_dir,
                                     log_path=service_log)

    # Maintenance gate: a live lease refuses and is never ended.
    broker = m._StubBroker()
    broker.status_result = m._StubResult(True, {
        "checked_at": 1000, "lease": {"lease_id": "a" * 32,
                                     "component": "share_gateway",
                                     "expires_at": 2000},
        "lease_expired": False, "instance": None, "last_outcome": None})
    result = check(1000.0, m._StubOps(), broker)
    expect(result["reason"] == "maintenance_in_force",
           "live maintenance lease refuses (maintenance_in_force)")
    expect(broker.end_calls == [], "live lease is never maintenance-ended")

    # Maintenance gate: an expired lease is REPORTED by the read-only check
    # without ending it - the ONE maintenance-end runs in the APPLY path inside
    # the share:orchestration lock (contract section 7.2 step 2 / 7.3).
    broker.status_result = m._StubResult(True, {
        "checked_at": 1000, "lease": {"lease_id": "b" * 32,
                                     "component": "share_gateway",
                                     "expires_at": 999},
        "lease_expired": True, "instance": None, "last_outcome": None})
    result = check(1000.0, m._StubOps(), broker)
    expect(result["reason"] == "maintenance_lease_expired",
           "expired lease refuses read-only (maintenance_lease_expired)")
    expect(broker.end_calls == [],
           "the read-only check never ends an expired lease")

    # Budget: no lease; the instance is confirmed gone with abrupt evidence.
    broker.status_result = m._StubResult(True, {
        "checked_at": 1000, "lease": None, "lease_expired": None,
        "instance": None, "last_outcome": None})
    ops = m._StubOps()
    ops.pid_state = {"pid_file": "x", "pid_file_present": False,
                     "pid": None, "pid_matches": None,
                     "port_listening": False, "state": "pid_file_absent",
                     "identity": "unknown", "identity_reason": "pid_file_absent"}
    ops.alive = False
    Path(service_log).write_text(
        "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
        "port=%d pid=4242\n" % m.BACKEND_PORT, encoding="utf-8")
    Path(lifecycle_log).write_text(
        "2026-10-03 09:02:00 force stop pid=4242 port=%d "
        "by=taskkill result=dead\n" % m.BACKEND_PORT, encoding="utf-8")

    # The first candidate observation only awaits the second confirmation.
    m.write_watch_state({"boot_id": boot, "consecutive_failures": 0,
                         "restart_attempts_utc": [], "idle_reason": "",
                         "last_decision": None}, state)
    result = check(1000.0, ops, broker)
    expect(result["reason"] == "awaiting_confirmation",
           "first candidate observation awaits the second confirmation")

    # Three attempts inside the hour exhaust the budget; nothing starts.
    attempts = [m._utc_iso(900.0), m._utc_iso(950.0), m._utc_iso(960.0)]
    m.write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                         "restart_attempts_utc": attempts, "idle_reason": "",
                         "last_decision": None}, state)
    result = check(1000.0, ops, broker)
    expect(result["reason"] == "budget_exhausted",
           "three attempts inside the hour exhaust the budget")
    expect(ops.starts == 0, "a budget-exhausted check never calls start()")

    # Attempts older than one hour are pruned, not counted.
    m.write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                         "restart_attempts_utc": attempts, "idle_reason": "",
                         "last_decision": None}, state)
    result = check(5000.0, ops, broker)
    expect(result["ok"] is True and result["attempts_this_hour"] == 0,
           "attempts older than one hour are pruned (budget not exhausted)")
finally:
    import shutil
    shutil.rmtree(root, ignore_errors=True)

if failures:
    print("FAILURES: " + "; ".join(failures))
    sys.exit(1)
print("maintenance gate + budget harness: PASS")
'@
$gates = Invoke-ChildProcess 'python' @('-B', $harnessScript, $root)
foreach ($line in @($gates.Output -split "`r?`n")) {
    if ($line) { Write-Host "  $line" }
}
Assert-True ($gates.ExitCode -eq 0) ("maintenance gate + budget harness exits 0 (got {0})" -f $gates.ExitCode)
Assert-Contains $gates.Output 'maintenance gate + budget harness: PASS' 'the harness reports PASS'
Remove-Item -LiteralPath $harnessDir -Recurse -Force -ErrorAction SilentlyContinue

# ===========================================================================
# 9. The notification script parses and maps only the five fixed codes.
# ===========================================================================
$notificationText = [IO.File]::ReadAllText($notificationScript)
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    $notificationScript, [ref]$tokens, [ref]$parseErrors) | Out-Null
Assert-True ($parseErrors.Count -eq 0) 'the notification script parses'
foreach ($code in @('share_public_failed', 'share_public_recovered',
                    'share_public_manual_required',
                    'share_public_budget_exhausted',
                    'share_public_degraded')) {
    Assert-Contains $notificationText $code ("the notification script maps {0}" -f $code)
}
Assert-Contains $notificationText 'ShowBalloonTip(8000)' 'the notification shows an 8 second balloon'
Assert-Contains $notificationText 'exit 1' 'a notification failure exits 1'
Assert-False ($notificationText.Contains('Invoke-Expression')) 'the notification script evaluates no arbitrary text'
Assert-False ($notificationText.Contains('Set-Content')) 'the notification script writes no file'

# ===========================================================================
if ($script:failures -gt 0) {
    Write-Host ("share-backend supervision tests: {0} FAILURES" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'share-backend supervision tests: ALL PASS' -ForegroundColor Green
exit 0
